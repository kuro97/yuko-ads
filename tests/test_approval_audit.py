from __future__ import annotations

import json
import multiprocessing
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from services.approval_audit import (
    ApprovalAuditConflictError,
    ApprovalAuditCorruptionError,
    ApprovalAuditSequenceError,
    ApprovalPermitInvalidError,
    append_event,
    append_delivery_event,
    append_item_result,
    append_report_check,
    consume_permit,
    find_operation,
    find_operation_by_id,
    find_report_check,
    issue_item_permit,
    read_action_history,
    reserve_operation,
    revoke_permit,
)
from services.approval_checker_models import (
    ActionBatchManifest,
    ActionKind,
    ActionOrigin,
    ActionPermit,
    ActionResult,
    ActionResultAuditEvent,
    ActionReview,
    ApprovalDecision,
    CheckAuditEvent,
    DeliveryAuditEvent,
    DeliveryChannel,
    DeliveryKind,
    OperationAttemptAttestation,
    OperationState,
    ReconciliationAuditEvent,
    ReportCheckAuditEvent,
    ReportTemplate,
    ReportVerdict,
    TimeWindow,
    TransportErrorCode,
    UnpauseManifest,
    manifest_sha256,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64


def _batch(*, count: int = 1, key: str | None = None) -> ActionBatchManifest:
    idempotency_key = key or str(uuid.uuid4())
    actions = tuple(
        UnpauseManifest(
            kind=ActionKind.UNPAUSE,
            manifest_id=f"item-{index}",
            origin=ActionOrigin.WEB,
            idempotency_key=str(uuid.uuid4()),
            prepared_at=NOW,
            ad_id=f"ad-{index}",
            adset_id=f"adset-{index}",
            expected_before_status="PAUSED",
            expected_after_status="ACTIVE",
            pre_inventory_sha256=SHA_A,
        )
        for index in range(count)
    )
    draft = ActionBatchManifest(
        batch_manifest_id=f"batch-{uuid.uuid4()}",
        correlation_id="approval-run-1",
        idempotency_key=idempotency_key,
        prepared_at=NOW,
        subject_ids=tuple(f"ad:ad-{index}" for index in range(count)),
        actions=actions,
        manifest_sha256=SHA_A,
    )
    return replace(draft, manifest_sha256=manifest_sha256(draft))


def _review_and_persist(
    path: Path,
    batch: ActionBatchManifest,
    *,
    item_index: int,
    checked_at: datetime,
    decision: ApprovalDecision = ApprovalDecision.APPROVED,
) -> ActionReview:
    operation = find_operation(batch.idempotency_key, path)
    assert operation is not None
    item = batch.actions[item_index]
    review = ActionReview(
        check_id=str(uuid.uuid4()),
        operation_id=operation.operation_id,
        idempotency_key=batch.idempotency_key,
        batch_manifest_id=batch.batch_manifest_id,
        item_id=item.manifest_id,
        item_index=item_index,
        action_kind=item.kind,
        decision=decision,
        checked_at=checked_at,
        expires_at=checked_at + timedelta(minutes=2) if decision is ApprovalDecision.APPROVED else None,
        batch_manifest_sha256=batch.manifest_sha256,
        item_manifest_sha256=manifest_sha256(item),
        evidence_state_sha256=SHA_A if decision is ApprovalDecision.APPROVED else None,
        subject_ids=(f"ad:{item.ad_id}",),
        issues=(),
        audit_persisted=True,
    )
    append_event(
        CheckAuditEvent(
            schema_version=1,
            event="check",
            event_id=str(uuid.uuid4()),
            written_at=checked_at,
            operation_id=review.operation_id,
            idempotency_key=review.idempotency_key,
            batch_manifest_id=review.batch_manifest_id,
            batch_manifest_sha256=review.batch_manifest_sha256,
            subject_ids=review.subject_ids,
            check_id=review.check_id,
            item_id=review.item_id,
            item_index=review.item_index,
            action_kind=review.action_kind,
            item_manifest_sha256=review.item_manifest_sha256,
            decision=review.decision,
            checked_at=review.checked_at,
            expires_at=review.expires_at,
            final_live_state_sha256=review.evidence_state_sha256,
            issue_codes=(),
        ),
        path,
    )
    return review


def _permit(path: Path, batch: ActionBatchManifest, item_index: int = 0) -> ActionPermit:
    checked_at = NOW + timedelta(seconds=1 + item_index * 10)
    review = _review_and_persist(path, batch, item_index=item_index, checked_at=checked_at)
    return issue_item_permit(
        review,
        batch,
        batch.actions[item_index],
        checked_at + timedelta(seconds=1),
        path,
    )


def _result_event(
    path: Path,
    batch: ActionBatchManifest,
    permit: ActionPermit,
    attempt_id: str | OperationAttemptAttestation,
    *,
    result: ActionResult = ActionResult.CONFIRMED,
    completed_at: datetime = NOW + timedelta(seconds=5),
) -> ActionResultAuditEvent:
    operation = find_operation(batch.idempotency_key, path)
    assert operation is not None
    return ActionResultAuditEvent(
        schema_version=1,
        event="action_result",
        event_id=str(uuid.uuid4()),
        written_at=completed_at,
        operation_id=operation.operation_id,
        idempotency_key=batch.idempotency_key,
        batch_manifest_id=batch.batch_manifest_id,
        batch_manifest_sha256=batch.manifest_sha256,
        subject_ids=permit.subject_ids,
        permit_id=permit.permit_id,
        attempt_id=(
            attempt_id.attempt_id
            if isinstance(attempt_id, OperationAttemptAttestation)
            else attempt_id
        ),
        item_id=permit.item_id,
        item_index=permit.item_index,
        action_kind=permit.action_kind,
        result=result,
        exact_created_ids=(),
        postcondition_sha256=SHA_A,
        reason_code="TARGET_ACTIVE",
        remote_may_have_changed=True,
        reconciliation_required=result is not ActionResult.CONFIRMED,
        completed_at=completed_at,
    )


def _report_event(
    *,
    event_id: str = "event-report-1",
    check_id: str = "check-report-1",
    verdict: ReportVerdict = ReportVerdict.VERIFIED,
) -> ReportCheckAuditEvent:
    return ReportCheckAuditEvent(
        schema_version=1,
        event="report_check",
        event_id=event_id,
        written_at=NOW + timedelta(seconds=2),
        correlation_id="report-run-1",
        check_id=check_id,
        report_template=ReportTemplate.AUTOPILOT,
        verdict=verdict,
        manifest_sha256=SHA_A,
        evidence_state_sha256=SHA_A,
        issue_codes=(),
        checked_at=NOW + timedelta(seconds=1),
    )


def _delivery_event(
    *,
    event_id: str = "event-delivery-1",
    delivery_id: str = "delivery-1",
    check_id: str = "check-report-1",
) -> DeliveryAuditEvent:
    return DeliveryAuditEvent(
        schema_version=1,
        event="delivery",
        event_id=event_id,
        written_at=NOW + timedelta(seconds=4),
        delivery_id=delivery_id,
        delivery_kind=DeliveryKind.REPORT,
        reference_id=check_id,
        payload_sha256=SHA_A,
        buttons_sha256="b" * 64,
        manifest_sha256=SHA_A,
        report_verdict=ReportVerdict.VERIFIED,
        action_result=None,
        channel=DeliveryChannel.ADS,
        sent=True,
        fallback_sent=False,
        transport_error_code=None,
        delivered_at=NOW + timedelta(seconds=3),
    )


def _reserve_worker(batch: ActionBatchManifest, path: str, queue: multiprocessing.Queue) -> None:
    try:
        operation = reserve_operation(batch, NOW, Path(path))
        queue.put(("ok", operation.operation_id))
    except Exception as exc:  # pragma: no cover - assertion проверяет subprocess result
        queue.put(("error", type(exc).__name__))


def _consume_worker(
    permit: ActionPermit,
    batch: ActionBatchManifest,
    path: str,
    queue: multiprocessing.Queue,
) -> None:
    try:
        attempt_id = consume_permit(
            permit,
            batch,
            permit.evidence_state_sha256,
            permit.issued_at + timedelta(seconds=1),
            Path(path),
        )
        queue.put(("ok", attempt_id.attempt_id))
    except Exception as exc:  # pragma: no cover - assertion проверяет subprocess result
        queue.put(("error", getattr(exc, "code", type(exc).__name__)))


def _revoke_worker(
    permit: ActionPermit,
    path: str,
    queue: multiprocessing.Queue,
) -> None:
    try:
        operation = revoke_permit(
            permit,
            "PRECONDITION_MISMATCH",
            permit.issued_at + timedelta(seconds=1),
            Path(path),
        )
        queue.put(("ok", operation.operation_id))
    except Exception as exc:  # pragma: no cover - assertion проверяет subprocess result
        queue.put(("error", getattr(exc, "code", type(exc).__name__)))


def _append_report_worker(
    event: ReportCheckAuditEvent,
    path: str,
    queue: multiprocessing.Queue,
) -> None:
    try:
        event_id = append_report_check(event, Path(path))
        queue.put(("ok", event_id))
    except Exception as exc:  # pragma: no cover - assertion проверяет subprocess result
        queue.put(("error", getattr(exc, "code", type(exc).__name__)))


def _append_delivery_worker(
    event: DeliveryAuditEvent,
    path: str,
    queue: multiprocessing.Queue,
) -> None:
    try:
        event_id = append_delivery_event(event, Path(path))
        queue.put(("ok", event_id))
    except Exception as exc:  # pragma: no cover - assertion проверяет subprocess result
        queue.put(("error", getattr(exc, "code", type(exc).__name__)))


def _run_race(target: object, args: tuple[object, ...], process_count: int = 8) -> list[tuple[str, str]]:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [context.Process(target=target, args=(*args, queue)) for _ in range(process_count)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    return [queue.get(timeout=2) for _ in processes]


def test_reserve_is_idempotent_and_conflict_is_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()

    first = reserve_operation(batch, NOW, path)
    second = reserve_operation(batch, NOW + timedelta(seconds=1), path)

    assert first.operation_id == second.operation_id
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    changed_action = replace(batch.actions[0], ad_id="ad-other", manifest_id="item-other")
    changed = replace(
        batch,
        actions=(changed_action,),
        subject_ids=("ad:ad-other",),
        manifest_sha256=SHA_A,
    )
    changed = replace(changed, manifest_sha256=manifest_sha256(changed))
    with pytest.raises(ApprovalAuditConflictError):
        reserve_operation(changed, NOW, path)


def test_find_operation_by_exact_id_never_confuses_idempotency_key(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    operation = reserve_operation(batch, NOW, path)

    assert find_operation_by_id(operation.operation_id, path) == operation
    assert find_operation_by_id(batch.idempotency_key, path) is None
    assert find_operation_by_id(str(uuid.uuid4()), path) is None
    with pytest.raises(ValueError, match="operation_id"):
        find_operation_by_id("not-an-operation-id", path)
    with pytest.raises(ValueError, match="canonical"):
        find_operation_by_id(operation.operation_id.upper(), path)


def test_find_operation_by_id_is_fail_closed_on_corrupt_wal(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    operation = reserve_operation(_batch(), NOW, path)
    path.write_bytes(path.read_bytes() + b'{"event":"broken"')

    with pytest.raises(ApprovalAuditCorruptionError, match="оборванную"):
        find_operation_by_id(operation.operation_id, path)


def test_multiprocess_same_idempotency_key_creates_one_reservation(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()

    results = _run_race(_reserve_worker, (batch, str(path)))

    assert {status for status, _ in results} == {"ok"}
    assert len({operation_id for _, operation_id in results}) == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_report_check_append_and_replay_do_not_change_action_state(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    before = reserve_operation(batch, NOW, path)
    event = _report_event()

    first_event_id = append_report_check(event, path)
    second_event_id = append_report_check(event, path)
    after = find_operation(batch.idempotency_key, path)

    assert first_event_id == second_event_id == event.event_id
    assert after == before
    lines = path.read_text(encoding="utf-8").splitlines()
    assert sum('"event":"report_check"' in line for line in lines) == 1


def test_report_check_replay_conflict_is_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    event = _report_event()
    append_report_check(event, path)

    with pytest.raises(ApprovalAuditConflictError):
        append_report_check(replace(event, verdict=ReportVerdict.BLOCKED), path)
    with pytest.raises(ApprovalAuditConflictError):
        append_report_check(
            replace(event, event_id="event-report-2", verdict=ReportVerdict.BLOCKED),
            path,
        )
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_multiprocess_report_check_replay_appends_once(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    event = _report_event()

    results = _run_race(_append_report_worker, (event, str(path)))

    assert results == [("ok", event.event_id)] * len(results)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert sum('"event":"report_check"' in line for line in lines) == 1


def test_delivery_append_replay_and_find_report_proof(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    report = _report_event()
    delivery = _delivery_event()
    append_report_check(report, path)

    persisted_report = find_report_check(report.check_id, path)
    first = append_delivery_event(delivery, path)
    replay = append_delivery_event(delivery, path)

    assert persisted_report == report
    assert first == replay == delivery.event_id
    lines = path.read_text(encoding="utf-8").splitlines()
    assert sum('"event":"delivery"' in line for line in lines) == 1
    encoded = lines[-1]
    persisted = json.loads(encoded)
    assert persisted["payload_sha256"] == SHA_A
    assert persisted["buttons_sha256"] == "b" * 64
    for forbidden in ("text", "buttons", "phone", "payment_id", "raw_error"):
        assert forbidden not in persisted


def test_delivery_conflict_or_missing_proof_is_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    report = _report_event()
    delivery = _delivery_event()

    with pytest.raises(ApprovalAuditSequenceError, match="report check proof"):
        append_delivery_event(delivery, path)
    append_report_check(report, path)
    append_delivery_event(delivery, path)
    with pytest.raises(ApprovalAuditConflictError):
        append_delivery_event(replace(delivery, sent=False, fallback_sent=True), path)
    with pytest.raises(ApprovalAuditConflictError):
        append_delivery_event(replace(delivery, payload_sha256="c" * 64), path)
    with pytest.raises(ApprovalAuditConflictError):
        append_delivery_event(replace(delivery, buttons_sha256="d" * 64), path)
    with pytest.raises(ApprovalAuditConflictError):
        append_delivery_event(
            replace(
                delivery,
                event_id="event-delivery-2",
                sent=False,
                fallback_sent=True,
                transport_error_code=TransportErrorCode.NETWORK_ERROR,
            ),
            path,
        )
    assert sum(
        '"event":"delivery"' in line
        for line in path.read_text(encoding="utf-8").splitlines()
    ) == 1


def test_multiprocess_delivery_replay_appends_once(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    report = _report_event()
    delivery = _delivery_event()
    append_report_check(report, path)

    results = _run_race(_append_delivery_worker, (delivery, str(path)))

    assert results == [("ok", delivery.event_id)] * len(results)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert sum('"event":"delivery"' in line for line in lines) == 1


def test_permit_is_single_use_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    reserve_operation(batch, NOW, path)
    permit = _permit(path, batch)

    results = _run_race(_consume_worker, (permit, batch, str(path)))

    assert sum(status == "ok" for status, _ in results) == 1
    assert sum(value == "PERMIT_INVALID" for status, value in results if status == "error") == 7
    operation = find_operation(batch.idempotency_key, path)
    assert operation is not None
    assert operation.state is OperationState.UNKNOWN
    assert operation.reconciliation_required is True
    assert len([line for line in path.read_text().splitlines() if '"event":"action_attempt"' in line]) == 1


def test_issue_permit_defense_blocks_when_global_enforcement_is_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    reserve_operation(batch, NOW, path)
    review = _review_and_persist(
        path,
        batch,
        item_index=0,
        checked_at=NOW + timedelta(seconds=1),
    )
    before = path.read_bytes()
    monkeypatch.setattr(config, "REPORT_CHECKER_ENFORCE_ACTIONS", False)

    with pytest.raises(ApprovalPermitInvalidError, match="enforcement выключен"):
        issue_item_permit(
            review,
            batch,
            batch.actions[0],
            NOW + timedelta(seconds=2),
            path,
        )

    assert path.read_bytes() == before
    assert b'"event":"permit_issued"' not in before


def test_issue_permit_missing_enforcement_config_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    reserve_operation(batch, NOW, path)
    review = _review_and_persist(
        path,
        batch,
        item_index=0,
        checked_at=NOW + timedelta(seconds=1),
    )
    monkeypatch.delattr(config, "REPORT_CHECKER_ENFORCE_ACTIONS")

    with pytest.raises(ApprovalPermitInvalidError, match="enforcement выключен"):
        issue_item_permit(
            review,
            batch,
            batch.actions[0],
            NOW + timedelta(seconds=2),
            path,
        )


def test_revoke_is_idempotent_permanent_and_blocks_batch_successor(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch(count=2)
    reserve_operation(batch, NOW, path)
    permit = _permit(path, batch)
    before_revoke = len(path.read_text(encoding="utf-8").splitlines())

    revoked = revoke_permit(
        permit,
        "PRECONDITION_MISMATCH",
        NOW + timedelta(seconds=3),
        path,
    )
    replayed = revoke_permit(
        permit,
        "PRECONDITION_MISMATCH",
        NOW + timedelta(seconds=4),
        path,
    )

    assert revoked.state is OperationState.DENIED
    assert replayed.state is OperationState.DENIED
    assert revoked.items[0].permit_revoked_at == NOW + timedelta(seconds=3)
    assert revoked.items[0].permit_revocation_reason_code == "PRECONDITION_MISMATCH"
    assert len(path.read_text(encoding="utf-8").splitlines()) == before_revoke + 1
    with pytest.raises(ApprovalPermitInvalidError, match="отозван"):
        consume_permit(permit, batch, SHA_A, NOW + timedelta(seconds=5), path)
    with pytest.raises(ApprovalPermitInvalidError, match="другой причиной"):
        revoke_permit(
            permit,
            "INPUT_DRIFT",
            NOW + timedelta(seconds=5),
            path,
        )
    with pytest.raises(ApprovalAuditSequenceError, match="предыдущий item"):
        _review_and_persist(
            path,
            batch,
            item_index=1,
            checked_at=NOW + timedelta(seconds=5),
        )


def test_multiprocess_same_revoke_appends_exactly_one_event(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    reserve_operation(batch, NOW, path)
    permit = _permit(path, batch)

    results = _run_race(_revoke_worker, (permit, str(path)))

    assert {status for status, _ in results} == {"ok"}
    assert len({operation_id for _, operation_id in results}) == 1
    lines = path.read_text(encoding="utf-8").splitlines()
    assert sum('"event":"permit_revoked"' in line for line in lines) == 1


def test_used_permit_cannot_be_revoked(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    reserve_operation(batch, NOW, path)
    permit = _permit(path, batch)
    consume_permit(permit, batch, SHA_A, NOW + timedelta(seconds=3), path)

    with pytest.raises(ApprovalPermitInvalidError, match="использованный"):
        revoke_permit(
            permit,
            "PRECONDITION_MISMATCH",
            NOW + timedelta(seconds=4),
            path,
        )


def test_successor_cannot_get_check_or_permit_until_predecessor_confirmed(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch(count=2)
    reserve_operation(batch, NOW, path)

    with pytest.raises(ApprovalAuditSequenceError, match="предыдущий item"):
        _review_and_persist(path, batch, item_index=1, checked_at=NOW + timedelta(seconds=1))

    first_permit = _permit(path, batch, 0)
    attempt_id = consume_permit(
        first_permit,
        batch,
        SHA_A,
        NOW + timedelta(seconds=3),
        path,
    )
    with pytest.raises(ApprovalAuditSequenceError, match="предыдущий item"):
        _review_and_persist(path, batch, item_index=1, checked_at=NOW + timedelta(seconds=4))

    append_item_result(
        _result_event(path, batch, first_permit, attempt_id, completed_at=NOW + timedelta(seconds=5)),
        path,
    )
    second = _permit(path, batch, 1)
    assert second.item_index == 1


def test_crash_after_attempt_recovers_unknown_and_never_replays(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    reserve_operation(batch, NOW, path)
    permit = _permit(path, batch)
    attempt_id = consume_permit(permit, batch, SHA_A, NOW + timedelta(seconds=3), path)

    recovered = find_operation(batch.idempotency_key, path)
    assert recovered is not None
    assert recovered.state is OperationState.UNKNOWN
    assert recovered.items[0].attempt_id == attempt_id.attempt_id
    assert recovered.items[0].result is ActionResult.UNKNOWN
    assert recovered.reconciliation_required is True
    with pytest.raises(ApprovalPermitInvalidError):
        consume_permit(permit, batch, SHA_A, NOW + timedelta(seconds=4), path)
    review = replace(
        _review_from_record(recovered, batch),
        check_id=str(uuid.uuid4()),
        checked_at=NOW + timedelta(seconds=4),
        expires_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ApprovalPermitInvalidError):
        issue_item_permit(review, batch, batch.actions[0], NOW + timedelta(seconds=5), path)


def _review_from_record(operation: object, batch: ActionBatchManifest) -> ActionReview:
    return ActionReview(
        check_id=str(uuid.uuid4()),
        operation_id=operation.operation_id,  # type: ignore[attr-defined]
        idempotency_key=batch.idempotency_key,
        batch_manifest_id=batch.batch_manifest_id,
        item_id=batch.actions[0].manifest_id,
        item_index=0,
        action_kind=batch.actions[0].kind,
        decision=ApprovalDecision.APPROVED,
        checked_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        batch_manifest_sha256=batch.manifest_sha256,
        item_manifest_sha256=manifest_sha256(batch.actions[0]),
        evidence_state_sha256=SHA_A,
        subject_ids=("ad:ad-0",),
        issues=(),
        audit_persisted=False,
    )


def test_confirmed_result_and_history_are_reconstructed_from_wal(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    reserve_operation(batch, NOW, path)
    permit = _permit(path, batch)
    attempt_id = consume_permit(permit, batch, SHA_A, NOW + timedelta(seconds=3), path)

    completed = append_item_result(
        _result_event(path, batch, permit, attempt_id),
        path,
    )
    history = read_action_history(
        TimeWindow(
            start=NOW,
            end=NOW + timedelta(days=1),
            timezone_name="UTC",
            semantic="DAY",
        ),
        path,
    )

    assert completed.state is OperationState.CONFIRMED
    assert completed.reconciliation_required is False
    assert len(history) == 1
    assert history[0].attempt_id == attempt_id.attempt_id
    assert history[0].result is ActionResult.CONFIRMED


def test_action_delivery_requires_and_preserves_confirmed_operation_proof(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    reserve_operation(batch, NOW, path)
    permit = _permit(path, batch)
    attempt_id = consume_permit(permit, batch, SHA_A, NOW + timedelta(seconds=3), path)
    operation = append_item_result(
        _result_event(path, batch, permit, attempt_id),
        path,
    )
    delivery = DeliveryAuditEvent(
        schema_version=1,
        event="delivery",
        event_id="event-action-delivery-1",
        written_at=NOW + timedelta(seconds=7),
        delivery_id="action-delivery-1",
        delivery_kind=DeliveryKind.ACTION,
        reference_id=batch.idempotency_key,
        payload_sha256=SHA_A,
        buttons_sha256=None,
        manifest_sha256=batch.manifest_sha256,
        report_verdict=None,
        action_result=ActionResult.CONFIRMED,
        channel=DeliveryChannel.ADS,
        sent=True,
        fallback_sent=False,
        transport_error_code=None,
        delivered_at=NOW + timedelta(seconds=6),
    )

    append_delivery_event(delivery, path)

    assert find_operation(batch.idempotency_key, path) == operation


def test_crash_attempt_can_be_reconciled_without_replaying_mutation(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    operation = reserve_operation(batch, NOW, path)
    permit = _permit(path, batch)
    attempt_id = consume_permit(permit, batch, SHA_A, NOW + timedelta(seconds=3), path)

    append_event(
        ReconciliationAuditEvent(
            schema_version=1,
            event="reconciliation",
            event_id=str(uuid.uuid4()),
            written_at=NOW + timedelta(seconds=6),
            operation_id=operation.operation_id,
            idempotency_key=batch.idempotency_key,
            batch_manifest_id=batch.batch_manifest_id,
            batch_manifest_sha256=batch.manifest_sha256,
            subject_ids=permit.subject_ids,
            item_id=permit.item_id,
            item_index=permit.item_index,
            prior_attempt_id=attempt_id.attempt_id,
            final_result=ActionResult.CONFIRMED,
            exact_created_ids=(),
            postcondition_sha256=SHA_A,
            reconciled_at=NOW + timedelta(seconds=6),
        ),
        path,
    )

    recovered = find_operation(batch.idempotency_key, path)
    assert recovered is not None
    assert recovered.state is OperationState.CONFIRMED
    assert recovered.reconciliation_required is False
    assert recovered.items[0].attempt_id == attempt_id.attempt_id


@pytest.mark.parametrize(
    "broken",
    [
        b'{"schema_version":1',
        b'{"schema_version":1,"event":"invented"}\n',
        b"not-json\n",
    ],
)
def test_corruption_blocks_reads_and_new_writes(tmp_path: Path, broken: bytes) -> None:
    path = tmp_path / "approval.jsonl"
    path.write_bytes(broken)
    batch = _batch()

    with pytest.raises(ApprovalAuditCorruptionError):
        find_operation(batch.idempotency_key, path)
    with pytest.raises(ApprovalAuditCorruptionError):
        reserve_operation(batch, NOW, path)
    assert path.read_bytes() == broken


def test_unknown_extra_field_is_treated_as_possible_secret_and_blocks_wal(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    reserve_operation(batch, NOW, path)
    event = json.loads(path.read_text(encoding="utf-8"))
    event["access_token"] = "must-not-be-accepted"
    path.write_text(json.dumps(event, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(ApprovalAuditCorruptionError, match="набор полей"):
        find_operation(batch.idempotency_key, path)


def test_report_audit_rejects_pii_like_issue_and_corrupted_secret_field(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approval.jsonl"
    unsafe = replace(_report_event(), issue_codes=("customer phone 15550100482",))
    original = path.read_bytes() if path.exists() else b""

    with pytest.raises(ApprovalAuditCorruptionError, match="machine-readable"):
        append_report_check(unsafe, path)
    assert path.read_bytes() == original

    safe = _report_event()
    append_report_check(safe, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["raw_text"] = "lead name and phone"
    broken = json.dumps(payload, separators=(",", ":")) + "\n"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(ApprovalAuditCorruptionError, match="набор полей"):
        append_report_check(safe, path)
    assert path.read_text(encoding="utf-8") == broken


def test_delivery_corruption_with_text_or_buttons_blocks_entire_wal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approval.jsonl"
    report = _report_event()
    delivery = _delivery_event()
    append_report_check(report, path)
    append_delivery_event(delivery, path)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    lines[-1]["buttons"] = [{"text": "confirm"}]
    broken = "\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(ApprovalAuditCorruptionError, match="набор полей"):
        find_report_check(report.check_id, path)
    assert path.read_text(encoding="utf-8") == broken


def test_writes_use_append_mode_flock_and_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import services.approval_audit as audit

    path = tmp_path / "approval.jsonl"
    observed_flags: list[int] = []
    fsync_calls: list[int] = []
    flock_calls: list[int] = []
    real_open = audit.os.open
    real_fsync = audit.os.fsync
    real_flock = audit.fcntl.flock

    def recording_open(raw_path: object, flags: int, mode: int = 0o777) -> int:
        observed_flags.append(flags)
        return real_open(raw_path, flags, mode)

    def recording_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    def recording_flock(fd: int, operation: int) -> None:
        flock_calls.append(operation)
        real_flock(fd, operation)

    monkeypatch.setattr(audit.os, "open", recording_open)
    monkeypatch.setattr(audit.os, "fsync", recording_fsync)
    monkeypatch.setattr(audit.fcntl, "flock", recording_flock)

    reserve_operation(_batch(), NOW, path)

    assert observed_flags and all(flags & os.O_APPEND for flags in observed_flags)
    assert fsync_calls
    assert audit.fcntl.LOCK_EX in flock_calls
    assert audit.fcntl.LOCK_UN in flock_calls


def test_wal_contains_only_structured_ids_digests_and_codes(tmp_path: Path) -> None:
    path = tmp_path / "approval.jsonl"
    batch = _batch()
    reserve_operation(batch, NOW, path)
    permit = _permit(path, batch)
    attempt_id = consume_permit(permit, batch, SHA_A, NOW + timedelta(seconds=3), path)
    append_item_result(_result_event(path, batch, permit, attempt_id), path)

    forbidden_keys = {
        "token",
        "secret",
        "password",
        "body",
        "text",
        "email",
        "phone",
        "lead_name",
        "payment_amount",
    }
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        assert forbidden_keys.isdisjoint(event)
