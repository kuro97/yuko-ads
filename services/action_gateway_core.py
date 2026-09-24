"""Provider-neutral one-item execution и read-only crash recovery."""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol

from services.approval_audit import (
    ApprovalAuditError,
    append_event,
    append_item_result,
    consume_permit,
    find_operation,
    revoke_permit,
)
from services.approval_checker_models import (
    ActionBatchManifest,
    ActionExecution,
    ActionKind,
    ActionManifest,
    ActionObservation,
    ActionPermit,
    ActionResult,
    ActionResultAuditEvent,
    ActionReview,
    ActionRun,
    SafetyDecision,
    OperationAttemptAttestation,
    OperationItemRecord,
    OperationRecord,
    OperationState,
    ReconciliationAuditEvent,
    manifest_sha256,
)


logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


_TRANSLIT = str.maketrans(
    "абвгдежзийклмнопрстуфхцчшщъыьэюяё",
    "abvgdejziyklmnoprstufhccss_y_eyae",
)


def machine_reason(head: str, exc: BaseException) -> str:
    """«<HEAD>:<CLASS>:<MSG>» — машинный код с отпечатком исключения, проходит _reason_code (≤128)."""
    raw = f"{type(exc).__name__}:{exc}".lower().translate(_TRANSLIT)
    detail = re.sub(r"[^a-z0-9:]+", "_", raw).strip("_:").upper()
    return (f"{head}:{detail}")[:128] if detail else head


def unknown_outcome_reason(exc: BaseException) -> str:
    """Код «исход неизвестен» с отпечатком исключения — машинный, чтобы пройти _reason_code.

    Системный журнал хранится недолго, а задание в RECONCILE_REQUIRED висит неделями — без отпечатка
    в коде причина теряется. Голова кода остаётся PROVIDER_OUTCOME_UNKNOWN (сравнения
    идут по split(":")[0]); хвост — класс и сообщение в верхнем регистре латиницей, до 128 символов.
    """
    return machine_reason("PROVIDER_OUTCOME_UNKNOWN", exc)


@dataclass(frozen=True, slots=True)
class AdapterMutationResult:
    """Санитизированный provider-result без raw body и callback."""

    result: ActionResult | None
    created_ids: tuple[str, ...] = ()
    reason_code: str = "PROVIDER_ACCEPTED"
    remote_may_have_changed: bool = True


@dataclass(frozen=True, slots=True)
class AdapterPostconditionResult:
    """Независимый live post-read и его kind-specific оценка."""

    result: ActionResult
    observation: ActionObservation | None
    created_ids: tuple[str, ...] = ()
    reason_code: str = "POSTCONDITION_UNKNOWN"
    remote_may_have_changed: bool = True


@dataclass(frozen=True, slots=True)
class _ScopedExecution:
    """Результат provider-фазы, который ещё нельзя фиксировать как terminal."""

    attempt_id: str
    mutation: AdapterMutationResult
    post: AdapterPostconditionResult


