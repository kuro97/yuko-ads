"""Immutable owner-action models и exact-lineage SQLite transactions."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from services.database_migrations import apply_runtime_migrations
from services.owner_action_models import (
    EvidenceRecord,
    OwnerDecisionKind,
    PermitRetirementReason,
    ProposalKind,
    ProposalOrigin,
    ProposedActionPlan,
    ProposedTarget,
    SystemRedeliveryReason,
    canonical_json,
    canonical_sha256,
    proposal_hashes,
)
from services.owner_action_repository import (
    OwnerActionIdempotencyConflict,
    OwnerActionLifecycleConflict,
    OwnerActionLineageError,
    OwnerActionPermitUnavailable,
    OwnerActionRepository,
    OwnerActionTokenUnavailable,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
SHA_CONFIG = "c" * 64
SHA_LIVE = "e" * 64


@pytest.fixture
def repository(tmp_path) -> OwnerActionRepository:
    db_path = tmp_path / "owner-actions.db"
    apply_runtime_migrations(str(db_path))
    return OwnerActionRepository(db_path)


def _plan(
    *,
    idempotency_key: str = "owner-action:test:pause:1",
    claim_id: str = "claim-pause-1",
    subject_id: str = "ad-100",
    summary: str = "Пауза дорогого объявления",
) -> ProposedActionPlan:
    target_payload = {"status": "PAUSED", "reason": "CPL_HIGH"}
    evidence_payload = {"effective_status": "ACTIVE", "page_count": 1}
    return ProposedActionPlan(
        proposal_kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        idempotency_key=idempotency_key,
        source_ref="autopilot:slot:2026-07-27T10",
        actor="autopilot",
        summary=summary,
        targets=(
            ProposedTarget(
                claim_id=claim_id,
                ordinal=0,
                action_kind="PAUSE_AD",
                account_id="act-1",
                adset_id="adset-10",
                subject_id=subject_id,
                city="CityA",
                language="L1",
                intended_payload=target_payload,
                intended_payload_sha256=canonical_sha256(target_payload),
            ),
        ),
        evidence=(
            EvidenceRecord(
                evidence_kind="PROPOSAL_INVENTORY",
                source_system="FACEBOOK",
                subject_id=subject_id,
                observed_at=NOW,
                complete=True,
                payload=evidence_payload,
                payload_sha256=canonical_sha256(evidence_payload),
            ),
        ),
        config_version_sha256=SHA_CONFIG,
        valid_until=NOW + timedelta(hours=4),
        staged_media_root=None,
    )


def _multi_plan() -> ProposedActionPlan:
    base = _plan(
        idempotency_key="owner-action:test:multi:1",
        claim_id="claim-multi-0",
        subject_id="ad-multi-0",
        summary="Последовательная пауза двух объявлений",
    )
    second_payload = {"status": "PAUSED", "reason": "CPL_HIGH_2"}
    second = ProposedTarget(
        claim_id="claim-multi-1",
        ordinal=1,
        action_kind="PAUSE_AD",
        account_id="act-1",
        adset_id="adset-20",
        subject_id="ad-multi-1",
        city="CityC",
        language="L2",
        intended_payload=second_payload,
        intended_payload_sha256=canonical_sha256(second_payload),
    )
    return replace(base, targets=(base.targets[0], second))


def _connection(repository: OwnerActionRepository) -> sqlite3.Connection:
    connection = sqlite3.connect(repository._db_path)  # noqa: SLF001
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _move_to_pending_owner(
    repository: OwnerActionRepository,
    proposal_id: str,
) -> int:
    return repository.transition_lifecycle(
        proposal_id,
        expected_state="DELIVERY_PENDING",
        expected_version=1,
        new_state="PENDING_OWNER",
        delivery_generation=1,
        actor="delivery-worker",
        now=NOW + timedelta(minutes=1),
    )


def _insert_bound_callback(
    repository: OwnerActionRepository,
    *,
    proposal_id: str,
    token_id: str = "token-1",
    update_id: int = 1001,
    callback_query_id: str = "callback-1",
    decision: OwnerDecisionKind = OwnerDecisionKind.APPROVE,
) -> tuple[str, int, str]:
    raw_update = {
        "update_id": update_id,
        "callback_query": {"id": callback_query_id},
    }
    raw_update_json = canonical_json(raw_update).decode("utf-8")
    raw_update_sha256 = hashlib.sha256(raw_update_json.encode("utf-8")).hexdigest()
    delivery_id = f"delivery-{token_id}"
    rendered_text = "Одобрить действие?"
    button_spec = [{"decision": decision.value, "nonce": f"nonce-{token_id}"}]
    connection = _connection(repository)
    try:
        connection.execute(
            """
            INSERT INTO telegram_update_inbox (
                update_id, ingress_kind, bot_token_identity_sha256,
                raw_update_json, raw_update_sha256, state, received_at
            ) VALUES (?, 'GET_UPDATES', ?, ?, ?, 'PROCESSING', ?)
            """,
            (
                update_id,
                "b" * 64,
                raw_update_json,
                raw_update_sha256,
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO telegram_delivery_outbox (
                delivery_id, purpose, proposal_id, generation, dedupe_key,
                rendered_text, rendered_text_sha256, button_spec_json,
                button_spec_sha256, state, telegram_chat_id,
                telegram_message_id, created_at, sent_at
            ) VALUES (?, 'OWNER_PROPOSAL', ?, 1, ?, ?, ?, ?, ?, 'SENT', ?, ?, ?, ?)
            """,
            (
                delivery_id,
                proposal_id,
                f"proposal:{proposal_id}:generation:1",
                rendered_text,
                canonical_sha256(rendered_text),
                canonical_json(button_spec).decode("utf-8"),
                canonical_sha256(button_spec),
                777,
                888,
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
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id,
                f"nonce-{token_id}",
                "a" * 64,
                proposal_id,
                delivery_id,
                decision.value,
                42,
                777,
                888,
                NOW.isoformat(),
                (NOW + timedelta(hours=2)).isoformat(),
                (NOW + timedelta(seconds=1)).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return raw_update_sha256, update_id, callback_query_id


def _approve(repository: OwnerActionRepository, proposal_id: str):
    _move_to_pending_owner(repository, proposal_id)
    ingress_sha256, update_id, callback_query_id = _insert_bound_callback(
        repository,
        proposal_id=proposal_id,
    )
    return repository._record_owner_decision(  # noqa: SLF001
        proposal_id=proposal_id,
        token_id="token-1",
        decision=OwnerDecisionKind.APPROVE,
        owner_user_id=42,
        chat_id=777,
        message_id=888,
        delivery_generation=1,
        telegram_update_id=update_id,
        callback_query_id=callback_query_id,
        trusted_ingress_sha256=ingress_sha256,
        reason_text=None,
        actor="telegram-owner",
        expected_lifecycle_version=2,
        now=NOW + timedelta(minutes=2),
    )


def _execution_lineage(
    repository: OwnerActionRepository,
    plan: ProposedActionPlan,
):
    receipt = repository.propose_action(plan, now=NOW)
    decision = _approve(repository, receipt.proposal_id)
    assert decision.decision_id is not None
    view = repository.get_proposal(receipt.proposal_id)
    assert view is not None and view.active_job_id is not None
    version = repository.queue_execution(
        proposal_id=receipt.proposal_id,
        decision_id=decision.decision_id,
        job_id=view.active_job_id,
        expected_lifecycle_version=3,
        actor="executor",
        now=NOW + timedelta(minutes=3),
    )
    return receipt, decision.decision_id, view.active_job_id, version


def _permit_manifest(target: ProposedTarget) -> dict[str, str]:
    return {
        "claim_id": target.claim_id,
        "operation_kind": target.action_kind,
        "account_id": target.account_id,
        "resource_id": target.subject_id,
        "payload_sha256": target.intended_payload_sha256,
    }


def test_models_are_deeply_immutable_and_hashes_are_canonical() -> None:
    plan = _plan()
    reordered_payload = {"reason": "CPL_HIGH", "status": "PAUSED"}
    equivalent_plan = replace(
        plan,
        targets=(
            replace(
                plan.targets[0],
                intended_payload=reordered_payload,
                intended_payload_sha256=canonical_sha256(reordered_payload),
            ),
        ),
    )

    assert proposal_hashes(plan) == proposal_hashes(equivalent_plan)
    with pytest.raises(TypeError):
        plan.targets[0].intended_payload["status"] = "ACTIVE"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        plan.summary = "Подмена"  # type: ignore[misc]


def test_propose_action_is_atomic_idempotent_and_detects_conflict(
    repository: OwnerActionRepository,
) -> None:
    plan = _plan()

    first = repository.propose_action(plan, now=NOW)
    duplicate = repository.propose_action(plan, now=NOW + timedelta(minutes=1))

    assert first.deduplicated is False
    assert duplicate.deduplicated is True
    assert duplicate.proposal_id == first.proposal_id
    assert duplicate.proposal_sha256 == first.proposal_sha256
    view = repository.get_proposal(first.proposal_id)
    assert view is not None
    assert view.plan == plan
    assert view.state == "DELIVERY_PENDING"

    with pytest.raises(OwnerActionIdempotencyConflict, match="PAYLOAD_CONFLICT"):
        repository.propose_action(
            replace(plan, summary="Другой immutable plan"),
            now=NOW + timedelta(minutes=2),
        )

    connection = _connection(repository)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM owner_action_proposals"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM owner_action_events"
        ).fetchone()[0] == 1
        stored_summary = connection.execute(
            "SELECT summary FROM owner_action_proposals"
        ).fetchone()[0]
        assert stored_summary == plan.summary
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE owner_action_proposals SET summary = 'tampered'"
            )
    finally:
        connection.close()


