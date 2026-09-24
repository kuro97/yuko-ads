"""Независимый верификатор исполненных owner-действий (волна E, блок E1)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from services import action_verifier
from services.database_migrations import apply_runtime_migrations


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
EXECUTED_AT = NOW - timedelta(hours=1)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "verifier.db"
    apply_runtime_migrations(str(path))
    return path


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


_PAYLOADS = {
    "PAUSE": {
        "schema_version": 1,
        "operation": "PAUSE_AD",
        "ad_id": "ad-1",
        "adset_id": "adset-1",
        "expected_before_status": "ACTIVE",
        "expected_after_status": "PAUSED",
        "reason_code": "WASTER",
    },
    "UNPAUSE": {
        "schema_version": 1,
        "operation": "UNPAUSE_AD",
        "ad_id": "ad-1",
        "adset_id": "adset-1",
        "expected_before_status": "PAUSED",
        "expected_after_status": "ACTIVE",
    },
    "SCALE": {
        "schema_version": 1,
        "operation": "SET_ADSET_BUDGET",
        "adset_id": "adset-1",
        "candidate_ad_id": "ad-1",
        "expected_current_budget_usd": "20",
        "target_budget_usd": "30",
    },
    "LAUNCH": {"schema_version": 1, "operation": "CREATE_AD"},
}
_ACTION_KIND = {
    "PAUSE": "PAUSE_AD",
    "UNPAUSE": "UNPAUSE_AD",
    "SCALE": "SET_ADSET_BUDGET",
    "LAUNCH": "CREATE_AD",
}


def _build_executed(
    path: Path,
    *,
    kind: str = "PAUSE",
    name: str = "p1",
    valid_until: datetime = NOW + timedelta(hours=6),
    card_message_id: int = 5001,
) -> tuple[str, str]:
    """Полная lineage до CONFIRMED attempt: proposal → APPROVE → job → attempt."""

    proposal_id = f"proposal-{name}"
    claim_id = f"claim-{name}"
    payload = _PAYLOADS[kind]
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    subject_id = "adset-1" if kind == "SCALE" else "ad-1"
    # Триггер 022 требует resource_id = subject для PAUSE/UNPAUSE и adset иначе.
    resource_id = subject_id if kind in {"PAUSE", "UNPAUSE"} else "adset-1"
    connection = _connect(path)
    try:
        connection.execute(
            """
            INSERT INTO owner_action_proposals (
                proposal_id, proposal_kind, origin, idempotency_key, source_ref,
                requested_by_actor, summary, plan_json, plan_sha256,
                targets_sha256, evidence_sha256, config_version_sha256,
                proposal_sha256, staged_media_root, created_at, valid_until
            ) VALUES (?, ?, 'AUTOPILOT', ?, ?, 'autopilot', ?, '{}', ?, ?, ?, ?,
                      ?, NULL, ?, ?)
            """,
            (
                proposal_id,
                kind,
                f"idem-{name}",
                f"scope-{name}",
                f"действие {kind}",
                _sha(f"plan-{name}"),
                _sha(f"targets-{name}"),
                _sha(f"evidence-{name}"),
                _sha(f"config-{name}"),
                _sha(f"proposal-{name}"),
                _iso(EXECUTED_AT - timedelta(minutes=30)),
                _iso(valid_until),
            ),
        )
        connection.execute(
            """
            INSERT INTO owner_action_proposal_targets (
                proposal_id, claim_id, ordinal, action_kind, account_id,
                adset_id, subject_id, city, language, intended_payload_json,
                intended_payload_sha256, created_at
            ) VALUES (?, ?, 0, ?, 'act-1', 'adset-1', ?, 'CityA', 'L1', ?, ?, ?)
            """,
            (
                proposal_id,
                claim_id,
                _ACTION_KIND[kind],
                subject_id,
                payload_json,
                payload_sha,
                _iso(EXECUTED_AT - timedelta(minutes=30)),
            ),
        )
        # Доставленная карточка — ветка следа для вердикта.
        connection.execute(
            """
            INSERT INTO telegram_delivery_outbox (
                delivery_id, purpose, proposal_id, generation, dedupe_key,
                rendered_text, rendered_text_sha256, button_spec_json,
                button_spec_sha256, state, telegram_chat_id,
                telegram_message_id, created_at, sent_at
            ) VALUES (?, 'OWNER_PROPOSAL', ?, 1, ?, ?, ?, '[]', ?, 'SENT', 777,
                      ?, ?, ?)
            """,
            (
                f"delivery-{name}",
                proposal_id,
                f"owner:{proposal_id}:1",
                f"карточка {name}",
                _sha(f"rendered-{name}"),
                _sha("[]"),
                card_message_id,
                _iso(EXECUTED_AT - timedelta(minutes=20)),
                _iso(EXECUTED_AT - timedelta(minutes=20)),
            ),
        )
        connection.execute(
            """
            INSERT INTO telegram_update_inbox (
                update_id, ingress_kind, bot_token_identity_sha256,
                raw_update_json, raw_update_sha256, state, received_at,
                processed_at
            ) VALUES (?, 'GET_UPDATES', ?, '{}', ?, 'PROCESSED', ?, ?)
            """,
            (
                abs(hash(name)) % 100_000 + 10,
                _sha(f"bot-{name}"),
                _sha(f"update-{name}"),
                _iso(EXECUTED_AT - timedelta(minutes=15)),
                _iso(EXECUTED_AT - timedelta(minutes=15)),
            ),
        )
        update_id = abs(hash(name)) % 100_000 + 10
        token_id = f"token-{name}"
        connection.execute(
            """
            INSERT INTO owner_callback_tokens (
                token_id, public_nonce, token_mac_sha256, proposal_id,
                delivery_id, delivery_generation, decision_kind,
                expected_owner_user_id, expected_chat_id, expected_message_id,
                created_at, expires_at, bound_at, consumed_at,
                consumed_update_id, consumed_callback_query_id
            ) VALUES (?, ?, ?, ?, ?, 1, 'APPROVE', 42, 777, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id,
                f"nonce-{name}",
                _sha(f"mac-{name}"),
                proposal_id,
                f"delivery-{name}",
                card_message_id,
                _iso(EXECUTED_AT - timedelta(minutes=20)),
                _iso(valid_until),
                _iso(EXECUTED_AT - timedelta(minutes=20)),
                _iso(EXECUTED_AT - timedelta(minutes=10)),
                update_id,
                f"callback-{name}",
            ),
        )
        decision_id = f"decision-{name}"
        connection.execute(
            """
            INSERT INTO owner_action_decisions (
                decision_id, proposal_id, proposal_sha256, decision_kind,
                owner_user_id, chat_id, message_id, delivery_generation,
                telegram_update_id, callback_query_id, callback_token_id,
                trusted_ingress_sha256, reason_text, recorded_at
            ) VALUES (?, ?, ?, 'APPROVE', 42, 777, ?, 1, ?, ?, ?, ?, NULL, ?)
            """,
            (
                decision_id,
                proposal_id,
                _sha(f"proposal-{name}"),
                card_message_id,
                update_id,
                f"callback-{name}",
                token_id,
                _sha(f"ingress-{name}"),
                _iso(EXECUTED_AT - timedelta(minutes=10)),
            ),
        )
        job_id = f"job-{name}"
        connection.execute(
            """
            INSERT INTO owner_execution_jobs (
                job_id, proposal_id, decision_id, state, created_at, updated_at
            ) VALUES (?, ?, ?, 'REVIEWING', ?, ?)
            """,
            (job_id, proposal_id, decision_id, _iso(EXECUTED_AT), _iso(EXECUTED_AT)),
        )
        connection.execute(
            """
            INSERT INTO owner_action_lifecycle (
                proposal_id, state, version, delivery_generation,
                active_decision_id, active_job_id, updated_at
            ) VALUES (?, 'LIVE_REVIEW', 1, 1, ?, ?, ?)
            """,
            (proposal_id, decision_id, job_id, _iso(EXECUTED_AT)),
        )
        permit_id = f"permit-{name}"
        connection.execute(
            """
            INSERT INTO owner_technical_permits (
                permit_id, secret_sha256, proposal_id, decision_id, job_id,
                claim_id, operation_kind, account_id, resource_id,
                exact_payload_sha256, manifest_json, manifest_sha256,
                live_evidence_sha256, phase, sequence_no, issued_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'act-1', ?, ?, '{}', ?, ?, 'ISSUED',
                      1, ?, ?)
            """,
            (
                permit_id,
                _sha(f"secret-{name}"),
                proposal_id,
                decision_id,
                job_id,
                claim_id,
                _ACTION_KIND[kind],
                resource_id,
                payload_sha,
                _sha(f"manifest-{name}"),
                _sha(f"live-{name}"),
                _iso(EXECUTED_AT - timedelta(minutes=1)),
                _iso(EXECUTED_AT + timedelta(minutes=5)),
            ),
        )
        connection.execute(
            "UPDATE owner_technical_permits SET phase='CONSUMED', consumed_at=? "
            "WHERE permit_id=?",
            (_iso(EXECUTED_AT), permit_id),
        )
        attempt_id = f"attempt-{name}"
        connection.execute(
            """
            INSERT INTO owner_action_attempts (
                attempt_id, permit_id, proposal_id, decision_id, job_id,
                claim_id, operation_kind, account_id, resource_id,
                exact_payload_sha256, state, provider_request_id, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'act-1', ?, ?, 'ATTEMPT_STARTED', ?, ?)
            """,
            (
                attempt_id,
                permit_id,
                proposal_id,
                decision_id,
                job_id,
                claim_id,
                _ACTION_KIND[kind],
                resource_id,
                payload_sha,
                f"request-{name}",
                _iso(EXECUTED_AT),
            ),
        )
        connection.execute(
            "UPDATE owner_action_attempts SET state='CONFIRMED', completed_at=?, "
            "provider_result_sha256=? WHERE attempt_id=?",
            (_iso(EXECUTED_AT), _sha(f"provider-{name}"), attempt_id),
        )
        for version, state in ((2, "PERMIT_ISSUED"), (3, "ATTEMPT_STARTED"), (4, "EXECUTED")):
            connection.execute(
                "UPDATE owner_action_lifecycle SET state=?, version=?, updated_at=? "
                "WHERE proposal_id=?",
                (state, version, _iso(EXECUTED_AT), proposal_id),
            )
        connection.execute(
            "UPDATE owner_execution_jobs SET state='EXECUTED', updated_at=? WHERE job_id=?",
            (_iso(EXECUTED_AT), job_id),
        )
        connection.commit()
    finally:
        connection.close()
    return proposal_id, claim_id


