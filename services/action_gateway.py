"""Закрытый gateway для единственной попытки после owner consent.

Обычные producer-модули больше не могут получить permit или запустить adapter.
Доступ к sealed adapters остаётся только у owner executor через приватную session.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import config
from services import action_adapter_registry as _adapter_registry
from services.action_gateway_core import (
    AdapterMutationResult,
    AdapterPostconditionResult,
    execute_approved_item,
    recover_incomplete_operation,
    unknown_outcome_reason,
)
from services.action_manifests import build_action_batch
from services.approval_audit import (
    find_operation,
    find_operation_by_id,
    issue_item_permit,
    reserve_operation,
    revoke_permit,
)
from services.approval_checker import review_action_item
from services.approval_checker_models import (
    ActionBatchManifest,
    AssetRecoveryManifest,
    ActionExecution,
    ActionManifest,
    ActionResult,
    ActionReview,
    ActionRun,
    SafetyDecision,
    LaunchManifest,
    OperationRecord,
    OperationState,
    PauseManifest,
    OperationItemRecord,
    ScaleManifest,
    ShadowBatchEvaluation,
    ShadowItemEvaluation,
    UnpauseManifest,
    manifest_sha256,
)

if TYPE_CHECKING:
    from services.owner_action_models import ActionAttemptAttestation


_ACTION_TYPES = (
    LaunchManifest,
    AssetRecoveryManifest,
    PauseManifest,
    UnpauseManifest,
    ScaleManifest,
)


class OwnerConsentRequired(PermissionError):
    """Прямой gateway-вызов не содержит персонального решения владельца."""


class _OwnerExecutionSession:
    """Удерживает adapter-owned lock от live review до provider результата."""

    def __init__(self, item: ActionManifest, now: datetime) -> None:
        self._item = item
        self._now = now
        self._adapter = _adapter_registry._adapter_for_kind(item.kind)
        self._scope = self._adapter.execution_scope(item, now)
        self._entered = False
        self._mutation: AdapterMutationResult | None = None
        self._postcondition: AdapterPostconditionResult | None = None

    def __enter__(self) -> _OwnerExecutionSession:
        self._scope.__enter__()
        self._entered = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._entered = False
        return bool(self._scope.__exit__(exc_type, exc, traceback))

    def read_precondition(self):
        if not self._entered:
            raise RuntimeError("Owner execution session ещё не открыта")
        return self._adapter.read_precondition(self._item, self._now)

    def execute_attempt(
        self,
        attestation: ActionAttemptAttestation,
    ) -> ActionExecution:
        """Делает ровно один provider call; неизвестный результат не повторяет."""

        if not self._entered:
            raise RuntimeError("Owner execution session ещё не открыта")
        try:
            mutation = self._adapter.mutate(
                self._item,
                self._now,
                attempt=attestation,
            )
        except Exception as exc:
            # Fail-closed семантика сохраняется (UNKNOWN → reconcile), но само
            # исключение обязано попасть в журнал. Именно
            # ЭТОТ путь использует owner-executor, а logger в _core
            # лёг мимо — причина PROVIDER_OUTCOME_UNKNOWN снова была немой.
            logging.getLogger(__name__).exception(
                "action_gateway: adapter.mutate(%s) упал — исход у провайдера "
                "неизвестен",
                getattr(self._item, "manifest_id", "?"),
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
                postcondition = self._adapter.read_postcondition(
                    self._item,
                    tuple(mutation.created_ids),
                    self._now,
                )
            except Exception:
                postcondition = AdapterPostconditionResult(
                    result=ActionResult.UNKNOWN,
                    observation=None,
                    created_ids=tuple(mutation.created_ids),
                    reason_code="POSTCONDITION_UNAVAILABLE",
                    remote_may_have_changed=True,
                )
        else:
            postcondition = AdapterPostconditionResult(
                result=mutation.result,
                observation=None,
                created_ids=tuple(mutation.created_ids),
                reason_code=mutation.reason_code,
                remote_may_have_changed=mutation.remote_may_have_changed,
            )
        if not isinstance(postcondition, AdapterPostconditionResult):
            postcondition = AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                created_ids=tuple(mutation.created_ids),
                reason_code="POSTCONDITION_RESULT_INVALID",
                remote_may_have_changed=True,
            )
        if (
            postcondition.result is ActionResult.CONFIRMED
            and postcondition.observation is None
        ):
            postcondition = AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                created_ids=postcondition.created_ids,
                reason_code="CONFIRMATION_WITHOUT_LIVE_READ",
                remote_may_have_changed=True,
            )
        if postcondition.observation is not None and (
            len(postcondition.observation.digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in postcondition.observation.digest
            )
            or len(postcondition.observation.unrelated_state_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in postcondition.observation.unrelated_state_digest
            )
            or postcondition.observation.observed_at.tzinfo is None
            or postcondition.observation.observed_at.utcoffset() is None
        ):
            postcondition = AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                created_ids=postcondition.created_ids,
                reason_code="POSTCONDITION_INVALID",
                remote_may_have_changed=True,
            )
        self._mutation = mutation
        self._postcondition = postcondition
        return ActionExecution(
            attempt_id=attestation.attempt_id,
            item_id=self._item.manifest_id,
            item_index=0,
            action_manifest_id=self._item.manifest_id,
            result=postcondition.result,
            started_at=attestation.consumed_at,
            completed_at=self._now,
            created_ids=tuple(postcondition.created_ids),
            reason_code=postcondition.reason_code,
            remote_may_have_changed=postcondition.remote_may_have_changed,
        )

    def finalize_after_scope(self, execution: ActionExecution) -> ActionExecution:
        """Завершает local side effects только после освобождения provider lock."""

        if self._entered:
            raise RuntimeError("finalize_after_scope вызван до освобождения scope")
        if self._mutation is None or self._postcondition is None:
            raise RuntimeError("Provider attempt ещё не выполнялся")
        try:
            self._adapter.finalize_after_scope(
                self._item,
                self._mutation,
                self._postcondition,
                self._now,
            )
        except Exception:
            return replace(
                execution,
                result=ActionResult.UNKNOWN,
                reason_code="FINALIZE_FAILED",
                remote_may_have_changed=True,
            )
        return execution


def _open_owner_execution_session(
    item: ActionManifest,
    now: datetime,
) -> _OwnerExecutionSession:
    """Единственная приватная точка получения sealed adapter."""

    if type(item) not in _ACTION_TYPES:
        raise TypeError("Неподдерживаемый owner action manifest")
    _require_aware(now)
    return _OwnerExecutionSession(item, now)


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")


def _clock(explicit: datetime | None) -> datetime:
    current = explicit or datetime.now(timezone.utc)
    _require_aware(current)
    return current


def _validate_batch(manifest: ActionBatchManifest) -> None:
    if type(manifest) is not ActionBatchManifest:
        raise TypeError("execute_action_batch принимает ActionBatchManifest")
    if manifest.manifest_sha256 != manifest_sha256(manifest):
        raise ValueError("batch manifest digest изменён")
    for item in manifest.actions:
        if type(item) not in _ACTION_TYPES:
            raise TypeError("batch содержит неподдерживаемый action manifest")
        if item.idempotency_key != manifest.idempotency_key:
            raise ValueError("item idempotency_key не совпадает с batch")
    exact_subject_ids = tuple(_subject_id(item) for item in manifest.actions)
    if manifest.subject_ids != exact_subject_ids:
        raise ValueError("batch subject_ids не совпадают с exact action subjects")


def _subject_id(item: ActionManifest) -> str:
    if type(item) is LaunchManifest:
        return f"card:{item.trello.card_id}"
    if type(item) in {PauseManifest, UnpauseManifest}:
        return f"ad:{item.ad_id}"
    if type(item) is AssetRecoveryManifest:
        return f"adset:{item.target_adset_id}"
    if type(item) is ScaleManifest:
        return f"adset:{item.adset_id}"
    raise TypeError("Неподдерживаемый action manifest")


def _has_progress(record: OperationRecord) -> bool:
    return any(
        item.check_id is not None
        or item.permit_id is not None
        or item.attempt_id is not None
        for item in record.items
    )


def _confirmed_prefix_length(record: OperationRecord) -> int:
    length = 0
    for item in record.items:
        if item.result is not ActionResult.CONFIRMED:
            break
        length += 1
    return length


def _remaining_items_are_untouched(record: OperationRecord, start: int) -> bool:
    return all(
        item.check_id is None and item.permit_id is None and item.attempt_id is None
        for item in record.items[start:]
    )


def _execution_from_record(item: OperationItemRecord) -> ActionExecution:
    return ActionExecution(
        attempt_id=item.attempt_id or "",
        item_id=item.item_id,
        item_index=item.item_index,
        action_manifest_id=item.item_id,
        result=item.result or ActionResult.UNKNOWN,
        started_at=item.attempted_at,
        completed_at=item.completed_at,
        created_ids=item.exact_created_ids,
        reason_code=(
            "RECONCILIATION_PENDING"
            if item.reconciliation_required
            else "PERSISTED_ACTION_RESULT"
        ),
        remote_may_have_changed=item.attempt_id is not None,
    )


def _result_for_state(state: OperationState) -> ActionResult | None:
    return {
        OperationState.CONFIRMED: ActionResult.CONFIRMED,
        OperationState.PARTIAL: ActionResult.PARTIAL,
        OperationState.FAILED: ActionResult.FAILED,
        OperationState.UNKNOWN: ActionResult.UNKNOWN,
    }.get(state)


def _run_from_record(record: OperationRecord) -> ActionRun:
    executions = tuple(
        _execution_from_record(item)
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
    state = record.state
    if state is OperationState.DENIED and any(
        item.result is ActionResult.CONFIRMED for item in record.items
    ):
        # Audit хранит per-item DENIED; на уровне batch уже выполненная часть
        # никогда не должна исчезать из итогового состояния.
        state = OperationState.PARTIAL
    return ActionRun(
        operation_id=record.operation_id,
        idempotency_key=record.idempotency_key,
        batch_manifest_id=record.batch_manifest_id,
        batch_manifest_sha256=record.batch_manifest_sha256,
        state=state,
        reviews=(),
        executions=executions,
        result=_result_for_state(state),
        shadow_evaluation=None,
        shadow_items=(),
        dry_run=False,
        provider_mutation_count=sum(bool(item.attempt_id) for item in record.items),
        first_unprocessed_index=first_unprocessed,
        stop_reason_code=(
            None if first_unprocessed is None else "PERSISTED_OPERATION_STATE"
        ),
        reconciliation_required=record.reconciliation_required,
    )


def _shadow_run(record: OperationRecord, review: ActionReview | None) -> ActionRun:
    if review is not None:
        first_approved = (
            review.decision is SafetyDecision.SAFE and review.audit_persisted
        )
        reviews = (review,)
    else:
        first = record.items[0]
        first_approved = first.decision is SafetyDecision.SAFE
        reviews = ()
    first_state = (
        ShadowItemEvaluation.FIRST_ITEM_WOULD_APPROVE
        if first_approved
        else ShadowItemEvaluation.FIRST_ITEM_WOULD_DENY
    )
    shadow_items = (
        first_state,
        *(
            ShadowItemEvaluation.NOT_EVALUATED_REQUIRES_LIVE_SEQUENCE
            for _ in record.items[1:]
        ),
    )
    return ActionRun(
        operation_id=record.operation_id,
        idempotency_key=record.idempotency_key,
        batch_manifest_id=record.batch_manifest_id,
        batch_manifest_sha256=record.batch_manifest_sha256,
        state=OperationState.SHADOW_FIRST_ITEM_ONLY,
        reviews=reviews,
        executions=(),
        result=None,
        shadow_evaluation=ShadowBatchEvaluation.INCOMPLETE,
        shadow_items=shadow_items,
        dry_run=True,
        provider_mutation_count=0,
        first_unprocessed_index=0,
        stop_reason_code="SHADOW_FIRST_ITEM_ONLY",
        reconciliation_required=False,
    )


def _stopped_run(
    record: OperationRecord,
    reviews: tuple[ActionReview, ...],
    executions: tuple[ActionExecution, ...],
    item_index: int,
    reason_code: str,
) -> ActionRun:
    confirmed = sum(
        execution.result is ActionResult.CONFIRMED for execution in executions
    )
    current_result = executions[-1].result if executions else None
    if confirmed:
        state = OperationState.PARTIAL
        result = ActionResult.PARTIAL
    elif current_result is ActionResult.UNKNOWN:
        state = OperationState.UNKNOWN
        result = ActionResult.UNKNOWN
    elif current_result is ActionResult.FAILED:
        state = OperationState.FAILED
        result = ActionResult.FAILED
    elif current_result is ActionResult.PARTIAL:
        state = OperationState.PARTIAL
        result = ActionResult.PARTIAL
    else:
        state = OperationState.DENIED
        result = None
    return ActionRun(
        operation_id=record.operation_id,
        idempotency_key=record.idempotency_key,
        batch_manifest_id=record.batch_manifest_id,
        batch_manifest_sha256=record.batch_manifest_sha256,
        state=state,
        reviews=reviews,
        executions=executions,
        result=result,
        shadow_evaluation=None,
        shadow_items=(),
        dry_run=False,
        provider_mutation_count=sum(bool(item.attempt_id) for item in executions),
        first_unprocessed_index=item_index,
        stop_reason_code=reason_code,
        reconciliation_required=(
            current_result in {ActionResult.PARTIAL, ActionResult.UNKNOWN}
            or record.reconciliation_required
        ),
    )


def _execute_action_without_owner_consent(
    manifest: ActionManifest,
    now: datetime | None = None,
) -> ActionRun:
    """Старый внутренний путь оставлен только для чтения/recovery совместимости."""

    if type(manifest) not in _ACTION_TYPES:
        raise TypeError("execute_action принимает immutable ActionManifest")
    batch = build_action_batch(
        (manifest,),
        correlation_id=manifest.manifest_id,
        idempotency_key=manifest.idempotency_key,
        # Batch hash не должен меняться на replay из-за нового времени вызова.
        now=manifest.prepared_at,
    )
    return _execute_action_batch_without_owner_consent(batch, now)


def _execute_action_batch_without_owner_consent(
    manifest: ActionBatchManifest,
    now: datetime | None = None,
) -> ActionRun:
    """Legacy реализация недоступна producer-модулям."""

    _validate_batch(manifest)
    started_at = _clock(now)
    enforce_actions = config.REPORT_CHECKER_ENFORCE_ACTIONS is True
    record = reserve_operation(manifest, started_at)

    start_index = 0
    reviews: list[ActionReview] = []
    executions: list[ActionExecution] = []
    if _has_progress(record):
        has_permit_or_attempt = any(
            item.permit_id is not None or item.attempt_id is not None
            for item in record.items
        )
        if not enforce_actions and not has_permit_or_attempt:
            return _shadow_run(record, None)
        if enforce_actions and record.reconciliation_required:
            return recover_incomplete_operation(
                record,
                _adapter_registry._FixedAdapterRegistry(),
                started_at,
            )
        confirmed_prefix = _confirmed_prefix_length(record)
        if (
            enforce_actions
            and confirmed_prefix > 0
            and confirmed_prefix < len(record.items)
            and _remaining_items_are_untouched(record, confirmed_prefix)
        ):
            # После crash между item-ами можно продолжить только с первого
            # нетронутого successor; подтверждённые mutations не повторяются.
            start_index = confirmed_prefix
            executions.extend(
                _execution_from_record(item) for item in record.items[:confirmed_prefix]
            )
        else:
            return _run_from_record(record)

    for item_index in range(start_index, len(manifest.actions)):
        item = manifest.actions[item_index]
        step_now = _clock(now)
        review = review_action_item(manifest, item, item_index, step_now)
        reviews.append(review)

        if not enforce_actions:
            refreshed = find_operation(manifest.idempotency_key) or record
            return _shadow_run(refreshed, review)

        if (
            review.decision is not SafetyDecision.SAFE
            or not review.audit_persisted
            or review.issues
        ):
            refreshed = find_operation(manifest.idempotency_key) or record
            return _stopped_run(
                refreshed,
                tuple(reviews),
                tuple(executions),
                item_index,
                "APPROVAL_DENIED",
            )

        try:
            permit = issue_item_permit(review, manifest, item, step_now)
        except Exception:
            refreshed = find_operation(manifest.idempotency_key) or record
            return _stopped_run(
                refreshed,
                tuple(reviews),
                tuple(executions),
                item_index,
                "PERMIT_ISSUE_FAILED",
            )

        try:
            adapter = _adapter_registry._adapter_for_kind(item.kind)
            execution = execute_approved_item(
                manifest,
                item,
                item_index,
                review,
                permit,
                adapter,
                step_now,
            )
        except Exception:
            reason_code = "ADAPTER_EXECUTION_FAILED"
            try:
                revoke_permit(permit, reason_code, step_now)
            except Exception:
                reason_code = "PERMIT_REVOCATION_FAILED"
            refreshed = find_operation(manifest.idempotency_key) or record
            return _stopped_run(
                refreshed,
                tuple(reviews),
                tuple(executions),
                item_index,
                reason_code,
            )
        executions.append(execution)
        refreshed = find_operation(manifest.idempotency_key) or record
        if (
            execution.result is not ActionResult.CONFIRMED
            or refreshed.items[item_index].result is not ActionResult.CONFIRMED
        ):
            return _stopped_run(
                refreshed,
                tuple(reviews),
                tuple(executions),
                item_index,
                execution.reason_code,
            )
        record = refreshed

    return ActionRun(
        operation_id=record.operation_id,
        idempotency_key=record.idempotency_key,
        batch_manifest_id=record.batch_manifest_id,
        batch_manifest_sha256=record.batch_manifest_sha256,
        state=OperationState.CONFIRMED,
        reviews=tuple(reviews),
        executions=tuple(executions),
        result=ActionResult.CONFIRMED,
        shadow_evaluation=None,
        shadow_items=(),
        dry_run=False,
        provider_mutation_count=sum(bool(item.attempt_id) for item in executions),
        first_unprocessed_index=None,
        stop_reason_code=None,
        reconciliation_required=False,
    )


def execute_action(
    manifest: ActionManifest,
    now: datetime | None = None,
) -> ActionRun:
    """Запрещает старый обход owner proposal/Telegram consent."""

    del manifest, now
    raise OwnerConsentRequired(
        "Прямое выполнение запрещено: сначала нужно owner-approved proposal"
    )


def execute_action_batch(
    manifest: ActionBatchManifest,
    now: datetime | None = None,
) -> ActionRun:
    """Запрещает автоматическую выдачу permit для technically SAFE batch."""

    del manifest, now
    raise OwnerConsentRequired(
        "Прямое batch-выполнение запрещено: требуется owner-approved executor"
    )


def reconcile_operation(
    operation_id: str,
    now: datetime | None = None,
) -> ActionRun:
    """Восстанавливает pending attempt только live read-ом без mutation retry."""

    record = find_operation_by_id(operation_id)
    if record is None:
        raise LookupError("operation_id не найден")
    checked_at = _clock(now)
    if not record.reconciliation_required:
        return _run_from_record(record)
    return recover_incomplete_operation(
        record,
        _adapter_registry._FixedAdapterRegistry(),
        checked_at,
    )


def get_operation(operation_id: str) -> ActionRun | None:
    """Возвращает сохранённое состояние без provider reads."""

    record = find_operation_by_id(operation_id)
    return None if record is None else _run_from_record(record)
