from __future__ import annotations

import multiprocessing
import os
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from services.action_gateway_core import (
    AdapterMutationResult,
    AdapterPostconditionResult,
    execute_approved_item,
    recover_incomplete_operation,
)
from services.approval_audit import (
    append_event,
    consume_permit,
    find_operation,
    issue_item_permit,
    reserve_operation,
)
from services.approval_checker_models import (
    ActionBatchManifest,
    ActionKind,
    ActionObservation,
    ActionOrigin,
    ActionPermit,
    ActionResult,
    ActionReview,
    CheckAuditEvent,
    OperationItemRecord,
    OperationState,
    SafetyDecision,
    UnpauseManifest,
    manifest_sha256,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _batch(count: int = 1) -> ActionBatchManifest:
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
        correlation_id="gateway-mp",
        idempotency_key=str(uuid.uuid4()),
        prepared_at=NOW,
        subject_ids=tuple(f"ad:ad-{index}" for index in range(count)),
        actions=actions,
        manifest_sha256=SHA_A,
    )
    return replace(draft, manifest_sha256=manifest_sha256(draft))


def _review_and_permit(
    batch: ActionBatchManifest,
    item_index: int,
    checked_at: datetime,
) -> tuple[ActionReview, ActionPermit]:
    operation = find_operation(batch.idempotency_key)
    assert operation is not None
    item = batch.actions[item_index]
    subject_ids = (f"ad:{item.ad_id}",)
    review = ActionReview(
        check_id=str(uuid.uuid4()),
        operation_id=operation.operation_id,
        idempotency_key=batch.idempotency_key,
        batch_manifest_id=batch.batch_manifest_id,
        item_id=item.manifest_id,
        item_index=item_index,
        action_kind=item.kind,
        decision=SafetyDecision.SAFE,
        checked_at=checked_at,
        expires_at=checked_at + timedelta(minutes=2),
        batch_manifest_sha256=batch.manifest_sha256,
        item_manifest_sha256=manifest_sha256(item),
        evidence_state_sha256=SHA_A,
        subject_ids=subject_ids,
        issues=(),
        audit_persisted=True,
    )
    append_event(
        CheckAuditEvent(
            schema_version=1,
            event="check",
            event_id=str(uuid.uuid4()),
            written_at=checked_at,
            operation_id=operation.operation_id,
            idempotency_key=batch.idempotency_key,
            batch_manifest_id=batch.batch_manifest_id,
            batch_manifest_sha256=batch.manifest_sha256,
            subject_ids=subject_ids,
            check_id=review.check_id,
            item_id=item.manifest_id,
            item_index=item_index,
            action_kind=item.kind,
            item_manifest_sha256=review.item_manifest_sha256,
            decision=SafetyDecision.SAFE,
            checked_at=checked_at,
            expires_at=review.expires_at,
            final_live_state_sha256=SHA_A,
            issue_codes=(),
        )
    )
    permit = issue_item_permit(review, batch, item, checked_at + timedelta(seconds=1))
    return review, permit


class _SuccessfulAdapter:
    def __init__(self, marker_path: str | None = None) -> None:
        self.marker_path = marker_path
        self.mutation_calls = 0
        self.reconciled: list[int] = []
        self.scope_active = False
        self.events: list[str] = []

    @contextmanager
    def execution_scope(self, item: object, now: datetime):
        assert not self.scope_active
        self.scope_active = True
        self.events.append("scope_enter")
        try:
            yield
        finally:
            self.events.append("scope_exit")
            self.scope_active = False

    def read_precondition(self, item: object, now: datetime) -> ActionObservation:
        assert self.scope_active
        self.events.append("pre_read")
        ad_id = item.ad_id  # type: ignore[attr-defined]
        return ActionObservation(now, SHA_A, "PAUSED", (f"ad:{ad_id}",), SHA_B)

    def mutate(
        self, item: object, now: datetime, *, attempt: object
    ) -> AdapterMutationResult:
        del attempt
        assert self.scope_active
        self.events.append("mutate")
        self.mutation_calls += 1
        if self.marker_path is not None:
            with Path(self.marker_path).open("a", encoding="utf-8") as marker:
                marker.write("mutation\n")
                marker.flush()
                os.fsync(marker.fileno())
        return AdapterMutationResult(result=None, reason_code="PROVIDER_ACCEPTED")

    def read_postcondition(
        self,
        item: object,
        created_ids: tuple[str, ...],
        now: datetime,
    ) -> AdapterPostconditionResult:
        assert self.scope_active
        self.events.append("post_read")
        ad_id = item.ad_id  # type: ignore[attr-defined]
        observation = ActionObservation(now, SHA_B, "ACTIVE", (f"ad:{ad_id}",), SHA_B)
        return AdapterPostconditionResult(
            ActionResult.CONFIRMED,
            observation,
            created_ids,
            "TARGET_ACTIVE",
            True,
        )

    def finalize_after_scope(
        self,
        item: object,
        mutation: AdapterMutationResult,
        post: AdapterPostconditionResult,
        now: datetime,
    ) -> None:
        assert not self.scope_active
        self.events.append("finalize")

    def reconcile(
        self,
        item: OperationItemRecord,
        now: datetime,
    ) -> AdapterPostconditionResult:
        assert not self.scope_active
        self.reconciled.append(item.item_index)
        observation = ActionObservation(now, SHA_B, "ACTIVE", item.subject_ids, SHA_B)
        return AdapterPostconditionResult(
            ActionResult.CONFIRMED,
            observation,
            item.exact_created_ids,
            "TARGET_ACTIVE",
            True,
        )