def _rows(path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    connection = _connect(path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


@pytest.fixture
def no_side_effects(monkeypatch):
    """Никаких реальных вызовов FB/Telegram: только записанные обращения."""

    alerts: list[tuple[str, str]] = []
    retries: list[str] = []
    proposals: list[str] = []

    monkeypatch.setattr(
        action_verifier,
        "_send_critical",
        lambda title, detail: alerts.append((title, detail)),
    )

    def _fake_retry(proposal_id: str, *, now: datetime) -> str:
        retries.append(proposal_id)
        return "ALL_CLAIMS_TERMINAL"

    monkeypatch.setattr(action_verifier, "_retry_same_approval", _fake_retry)

    def _fake_repeat(**kwargs) -> str:
        proposals.append(str(kwargs["proposal_id"]))
        return "proposal-repeat"

    monkeypatch.setattr(action_verifier, "_create_repeat_proposal", _fake_repeat)
    return {"alerts": alerts, "retries": retries, "repeats": proposals}


# ---------------------------------------------------------------------------
# VERIFIED
# ---------------------------------------------------------------------------


def test_pause_verified_writes_memory_and_marks_lifecycle(
    db_path: Path,
    monkeypatch,
    no_side_effects,
) -> None:
    proposal_id, claim_id = _build_executed(db_path, kind="PAUSE")
    monkeypatch.setattr(
        action_verifier,
        "read_live_ad_state",
        lambda ad_id: {"configured_status": "PAUSED", "effective_status": "PAUSED"},
    )

    run = action_verifier.verify_executed_actions(
        worker_id="verifier-1",
        now=NOW,
        db_path=db_path,
    )

    assert (run.checked, run.verified, run.mismatch, run.unverifiable) == (1, 1, 0, 0)
    memory = _rows(db_path, "SELECT * FROM action_verifications")
    assert len(memory) == 1
    assert memory[0]["verdict"] == "VERIFIED"
    assert memory[0]["kind"] == "PAUSE"
    assert memory[0]["check_seq"] == 1
    assert json.loads(memory[0]["observed_json"])["effective_status"] == "PAUSED"
    state = _rows(db_path, "SELECT * FROM action_verification_state")[0]
    assert (state["state"], state["retry_count"]) == ("VERIFIED", 0)
    lifecycle = _rows(db_path, "SELECT state FROM owner_action_lifecycle")[0]
    assert lifecycle["state"] == "VERIFIED"
    trail = _rows(
        db_path,
        "SELECT * FROM owner_trail_messages WHERE trail_kind='VERIFICATION_VERDICT'",
    )
    assert len(trail) == 1
    assert "Проверено в FB" in trail[0]["rendered_text"]
    assert trail[0]["reply_to_message_id"] == 5001
    events = _rows(
        db_path,
        "SELECT event_type FROM owner_action_events WHERE proposal_id=?",
        (proposal_id,),
    )
    assert "ACTION_VERIFICATION_VERIFIED" in {row["event_type"] for row in events}
    assert claim_id == state["claim_id"]
    assert no_side_effects["alerts"] == []


def test_scale_verified_compares_live_daily_budget(
    db_path: Path,
    monkeypatch,
    no_side_effects,
) -> None:
    _build_executed(db_path, kind="SCALE", name="s1")
    monkeypatch.setattr(
        action_verifier,
        "read_live_adset_budget",
        lambda adset_id: Decimal("30"),
    )

    run = action_verifier.verify_executed_actions(
        worker_id="verifier-1",
        now=NOW,
        db_path=db_path,
    )

    assert (run.verified, run.mismatch) == (1, 0)
    assert _rows(db_path, "SELECT verdict FROM action_verifications")[0]["verdict"] == (
        "VERIFIED"
    )


def test_launch_reuses_watchdog_verdict_without_second_mechanism(
    db_path: Path,
    no_side_effects,
) -> None:
    proposal_id, _ = _build_executed(db_path, kind="LAUNCH", name="l1")
    connection = _connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO launch_watchdogs (
                watchdog_id, proposal_id, decision_id, job_id, state,
                expected_count, verified_count, verify_deadline_at, created_at,
                updated_at, verified_at
            ) VALUES ('wd-1', ?, 'decision-l1', 'job-l1', 'VERIFIED', 1, 1, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                _iso(NOW + timedelta(hours=12)),
                _iso(EXECUTED_AT),
                _iso(EXECUTED_AT),
                _iso(EXECUTED_AT),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    run = action_verifier.verify_executed_actions(
        worker_id="verifier-1",
        now=NOW,
        db_path=db_path,
    )

    assert (run.verified, run.mismatch) == (1, 0)
    memory = _rows(db_path, "SELECT * FROM action_verifications")[0]
    assert memory["reason_code"] == "LAUNCH_WATCHDOG_VERIFIED"
    assert json.loads(memory["observed_json"])["watchdog_state"] == "VERIFIED"
    # Lifecycle запусков ведёт launch_verify — верификатор его не двигает.
    assert _rows(db_path, "SELECT state FROM owner_action_lifecycle")[0]["state"] == (
        "EXECUTED"
    )


# ---------------------------------------------------------------------------
# UNVERIFIABLE
# ---------------------------------------------------------------------------


def test_unverifiable_does_not_finish_and_retries_next_tick(
    db_path: Path,
    monkeypatch,
    no_side_effects,
) -> None:
    _build_executed(db_path, kind="PAUSE", name="u1")
    monkeypatch.setattr(action_verifier, "read_live_ad_state", lambda ad_id: None)

    first = action_verifier.verify_executed_actions(
        worker_id="verifier-1",
        now=NOW,
        db_path=db_path,
    )

    assert (first.verified, first.mismatch, first.unverifiable) == (0, 0, 1)
    state = _rows(db_path, "SELECT * FROM action_verification_state")[0]
    assert state["state"] == "PENDING"
    assert state["last_verdict"] == "UNVERIFIABLE"
    assert _rows(db_path, "SELECT state FROM owner_action_lifecycle")[0]["state"] == (
        "EXECUTED"
    )
    # Молчим владельцу: недоступный FB — не «расхождение».
    assert _rows(db_path, "SELECT * FROM owner_trail_messages") == []

    monkeypatch.setattr(
        action_verifier,
        "read_live_ad_state",
        lambda ad_id: {"configured_status": "PAUSED", "effective_status": "PAUSED"},
    )
    second = action_verifier.verify_executed_actions(
        worker_id="verifier-1",
        now=NOW + timedelta(minutes=15),
        db_path=db_path,
    )

    assert second.verified == 1
    seqs = [row["check_seq"] for row in _rows(db_path, "SELECT check_seq FROM action_verifications ORDER BY check_seq")]
    assert seqs == [1, 2]
    assert _rows(db_path, "SELECT state FROM action_verification_state")[0]["state"] == (
        "VERIFIED"
    )


# ---------------------------------------------------------------------------
# MISMATCH
# ---------------------------------------------------------------------------


def test_mismatch_with_unchanged_state_retries_same_approval(
    db_path: Path,
    monkeypatch,
    no_side_effects,
) -> None:
    proposal_id, _ = _build_executed(db_path, kind="PAUSE", name="m1")
    # Объявление всё ещё ACTIVE — ровно то предсостояние, которое одобрял владелец.
    monkeypatch.setattr(
        action_verifier,
        "read_live_ad_state",
        lambda ad_id: {"configured_status": "ACTIVE", "effective_status": "ACTIVE"},
    )

    run = action_verifier.verify_executed_actions(
        worker_id="verifier-1",
        now=NOW,
        db_path=db_path,
    )

    assert (run.mismatch, run.retried, run.escalated) == (1, 1, 0)
    assert no_side_effects["retries"] == [proposal_id]
    assert no_side_effects["repeats"] == []
    assert no_side_effects["alerts"] == []
    state = _rows(db_path, "SELECT * FROM action_verification_state")[0]
    assert (state["state"], state["retry_count"]) == ("PENDING", 1)
    memory = _rows(db_path, "SELECT * FROM action_verifications")[0]
    assert memory["verdict"] == "MISMATCH"
    assert memory["retry_no"] == 1
    assert memory["reason_code"].startswith("RETRY_SAME_APPROVAL")
    trail = _rows(db_path, "SELECT rendered_text FROM owner_trail_messages")[0]
    assert "Расхождение" in trail["rendered_text"]


def test_mismatch_with_changed_state_creates_repeat_proposal_and_alerts(
    db_path: Path,
    monkeypatch,
    no_side_effects,
) -> None:
    proposal_id, _ = _build_executed(db_path, kind="PAUSE", name="m2")
    # Живое состояние — третье: цель ушла из того предсостояния, что одобряли.
    monkeypatch.setattr(
        action_verifier,
        "read_live_ad_state",
        lambda ad_id: {
            "configured_status": "ACTIVE",
            "effective_status": "CAMPAIGN_PAUSED",
        },
    )

    run = action_verifier.verify_executed_actions(
        worker_id="verifier-1",
        now=NOW,
        db_path=db_path,
    )

    assert (run.mismatch, run.retried, run.escalated) == (1, 0, 1)
    assert no_side_effects["retries"] == []
    assert no_side_effects["repeats"] == [proposal_id]
    assert len(no_side_effects["alerts"]) == 1
    title, detail = no_side_effects["alerts"][0]
    assert "не выполнено" in title
    assert "CAMPAIGN_PAUSED" in detail
    state = _rows(db_path, "SELECT * FROM action_verification_state")[0]
    assert state["state"] == "ESCALATED"
    assert state["retry_proposal_id"] == "proposal-repeat"
    memory = _rows(db_path, "SELECT reason_code FROM action_verifications")[0]
    assert memory["reason_code"] == "STATE_CHANGED_SINCE_APPROVAL"


def test_expired_approval_blocks_retry_and_escalates(
    db_path: Path,
    monkeypatch,
    no_side_effects,
) -> None:
    _build_executed(
        db_path,
        kind="PAUSE",
        name="m3",
        valid_until=NOW - timedelta(minutes=5),
    )
    monkeypatch.setattr(
        action_verifier,
        "read_live_ad_state",
        lambda ad_id: {"configured_status": "ACTIVE", "effective_status": "ACTIVE"},
    )

    run = action_verifier.verify_executed_actions(
        worker_id="verifier-1",
        now=NOW,
        db_path=db_path,
    )

    assert (run.retried, run.escalated) == (0, 1)
    assert _rows(db_path, "SELECT reason_code FROM action_verifications")[0][
        "reason_code"
    ] == "APPROVAL_TTL_EXPIRED"


def test_three_retries_stop_with_alert_and_no_new_proposal(
    db_path: Path,
    monkeypatch,
    no_side_effects,
) -> None:
    _build_executed(db_path, kind="PAUSE", name="m4")
    monkeypatch.setattr(
        action_verifier,
        "read_live_ad_state",
        lambda ad_id: {"configured_status": "ACTIVE", "effective_status": "ACTIVE"},
    )

    for tick in range(4):
        action_verifier.verify_executed_actions(
            worker_id="verifier-1",
            now=NOW + timedelta(minutes=15 * tick),
            db_path=db_path,
        )

    assert len(no_side_effects["retries"]) == action_verifier.MAX_VERIFY_RETRIES
    assert no_side_effects["repeats"] == []
    assert len(no_side_effects["alerts"]) == 1
    assert "после 3 повторов" in no_side_effects["alerts"][0][0]
    state = _rows(db_path, "SELECT * FROM action_verification_state")[0]
    assert (state["state"], state["retry_count"]) == ("ESCALATED", 3)
    verdicts = [
        row["reason_code"]
        for row in _rows(db_path, "SELECT reason_code FROM action_verifications ORDER BY check_seq")
    ]
    assert verdicts[-1] == "RETRY_BUDGET_EXHAUSTED"

    # Терминальное состояние больше не выбирается: пятый тик ничего не делает.
    after = action_verifier.verify_executed_actions(
        worker_id="verifier-1",
        now=NOW + timedelta(hours=1),
        db_path=db_path,
    )
    assert after.checked == 0


def test_window_excludes_executions_older_than_24h(
    db_path: Path,
    monkeypatch,
    no_side_effects,
) -> None:
    _build_executed(db_path, kind="PAUSE", name="old")
    monkeypatch.setattr(
        action_verifier,
        "read_live_ad_state",
        lambda ad_id: {"configured_status": "PAUSED", "effective_status": "PAUSED"},
    )

    run = action_verifier.verify_executed_actions(
        worker_id="verifier-1",
        now=EXECUTED_AT + timedelta(hours=25),
        db_path=db_path,
    )

    assert run.checked == 0


def test_verifier_never_mutates_facebook(monkeypatch) -> None:
    """AST-регрессия: в модуле нет ни одного provider write."""

    import ast

    source = Path("services/action_verifier.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    write_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"post", "delete", "put"}
    }
    assert write_calls == set()
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "integrations.facebook_ads_mutation_transport" not in imports


# --- LAUNCH-сверка: исполненные claim'ы, пагинация, account-контекст ---------


def _launch_plan_json() -> str:
    return json.dumps(
        {
            "targets": [
                {
                    "claim_id": "launch:m:0",
                    "intended_payload": {
                        "destinations": [
                            {
                                "adset_id": "adset-1",
                                "account_id": "111",
                                "creatives": [{"ad_name": "CityA | Тест [PRODA]"}],
                            }
                        ]
                    },
                },
                {
                    "claim_id": "launch:m:1",
                    "intended_payload": {
                        "destinations": [
                            {
                                "adset_id": "adset-2",
                                "account_id": "222",
                                "creatives": [{"ad_name": "CityB | Тест [PRODA]"}],
                            }
                        ]
                    },
                },
            ]
        }
    )


def test_reconcile_launch_checks_only_executed_claims(monkeypatch):
    fetched: list[tuple[str, str | None]] = []

    def fake_fetch(adset_id, account_id):
        fetched.append((adset_id, account_id))
        return {"CityA | Тест [PRODA]"}

    monkeypatch.setattr(action_verifier, "_fetch_adset_ad_names", fake_fetch)
    verdict, reason, observed = action_verifier._reconcile_launch_by_names(
        _launch_plan_json(),
        executed_claim_ids=frozenset({"launch:m:0"}),
    )
    # Второй claim не исполнялся: его адсет не читается и «пропавшим» не считается.
    assert fetched == [("adset-1", "111")]
    assert verdict == action_verifier._VERDICT_VERIFIED
    assert reason == "LAUNCH_EXECUTED_NAMES_LIVE"
    assert observed["unexecuted_names"] == 1


def test_reconcile_launch_without_attempts_checks_whole_plan(monkeypatch):
    monkeypatch.setattr(
        action_verifier, "_fetch_adset_ad_names", lambda adset_id, account_id: set()
    )
    verdict, reason, _observed = action_verifier._reconcile_launch_by_names(
        _launch_plan_json()
    )
    assert verdict == action_verifier._VERDICT_MISMATCH
    assert reason == "LAUNCH_NO_NAMES_LIVE"


def test_reconcile_writes_verdict_into_auto_launch_attempt(monkeypatch):
    """Вердикт сверки уходит в попытку auto_launch по городам."""
    import services.auto_launch as auto_launch

    live = {
        "adset-1": {"CityA | Тест [PRODA]": "ad-1"},
        "adset-2": {},
    }
    monkeypatch.setattr(action_verifier, "_fetch_adset_ads", lambda adset_id, account_id: live.get(adset_id))
    monkeypatch.setattr(auto_launch, "_find_gateway_attempt", lambda key: ("attempt-key", {}))
    successes: list[tuple] = []
    failures: list[tuple] = []
    monkeypatch.setattr(auto_launch, "_record_city_success", lambda key, city, ids: successes.append((key, city, ids)))
    monkeypatch.setattr(auto_launch, "_record_city_failure", lambda key, city, err: failures.append((key, city, err)))

    plan = json.dumps({
        "targets": [
            {"claim_id": "launch:m:0", "intended_payload": {"destinations": [
                {"city": "CityA", "adset_id": "adset-1", "account_id": "111",
                 "creatives": [{"ad_name": "CityA | Тест [PRODA]"}]}]}},
            {"claim_id": "launch:m:1", "intended_payload": {"destinations": [
                {"city": "CityB", "adset_id": "adset-2", "account_id": "222",
                 "creatives": [{"ad_name": "CityB | Тест [PRODA]"}]}]}},
        ]
    }, ensure_ascii=False)
    action_verifier._record_launch_reconcile(plan, "idem-1", executed_claim_ids=frozenset({"launch:m:0", "launch:m:1"}))
    assert successes == [("attempt-key", "CityA", ["ad-1"])]
    assert failures and failures[0][:2] == ("attempt-key", "CityB") and "не найдены" in failures[0][2]

    # Только неизвестные claim'ы: подтверждённый второй город не трогаем.
    successes.clear()
    failures.clear()
    action_verifier._record_launch_reconcile(plan, "idem-1", executed_claim_ids=frozenset({"launch:m:0"}))
    assert successes == [("attempt-key", "CityA", ["ad-1"])] and failures == []


def test_executed_claim_ids_are_only_unknown_attempts(tmp_path):
    """Сверяются только attempts в RECONCILE_REQUIRED, а не все (вечный PARTIAL_NAMES_LIVE)."""
    import sqlite3

    path = tmp_path / "owner.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE owner_action_attempts (proposal_id TEXT, claim_id TEXT, state TEXT)")
    conn.executemany(
        "INSERT INTO owner_action_attempts VALUES (?, ?, ?)",
        [("p1", "c-confirmed", "CONFIRMED"), ("p1", "c-unknown", "RECONCILE_REQUIRED"),
         ("p1", "c-noeffect", "FAILED_NO_EFFECT"), ("p2", "c-other", "RECONCILE_REQUIRED")],
    )
    conn.commit()
    conn.close()
    assert action_verifier._executed_claim_ids(path, "p1") == frozenset({"c-unknown"})


def test_reconcile_launch_unproven_fetch_never_yields_no_effect(monkeypatch):
    monkeypatch.setattr(
        action_verifier, "_fetch_adset_ad_names", lambda adset_id, account_id: None
    )
    verdict, reason, _observed = action_verifier._reconcile_launch_by_names(
        _launch_plan_json(),
        executed_claim_ids=frozenset({"launch:m:0"}),
    )
    assert verdict == action_verifier._VERDICT_UNVERIFIABLE
    assert reason == "FB_ADSET_UNAVAILABLE"


def test_fetch_adset_ad_names_paginates_past_archived_tail(monkeypatch):
    pages = [
        {
            "data": [
                {"name": f"Архив {i}", "status": "ARCHIVED"} for i in range(200)
            ],
            "paging": {"next": "url", "cursors": {"after": "c2"}},
        },
        {
            "data": [{"name": "Живое объявление", "status": "ACTIVE"}],
            "paging": {},
        },
    ]

    class _Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    calls: list[dict] = []

    def fake_get(url, params=None):
        calls.append(dict(params or {}))
        return _Response(pages[len(calls) - 1])

    import agent.fb_common as fb_common
    from services import fb_token_provider

    monkeypatch.setattr(fb_common, "_throttled_get", fake_get)
    monkeypatch.setattr(fb_token_provider, "get_fb_token", lambda: "token")
    monkeypatch.setattr(
        action_verifier, "_launch_account_context", lambda account_id: None
    )
    live = action_verifier._fetch_adset_ad_names("adset-1", "111")
    assert live == {"Живое объявление"}
    assert len(calls) == 2
    assert calls[1]["after"] == "c2"


def test_fetch_adset_ad_names_broken_paging_is_unproven(monkeypatch):
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "data": [{"name": "Что-то", "status": "ACTIVE"}],
                "paging": {"next": "url", "cursors": {}},
            }

    import agent.fb_common as fb_common
    from services import fb_token_provider

    monkeypatch.setattr(fb_common, "_throttled_get", lambda url, params=None: _Response())
    monkeypatch.setattr(fb_token_provider, "get_fb_token", lambda: "token")
    monkeypatch.setattr(
        action_verifier, "_launch_account_context", lambda account_id: None
    )
    assert action_verifier._fetch_adset_ad_names("adset-1", "111") is None


def test_launch_account_context_routes_online_and_offline(monkeypatch):
    import config
    from services import fb_token_provider

    monkeypatch.setattr(config, "FB_ACCOUNT_ID_ONLINE", "334355943837505", raising=False)
    monkeypatch.setattr(
        fb_token_provider,
        "offline_account_context",
        lambda account_id: f"offline:{account_id}",
    )
    assert action_verifier._launch_account_context("act_334355943837505") == "online"
    assert action_verifier._launch_account_context("12345") == "offline:12345"