def test_lifecycle_cas_rejects_stale_version_without_partial_event(
    repository: OwnerActionRepository,
) -> None:
    receipt = repository.propose_action(_plan(), now=NOW)

    new_version = _move_to_pending_owner(repository, receipt.proposal_id)

    assert new_version == 2
    with pytest.raises(OwnerActionLifecycleConflict, match="CAS_CONFLICT"):
        repository.transition_lifecycle(
            receipt.proposal_id,
            expected_state="PENDING_OWNER",
            expected_version=1,
            new_state="REJECTED",
            actor="stale-worker",
            now=NOW + timedelta(minutes=2),
        )
    view = repository.get_proposal(receipt.proposal_id)
    assert view is not None
    assert (view.state, view.lifecycle_version, view.delivery_generation) == (
        "PENDING_OWNER",
        2,
        1,
    )
    connection = _connection(repository)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM owner_action_events"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_list_proposals_uses_stable_parameterized_cursor(
    repository: OwnerActionRepository,
) -> None:
    first = repository.propose_action(
        _plan(idempotency_key="owner-action:list:1", claim_id="claim-list-1"),
        now=NOW,
    )
    second = repository.propose_action(
        _plan(
            idempotency_key="owner-action:list:2",
            claim_id="claim-list-2",
            subject_id="ad-200",
        ),
        now=NOW + timedelta(seconds=1),
    )

    page_one = repository.list_proposals(state="DELIVERY_PENDING", cursor=None, limit=1)
    page_two = repository.list_proposals(
        state="DELIVERY_PENDING",
        cursor=page_one.next_cursor,
        limit=1,
    )

    assert [item.proposal_id for item in page_one.items] == [second.proposal_id]
    assert page_one.next_cursor is not None
    assert [item.proposal_id for item in page_two.items] == [first.proposal_id]
    assert page_two.next_cursor is None


