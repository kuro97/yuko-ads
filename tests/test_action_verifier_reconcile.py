"""Reconcile-фаза верификатора: RECONCILE_REQUIRED закрывается фактами провайдера.

Регрессия: PROVIDER_OUTCOME_UNKNOWN вешал задание навсегда (переход
из RECONCILE_REQUIRED разрешён миграцией 026, но исполнителя не было) —
висяки копились, запуск добивался руками.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_action_verifier import (  # noqa: F401 — реюз lineage-хелперов
    _build_executed,
    _connect,
    _iso,
    db_path,
)
from services.action_verifier import reconcile_unknown_outcomes

NOW = datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc)


def _to_reconcile_required(path: Path, name: str) -> str:
    """Переводит lineage от _build_executed в RECONCILE_REQUIRED."""

    proposal_id = f"proposal-{name}"
    connection = _connect(path)
    try:
        # Attempt остаётся CONFIRMED — отбор reconcile идёт по lifecycle.
        # _build_executed оставил lifecycle в EXECUTED(v4); матрица 026
        # разрешает EXECUTED → RECONCILE_REQUIRED.
        connection.execute(
            "UPDATE owner_action_lifecycle SET state='RECONCILE_REQUIRED', "
            "version=5, updated_at=? WHERE proposal_id=?",
            (_iso(NOW - timedelta(hours=1)), proposal_id),
        )
        connection.execute(
            "UPDATE owner_execution_jobs SET state='RECONCILE_REQUIRED', "
            "updated_at=? WHERE proposal_id=?",
            (_iso(NOW - timedelta(hours=1)), proposal_id),
        )
        connection.commit()
    finally:
        connection.close()
    return proposal_id


def _states(path: Path, proposal_id: str) -> tuple[str, str]:
    connection = _connect(path)
    try:
        lifecycle = connection.execute(
            "SELECT state FROM owner_action_lifecycle WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()[0]
        job = connection.execute(
            "SELECT state FROM owner_execution_jobs WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()[0]
        return str(lifecycle), str(job)
    finally:
        connection.close()


def test_pause_effect_confirmed_closes_as_verified(db_path, monkeypatch):
    _build_executed(db_path, kind="PAUSE", name="rc1")
    proposal_id = _to_reconcile_required(db_path, "rc1")
    monkeypatch.setattr(
        "services.action_verifier.read_live_ad_state",
        lambda ad_id: {"configured_status": "PAUSED", "effective_status": "PAUSED"},
    )

    run = reconcile_unknown_outcomes(worker_id="t", now=NOW, db_path=db_path)

    assert (run.confirmed, run.no_effect, run.unverifiable) == (1, 0, 0)
    assert _states(db_path, proposal_id) == ("VERIFIED", "COMPLETE")


def test_pause_no_effect_closes_for_reissue(db_path, monkeypatch):
    _build_executed(db_path, kind="PAUSE", name="rc2")
    proposal_id = _to_reconcile_required(db_path, "rc2")
    monkeypatch.setattr(
        "services.action_verifier.read_live_ad_state",
        lambda ad_id: {"configured_status": "ACTIVE", "effective_status": "ACTIVE"},
    )

    run = reconcile_unknown_outcomes(worker_id="t", now=NOW, db_path=db_path)

    assert (run.confirmed, run.no_effect, run.unverifiable) == (0, 1, 0)
    assert _states(db_path, proposal_id) == ("FAILED_NO_EFFECT", "FAILED_NO_EFFECT")


def test_fb_unavailable_keeps_reconcile_required(db_path, monkeypatch):
    _build_executed(db_path, kind="PAUSE", name="rc3")
    proposal_id = _to_reconcile_required(db_path, "rc3")
    monkeypatch.setattr(
        "services.action_verifier.read_live_ad_state", lambda ad_id: None
    )

    run = reconcile_unknown_outcomes(worker_id="t", now=NOW, db_path=db_path)

    assert (run.confirmed, run.no_effect, run.unverifiable) == (0, 0, 1)
    assert _states(db_path, proposal_id) == ("RECONCILE_REQUIRED", "RECONCILE_REQUIRED")


def test_launch_names_verdicts(monkeypatch):
    """LAUNCH-сверка по exact-именам: все живы → VERIFIED, ни одного → MISMATCH,
    частично → VERIFIED с missing_names: живая часть — факт,
    пропавшая пишется в попытку auto_launch по городам и дозапускается только она —
    дублей живой части не будет; раньше это был вечный UNVERIFIABLE."""

    from services.action_verifier import (
        _reconcile_launch_by_names,
        _VERDICT_MISMATCH,
        _VERDICT_UNVERIFIABLE,
        _VERDICT_VERIFIED,
    )

    plan = json.dumps({
        "targets": [{
            "intended_payload": {
                "destinations": [{
                    "adset_id": "adset-1",
                    "creatives": [
                        {"ad_name": "Город | Карточка / 1"},
                        {"ad_name": "Город | Карточка / 2"},
                    ],
                }]
            }
        }]
    }, ensure_ascii=False)

    from types import SimpleNamespace

    def _live(names):
        payload = {"data": [{"name": n, "status": "ACTIVE"} for n in names]}
        return lambda url, params=None: SimpleNamespace(
            status_code=200, json=lambda: payload
        )

    monkeypatch.setattr(
        "services.fb_token_provider.get_fb_token", lambda: "test-token"
    )

    monkeypatch.setattr("agent.fb_common._throttled_get",
                        _live(["Город | Карточка / 1", "Город | Карточка / 2"]))
    assert _reconcile_launch_by_names(plan)[0] == _VERDICT_VERIFIED

    monkeypatch.setattr("agent.fb_common._throttled_get", _live([]))
    assert _reconcile_launch_by_names(plan)[0] == _VERDICT_MISMATCH

    monkeypatch.setattr("agent.fb_common._throttled_get",
                        _live(["Город | Карточка / 1"]))
    verdict, reason, observed = _reconcile_launch_by_names(plan)
    assert (verdict, reason, observed["missing_names"]) == (_VERDICT_VERIFIED, "LAUNCH_PARTIAL_NAMES_LIVE", 1)
    assert _VERDICT_UNVERIFIABLE  # символ остаётся для FB_ADSET_UNAVAILABLE