class _RejectedPreconditionAdapter(_SuccessfulAdapter):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def read_precondition(self, item: object, now: datetime) -> ActionObservation:
        if self.mode == "read_error":
            assert self.scope_active
            self.events.append("pre_read")
            raise RuntimeError("raw provider detail must not reach WAL")
        observation = super().read_precondition(item, now)
        if self.mode == "mismatch":
            return replace(observation, digest=SHA_B)
        if self.mode == "subject_drift":
            return replace(observation, subject_ids=("ad:other",))
        raise AssertionError(f"unknown test mode: {self.mode}")


class _MutationExceptionAdapter(_SuccessfulAdapter):
    def mutate(
        self, item: object, now: datetime, *, attempt: object
    ) -> AdapterMutationResult:
        del attempt
        assert self.scope_active
        self.events.append("mutate")
        self.mutation_calls += 1
        raise TimeoutError("ambiguous provider timeout")


class _ScopeAcquisitionFailureAdapter(_SuccessfulAdapter):
    @contextmanager
    def execution_scope(self, item: object, now: datetime):
        self.events.append("scope_enter_failed")
        raise RuntimeError("lock unavailable")
        yield  # pragma: no cover


class _ScopeReleaseFailureAdapter(_SuccessfulAdapter):
    @contextmanager
    def execution_scope(self, item: object, now: datetime):
        self.scope_active = True
        self.events.append("scope_enter")
        try:
            yield
        finally:
            self.events.append("scope_exit_failed")
            self.scope_active = False
            raise RuntimeError("unlock failed")


class _FinalizerFailureAdapter(_SuccessfulAdapter):
    def finalize_after_scope(
        self,
        item: object,
        mutation: AdapterMutationResult,
        post: AdapterPostconditionResult,
        now: datetime,
    ) -> None:
        assert not self.scope_active
        self.events.append("finalize_failed")
        raise RuntimeError("cap commit failed")


def _execute_worker(
    batch: ActionBatchManifest,
    review: ActionReview,
    permit: ActionPermit,
    marker_path: str,
    queue: multiprocessing.Queue,
) -> None:
    execution = execute_approved_item(
        batch,
        batch.actions[0],
        0,
        review,
        permit,
        _SuccessfulAdapter(marker_path),
        NOW + timedelta(seconds=3),
    )
    queue.put(execution.result.value)


def _consume_and_crash_worker(
    permit: ActionPermit,
    batch: ActionBatchManifest,
    queue: multiprocessing.Queue,
) -> None:
    attempt_id = consume_permit(
        permit,
        batch,
        SHA_A,
        permit.issued_at + timedelta(seconds=1),
    )
    queue.put(attempt_id.attempt_id)
    # Процесс завершается без action_result — это и есть crash boundary.


@pytest.fixture
def isolated_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REPORT_CHECKER_DATA_ROOT", str(tmp_path))
    path = tmp_path / "approval_checker_audit.jsonl"
    monkeypatch.setattr(config, "REPORT_CHECKER_AUDIT_PATH", path)
    monkeypatch.setattr(
        config,
        "REPORT_CHECKER_OPERATION_LOCK_PATH",
        tmp_path / "approval_checker.lock",
    )
    return path


def test_same_permit_race_calls_remote_once(
    isolated_audit: Path, tmp_path: Path
) -> None:
    batch = _batch()
    reserve_operation(batch, NOW)
    review, permit = _review_and_permit(batch, 0, NOW + timedelta(seconds=1))
    marker_path = tmp_path / "provider-calls.txt"
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_execute_worker,
            args=(batch, review, permit, str(marker_path), queue),
        )
        for _ in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    results = [queue.get(timeout=2) for _ in processes]
    assert results.count(ActionResult.CONFIRMED.value) == 1
    assert marker_path.read_text(encoding="utf-8").splitlines() == ["mutation"]
    operation = find_operation(batch.idempotency_key)
    assert operation is not None
    assert operation.state is OperationState.CONFIRMED


