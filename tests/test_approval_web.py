"""T25: HTTP/UX idempotency, typed states и GET-only reconciliation."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from agent.database import init_db
from services.action_producer_gateway import (
    ProducerIdempotencyConflict,
    reserve_idempotency,
)
from services.approval_checker_models import ActionResult, OperationState
from tests.conftest import TEST_API_KEY
from web import app as app_module


ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def client(tmp_path):
    init_db(str(tmp_path / "decisions.db"), str(tmp_path / "missing.json"))
    return TestClient(app_module.app)


def _key_headers(key: str | None = None) -> dict[str, str]:
    return {**HEADERS, "Idempotency-Key": key or str(uuid.uuid4())}


def _run(
    *,
    result: ActionResult | None,
    state: OperationState,
    reconciliation_required: bool = False,
):
    return SimpleNamespace(
        operation_id=str(uuid.uuid4()),
        idempotency_key=str(uuid.uuid4()),
        batch_manifest_id=str(uuid.uuid4()),
        batch_manifest_sha256="a" * 64,
        check_ids=(str(uuid.uuid4()),),
        decision=None,
        state=state,
        result=result,
        shadow_evaluation=None,
        shadow_items=(),
        completed_items=1 if result is ActionResult.CONFIRMED else 0,
        total_items=1,
        first_unprocessed_index=None if result is ActionResult.CONFIRMED else 0,
        dry_run=False,
        provider_mutation_count=1 if result is not None else 0,
        reconciliation_required=reconciliation_required,
        stop_reason_code=None,
    )


def _assert_typed_action(payload: dict) -> None:
    assert {
        "operation_id",
        "idempotency_key",
        "batch_manifest_id",
        "check_ids",
        "decision",
        "state",
        "result",
        "shadow_evaluation",
        "shadow_items",
        "completed_items",
        "total_items",
        "first_unprocessed_index",
        "dry_run",
        "provider_mutation_count",
        "reconciliation_required",
    }.issubset(payload)


@pytest.mark.parametrize(
    "key",
    [None, "not-a-uuid", str(uuid.uuid1()), str(uuid.uuid4()).upper()],
)
def test_pause_requires_client_owned_canonical_uuid4(client, monkeypatch, key) -> None:
    execute = Mock()
    monkeypatch.setattr("services.action_producer_gateway.execute_pause", execute)
    headers = dict(HEADERS)
    if key is not None:
        headers["Idempotency-Key"] = key

    response = client.post("/api/analytics/ad-1/pause", headers=headers, json={})

    assert response.status_code == 400
    execute.assert_not_called()


def test_same_key_replay_is_bound_to_exact_action_and_entity(tmp_path) -> None:
    init_db(str(tmp_path / "commands.db"), str(tmp_path / "missing.json"))
    key = str(uuid.uuid4())
    payload = {"kind": "PAUSE", "ad_id": "ad-1"}

    assert reserve_idempotency("web:click", payload, key) == key
    assert reserve_idempotency("web:click", payload, key) == key
    with pytest.raises(ProducerIdempotencyConflict):
        reserve_idempotency("web:click", {"kind": "PAUSE", "ad_id": "ad-2"}, key)
    with pytest.raises(ProducerIdempotencyConflict):
        reserve_idempotency("web:click", {"kind": "UNPAUSE", "ad_id": "ad-1"}, key)
    with pytest.raises(ProducerIdempotencyConflict):
        reserve_idempotency("web:other", payload, key)


def test_pause_confirmed_returns_typed_200_and_writes_effect_once(client, monkeypatch) -> None:
    run = _run(result=ActionResult.CONFIRMED, state=OperationState.CONFIRMED)
    outcome = SimpleNamespace(confirmed=True, run=run, action="CONFIRMED", reason=None)
    monkeypatch.setattr("services.action_producer_gateway.execute_pause", Mock(return_value=outcome))
    save = Mock(side_effect=[True, False])
    notify = Mock()
    monkeypatch.setattr(app_module.decisions_repo, "save_decision", save)
    monkeypatch.setattr(app_module, "notify", notify)
    key = str(uuid.uuid4())

    first = client.post("/api/analytics/ad-1/pause", headers=_key_headers(key), json={})
    replay = client.post("/api/analytics/ad-1/pause", headers=_key_headers(key), json={})

    assert first.status_code == replay.status_code == 200
    _assert_typed_action(first.json())
    assert first.json()["state"] == "CONFIRMED"
    assert first.json()["result"] == "CONFIRMED"
    assert notify.call_count == 1


def test_pause_unknown_returns_typed_202_without_success_side_effect(client, monkeypatch) -> None:
    run = _run(
        result=ActionResult.UNKNOWN,
        state=OperationState.UNKNOWN,
        reconciliation_required=True,
    )
    outcome = SimpleNamespace(confirmed=False, run=run, action="UNKNOWN", reason=None)
    monkeypatch.setattr("services.action_producer_gateway.execute_pause", Mock(return_value=outcome))
    save = Mock()
    notify = Mock()
    monkeypatch.setattr(app_module.decisions_repo, "save_decision", save)
    monkeypatch.setattr(app_module, "notify", notify)

    response = client.post("/api/analytics/ad-1/pause", headers=_key_headers(), json={})

    assert response.status_code == 202
    _assert_typed_action(response.json())
    assert response.json()["state"] == "UNKNOWN"
    assert response.json()["reconciliation_required"] is True
    save.assert_not_called()
    notify.assert_not_called()


def test_pause_denied_returns_typed_409_with_zero_provider_and_success_effects(
    client, monkeypatch
) -> None:
    run = _run(result=None, state=OperationState.DENIED)
    run.provider_mutation_count = 0
    outcome = SimpleNamespace(
        confirmed=False,
        run=run,
        action="DENIED",
        reason="APPROVAL_DENIED",
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.execute_pause", Mock(return_value=outcome)
    )
    save = Mock()
    notify = Mock()
    monkeypatch.setattr(app_module.decisions_repo, "save_decision", save)
    monkeypatch.setattr(app_module, "notify", notify)

    response = client.post("/api/analytics/ad-1/pause", headers=_key_headers(), json={})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "APPROVAL_DENIED"
    assert detail["operation_id"] == run.operation_id
    save.assert_not_called()
    notify.assert_not_called()


@pytest.mark.parametrize(
    "error,status,code",
    [
        (ProducerIdempotencyConflict("IDEMPOTENCY_CONFLICT"), 409, "IDEMPOTENCY_CONFLICT"),
        (RuntimeError("provider token=secret"), 502, "CHECKER_UNAVAILABLE"),
    ],
)
def test_pause_errors_use_typed_http_envelope(client, monkeypatch, error, status, code) -> None:
    monkeypatch.setattr(
        "services.action_producer_gateway.execute_pause", Mock(side_effect=error)
    )

    response = client.post("/api/analytics/ad-1/pause", headers=_key_headers(), json={})

    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert "secret" not in response.text


def test_get_reconciliation_is_read_only_and_never_reposts(client, monkeypatch) -> None:
    pending = _run(
        result=ActionResult.UNKNOWN,
        state=OperationState.UNKNOWN,
        reconciliation_required=True,
    )
    confirmed = _run(result=ActionResult.CONFIRMED, state=OperationState.CONFIRMED)
    confirmed.operation_id = pending.operation_id
    get_operation = Mock(return_value=pending)
    reconcile = Mock(return_value=confirmed)
    execute_pause = Mock()
    monkeypatch.setattr("services.action_gateway.get_operation", get_operation)
    monkeypatch.setattr("services.action_gateway.reconcile_operation", reconcile)
    monkeypatch.setattr("services.action_producer_gateway.execute_pause", execute_pause)

    response = client.get(
        f"/api/approval/actions/{pending.operation_id}", headers=HEADERS
    )

    assert response.status_code == 200
    _assert_typed_action(response.json())
    assert response.json()["state"] == "CONFIRMED"
    reconcile.assert_called_once()
    execute_pause.assert_not_called()


def _function_body(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    assert match is not None, f"JS function {name} не найдена"
    depth = 1
    index = match.end()
    while depth and index < len(source):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    assert depth == 0, f"JS function {name} имеет незакрытый блок"
    return source[match.end(): index - 1]


def test_ui_post_actions_use_uuid_key_and_disable_post_retry() -> None:
    source = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    # Ключ генерирует хелпер generateActionUuid — crypto.randomUUID
    # только в secure context, по голому HTTP — UUID v4 из getRandomValues
    # с битами версии (0x40) и варианта (0x80). Ключ переживает повтор клика
    # через sessionStorage.
    key_body = _function_body(source, "actionIdempotencyKey")
    assert "generateActionUuid()" in key_body
    assert "sessionStorage.getItem" in key_body and "sessionStorage.setItem" in key_body
    uuid_body = _function_body(source, "generateActionUuid")
    assert "crypto.randomUUID()" in uuid_body
    assert "crypto.getRandomValues" in uuid_body
    assert "0x40" in uuid_body and "0x80" in uuid_body
    for name in ("launchOne", "launchOneAndWait", "pauseAd", "unpauseAd"):
        body = _function_body(source, name)
        assert "Idempotency-Key" in body
        assert re.search(r"retries\s*:\s*0", body)


def test_ui_timeout_and_unknown_reconcile_with_get_only() -> None:
    source = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    poll = _function_body(source, "pollApprovalAction")
    assert "method: 'GET'" in poll or 'method: "GET"' in poll
    assert "POST" not in poll
    for name in ("launchOne", "launchOneAndWait", "pauseAd", "unpauseAd"):
        body = _function_body(source, name)
        assert "pollApprovalAction" in body


def test_incomplete_metrics_snapshot_keeps_window_pending_for_retry(monkeypatch) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 22, 6, 30)
            return value.replace(tzinfo=tz) if tz is not None else value

    writes: list[dict] = []
    strict = Mock(return_value=SimpleNamespace(complete=False))
    legacy = Mock(return_value={"date": "2026-07-21", "fetched": 1, "upserted": 1})
    monkeypatch.setattr(app_module, "datetime", FrozenDateTime)
    monkeypatch.setattr("services.creative_backfill._get_state_by_key", lambda _: {})
    monkeypatch.setattr("services.creative_backfill._save_state_by_key", lambda _, value: writes.append(value))
    monkeypatch.setattr("services.metrics_snapshot.capture_daily_snapshot_complete", strict)
    monkeypatch.setattr("services.metrics_snapshot.capture_daily_snapshot", legacy)
    # Однокабинетный сценарий: карта роутинга без cabinet_b
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan", lambda: ("152882611033373",)
    )

    app_module._cron_metrics_snapshot()

    strict.assert_called_once()
    legacy.assert_not_called()
    assert writes
    assert all(item.get("last_window") != "2026-07-22" for item in writes)
    assert any(item.get("pending_window") == "2026-07-22" for item in writes)


def test_metrics_snapshot_runs_per_routed_account(monkeypatch) -> None:
    """Мультикабинетная карта: строгий снапшот зовётся на КАЖДЫЙ кабинет,
    состояние ведётся раздельно (legacy-ключ у дефолтного, суффикс у прочих)."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 19, 6, 30)
            return value.replace(tzinfo=tz) if tz is not None else value

    writes: list[tuple[str, dict]] = []
    accounts_seen: list[str] = []

    def strict(now=None):
        from services.fb_token_provider import get_fb_account_id

        accounts_seen.append(get_fb_account_id())
        return SimpleNamespace(
            complete=True,
            target_date=datetime(2026, 8, 18).date(),
            fetched_ad_ids=("a",),
            upserted_ad_ids=("a",),
            incomplete_reason_codes=(),
        )

    monkeypatch.setattr(app_module, "datetime", FrozenDateTime)
    monkeypatch.setattr("services.creative_backfill._get_state_by_key", lambda _: {})
    monkeypatch.setattr(
        "services.creative_backfill._save_state_by_key",
        lambda key, value: writes.append((key, value)),
    )
    monkeypatch.setattr(
        "services.metrics_snapshot.capture_daily_snapshot_complete", strict
    )
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan",
        lambda: ("152882611033373", "29716040622546856"),
    )

    app_module._cron_metrics_snapshot()

    assert accounts_seen == ["152882611033373", "29716040622546856"]
    done_keys = {k for k, v in writes if v.get("last_window") == "2026-08-19"}
    assert done_keys == {"metrics_snapshot", "metrics_snapshot:29716040622546856"}