class ActionAdapter(Protocol):
    """Минимальный protocol; concrete adapters живут выше neutral core."""

    def execution_scope(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> AbstractContextManager[None]: ...

    def read_precondition(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> ActionObservation: ...

    def mutate(
        self,
        item: ActionManifest,
        now: datetime,
        *,
        # Core передаёт батч-аттестацию consume_permit; адаптеры, чья граница
        # требует owner-аттестацию с account_id, обязаны отвергнуть её fail-closed.
        attempt: OperationAttemptAttestation,
    ) -> AdapterMutationResult: ...

    def read_postcondition(
        self,
        item: ActionManifest,
        created_ids: tuple[str, ...],
        now: datetime,
    ) -> AdapterPostconditionResult: ...

    def finalize_after_scope(
        self,
        item: ActionManifest,
        mutation: AdapterMutationResult,
        post: AdapterPostconditionResult,
        now: datetime,
    ) -> None: ...

    def reconcile(
        self,
        item: OperationItemRecord,
        now: datetime,
    ) -> AdapterPostconditionResult: ...


class _AdapterRegistry(Protocol):
    def for_kind(self, kind: ActionKind) -> ActionAdapter: ...


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")


def _reason_code(value: str, fallback: str) -> str:
    return (
        value
        if isinstance(value, str) and _MACHINE_CODE_RE.fullmatch(value)
        else fallback
    )


def _execution_without_attempt(
    item: ActionManifest,
    item_index: int,
    result: ActionResult,
    reason_code: str,
    now: datetime,
) -> ActionExecution:
    return ActionExecution(
        attempt_id="",
        item_id=item.manifest_id,
        item_index=item_index,
        action_manifest_id=item.manifest_id,
        result=result,
        started_at=None,
        completed_at=now,
        created_ids=(),
        reason_code=reason_code,
        remote_may_have_changed=False,
    )


def _revoke_before_attempt(
    permit: ActionPermit,
    item: ActionManifest,
    item_index: int,
    reason_code: str,
    now: datetime,
) -> ActionExecution:
    """Не позволяет issued permit остаться пригодным после failed pre-read."""

    try:
        revoke_permit(permit, reason_code, now)
        execution_reason = reason_code
    except Exception:
        # Даже при проблеме audit provider не вызывается. Checker health затем
        # блокирует весь action-контур до восстановления WAL.
        execution_reason = "PERMIT_REVOCATION_FAILED"
    return _execution_without_attempt(
        item,
        item_index,
        ActionResult.FAILED,
        execution_reason,
        now,
    )


def _validate_approved_contract(
    batch: ActionBatchManifest,
    item: ActionManifest,
    item_index: int,
    review: ActionReview,
    permit: ActionPermit,
) -> None:
    if batch.manifest_sha256 != manifest_sha256(batch):
        raise ValueError("batch manifest digest изменён")
    if not 0 <= item_index < len(batch.actions) or batch.actions[item_index] != item:
        raise ValueError("item не совпадает с batch index")
    if review.decision is not SafetyDecision.SAFE or review.issues:
        raise ValueError(
            "execute_approved_item принимает только чистый SAFE review"
        )
    expected = (
        review.operation_id == permit.operation_id,
        review.check_id == permit.check_id,
        review.idempotency_key == permit.idempotency_key == batch.idempotency_key,
        review.batch_manifest_id == permit.batch_manifest_id == batch.batch_manifest_id,
        review.batch_manifest_sha256
        == permit.batch_manifest_sha256
        == batch.manifest_sha256,
        review.item_id == permit.item_id == item.manifest_id,
        review.item_index == permit.item_index == item_index,
        review.action_kind is permit.action_kind is item.kind,
        review.item_manifest_sha256
        == permit.item_manifest_sha256
        == manifest_sha256(item),
        review.subject_ids == permit.subject_ids,
        review.evidence_state_sha256 == permit.evidence_state_sha256,
        review.audit_persisted,
    )
    if not all(expected):
        raise ValueError("review/permit не совпадают с immutable item")


def _append_result(
    batch: ActionBatchManifest,
    item: ActionManifest,
    permit: ActionPermit,
    attempt_id: str,
    result: ActionResult,
    created_ids: tuple[str, ...],
    reason_code: str,
    remote_may_have_changed: bool,
    postcondition_sha256: str | None,
    completed_at: datetime,
) -> None:
    append_item_result(
        ActionResultAuditEvent(
            schema_version=1,
            event="action_result",
            event_id=str(uuid.uuid4()),
            written_at=completed_at,
            operation_id=permit.operation_id,
            idempotency_key=batch.idempotency_key,
            batch_manifest_id=batch.batch_manifest_id,
            batch_manifest_sha256=batch.manifest_sha256,
            subject_ids=permit.subject_ids,
            permit_id=permit.permit_id,
            attempt_id=attempt_id,
            item_id=item.manifest_id,
            item_index=permit.item_index,
            action_kind=item.kind,
            result=result,
            exact_created_ids=tuple(created_ids),
            postcondition_sha256=postcondition_sha256,
            reason_code=_reason_code(reason_code, "ADAPTER_RESULT"),
            remote_may_have_changed=remote_may_have_changed,
            reconciliation_required=result
            in {ActionResult.PARTIAL, ActionResult.UNKNOWN},
            completed_at=completed_at,
        )
    )


def execute_approved_item(
    batch: ActionBatchManifest,
    item: ActionManifest,
    item_index: int,
    review: ActionReview,
    permit: ActionPermit,
    adapter: ActionAdapter,
    now: datetime,
) -> ActionExecution:
    """Выполняет item внутри единого adapter-owned mutation scope."""

    _require_aware(now)
    _validate_approved_contract(batch, item, item_index, review, permit)
    try:
        scope = adapter.execution_scope(item, now)
    except Exception:
        return _revoke_before_attempt(
            permit,
            item,
            item_index,
            "EXECUTION_SCOPE_FAILED",
            now,
        )

    entered = False
    scoped: ActionExecution | _ScopedExecution | None = None
    scope_exit_failed = False
    try:
        with scope:
            entered = True
            scoped = _execute_in_scope(
                batch,
                item,
                item_index,
                permit,
                review,
                adapter,
                now,
            )
    except Exception:
        if not entered:
            return _revoke_before_attempt(
                permit,
                item,
                item_index,
                "EXECUTION_SCOPE_FAILED",
                now,
            )
        scope_exit_failed = True
    if scoped is None:  # pragma: no cover - защитный invariant
        return _revoke_before_attempt(
            permit,
            item,
            item_index,
            "EXECUTION_SCOPE_FAILED",
            now,
        )
    if isinstance(scoped, ActionExecution):
        return scoped

    post = scoped.post
    if scope_exit_failed:
        post = AdapterPostconditionResult(
            result=ActionResult.UNKNOWN,
            observation=None,
            created_ids=scoped.post.created_ids,
            reason_code="EXECUTION_SCOPE_RELEASE_FAILED",
            remote_may_have_changed=True,
        )
    else:
        try:
            adapter.finalize_after_scope(
                item,
                scoped.mutation,
                scoped.post,
                now,
            )
        except Exception:
            post = AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                created_ids=scoped.post.created_ids,
                reason_code="FINALIZE_AFTER_SCOPE_FAILED",
                remote_may_have_changed=True,
            )
    return _persist_terminal_result(
        batch,
        item,
        item_index,
        permit,
        scoped.attempt_id,
        post,
        now,
    )


def _execute_in_scope(
    batch: ActionBatchManifest,
    item: ActionManifest,
    item_index: int,
    permit: ActionPermit,
    review: ActionReview,
    adapter: ActionAdapter,
    now: datetime,
) -> ActionExecution | _ScopedExecution:
    """Pre-read, attempt, provider и post-read под одним удерживаемым scope."""

    try:
        precondition = adapter.read_precondition(item, now)
    except Exception:
        return _revoke_before_attempt(
            permit,
            item,
            item_index,
            "PRECONDITION_READ_FAILED",
            now,
        )
    if precondition.subject_ids != permit.subject_ids:
        return _revoke_before_attempt(
            permit,
            item,
            item_index,
            "PRECONDITION_SUBJECT_DRIFT",
            now,
        )
    if precondition.digest != permit.evidence_state_sha256:
        return _revoke_before_attempt(
            permit,
            item,
            item_index,
            "PRECONDITION_MISMATCH",
            now,
        )
    if (
        _SHA256_RE.fullmatch(precondition.digest) is None
        or _SHA256_RE.fullmatch(precondition.unrelated_state_digest) is None
        or precondition.observed_at.tzinfo is None
        or precondition.observed_at.utcoffset() is None
        or precondition.observed_at < review.checked_at
    ):
        return _revoke_before_attempt(
            permit,
            item,
            item_index,
            "PRECONDITION_INVALID",
            now,
        )

    try:
        attempt = consume_permit(
            permit,
            batch,
            precondition.digest,
            now,
        )
    except (ApprovalAuditError, OSError, ValueError):
        return _execution_without_attempt(
            item, item_index, ActionResult.FAILED, "ATTEMPT_AUDIT_FAILED", now
        )

    try:
        mutation = adapter.mutate(item, now, attempt=attempt)
    except Exception as exc:
        # Fail-closed семантика сохраняется (UNKNOWN → reconcile), но само
        # исключение обязано попасть в журнал: немой except прятал настоящую
        # причину «исход неизвестен», и однажды её пришлось выковыривать
        # шпионом-перехватчиком. Секретов в трейсе нет — токены в сообщениях
        # ошибок адаптеров не встречаются, а стек нужен целиком.
        logger.exception(
            "action_gateway: adapter.mutate(%s) упал — исход у провайдера неизвестен",
            getattr(item, "manifest_id", "?"),
        )
        mutation = AdapterMutationResult(
            result=ActionResult.UNKNOWN,
            reason_code=unknown_outcome_reason(exc),
            remote_may_have_changed=True,
        )
    if not isinstance(mutation, AdapterMutationResult):
        mutation = AdapterMutationResult(
            result=ActionResult.UNKNOWN,
            reason_code="PROVIDER_RESULT_INVALID",
            remote_may_have_changed=True,
        )

    if mutation.result is None:
        try:
            post = adapter.read_postcondition(item, tuple(mutation.created_ids), now)
        except Exception:
            post = AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                created_ids=tuple(mutation.created_ids),
                reason_code="POSTCONDITION_UNAVAILABLE",
                remote_may_have_changed=True,
            )
        if not isinstance(post, AdapterPostconditionResult):
            post = AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                created_ids=tuple(mutation.created_ids),
                reason_code="POSTCONDITION_RESULT_INVALID",
                remote_may_have_changed=True,
            )
    else:
        post = AdapterPostconditionResult(
            result=mutation.result,
            observation=None,
            created_ids=tuple(mutation.created_ids),
            reason_code=mutation.reason_code,
            remote_may_have_changed=mutation.remote_may_have_changed,
        )

    if post.result is ActionResult.CONFIRMED and post.observation is None:
        post = AdapterPostconditionResult(
            result=ActionResult.UNKNOWN,
            observation=None,
            created_ids=post.created_ids,
            reason_code="CONFIRMATION_WITHOUT_LIVE_READ",
            remote_may_have_changed=True,
        )
    post_digest = None if post.observation is None else post.observation.digest
    if post.observation is not None and (
        _SHA256_RE.fullmatch(post_digest or "") is None
        or _SHA256_RE.fullmatch(post.observation.unrelated_state_digest) is None
        or post.observation.observed_at.tzinfo is None
        or post.observation.observed_at.utcoffset() is None
        or post.observation.observed_at < precondition.observed_at
    ):
        post = AdapterPostconditionResult(
            result=ActionResult.UNKNOWN,
            observation=None,
            created_ids=post.created_ids,
            reason_code="POSTCONDITION_DIGEST_INVALID",
            remote_may_have_changed=True,
        )
        post_digest = None

    return _ScopedExecution(
        attempt_id=attempt.attempt_id,
        mutation=mutation,
        post=post,
    )