def test_crash_between_items_reconciles_attempt_and_never_touches_successor(
    isolated_audit: Path,
) -> None:
    batch = _batch(count=3)
    reserve_operation(batch, NOW)

    first_review, first_permit = _review_and_permit(
        batch, 0, NOW + timedelta(seconds=1)
    )
    first_execution = execute_approved_item(
        batch,
        batch.actions[0],
        0,
        first_review,
        first_permit,
        _SuccessfulAdapter(),
        NOW + timedelta(seconds=3),
    )
    assert first_execution.result is ActionResult.CONFIRMED

    _, second_permit = _review_and_permit(batch, 1, NOW + timedelta(seconds=4))
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(
        target=_consume_and_crash_worker,
        args=(second_permit, batch, queue),
    )
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 0
    assert queue.get(timeout=2)

    crashed = find_operation(batch.idempotency_key)
    assert crashed is not None
    assert crashed.items[0].result is ActionResult.CONFIRMED
    assert crashed.items[1].result is ActionResult.UNKNOWN
    assert crashed.items[2].check_id is None

    adapter = _SuccessfulAdapter()
    run = recover_incomplete_operation(
        crashed,
        {ActionKind.UNPAUSE: adapter},
        NOW + timedelta(seconds=8),
    )

    assert adapter.mutation_calls == 0
    assert adapter.reconciled == [1]
    assert run.state is OperationState.PARTIAL
    assert run.result is ActionResult.PARTIAL
    assert run.first_unprocessed_index == 2
    final = find_operation(batch.idempotency_key)
    assert final is not None
    assert final.items[1].result is ActionResult.CONFIRMED
    assert final.items[2].permit_id is None


