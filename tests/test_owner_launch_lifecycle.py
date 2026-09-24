"""Exact-ID ACTIVE lifecycle owner-approved запуска."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services.database_migrations import apply_runtime_migrations
from services.launch_repository import (
    LaunchTargetObservation,
    LaunchWatchdogTarget,
    OwnerLaunchRepository,
)
from services.owner_action_models import (
    OwnerDecisionKind,
    ProposalKind,
    ProposalOrigin,
    ProposedActionPlan,
    ProposedTarget,
    canonical_json,
    canonical_sha256,
)
from services.owner_action_repository import OwnerActionRepository


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
FINGERPRINT = "f" * 64


def _connection(db_path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _executed_launch(db_path) -> tuple[str, str, str]:
    owner = OwnerActionRepository(db_path)
    payload = {"manifest_id": "manifest-1", "expected": "ACTIVE"}
    plan = ProposedActionPlan(
        proposal_kind=ProposalKind.LAUNCH,
        origin=ProposalOrigin.CRON,
        idempotency_key="launch:slot:2026-07-27",
        source_ref="trello:card-1",
        actor="auto-launch",
        summary="Запустить карточку",
        targets=(
            ProposedTarget(
                claim_id="claim-1",
                ordinal=0,
                action_kind="CREATE_AD",
                account_id="act_100",
                adset_id="adset-1",
                subject_id="card-1",
                city="CityA",
                language="L1",
                intended_payload=payload,
                intended_payload_sha256=canonical_sha256(payload),
            ),
        ),
        evidence=(),
        config_version_sha256="c" * 64,
        valid_until=NOW + timedelta(hours=4),
        staged_media_root=None,
    )
    receipt = owner.propose_action(plan, now=NOW)
    owner.transition_lifecycle(
        receipt.proposal_id,
        expected_state="DELIVERY_PENDING",
        expected_version=1,
        new_state="PENDING_OWNER",
        delivery_generation=1,
        actor="delivery",
        now=NOW + timedelta(seconds=1),
    )
    raw_update = {"update_id": 101, "callback_query": {"id": "callback-1"}}
    raw_json = canonical_json(raw_update).decode("utf-8")
    raw_sha = hashlib.sha256(raw_json.encode()).hexdigest()
    connection = _connection(db_path)
    try:
        connection.execute(
            """
            INSERT INTO telegram_update_inbox (
                update_id, ingress_kind, bot_token_identity_sha256,
                raw_update_json, raw_update_sha256, state, received_at
            ) VALUES (101, 'GET_UPDATES', ?, ?, ?, 'PROCESSING', ?)
            """,
            ("b" * 64, raw_json, raw_sha, NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO telegram_delivery_outbox (
                delivery_id, purpose, proposal_id, generation, dedupe_key,
                rendered_text, rendered_text_sha256, button_spec_json,
                state, telegram_chat_id, telegram_message_id, created_at, sent_at
            ) VALUES (
                'delivery-1', 'OWNER_PROPOSAL', ?, 1, ?, 'approve', ?,
                '[]', 'SENT', 777, 888, ?, ?
            )
            """,
            (
                receipt.proposal_id,
                f"proposal:{receipt.proposal_id}:1",
                canonical_sha256("approve"),
                NOW.isoformat(),
                (NOW + timedelta(seconds=1)).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO owner_callback_tokens (
                token_id, public_nonce, token_mac_sha256, proposal_id,
                delivery_id, delivery_generation, decision_kind,
                expected_owner_user_id, expected_chat_id, expected_message_id,
                created_at, expires_at, bound_at
            ) VALUES (
                'token-1', 'nonce-1', ?, ?, 'delivery-1', 1, 'APPROVE',
                42, 777, 888, ?, ?, ?
            )
            """,
            (
                "a" * 64,
                receipt.proposal_id,
                NOW.isoformat(),
                (NOW + timedelta(hours=1)).isoformat(),
                (NOW + timedelta(seconds=1)).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    decision = owner._record_owner_decision(  # noqa: SLF001
        proposal_id=receipt.proposal_id,
        token_id="token-1",
        decision=OwnerDecisionKind.APPROVE,
        owner_user_id=42,
        chat_id=777,
        message_id=888,
        delivery_generation=1,
        telegram_update_id=101,
        callback_query_id="callback-1",
        trusted_ingress_sha256=raw_sha,
        reason_text=None,
        actor="owner",
        expected_lifecycle_version=2,
        now=NOW + timedelta(seconds=2),
    )
    assert decision.decision_id is not None
    proposal = owner.get_proposal(receipt.proposal_id)
    assert proposal is not None and proposal.active_job_id is not None
    version = owner.queue_execution(
        proposal_id=receipt.proposal_id,
        decision_id=decision.decision_id,
        job_id=proposal.active_job_id,
        expected_lifecycle_version=3,
        actor="executor",
        now=NOW + timedelta(seconds=3),
    )
    version = owner.begin_live_review(
        proposal_id=receipt.proposal_id,
        decision_id=decision.decision_id,
        job_id=proposal.active_job_id,
        expected_lifecycle_version=version,
        actor="executor",
        now=NOW + timedelta(seconds=4),
    )
    manifest = {"claim_id": "claim-1", "payload": payload}
    permit = owner.issue_technical_permit(
        proposal_id=receipt.proposal_id,
        decision_id=decision.decision_id,
        job_id=proposal.active_job_id,
        claim_id="claim-1",
        operation_kind="CREATE_AD",
        account_id="act_100",
        resource_id="adset-1",
        exact_payload_sha256=canonical_sha256(payload),
        manifest=manifest,
        manifest_sha256=canonical_sha256(manifest),
        live_evidence_sha256="e" * 64,
        expires_at=NOW + timedelta(minutes=5),
        actor="executor",
        expected_lifecycle_version=version,
        now=NOW + timedelta(seconds=5),
    )
    attempt = owner.consume_technical_permit(
        permit_secret=permit.secret,
        exact_payload_sha256=canonical_sha256(payload),
        actor="executor",
        now=NOW + timedelta(seconds=6),
    )
    owner.transition_attempt(
        attempt.attempt_id,
        expected_state="ATTEMPT_STARTED",
        new_state="CONFIRMED",
        actor="executor",
        provider_result={"created_ad_id": "ad-1"},
        reason_code="PROVIDER_CREATED",
        now=NOW + timedelta(seconds=7),
    )
    return receipt.proposal_id, decision.decision_id, proposal.active_job_id


@pytest.fixture
def launch_repository(tmp_path):
    db_path = tmp_path / "owner-launch.db"
    apply_runtime_migrations(str(db_path))
    proposal_id, decision_id, job_id = _executed_launch(db_path)
    repository = OwnerLaunchRepository(str(db_path))
    watchdog_id = repository.create_watchdog(
        proposal_id=proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        targets=(
            LaunchWatchdogTarget(
                claim_id="claim-1",
                account_id="act_100",
                adset_id="adset-1",
                expected_ad_name="CityA | Карточка",
                expected_fingerprint=FINGERPRINT,
                created_ad_id="ad-1",
            ),
        ),
        now=NOW + timedelta(seconds=8),
    )
    return db_path, repository, watchdog_id, proposal_id


def _observation(*, effective_status: str) -> LaunchTargetObservation:
    return LaunchTargetObservation(
        created_ad_id="ad-1",
        account_id="100",
        adset_id="adset-1",
        ad_name="CityA | Карточка",
        configured_status="ACTIVE",
        effective_status=effective_status,
        fingerprint=FINGERPRINT,
    )


def test_launch_pending_is_not_success(launch_repository) -> None:
    db_path, repository, watchdog_id, proposal_id = launch_repository
    leases = repository.claim_due(
        worker_id="verify-a",
        now=NOW + timedelta(seconds=9),
        limit=10,
    )
    transition = repository.record_observation(
        leases[0],
        observations={"ad-1": _observation(effective_status="PENDING_REVIEW")},
        fetch_complete=True,
        now=NOW + timedelta(seconds=10),
    )

    assert transition.state == "VERIFYING"
    assert transition.verified_count == 0
    connection = _connection(db_path)
    try:
        assert connection.execute(
            "SELECT state FROM owner_action_lifecycle WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()[0] == "VERIFYING"
        assert connection.execute(
            "SELECT COUNT(*) FROM telegram_delivery_outbox WHERE purpose = 'TRELLO_COMPLETE'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT state FROM launch_watchdogs WHERE watchdog_id = ?",
            (watchdog_id,),
        ).fetchone()[0] == "VERIFYING"
    finally:
        connection.close()


def test_verified_side_effect_transaction(launch_repository) -> None:
    db_path, repository, watchdog_id, proposal_id = launch_repository
    lease = repository.claim_due(
        worker_id="verify-a",
        now=NOW + timedelta(seconds=9),
        limit=10,
    )[0]
    pending = repository.record_observation(
        lease,
        observations={"ad-1": _observation(effective_status="PENDING_REVIEW")},
        fetch_complete=True,
        now=NOW + timedelta(seconds=10),
    )
    assert pending.next_verify_at == NOW + timedelta(minutes=1, seconds=10)

    active_lease = repository.claim_due(
        worker_id="verify-b",
        now=pending.next_verify_at,
        limit=10,
    )[0]
    verified = repository.record_observation(
        active_lease,
        observations={"ad-1": _observation(effective_status="ACTIVE")},
        fetch_complete=True,
        now=pending.next_verify_at,
    )

    assert verified.state == "VERIFIED"
    connection = _connection(db_path)
    try:
        watchdog = connection.execute(
            "SELECT state, verified_count FROM launch_watchdogs WHERE watchdog_id = ?",
            (watchdog_id,),
        ).fetchone()
        lifecycle = connection.execute(
            "SELECT state FROM owner_action_lifecycle WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT state FROM owner_execution_jobs WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        outbox = connection.execute(
            "SELECT state FROM telegram_delivery_outbox WHERE purpose = 'TRELLO_COMPLETE'"
        ).fetchone()
        events = connection.execute(
            "SELECT COUNT(*) FROM owner_action_events WHERE event_type = 'LAUNCH_VERIFIED'"
        ).fetchone()[0]
        assert tuple(watchdog) == ("VERIFIED", 1)
        assert lifecycle[0] == "VERIFIED"
        assert job[0] == "COMPLETE"
        assert outbox[0] == "PENDING"
        assert events == 1
    finally:
        connection.close()


def test_expired_lease_is_reclaimed_and_unknown_never_repeats_create(
    launch_repository,
) -> None:
    _db_path, repository, _watchdog_id, _proposal_id = launch_repository
    first = repository.claim_due(
        worker_id="dead-worker",
        now=NOW + timedelta(seconds=9),
        limit=10,
    )[0]
    assert repository.claim_due(
        worker_id="other-worker",
        now=NOW + timedelta(seconds=20),
        limit=10,
    ) == ()

    reclaimed = repository.claim_due(
        worker_id="other-worker",
        now=first.lease_until,
        limit=10,
    )[0]
    transition = repository.record_observation(
        reclaimed,
        observations={},
        fetch_complete=False,
        now=first.lease_until,
    )

    assert transition.state == "VERIFYING"
    assert transition.outcome == "UNKNOWN"
    assert transition.next_verify_at == first.lease_until + timedelta(minutes=1)


def test_retry_schedule_and_24_hour_reconciliation(launch_repository) -> None:
    _db_path, repository, _watchdog_id, _proposal_id = launch_repository
    checked_at = NOW + timedelta(seconds=9)
    delays = [1, 2, 5, 15, 30, 30]
    for delay_minutes in delays:
        lease = repository.claim_due(
            worker_id="verify-retry",
            now=checked_at,
            limit=1,
        )[0]
        transition = repository.record_observation(
            lease,
            observations={},
            fetch_complete=True,
            now=checked_at,
        )
        assert transition.next_verify_at == checked_at + timedelta(
            minutes=delay_minutes
        )
        checked_at = transition.next_verify_at

    deadline = NOW + timedelta(hours=24, seconds=8)
    lease = repository.claim_due(
        worker_id="verify-reconcile",
        now=deadline,
        limit=1,
    )[0]
    transition = repository.record_observation(
        lease,
        observations={},
        fetch_complete=False,
        now=deadline,
    )
    assert transition.state == "RECONCILE_REQUIRED"
    assert transition.reason_code == "LAUNCH_VERIFY_DEADLINE"


def test_exact_scope_mismatch_is_failed_visible(launch_repository) -> None:
    _db_path, repository, _watchdog_id, _proposal_id = launch_repository
    lease = repository.claim_due(
        worker_id="verify-a",
        now=NOW + timedelta(seconds=9),
        limit=1,
    )[0]
    wrong_scope = LaunchTargetObservation(
        created_ad_id="ad-1",
        account_id="different-account",
        adset_id="adset-1",
        ad_name="CityA | Карточка",
        configured_status="ACTIVE",
        effective_status="ACTIVE",
        fingerprint=FINGERPRINT,
    )

    transition = repository.record_observation(
        lease,
        observations={"ad-1": wrong_scope},
        fetch_complete=True,
        now=NOW + timedelta(seconds=10),
    )

    assert transition.state == "FAILED_VERIFICATION"
    assert transition.reason_code == "LAUNCH_EXACT_TARGET_MISMATCH"


def test_auto_launch_active_creates_proposal_without_provider_execution(
    tmp_path,
    monkeypatch,
) -> None:
    from services import auto_launch

    monkeypatch.setattr(
        auto_launch,
        "_AUTO_LAUNCH_STATE_FILE",
        tmp_path / "auto-launch.json",
    )
    auto_launch._save_auto_launch_state(  # noqa: SLF001
        {
            "schema_version": 2,
            "launched_today": [],
            "launched_ever": {},
            "launch_attempts": {},
            "last_launch_date": None,
        }
    )
    checked_plan = SimpleNamespace(media={})
    checker = SimpleNamespace(
        prepare_and_reserve=lambda *_args, **_kwargs: checked_plan
    )
    proposal_statuses: list[dict] = []

    def proposal_only(**kwargs):
        proposal_statuses.append(kwargs["status"])
        kwargs["status"]["proposal_state"] = "PENDING_OWNER"
        return {"proposal_id": "proposal-1"}

    with (
        patch(
            "services.auto_launch._get_autopilot_config",
            return_value={
                "launch_enabled": True,
                "enabled": True,
                "kill_switch": False,
                "max_launches_per_day": 1,
                "launch_checker": {"mode": "enforce"},
            },
        ),
        patch("services.auto_launch._analyze_coverage", return_value={}),
        patch("services.auto_launch._get_done_list_id", return_value="ready"),
        patch(
            "services.auto_launch._get_unlaunched_cards",
            return_value=[
                {
                    "id": "card-1",
                    "name": "Карточка",
                    "desc": "",
                    "labels": ["PRODA"],
                    "pos": 1,
                }
            ],
        ),
        patch("services.auto_launch._get_launch_checker", return_value=checker),
        patch("services.auto_launch._send_telegram"),
        patch("agent.launcher.launch_single", side_effect=proposal_only),
        patch("services.auto_launch._execute_launch") as provider_execution,
        patch("services.launch_checker_runtime.cleanup_prepared_media"),
    ):
        result = auto_launch.run_auto_launch(
            mode="active",
            max_launches=1,
            source="CRON",
        )

    provider_execution.assert_not_called()
    assert result["launched"] == []
    assert result["proposals"] == [
        {
            "card_id": "card-1",
            "card_name": "Карточка",
            "card_desc": "",
            "campaign_type": "leadgen",
            "cities": None,
            "reason": "запуск готовой карточки",
            "labels": ["PRODA"],
            "pos": 1,
            "proposal_id": "proposal-1",
            "state": "PENDING_OWNER",
        }
    ]
    assert auto_launch._load_auto_launch_state()["last_launch_date"] is None  # noqa: SLF001