def _persist_terminal_result(
    batch: ActionBatchManifest,
    item: ActionManifest,
    item_index: int,
    permit: ActionPermit,
    attempt_id: str,
    post: AdapterPostconditionResult,
    now: datetime,
) -> ActionExecution:
    """Пишет terminal result только после release scope и finalizer."""

    post_digest = None if post.observation is None else post.observation.digest
    try:
        _append_result(
            batch,
            item,
            permit,
            attempt_id,
            post.result,
            post.created_ids,
            post.reason_code,
            post.remote_may_have_changed,
            post_digest,
            now,
        )
        persisted_result = post.result
        persisted_reason = _reason_code(post.reason_code, "ADAPTER_RESULT")
    except Exception:
        # Mutation могла пройти, а result WAL — нет. Attempt остаётся UNKNOWN и
        # recovery делает только live-read, без повторного POST.
        persisted_result = ActionResult.UNKNOWN
        persisted_reason = "RESULT_AUDIT_FAILED"

    return ActionExecution(
        attempt_id=attempt_id,
        item_id=item.manifest_id,
        item_index=item_index,
        action_manifest_id=item.manifest_id,
        result=persisted_result,
        started_at=now,
        completed_at=now,
        created_ids=tuple(post.created_ids),
        reason_code=persisted_reason,
        remote_may_have_changed=post.remote_may_have_changed,
    )