def test_result_wal_failure_after_mutation_returns_unknown(
    isolated_audit: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.action_gateway_core as core

    batch = _batch()
    reserve_operation(batch, NOW)
    review, permit = _review_and_permit(batch, 0, NOW + timedelta(seconds=1))
    adapter = _SuccessfulAdapter()
    monkeypatch.setattr(
        core, "append_item_result", lambda event: (_ for _ in ()).throw(OSError("disk"))
    )

    execution = execute_approved_item(
        batch,
        batch.actions[0],
        0,
        review,
        permit,
        adapter,
        NOW + timedelta(seconds=3),
    )

    assert adapter.mutation_calls == 1
    assert execution.result is ActionResult.UNKNOWN
    operation = find_operation(batch.idempotency_key)
    assert operation is not None
    assert operation.state is OperationState.UNKNOWN
    assert operation.reconciliation_required is True


def test_execution_scope_spans_pre_attempt_mutation_post_and_result(
    isolated_audit: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.action_gateway_core as core

    batch = _batch()
    reserve_operation(batch, NOW)
    review, permit = _review_and_permit(batch, 0, NOW + timedelta(seconds=1))
    adapter = _SuccessfulAdapter()
    real_consume = core.consume_permit
    real_append = core.append_item_result

    def tracked_consume(*args: object, **kwargs: object) -> str:
        assert adapter.scope_active
        adapter.events.append("attempt_fsync")
        return real_consume(*args, **kwargs)  # type: ignore[arg-type]

    def tracked_append(*args: object, **kwargs: object):
        assert not adapter.scope_active
        adapter.events.append("result_fsync")
        return real_append(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(core, "consume_permit", tracked_consume)
    monkeypatch.setattr(core, "append_item_result", tracked_append)

    execution = execute_approved_item(
        batch,
        batch.actions[0],
        0,
        review,
        permit,
        adapter,
        NOW + timedelta(seconds=3),
    )

    assert execution.result is ActionResult.CONFIRMED
    assert adapter.events == [
        "scope_enter",
        "pre_read",
        "attempt_fsync",
        "mutate",
        "post_read",
        "scope_exit",
        "finalize",
        "result_fsync",
    ]
    assert adapter.scope_active is False


def test_scope_released_after_mutation_exception_and_unknown_is_persisted(
    isolated_audit: Path,
) -> None:
    batch = _batch()
    reserve_operation(batch, NOW)
    review, permit = _review_and_permit(batch, 0, NOW + timedelta(seconds=1))
    adapter = _MutationExceptionAdapter()

    execution = execute_approved_item(
        batch,
        batch.actions[0],
        0,
        review,
        permit,
        adapter,
        NOW + timedelta(seconds=3),
    )

    assert execution.result is ActionResult.UNKNOWN
    # Текст исключения едет в reason_code: журнал сервера живёт 40 часов, задание — неделями.
    assert execution.reason_code == "PROVIDER_OUTCOME_UNKNOWN:TIMEOUTERROR:AMBIGUOUS_PROVIDER_TIMEOUT"
    assert adapter.events == [
        "scope_enter",
        "pre_read",
        "mutate",
        "scope_exit",
        "finalize",
    ]
    assert adapter.scope_active is False
    operation = find_operation(batch.idempotency_key)
    assert operation is not None
    assert operation.state is OperationState.UNKNOWN
    assert operation.reconciliation_required is True


@pytest.mark.parametrize(
    ("adapter", "expected_reason", "finalizer_called"),
    [
        (
            _ScopeReleaseFailureAdapter(),
            "EXECUTION_SCOPE_RELEASE_FAILED",
            False,
        ),
        (_FinalizerFailureAdapter(), "FINALIZE_AFTER_SCOPE_FAILED", True),
    ],
)
def test_scope_exit_or_finalizer_failure_forces_unknown_after_remote_call(
    isolated_audit: Path,
    adapter: _SuccessfulAdapter,
    expected_reason: str,
    finalizer_called: bool,
) -> None:
    batch = _batch()
    reserve_operation(batch, NOW)
    review, permit = _review_and_permit(batch, 0, NOW + timedelta(seconds=1))

    execution = execute_approved_item(
        batch,
        batch.actions[0],
        0,
        review,
        permit,
        adapter,
        NOW + timedelta(seconds=3),
    )

    assert execution.result is ActionResult.UNKNOWN
    assert execution.reason_code == expected_reason
    assert execution.remote_may_have_changed is True
    assert adapter.scope_active is False
    assert adapter.mutation_calls == 1
    assert ("finalize_failed" in adapter.events) is finalizer_called
    operation = find_operation(batch.idempotency_key)
    assert operation is not None
    assert operation.state is OperationState.UNKNOWN
    assert operation.items[0].result is ActionResult.UNKNOWN
    assert operation.reconciliation_required is True


def test_scope_acquisition_failure_revokes_permit_without_any_remote_call(
    isolated_audit: Path,
) -> None:
    batch = _batch()
    reserve_operation(batch, NOW)
    review, permit = _review_and_permit(batch, 0, NOW + timedelta(seconds=1))
    adapter = _ScopeAcquisitionFailureAdapter()

    execution = execute_approved_item(
        batch,
        batch.actions[0],
        0,
        review,
        permit,
        adapter,
        NOW + timedelta(seconds=3),
    )

    assert execution.result is ActionResult.FAILED
    assert execution.reason_code == "EXECUTION_SCOPE_FAILED"
    assert adapter.mutation_calls == 0
    assert adapter.events == ["scope_enter_failed"]
    operation = find_operation(batch.idempotency_key)
    assert operation is not None
    assert operation.state is OperationState.DENIED
    assert operation.items[0].permit_revocation_reason_code == "EXECUTION_SCOPE_FAILED"


@pytest.mark.parametrize(
    ("mode", "reason_code"),
    [
        ("read_error", "PRECONDITION_READ_FAILED"),
        ("mismatch", "PRECONDITION_MISMATCH"),
        ("subject_drift", "PRECONDITION_SUBJECT_DRIFT"),
    ],
)
def test_failed_precondition_permanently_revokes_permit_before_remote_call(
    isolated_audit: Path,
    mode: str,
    reason_code: str,
) -> None:
    batch = _batch()
    reserve_operation(batch, NOW)
    review, permit = _review_and_permit(batch, 0, NOW + timedelta(seconds=1))
    rejected = _RejectedPreconditionAdapter(mode)

    execution = execute_approved_item(
        batch,
        batch.actions[0],
        0,
        review,
        permit,
        rejected,
        NOW + timedelta(seconds=3),
    )

    assert execution.result is ActionResult.FAILED
    assert execution.reason_code == reason_code
    assert rejected.mutation_calls == 0
    assert rejected.scope_active is False
    assert rejected.events[-1] == "scope_exit"
    revoked = find_operation(batch.idempotency_key)
    assert revoked is not None
    assert revoked.state is OperationState.DENIED
    assert revoked.items[0].attempt_id is None
    assert revoked.items[0].permit_revocation_reason_code == reason_code
    wal_text = isolated_audit.read_text(encoding="utf-8")
    assert wal_text.count('"event":"permit_revoked"') == 1
    assert "raw provider detail" not in wal_text

    # Даже если provider state снова совпал, старый permit навсегда непригоден.
    replay_adapter = _SuccessfulAdapter()
    replay = execute_approved_item(
        batch,
        batch.actions[0],
        0,
        review,
        permit,
        replay_adapter,
        NOW + timedelta(seconds=4),
    )
    assert replay.result is ActionResult.FAILED
    assert replay_adapter.mutation_calls == 0