def test_metrics_backfill_cron_runs_per_routed_account(monkeypatch) -> None:
    """Ночной курсорный бэкфилл метрик зовётся на КАЖДЫЙ кабинет карты
    роутинга — именно он штатный дневной писатель ad_daily_metrics."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 25, 3, 30)
            return value.replace(tzinfo=tz) if tz is not None else value

    state_writes: list[tuple[str, dict]] = []
    calls: list = []

    def fake_increment(account_id=None, **kwargs):
        calls.append(account_id)
        return {"fetched": 1, "upserted": 1, "rate_limited": False, "done": True}

    monkeypatch.setattr(app_module, "datetime", FrozenDateTime)
    monkeypatch.setattr("services.creative_backfill._get_state_by_key", lambda _: {})
    monkeypatch.setattr(
        "services.creative_backfill._save_state_by_key",
        lambda key, value: state_writes.append((key, value)),
    )
    monkeypatch.setattr(
        "services.metrics_backfill.run_metrics_backfill_increment", fake_increment
    )
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan",
        lambda: ("152882611033373", "29716040622546856"),
    )

    app_module._cron_metrics_backfill()

    assert calls == ["152882611033373", "29716040622546856"]
    assert ("metrics_backfill_cron", {"last_window": "2026-08-25-3"}) in state_writes
    # rate_limited не было — пометка окна не сбрасывалась
    assert ("metrics_backfill_cron", {"last_window": None}) not in state_writes


def test_metrics_backfill_cron_rate_limit_resets_window(monkeypatch) -> None:
    """Rate-limit ЛЮБОГО кабинета сбрасывает пометку окна — следующий тик
    повторит; при этом остальные кабинеты в этом тике всё равно обработаны."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 25, 4, 10)
            return value.replace(tzinfo=tz) if tz is not None else value

    state_writes: list[tuple[str, dict]] = []
    calls: list = []

    def fake_increment(account_id=None, **kwargs):
        calls.append(account_id)
        limited = account_id == "152882611033373"
        return {"fetched": 0, "upserted": 0, "rate_limited": limited, "done": False}

    monkeypatch.setattr(app_module, "datetime", FrozenDateTime)
    monkeypatch.setattr("services.creative_backfill._get_state_by_key", lambda _: {})
    monkeypatch.setattr(
        "services.creative_backfill._save_state_by_key",
        lambda key, value: state_writes.append((key, value)),
    )
    monkeypatch.setattr(
        "services.metrics_backfill.run_metrics_backfill_increment", fake_increment
    )
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan",
        lambda: ("152882611033373", "29716040622546856"),
    )

    app_module._cron_metrics_backfill()

    # Оба кабинета обработаны несмотря на rate-limit первого
    assert calls == ["152882611033373", "29716040622546856"]
    # Пометка окна сброшена — повтор в следующем тике
    assert state_writes[-1] == ("metrics_backfill_cron", {"last_window": None})