def _adapter_for_recovery(
    adapters: _AdapterRegistry | Mapping[ActionKind, ActionAdapter],
    kind: ActionKind,
) -> ActionAdapter:
    if isinstance(adapters, Mapping):
        return adapters[kind]
    return adapters.for_kind(kind)


def _run_from_record(record: OperationRecord) -> ActionRun:
    executions = tuple(
        ActionExecution(
            attempt_id=item.attempt_id or "",
            item_id=item.item_id,
            item_index=item.item_index,
            action_manifest_id=item.item_id,
            result=item.result or ActionResult.UNKNOWN,
            started_at=item.attempted_at,
            completed_at=item.completed_at,
            created_ids=item.exact_created_ids,
            reason_code=(
                "RECOVERY_PENDING"
                if item.result in {None, ActionResult.UNKNOWN}
                else "RECOVERED_WAL_RESULT"
            ),
            remote_may_have_changed=item.attempt_id is not None,
        )
        for item in record.items
        if item.attempt_id is not None
    )
    first_unprocessed = next(
        (
            item.item_index
            for item in record.items
            if item.result is not ActionResult.CONFIRMED
        ),
        None,
    )
    if record.state is OperationState.CONFIRMED:
        result: ActionResult | None = ActionResult.CONFIRMED
    elif record.state is OperationState.PARTIAL:
        result = ActionResult.PARTIAL
    elif record.state is OperationState.FAILED:
        result = ActionResult.FAILED
    elif record.state is OperationState.UNKNOWN:
        result = ActionResult.UNKNOWN
    else:
        result = None
    return ActionRun(
        operation_id=record.operation_id,
        idempotency_key=record.idempotency_key,
        batch_manifest_id=record.batch_manifest_id,
        batch_manifest_sha256=record.batch_manifest_sha256,
        state=record.state,
        reviews=(),
        executions=executions,
        result=result,
        shadow_evaluation=None,
        shadow_items=(),
        dry_run=False,
        provider_mutation_count=len(executions),
        first_unprocessed_index=first_unprocessed,
        stop_reason_code=(None if first_unprocessed is None else "RECOVERY_STOPPED"),
        reconciliation_required=record.reconciliation_required,
    )