def test_owner_decision_rolls_back_on_binding_mismatch_then_commits_exact_job(
    repository: OwnerActionRepository,
) -> None:
    receipt = repository.propose_action(_plan(), now=NOW)
    _move_to_pending_owner(repository, receipt.proposal_id)
    ingress_sha256, update_id, callback_query_id = _insert_bound_callback(
        repository,
        proposal_id=receipt.proposal_id,
    )
    decision_args = {
        "proposal_id": receipt.proposal_id,
        "token_id": "token-1",
        "decision": OwnerDecisionKind.APPROVE,
        "owner_user_id": 42,
        "chat_id": 777,
        "message_id": 888,
        "delivery_generation": 1,
        "telegram_update_id": update_id,
        "callback_query_id": callback_query_id,
        "trusted_ingress_sha256": ingress_sha256,
        "reason_text": None,
        "actor": "telegram-owner",
        "expected_lifecycle_version": 2,
        "now": NOW + timedelta(minutes=2),
    }

    with pytest.raises(OwnerActionTokenUnavailable, match="BINDING_MISMATCH"):
        repository._record_owner_decision(  # noqa: SLF001
            **{**decision_args, "chat_id": 999}
        )

    connection = _connection(repository)
    try:
        token = connection.execute(
            "SELECT consumed_at FROM owner_callback_tokens WHERE token_id = 'token-1'"
        ).fetchone()
        assert token["consumed_at"] is None
        assert connection.execute(
            "SELECT COUNT(*) FROM owner_action_decisions"
        ).fetchone()[0] == 0
    finally:
        connection.close()

    result = repository._record_owner_decision(**decision_args)  # noqa: SLF001

    assert result.accepted is True
    assert result.state == "APPROVED"
    assert result.decision is OwnerDecisionKind.APPROVE
    view = repository.get_proposal(receipt.proposal_id)
    assert view is not None
    assert view.active_decision_id == result.decision_id
    assert view.active_job_id is not None
    connection = _connection(repository)
    try:
        job = connection.execute(
            "SELECT proposal_id, decision_id, state FROM owner_execution_jobs"
        ).fetchone()
        assert tuple(job) == (receipt.proposal_id, result.decision_id, "QUEUED")
    finally:
        connection.close()

    replay = repository._record_owner_decision(**decision_args)  # noqa: SLF001
    assert replay.accepted is False
    assert replay.reason_code == "CALLBACK_REPLAY"


