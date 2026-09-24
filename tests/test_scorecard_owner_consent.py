"""Скоркард «согласие владельца» на реальных решениях (волна E, блок E6)."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.autopilot_feedback import owner_decision_counts
from services.database_migrations import apply_runtime_migrations


_TZ_LOCAL = timezone(timedelta(hours=5))
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "consent.db"
    apply_runtime_migrations(str(path))
    return path


def _insert_decision(
    path: Path,
    *,
    name: str,
    kind: str,
    recorded_at: datetime,
) -> None:
    """Минимальная валидная цепочка proposal → token → decision."""

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute(
            """
            INSERT INTO owner_action_proposals (
                proposal_id, proposal_kind, origin, idempotency_key, source_ref,
                requested_by_actor, summary, plan_json, plan_sha256,
                targets_sha256, evidence_sha256, config_version_sha256,
                proposal_sha256, staged_media_root, created_at, valid_until
            ) VALUES (?, 'PAUSE', 'AUTOPILOT', ?, ?, 'autopilot', ?, '{}', ?, ?,
                      ?, ?, ?, NULL, ?, ?)
            """,
            (
                f"proposal-{name}",
                f"idem-{name}",
                f"scope-{name}",
                f"сводка {name}",
                _sha(f"plan-{name}"),
                _sha(f"targets-{name}"),
                _sha(f"evidence-{name}"),
                _sha(f"config-{name}"),
                _sha(f"proposal-{name}"),
                _iso(recorded_at - timedelta(minutes=10)),
                _iso(recorded_at + timedelta(hours=24)),
            ),
        )
        connection.execute(
            """
            INSERT INTO telegram_delivery_outbox (
                delivery_id, purpose, proposal_id, generation, dedupe_key,
                rendered_text, rendered_text_sha256, button_spec_json,
                button_spec_sha256, state, telegram_chat_id,
                telegram_message_id, created_at, sent_at
            ) VALUES (?, 'OWNER_PROPOSAL', ?, 1, ?, 'карточка', ?, '[]', ?,
                      'SENT', 777, 100, ?, ?)
            """,
            (
                f"delivery-{name}",
                f"proposal-{name}",
                f"owner:{name}:1",
                _sha(f"rendered-{name}"),
                _sha("[]"),
                _iso(recorded_at),
                _iso(recorded_at),
            ),
        )
        update_id = abs(hash(name)) % 90_000 + 1
        connection.execute(
            """
            INSERT INTO telegram_update_inbox (
                update_id, ingress_kind, bot_token_identity_sha256,
                raw_update_json, raw_update_sha256, state, received_at
            ) VALUES (?, 'GET_UPDATES', ?, '{}', ?, 'PROCESSED', ?)
            """,
            (update_id, _sha(f"bot-{name}"), _sha(f"update-{name}"), _iso(recorded_at)),
        )
        connection.execute(
            """
            INSERT INTO owner_callback_tokens (
                token_id, public_nonce, token_mac_sha256, proposal_id,
                delivery_id, delivery_generation, decision_kind,
                expected_owner_user_id, expected_chat_id, expected_message_id,
                created_at, expires_at, bound_at, consumed_at,
                consumed_update_id, consumed_callback_query_id
            ) VALUES (?, ?, ?, ?, ?, 1, ?, 42, 777, 100, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"token-{name}",
                f"nonce-{name}",
                _sha(f"mac-{name}"),
                f"proposal-{name}",
                f"delivery-{name}",
                kind,
                _iso(recorded_at),
                _iso(recorded_at + timedelta(hours=24)),
                _iso(recorded_at),
                _iso(recorded_at),
                update_id,
                f"callback-{name}",
            ),
        )
        connection.execute(
            """
            INSERT INTO owner_action_decisions (
                decision_id, proposal_id, proposal_sha256, decision_kind,
                owner_user_id, chat_id, message_id, delivery_generation,
                telegram_update_id, callback_query_id, callback_token_id,
                trusted_ingress_sha256, reason_text, recorded_at
            ) VALUES (?, ?, ?, ?, 42, 777, 100, 1, ?, ?, ?, ?, NULL, ?)
            """,
            (
                f"decision-{name}",
                f"proposal-{name}",
                _sha(f"proposal-{name}"),
                kind,
                update_id,
                f"callback-{name}",
                f"token-{name}",
                _sha(f"ingress-{name}"),
                _iso(recorded_at),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_owner_decision_counts_uses_approve_share_of_resolved(db_path: Path) -> None:
    _insert_decision(db_path, name="a", kind="APPROVE", recorded_at=NOW - timedelta(days=1))
    _insert_decision(db_path, name="b", kind="APPROVE", recorded_at=NOW - timedelta(days=2))
    _insert_decision(db_path, name="c", kind="REJECT", recorded_at=NOW - timedelta(days=3))
    # POSTPONE — «ещё не решено», в знаменатель согласия не входит.
    _insert_decision(db_path, name="d", kind="POSTPONE", recorded_at=NOW - timedelta(days=1))
    # Вне окна — не считается.
    _insert_decision(db_path, name="old", kind="REJECT", recorded_at=NOW - timedelta(days=30))

    approve, reject, approve_ids, reject_ids = owner_decision_counts(
        db_path,
        window_start=NOW - timedelta(days=7),
        window_end=NOW,
    )

    assert (approve, reject) == (2, 1)
    assert set(approve_ids) == {"decision-a", "decision-b"}
    assert reject_ids == ("decision-c",)


def test_owner_decision_counts_is_empty_without_decisions(db_path: Path) -> None:
    assert owner_decision_counts(
        db_path,
        window_start=NOW - timedelta(days=7),
        window_end=NOW,
    ) == (0, 0, (), ())


def test_owner_decision_counts_tolerates_missing_table(tmp_path: Path) -> None:
    """Старая БД без контура одобрения — нули, а не падение скоркарда."""

    path = tmp_path / "legacy.db"
    sqlite3.connect(path).close()

    assert owner_decision_counts(
        path,
        window_start=NOW - timedelta(days=7),
        window_end=NOW,
    ) == (0, 0, (), ())


def test_owner_decision_counts_rejects_naive_window(db_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        owner_decision_counts(
            db_path,
            window_start=datetime(2026, 7, 20),
            window_end=NOW,
        )


def test_scorecard_agreement_reads_owner_decisions(db_path: Path, monkeypatch) -> None:
    import agent.database as agent_database
    from services.autopilot_feedback import get_feedback_stats

    _insert_decision(db_path, name="a", kind="APPROVE", recorded_at=NOW - timedelta(days=1))
    _insert_decision(db_path, name="b", kind="APPROVE", recorded_at=NOW - timedelta(days=2))
    _insert_decision(db_path, name="c", kind="REJECT", recorded_at=NOW - timedelta(days=3))
    monkeypatch.setattr(agent_database, "DB_PATH", str(db_path))

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return NOW.astimezone(tz) if tz else NOW

    monkeypatch.setattr("services.autopilot_feedback.datetime", _FrozenDatetime)

    stats = get_feedback_stats(since_days=7)

    assert stats == {
        "up": 2,
        "down": 1,
        "total": 3,
        "agreement_pct": pytest.approx(66.7, abs=0.05),
    }
    # Инвариант build_scorecard_request: agreement == up/(up+down)*100 ± 0.2.
    assert abs(stats["agreement_pct"] - stats["up"] / stats["total"] * 100) <= 0.2


def test_scorecard_agreement_falls_back_to_legacy_thumbs(
    db_path: Path,
    monkeypatch,
) -> None:
    """Пока терминальных решений в окне нет — историю старых недель не обнуляем."""

    import agent.database as agent_database
    from services.autopilot_feedback import get_feedback_stats, save_feedback

    monkeypatch.setattr(agent_database, "DB_PATH", str(db_path))
    save_feedback("11111111111", "p", "up")
    save_feedback("22222222222", "p", "down")

    stats = get_feedback_stats(since_days=7)

    assert (stats["up"], stats["down"], stats["total"]) == (1, 1, 2)
    assert stats["agreement_pct"] == pytest.approx(50.0)


def test_checker_evidence_matches_scorecard_source(db_path: Path, monkeypatch) -> None:
    """Кросс-проверка отчёта считает согласие из того же источника."""

    import config
    from services.approval_checker_models import (
        EvidenceRequest,
        Metric,
        SourceSystem,
        TimeWindow,
    )
    from services.approval_source_decisions import load_feedback_evidence

    _insert_decision(db_path, name="a", kind="APPROVE", recorded_at=NOW - timedelta(days=1))
    _insert_decision(db_path, name="b", kind="APPROVE", recorded_at=NOW - timedelta(days=2))
    _insert_decision(db_path, name="c", kind="REJECT", recorded_at=NOW - timedelta(days=3))
    # Наследственная таблица должна существовать: checker проверяет её схему.
    legacy = sqlite3.connect(db_path)
    try:
        legacy.execute(
            """
            CREATE TABLE autopilot_feedback(
                id INTEGER PRIMARY KEY, ad_id TEXT, action TEXT, verdict TEXT,
                created_at TEXT
            )
            """
        )
        legacy.commit()
    finally:
        legacy.close()
    monkeypatch.setattr(config, "REPORT_CHECKER_DECISIONS_DB_PATH", db_path)
    window = TimeWindow(NOW - timedelta(days=7), NOW, "UTC", "LAST_7D")
    request = EvidenceRequest(
        "req",
        "REPORT",
        None,
        NOW,
        (),
        (),
        (SourceSystem.AUTOPILOT_FEEDBACK,),
        (window,),
        (),
        (),
        (),
        (),
        (),
        False,
        False,
        300,
    )

    evidence = load_feedback_evidence(request, NOW)

    agreement = next(
        record
        for record in evidence.records
        if record.metric is Metric.AGREEMENT_PCT
    )
    assert evidence.complete is True
    assert float(agreement.value) == pytest.approx(66.7, abs=0.05)
    assert "numerator-up:2" in agreement.entity_ids
    assert "denominator-total:3" in agreement.entity_ids
    counts = {
        record.subject.subject_id: record.value
        for record in evidence.records
        if record.metric is Metric.FEEDBACK_COUNT
    }
    assert counts == {"up": 2, "down": 1, "total": 3}