def recover_incomplete_operation(
    record: OperationRecord,
    adapters: _AdapterRegistry | Mapping[ActionKind, ActionAdapter],
    now: datetime,
) -> ActionRun:
    """Сверяет только первый unresolved attempt и никогда не повторяет mutation."""

    _require_aware(now)
    unresolved = next(
        (
            item
            for item in record.items
            if item.attempt_id is not None
            and (item.result is ActionResult.UNKNOWN or item.reconciliation_required)
        ),
        None,
    )
    if unresolved is None:
        return _run_from_record(record)

    adapter = _adapter_for_recovery(adapters, unresolved.action_kind)
    try:
        post = adapter.reconcile(unresolved, now)
    except Exception:
        return _run_from_record(record)
    if (
        post.observation is None
        or _SHA256_RE.fullmatch(post.observation.digest) is None
    ):
        return _run_from_record(record)

    try:
        append_event(
            ReconciliationAuditEvent(
                schema_version=1,
                event="reconciliation",
                event_id=str(uuid.uuid4()),
                written_at=now,
                operation_id=record.operation_id,
                idempotency_key=record.idempotency_key,
                batch_manifest_id=record.batch_manifest_id,
                batch_manifest_sha256=record.batch_manifest_sha256,
                subject_ids=unresolved.subject_ids,
                item_id=unresolved.item_id,
                item_index=unresolved.item_index,
                prior_attempt_id=unresolved.attempt_id or "",
                final_result=post.result,
                exact_created_ids=tuple(post.created_ids),
                postcondition_sha256=post.observation.digest,
                reconciled_at=now,
            )
        )
        refreshed = find_operation(record.idempotency_key)
    except Exception:
        return _run_from_record(record)
    return _run_from_record(record if refreshed is None else refreshed)