def test_system_redelivery_is_atomic_revokes_old_tokens_and_rejects_stale_cas(
    repository: OwnerActionRepository,
) -> None:
    receipt = repository.propose_action(_plan(), now=NOW)
    _move_to_pending_owner(repository, receipt.proposal_id)
    _insert_bound_callback(repository, proposal_id=receipt.proposal_id)
    callback_secret = "callback-secret-with-at-least-32-bytes"

    prepared = repository.prepare_system_redelivery(
        receipt.proposal_id,
        expected_lifecycle_version=2,
        reason=SystemRedeliveryReason.CALLBACK_SECRET_ROTATION,
        rendered_text="Повторное подтверждение после ротации",
        owner_user_id=42,
        chat_id=777,
        callback_secret=callback_secret,
        actor="secret-rotation",
        now=NOW + timedelta(minutes=2),
    )

    assert prepared.generation == 2
    assert prepared.lifecycle_version == 3
    view = repository.get_proposal(receipt.proposal_id)
    assert view is not None
    assert (view.state, view.lifecycle_version, view.delivery_generation) == (
        "DELIVERY_PENDING",
        3,
        2,
    )
    connection = _connection(repository)
    try:
        old_token = connection.execute(
            """
            SELECT revoked_at, revoke_reason
            FROM owner_callback_tokens
            WHERE token_id = 'token-1'
            """
        ).fetchone()
        assert old_token["revoked_at"] is not None
        assert old_token["revoke_reason"] == "CALLBACK_SECRET_ROTATION"
        new_tokens = connection.execute(
            """
            SELECT public_nonce, token_mac_sha256, decision_kind,
                   expected_message_id, consumed_at, revoked_at
            FROM owner_callback_tokens
            WHERE proposal_id = ? AND delivery_generation = 2
            ORDER BY decision_kind
            """,
            (receipt.proposal_id,),
        ).fetchall()
        assert len(new_tokens) == 3
        assert all(
            row["expected_message_id"] is None
            and row["consumed_at"] is None
            and row["revoked_at"] is None
            for row in new_tokens
        )
        approve_token = next(
            row for row in new_tokens if row["decision_kind"] == "APPROVE"
        )
        material = (
            f"v1|{approve_token['public_nonce']}|{receipt.proposal_id}|2|APPROVE"
        ).encode("utf-8")
        assert approve_token["token_mac_sha256"] == hmac.new(
            callback_secret.encode("utf-8"),
            material,
            hashlib.sha256,
        ).hexdigest()
        outbox = connection.execute(
            """
            SELECT state, generation
            FROM telegram_delivery_outbox
            WHERE delivery_id = ?
            """,
            (prepared.delivery_id,),
        ).fetchone()
        assert tuple(outbox) == ("PENDING", 2)
        assert connection.execute(
            "SELECT COUNT(*) FROM owner_action_decisions"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM owner_execution_jobs"
        ).fetchone()[0] == 0
    finally:
        connection.close()

    with pytest.raises(OwnerActionLifecycleConflict, match="REDELIVERY"):
        repository.prepare_system_redelivery(
            receipt.proposal_id,
            expected_lifecycle_version=2,
            reason=SystemRedeliveryReason.REDELIVERY,
            rendered_text="Гоночный дубль",
            owner_user_id=42,
            chat_id=777,
            callback_secret=callback_secret,
            actor="racing-worker",
            now=NOW + timedelta(minutes=2),
        )
    connection = _connection(repository)
    try:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM telegram_delivery_outbox
            WHERE proposal_id = ? AND generation = 2
            """,
            (receipt.proposal_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_system_redelivery_rejects_unapproved_reason_before_writes(
    repository: OwnerActionRepository,
) -> None:
    receipt = repository.propose_action(_plan(), now=NOW)
    _move_to_pending_owner(repository, receipt.proposal_id)
    _insert_bound_callback(repository, proposal_id=receipt.proposal_id)

    with pytest.raises(ValueError, match="SystemRedeliveryReason"):
        repository.prepare_system_redelivery(
            receipt.proposal_id,
            expected_lifecycle_version=2,
            reason="MANUAL_RETRY",  # type: ignore[arg-type]
            rendered_text="Недопустимая доставка",
            owner_user_id=42,
            chat_id=777,
            callback_secret="callback-secret-with-at-least-32-bytes",
            actor="manual",
            now=NOW + timedelta(minutes=2),
        )

    view = repository.get_proposal(receipt.proposal_id)
    assert view is not None
    assert (view.state, view.lifecycle_version, view.delivery_generation) == (
        "PENDING_OWNER",
        2,
        1,
    )


def test_concurrent_system_redelivery_has_exactly_one_cas_winner(
    repository: OwnerActionRepository,
) -> None:
    receipt = repository.propose_action(_plan(), now=NOW)
    _move_to_pending_owner(repository, receipt.proposal_id)
    _insert_bound_callback(repository, proposal_id=receipt.proposal_id)
    barrier = threading.Barrier(2)

    def prepare(actor: str) -> str:
        barrier.wait()
        try:
            repository.prepare_system_redelivery(
                receipt.proposal_id,
                expected_lifecycle_version=2,
                reason=SystemRedeliveryReason.REDELIVERY,
                rendered_text="Гоночная повторная доставка",
                owner_user_id=42,
                chat_id=777,
                callback_secret="callback-secret-with-at-least-32-bytes",
                actor=actor,
                now=NOW + timedelta(minutes=2),
            )
        except OwnerActionLifecycleConflict:
            return "CONFLICT"
        return "PREPARED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(prepare, ("worker-a", "worker-b")))

    assert sorted(results) == ["CONFLICT", "PREPARED"]
    connection = _connection(repository)
    try:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM telegram_delivery_outbox
            WHERE proposal_id = ? AND generation = 2
            """,
            (receipt.proposal_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT COUNT(*) FROM owner_callback_tokens
            WHERE proposal_id = ? AND delivery_generation = 2
            """,
            (receipt.proposal_id,),
        ).fetchone()[0] == 3
    finally:
        connection.close()


def test_permit_consumption_and_attempt_result_enforce_exact_lineage(
    repository: OwnerActionRepository,
) -> None:
    receipt = repository.propose_action(_plan(), now=NOW)
    decision = _approve(repository, receipt.proposal_id)
    assert decision.decision_id is not None
    view = repository.get_proposal(receipt.proposal_id)
    assert view is not None and view.active_job_id is not None
    job_id = view.active_job_id
    decision_id = decision.decision_id

    version = repository.queue_execution(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        expected_lifecycle_version=3,
        actor="executor",
        now=NOW + timedelta(minutes=3),
    )
    version = repository.begin_live_review(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        expected_lifecycle_version=version,
        actor="executor",
        now=NOW + timedelta(minutes=4),
    )
    manifest = {
        "claim_id": "claim-pause-1",
        "operation_kind": "PAUSE_AD",
        "account_id": "act-1",
        "resource_id": "ad-100",
        "payload_sha256": _plan().targets[0].intended_payload_sha256,
    }
    with pytest.raises(OwnerActionLineageError, match="lineage"):
        repository.issue_technical_permit(
            proposal_id=receipt.proposal_id,
            decision_id=decision_id,
            job_id="job-from-another-proposal",
            claim_id="claim-pause-1",
            operation_kind="PAUSE_AD",
            account_id="act-1",
            resource_id="ad-100",
            exact_payload_sha256=_plan().targets[0].intended_payload_sha256,
            manifest=manifest,
            manifest_sha256=canonical_sha256(manifest),
            live_evidence_sha256=SHA_LIVE,
            expires_at=NOW + timedelta(minutes=6),
            actor="executor",
            expected_lifecycle_version=version,
            now=NOW + timedelta(minutes=5),
        )

    permit = repository.issue_technical_permit(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id="claim-pause-1",
        operation_kind="PAUSE_AD",
        account_id="act-1",
        resource_id="ad-100",
        exact_payload_sha256=_plan().targets[0].intended_payload_sha256,
        manifest=manifest,
        manifest_sha256=canonical_sha256(manifest),
        live_evidence_sha256=SHA_LIVE,
        expires_at=NOW + timedelta(minutes=6),
        actor="executor",
        expected_lifecycle_version=version,
        now=NOW + timedelta(minutes=5),
    )

    with pytest.raises(OwnerActionPermitUnavailable, match="SCOPE_OR_STATE"):
        repository.consume_technical_permit(
            permit_secret=permit.secret,
            exact_payload_sha256="f" * 64,
            actor="executor",
            expected_lifecycle_version=version + 1,
            now=NOW + timedelta(minutes=5, seconds=10),
        )

    attestation = repository.consume_technical_permit(
        permit_secret=permit.secret,
        exact_payload_sha256=_plan().targets[0].intended_payload_sha256,
        actor="executor",
        expected_lifecycle_version=version + 1,
        now=NOW + timedelta(minutes=5, seconds=20),
    )
    assert attestation.claim_id == "claim-pause-1"
    assert attestation.proposal_id == receipt.proposal_id
    with pytest.raises(OwnerActionPermitUnavailable):
        repository.consume_technical_permit(
            permit_secret=permit.secret,
            exact_payload_sha256=attestation.payload_sha256,
            actor="replay-worker",
            now=NOW + timedelta(minutes=5, seconds=30),
        )

    repository.transition_attempt(
        attestation.attempt_id,
        expected_state="ATTEMPT_STARTED",
        new_state="CONFIRMED",
        actor="executor",
        provider_request_id="fb-request-1",
        provider_result={"success": True},
        now=NOW + timedelta(minutes=5, seconds=40),
    )
    final_view = repository.get_proposal(receipt.proposal_id)
    assert final_view is not None
    assert final_view.state == "EXECUTED"
    connection = _connection(repository)
    try:
        attempts = connection.execute(
            """
            SELECT proposal_id, decision_id, job_id, claim_id, state
            FROM owner_action_attempts
            """
        ).fetchall()
        assert [tuple(row) for row in attempts] == [
            (
                receipt.proposal_id,
                decision_id,
                job_id,
                "claim-pause-1",
                "CONFIRMED",
            )
        ]
        event_sequences = [
            row[0]
            for row in connection.execute(
                """
                SELECT event_seq FROM owner_action_events
                WHERE proposal_id = ? ORDER BY event_seq
                """,
                (receipt.proposal_id,),
            )
        ]
        assert event_sequences == list(range(1, len(event_sequences) + 1))
    finally:
        connection.close()


def test_multi_target_claims_execute_in_ordinal_order_with_single_cas_winner(
    repository: OwnerActionRepository,
) -> None:
    plan = _multi_plan()
    receipt, decision_id, job_id, version = _execution_lineage(repository, plan)
    progress = repository.get_claim_progress(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
    )
    assert (progress.state, progress.next_claim_id, progress.next_ordinal) == (
        "READY",
        "claim-multi-0",
        0,
    )
    version = repository.begin_claim_live_review(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id="claim-multi-0",
        expected_lifecycle_state="EXECUTION_QUEUED",
        expected_lifecycle_version=version,
        actor="worker-1",
        now=NOW + timedelta(minutes=4),
    )
    with pytest.raises(OwnerActionLineageError, match="lineage"):
        repository.begin_claim_live_review(
            proposal_id=receipt.proposal_id,
            decision_id=decision_id,
            job_id=job_id,
            claim_id="claim-multi-0",
            expected_lifecycle_state="EXECUTION_QUEUED",
            expected_lifecycle_version=version - 1,
            actor="worker-2",
            now=NOW + timedelta(minutes=4),
        )

    second_manifest = _permit_manifest(plan.targets[1])
    with pytest.raises(OwnerActionLineageError, match="следующим"):
        repository.issue_technical_permit(
            proposal_id=receipt.proposal_id,
            decision_id=decision_id,
            job_id=job_id,
            claim_id=plan.targets[1].claim_id,
            operation_kind=plan.targets[1].action_kind,
            account_id=plan.targets[1].account_id,
            resource_id=plan.targets[1].subject_id,
            exact_payload_sha256=plan.targets[1].intended_payload_sha256,
            manifest=second_manifest,
            manifest_sha256=canonical_sha256(second_manifest),
            live_evidence_sha256=SHA_LIVE,
            expires_at=NOW + timedelta(minutes=7),
            actor="worker-1",
            expected_lifecycle_version=version,
            now=NOW + timedelta(minutes=5),
        )

    first_manifest = _permit_manifest(plan.targets[0])
    first_permit = repository.issue_technical_permit(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id=plan.targets[0].claim_id,
        operation_kind=plan.targets[0].action_kind,
        account_id=plan.targets[0].account_id,
        resource_id=plan.targets[0].subject_id,
        exact_payload_sha256=plan.targets[0].intended_payload_sha256,
        manifest=first_manifest,
        manifest_sha256=canonical_sha256(first_manifest),
        live_evidence_sha256=SHA_LIVE,
        expires_at=NOW + timedelta(minutes=7),
        actor="worker-1",
        expected_lifecycle_version=version,
        now=NOW + timedelta(minutes=5),
    )
    first_attempt = repository.consume_technical_permit(
        permit_secret=first_permit.secret,
        exact_payload_sha256=plan.targets[0].intended_payload_sha256,
        actor="worker-1",
        expected_lifecycle_version=version + 1,
        now=NOW + timedelta(minutes=5, seconds=10),
    )
    first_result = repository.transition_attempt(
        first_attempt.attempt_id,
        expected_state="ATTEMPT_STARTED",
        new_state="CONFIRMED",
        actor="worker-1",
        provider_result={"success": True},
        now=NOW + timedelta(minutes=5, seconds=20),
    )
    assert (
        first_result.aggregate_state,
        first_result.next_claim_id,
        first_result.lifecycle_version,
    ) == ("EXECUTION_QUEUED", "claim-multi-1", version + 3)

    progress = repository.get_claim_progress(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
    )
    assert (progress.state, progress.completed_claims, progress.next_claim_id) == (
        "READY",
        1,
        "claim-multi-1",
    )
    version = repository.begin_claim_live_review(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id="claim-multi-1",
        expected_lifecycle_state="EXECUTION_QUEUED",
        expected_lifecycle_version=first_result.lifecycle_version,
        actor="worker-1",
        now=NOW + timedelta(minutes=6),
    )
    second_permit = repository.issue_technical_permit(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id=plan.targets[1].claim_id,
        operation_kind=plan.targets[1].action_kind,
        account_id=plan.targets[1].account_id,
        resource_id=plan.targets[1].subject_id,
        exact_payload_sha256=plan.targets[1].intended_payload_sha256,
        manifest=second_manifest,
        manifest_sha256=canonical_sha256(second_manifest),
        live_evidence_sha256="d" * 64,
        expires_at=NOW + timedelta(minutes=8),
        actor="worker-1",
        expected_lifecycle_version=version,
        now=NOW + timedelta(minutes=6, seconds=10),
    )
    second_attempt = repository.consume_technical_permit(
        permit_secret=second_permit.secret,
        exact_payload_sha256=plan.targets[1].intended_payload_sha256,
        actor="worker-1",
        expected_lifecycle_version=version + 1,
        now=NOW + timedelta(minutes=6, seconds=20),
    )
    second_result = repository.transition_attempt(
        second_attempt.attempt_id,
        expected_state="ATTEMPT_STARTED",
        new_state="CONFIRMED",
        actor="worker-1",
        provider_result={"success": True},
        now=NOW + timedelta(minutes=6, seconds=30),
    )
    assert second_result.aggregate_state == "EXECUTED"
    assert second_result.next_claim_id is None
    final_progress = repository.get_claim_progress(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
    )
    assert (final_progress.state, final_progress.completed_claims) == ("COMPLETE", 2)


def test_reconcile_required_claim_blocks_remaining_multi_target_work(
    repository: OwnerActionRepository,
) -> None:
    plan = _multi_plan()
    receipt, decision_id, job_id, version = _execution_lineage(repository, plan)
    version = repository.begin_claim_live_review(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id=plan.targets[0].claim_id,
        expected_lifecycle_state="EXECUTION_QUEUED",
        expected_lifecycle_version=version,
        actor="worker",
        now=NOW + timedelta(minutes=4),
    )
    manifest = _permit_manifest(plan.targets[0])
    permit = repository.issue_technical_permit(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id=plan.targets[0].claim_id,
        operation_kind=plan.targets[0].action_kind,
        account_id=plan.targets[0].account_id,
        resource_id=plan.targets[0].subject_id,
        exact_payload_sha256=plan.targets[0].intended_payload_sha256,
        manifest=manifest,
        manifest_sha256=canonical_sha256(manifest),
        live_evidence_sha256=SHA_LIVE,
        expires_at=NOW + timedelta(minutes=7),
        actor="worker",
        expected_lifecycle_version=version,
        now=NOW + timedelta(minutes=5),
    )
    attempt = repository.consume_technical_permit(
        permit_secret=permit.secret,
        exact_payload_sha256=plan.targets[0].intended_payload_sha256,
        actor="worker",
        now=NOW + timedelta(minutes=5, seconds=10),
    )
    result = repository.transition_attempt(
        attempt.attempt_id,
        expected_state="ATTEMPT_STARTED",
        new_state="RECONCILE_REQUIRED",
        actor="worker",
        reason_code="UNKNOWN_PROVIDER_RESULT",
        now=NOW + timedelta(minutes=5, seconds=20),
    )

    assert result.aggregate_state == "RECONCILE_REQUIRED"
    progress = repository.get_claim_progress(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
    )
    assert progress.state == "RECONCILE_REQUIRED"
    assert progress.next_claim_id is None
    with pytest.raises(OwnerActionLineageError):
        repository.begin_claim_live_review(
            proposal_id=receipt.proposal_id,
            decision_id=decision_id,
            job_id=job_id,
            claim_id=plan.targets[1].claim_id,
            expected_lifecycle_state="EXECUTION_QUEUED",
            expected_lifecycle_version=result.lifecycle_version,
            actor="other-worker",
            now=NOW + timedelta(minutes=6),
        )


def test_expired_unattempted_permit_requires_fresh_review_before_reissue(
    repository: OwnerActionRepository,
) -> None:
    plan = _plan()
    receipt, decision_id, job_id, version = _execution_lineage(repository, plan)
    version = repository.begin_claim_live_review(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id=plan.targets[0].claim_id,
        expected_lifecycle_state="EXECUTION_QUEUED",
        expected_lifecycle_version=version,
        actor="worker",
        now=NOW + timedelta(minutes=4),
    )
    manifest = _permit_manifest(plan.targets[0])
    first_permit = repository.issue_technical_permit(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id=plan.targets[0].claim_id,
        operation_kind=plan.targets[0].action_kind,
        account_id=plan.targets[0].account_id,
        resource_id=plan.targets[0].subject_id,
        exact_payload_sha256=plan.targets[0].intended_payload_sha256,
        manifest=manifest,
        manifest_sha256=canonical_sha256(manifest),
        live_evidence_sha256=SHA_LIVE,
        expires_at=NOW + timedelta(minutes=5),
        actor="worker",
        expected_lifecycle_version=version,
        now=NOW + timedelta(minutes=4, seconds=30),
    )
    with pytest.raises(OwnerActionPermitUnavailable, match="NOT_EXPIRED"):
        repository.retire_unattempted_permit(
            permit_id=first_permit.permit_id,
            expected_lifecycle_version=version + 1,
            reason=PermitRetirementReason.PERMIT_EXPIRED,
            actor="worker",
            now=NOW + timedelta(minutes=4, seconds=40),
        )

    version = repository.retire_unattempted_permit(
        permit_id=first_permit.permit_id,
        expected_lifecycle_version=version + 1,
        reason=PermitRetirementReason.PERMIT_EXPIRED,
        actor="worker",
        now=NOW + timedelta(minutes=5),
    )
    with pytest.raises(OwnerActionPermitUnavailable):
        repository.consume_technical_permit(
            permit_secret=first_permit.secret,
            exact_payload_sha256=plan.targets[0].intended_payload_sha256,
            actor="stale-worker",
            now=NOW + timedelta(minutes=5, seconds=1),
        )
    version = repository.begin_claim_live_review(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id=plan.targets[0].claim_id,
        expected_lifecycle_state="EXECUTION_RETRY_WAIT",
        expected_lifecycle_version=version,
        actor="worker",
        now=NOW + timedelta(minutes=5, seconds=10),
    )
    with pytest.raises(OwnerActionPermitUnavailable, match="FRESH_LIVE_REVIEW"):
        repository.issue_technical_permit(
            proposal_id=receipt.proposal_id,
            decision_id=decision_id,
            job_id=job_id,
            claim_id=plan.targets[0].claim_id,
            operation_kind=plan.targets[0].action_kind,
            account_id=plan.targets[0].account_id,
            resource_id=plan.targets[0].subject_id,
            exact_payload_sha256=plan.targets[0].intended_payload_sha256,
            manifest=manifest,
            manifest_sha256=canonical_sha256(manifest),
            live_evidence_sha256=SHA_LIVE,
            expires_at=NOW + timedelta(minutes=7),
            actor="worker",
            expected_lifecycle_version=version,
            now=NOW + timedelta(minutes=5, seconds=20),
        )
    second_permit = repository.issue_technical_permit(
        proposal_id=receipt.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id=plan.targets[0].claim_id,
        operation_kind=plan.targets[0].action_kind,
        account_id=plan.targets[0].account_id,
        resource_id=plan.targets[0].subject_id,
        exact_payload_sha256=plan.targets[0].intended_payload_sha256,
        manifest=manifest,
        manifest_sha256=canonical_sha256(manifest),
        live_evidence_sha256="d" * 64,
        expires_at=NOW + timedelta(minutes=7),
        actor="worker",
        expected_lifecycle_version=version,
        now=NOW + timedelta(minutes=5, seconds=21),
    )
    assert second_permit.permit_id != first_permit.permit_id
    connection = _connection(repository)
    try:
        phases = connection.execute(
            """
            SELECT sequence_no, phase
            FROM owner_technical_permits
            WHERE proposal_id = ? AND claim_id = ?
            ORDER BY sequence_no
            """,
            (receipt.proposal_id, plan.targets[0].claim_id),
        ).fetchall()
        assert [tuple(row) for row in phases] == [(1, "EXPIRED"), (2, "ISSUED")]
    finally:
        connection.close()
    repository.consume_technical_permit(
        permit_secret=second_permit.secret,
        exact_payload_sha256=plan.targets[0].intended_payload_sha256,
        actor="worker",
        expected_lifecycle_version=version + 1,
        now=NOW + timedelta(minutes=5, seconds=30),
    )
    with pytest.raises(OwnerActionPermitUnavailable, match="RETIRE_LINEAGE"):
        repository.retire_unattempted_permit(
            permit_id=second_permit.permit_id,
            expected_lifecycle_version=version + 2,
            reason=PermitRetirementReason.PERMIT_REVOKED,
            actor="replay-worker",
            now=NOW + timedelta(minutes=5, seconds=40),
        )


def test_plan_rejects_hash_mismatch_and_noncanonical_target_order() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="не совпадает"):
        replace(
            plan.targets[0],
            intended_payload_sha256="0" * 64,
        )
    with pytest.raises(ValueError, match="непрерывными"):
        replace(
            plan,
            targets=(replace(plan.targets[0], ordinal=1),),
        )


def test_canonical_json_normalizes_timezone_and_mapping_order() -> None:
    local_time = datetime(2026, 7, 27, 15, 0, tzinfo=timezone(timedelta(hours=5)))
    assert canonical_json({"b": 2, "a": local_time}) == canonical_json(
        {"a": NOW, "b": 2}
    )
    assert json.loads(canonical_json({"amount": 1.50})) == {"amount": "1.5"}


def test_proposal_in_flight_by_idempotency_key_tracks_owner_and_executor_states(
    repository: OwnerActionRepository,
) -> None:
    """«В полёте» — от стейджинга до провайдера; отказ владельца полёт закрывает.

    Продюсер с durable-попыткой (auto_launch) по этому признаку отличает
    исполняющуюся цепочку от осиротевшей после падения процесса.
    """
    assert repository.proposal_in_flight_by_idempotency_key("owner-action:none") is False

    plan = _plan(idempotency_key="owner-action:test:in-flight:1")
    receipt = repository.propose_action(plan, now=NOW)
    assert repository.proposal_in_flight_by_idempotency_key(plan.idempotency_key) is True

    _move_to_pending_owner(repository, receipt.proposal_id)
    assert repository.proposal_in_flight_by_idempotency_key(plan.idempotency_key) is True

    queued_plan = _plan(
        idempotency_key="owner-action:test:in-flight:2",
        claim_id="claim-in-flight-2",
        subject_id="ad-in-flight-2",
    )
    _execution_lineage(repository, queued_plan)
    assert (
        repository.proposal_in_flight_by_idempotency_key(queued_plan.idempotency_key)
        is True
    )

    rejected_plan = _plan(
        idempotency_key="owner-action:test:in-flight:3",
        claim_id="claim-in-flight-3",
        subject_id="ad-in-flight-3",
    )
    rejected = repository.propose_action(rejected_plan, now=NOW)
    _move_to_pending_owner(repository, rejected.proposal_id)
    repository.transition_lifecycle(
        rejected.proposal_id,
        expected_state="PENDING_OWNER",
        expected_version=2,
        new_state="REJECTED",
        actor="telegram-owner",
        now=NOW + timedelta(minutes=2),
    )
    assert (
        repository.proposal_in_flight_by_idempotency_key(rejected_plan.idempotency_key)
        is False
    )
