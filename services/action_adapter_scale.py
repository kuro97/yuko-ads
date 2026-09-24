"""Закрытый SCALE adapter с cap reservation вне adset lock."""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
import logging
import threading

from services import budget_daily_cap
from services.action_gateway_core import (
    AdapterMutationResult,
    AdapterPostconditionResult,
)
from services.action_locks import adset_locks
from services.action_manifests import manifest_from_operation_record
from integrations.facebook_ads_mutation_transport import (
    AttestationRejected,
    MutationOutcomeUnknown,
    MutationRejected,
    set_adset_budget,
)
from services.action_remote_scale import _budget_to_minor_units
from services.approval_checker_models import (
    ActionBatchManifest,
    ActionManifest,
    ActionObservation,
    ActionResult,
    Metric,
    OperationItemRecord,
    ScaleManifest,
    SourceSystem,
    SubjectKind,
    manifest_sha256,
)
from services.owner_action_models import ActionAttemptAttestation
from services.approval_source_facebook import (
    _manifest_request,
    load_facebook_evidence,
    read_action_postcondition as read_facebook_postcondition,
    read_action_precondition as read_facebook_precondition,
)
from services.approval_sources import load_action_item_evidence


_ACTION_SOURCES = frozenset(
    {
        SourceSystem.FACEBOOK,
        SourceSystem.TRELLO,
        SourceSystem.MEDIA_BYTES,
        SourceSystem.AMO,
        SourceSystem.CDP_ERP,
    }
)
_CODE_OWNED_DAILY_CAP_PCT = 15.0
_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ScaleScopeState:
    manifest_id: str
    reservation_id: str
    provider_precondition: ActionObservation | None = None
    mutation_state: str = "NOT_ATTEMPTED"


_SCOPE_STATE: ContextVar[_ScaleScopeState | None] = ContextVar(
    "approval_scale_scope_state",
    default=None,
)
_FINALIZE_LOCK = threading.Lock()
_PENDING_FINALIZE: dict[str, _ScaleScopeState] = {}


def _require_scale(item: ActionManifest) -> ScaleManifest:
    if not isinstance(item, ScaleManifest):
        raise TypeError("ScaleActionAdapter принимает только ScaleManifest")
    return item


def _single_item_batch(item: ScaleManifest) -> ActionBatchManifest:
    draft = ActionBatchManifest(
        batch_manifest_id=f"precondition:{item.manifest_id}",
        correlation_id=f"precondition:{item.manifest_id}",
        idempotency_key=item.idempotency_key,
        prepared_at=item.prepared_at,
        subject_ids=(f"adset:{item.adset_id}",),
        actions=(item,),
        manifest_sha256="0" * 64,
    )
    return replace(draft, manifest_sha256=manifest_sha256(draft))


def _scope_state(item: ScaleManifest) -> _ScaleScopeState:
    state = _SCOPE_STATE.get()
    if state is None or state.manifest_id != item.manifest_id:
        raise RuntimeError("SCALE_EXECUTION_SCOPE_REQUIRED")
    return state


def _five_source_observation(
    item: ScaleManifest,
    now: datetime,
    provider: ActionObservation,
) -> ActionObservation:
    bundle = load_action_item_evidence(_single_item_batch(item), item, 0, now)
    sources = {source.source for source in bundle.sources}
    if sources != _ACTION_SOURCES or any(
        not source.complete or source.from_cache for source in bundle.sources
    ):
        raise RuntimeError("SCALE_FIVE_SOURCE_INCOMPLETE")
    if len(bundle.freshness) != len(_ACTION_SOURCES) or any(
        not freshness.fresh or not freshness.complete or freshness.from_cache
        for freshness in bundle.freshness
    ):
        raise RuntimeError("SCALE_FIVE_SOURCE_STALE")
    return ActionObservation(
        observed_at=now,
        digest=bundle.final_live_state_sha256,
        target_state=provider.target_state,
        subject_ids=(f"adset:{item.adset_id}",),
        unrelated_state_digest=provider.unrelated_state_digest,
    )


def _record_budget_raised(item: ScaleManifest) -> None:
    """Пишет решение BUDGET_RAISED по факту ИСПОЛНЕННОГО подъёма.

    Читатели журнала (services/evening_report.py, services/morning_digest.py)
    считают «⬆️ Подняли бюджет» именно по этому решению. Producer его писать не
    имеет права — предложение ещё не подъём, — поэтому запись живёт здесь, в
    точке подтверждённой мутации. ``effect_id`` привязан к manifest_id: повтор
    (finalize + последующий reconcile) идемпотентен, второй строки не появится.
    Сбой журнала НЕ отменяет исполненный подъём — только лог.
    """
    try:
        from agent.repositories import decisions_repo

        ad_id = item.candidate_ad_ids[0]
        applied_pct = (
            (item.target_budget - item.current_budget) / item.current_budget * 100
        )
        decisions_repo.save_decision(
            "default",
            ad_id,
            ad_id,
            "BUDGET_RAISED",
            # Формат совместим с обрезкой в evening_report (ищет "), "):
            # владельцу в отчёт уходит «adset X: 100→115 USD (+15%)».
            reason=(
                f"adset {item.adset_id}: {item.current_budget}→{item.target_budget} "
                f"{item.currency} (+{applied_pct:.0f}%), одобрено владельцем"
            ),
            confirmed_by="budget_pilot",
            effect_id=f"{item.manifest_id}:decision:BUDGET_RAISED",
        )
    except Exception as exc:
        # Журнал решений — отчётность, а не контур безопасности: исполненный
        # подъём нельзя откатить из-за неудачной записи.
        _logger.warning(
            "BUDGET_RAISED не записан для adset %s: %s",
            item.adset_id,
            type(exc).__name__,
        )


def _exact_budget(item: ScaleManifest, now: datetime) -> Decimal:
    evidence = load_facebook_evidence(
        _manifest_request(item, now),
        now,
        force_live=True,
    )
    if not evidence.complete or evidence.from_cache:
        raise RuntimeError("SCALE_BUDGET_UNAVAILABLE")
    matches = [
        record
        for record in evidence.records
        if record.subject.kind is SubjectKind.ADSET
        and record.subject.subject_id == item.adset_id
        and record.metric is Metric.DAILY_BUDGET
        and record.source is SourceSystem.FACEBOOK
    ]
    if len(matches) != 1 or not isinstance(matches[0].value, Decimal):
        raise RuntimeError("SCALE_BUDGET_MISSING")
    if matches[0].currency != item.currency:
        raise RuntimeError("SCALE_CURRENCY_CHANGED")
    return matches[0].value


class ScaleActionAdapter:
    """Резервирует дневной cap, затем меняет exact budget под adset lock."""

    def execution_scope(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> AbstractContextManager[None]:
        return self._reserved_scope(_require_scale(item), now)

    @staticmethod
    @contextmanager
    def _reserved_scope(item: ScaleManifest, now: datetime):
        # reserve_raise сам кратко берёт cap lock и полностью отпускает его
        # до захвата adset lock.
        reservation_id = budget_daily_cap.reserve_raise(
            item.adset_id,
            float(item.current_budget),
            float(item.target_budget),
            _CODE_OWNED_DAILY_CAP_PCT,
            now=now,
        )
        if reservation_id is None:
            raise RuntimeError("SCALE_DAILY_CAP_DENIED")
        state = _ScaleScopeState(item.manifest_id, reservation_id)
        token = _SCOPE_STATE.set(state)
        try:
            with adset_locks((item.adset_id,)):
                yield
        finally:
            _SCOPE_STATE.reset(token)
            if state.mutation_state == "NOT_ATTEMPTED":
                # Precondition/attempt отказались до provider: core не зовёт
                # finalizer, поэтому снимаем reserve сразу после unlock.
                budget_daily_cap.release_reservation(
                    item.adset_id,
                    reservation_id,
                    now=now,
                )
            else:
                with _FINALIZE_LOCK:
                    if item.manifest_id in _PENDING_FINALIZE:
                        raise RuntimeError("SCALE_FINALIZER_DUPLICATE")
                    _PENDING_FINALIZE[item.manifest_id] = state

    def read_precondition(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> ActionObservation:
        manifest = _require_scale(item)
        state = _scope_state(manifest)
        provider = read_facebook_precondition(manifest, now)
        if provider.target_state != manifest.expected_status:
            raise RuntimeError("SCALE_STATUS_CHANGED")
        if _exact_budget(manifest, now) != manifest.current_budget:
            raise RuntimeError("SCALE_BUDGET_CHANGED")
        state.provider_precondition = provider
        return _five_source_observation(manifest, now, provider)

    def mutate(
        self,
        item: ActionManifest,
        now: datetime,
        *,
        attempt: ActionAttemptAttestation,
    ) -> AdapterMutationResult:
        del now
        manifest = _require_scale(item)
        state = _scope_state(manifest)
        try:
            accepted = set_adset_budget(
                attempt,
                account_id=attempt.account_id,
                adset_id=manifest.adset_id,
                daily_budget_minor_units=_budget_to_minor_units(
                    manifest.target_budget
                ),
                payload_sha256=attempt.payload_sha256,
            )
        except (AttestationRejected, MutationRejected):
            state.mutation_state = "REJECTED"
            return AdapterMutationResult(
                result=ActionResult.FAILED,
                reason_code="SCALE_PROVIDER_REJECTED",
                remote_may_have_changed=False,
            )
        except MutationOutcomeUnknown:
            state.mutation_state = "UNKNOWN"
            return AdapterMutationResult(
                result=ActionResult.UNKNOWN,
                reason_code="SCALE_PROVIDER_OUTCOME_UNKNOWN",
                remote_may_have_changed=True,
            )
        if not accepted:
            state.mutation_state = "REJECTED"
            return AdapterMutationResult(
                result=ActionResult.FAILED,
                reason_code="SCALE_PROVIDER_REJECTED",
                remote_may_have_changed=False,
            )
        state.mutation_state = "ACCEPTED"
        return AdapterMutationResult(result=None)

    def read_postcondition(
        self,
        item: ActionManifest,
        created_ids: tuple[str, ...],
        now: datetime,
    ) -> AdapterPostconditionResult:
        del created_ids
        manifest = _require_scale(item)
        state = _scope_state(manifest)
        provider = read_facebook_postcondition(manifest, (), now)
        try:
            actual_budget = _exact_budget(manifest, now)
        except Exception:
            return AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=provider,
                reason_code="SCALE_POST_BUDGET_UNAVAILABLE",
                remote_may_have_changed=True,
            )
        baseline = state.provider_precondition
        if (
            actual_budget != manifest.target_budget
            or provider.target_state != manifest.expected_status
            or baseline is None
            or provider.unrelated_state_digest != baseline.unrelated_state_digest
        ):
            return AdapterPostconditionResult(
                result=ActionResult.PARTIAL,
                observation=provider,
                reason_code="SCALE_POSTCONDITION_MISMATCH",
                remote_may_have_changed=True,
            )
        return AdapterPostconditionResult(
            result=ActionResult.CONFIRMED,
            observation=provider,
            reason_code="TARGET_BUDGET_CONFIRMED",
            remote_may_have_changed=True,
        )

    def finalize_after_scope(
        self,
        item: ActionManifest,
        mutation: AdapterMutationResult,
        post: AdapterPostconditionResult,
        now: datetime,
    ) -> None:
        manifest = _require_scale(item)
        with _FINALIZE_LOCK:
            state = _PENDING_FINALIZE.pop(manifest.manifest_id, None)
        if state is None:
            raise RuntimeError("SCALE_FINALIZER_STATE_MISSING")
        if post.result is ActionResult.CONFIRMED:
            budget_daily_cap.commit_reservation(
                manifest.adset_id,
                state.reservation_id,
                now=now,
            )
            _record_budget_raised(manifest)
            return
        if (
            mutation.result is ActionResult.FAILED
            and not mutation.remote_may_have_changed
        ):
            budget_daily_cap.release_reservation(
                manifest.adset_id,
                state.reservation_id,
                now=now,
            )
        # PARTIAL/UNKNOWN после возможной mutation сохраняет pending.

    def reconcile(
        self,
        item: OperationItemRecord,
        now: datetime,
    ) -> AdapterPostconditionResult:
        try:
            manifest = manifest_from_operation_record(item, ScaleManifest)
            with adset_locks((manifest.adset_id,)):
                provider = read_facebook_postcondition(manifest, (), now)
                budget = _exact_budget(manifest, now)
        except Exception:
            return AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                reason_code="SCALE_RECONCILIATION_UNAVAILABLE",
                remote_may_have_changed=True,
            )
        if provider.target_state != manifest.expected_status:
            return AdapterPostconditionResult(
                result=ActionResult.PARTIAL,
                observation=provider,
                reason_code="SCALE_STATUS_CONFLICT",
                remote_may_have_changed=True,
            )
        if budget == manifest.target_budget:
            budget_daily_cap.reconcile_pending(
                manifest.adset_id,
                float(manifest.target_budget),
                now=now,
            )
            # Сверка тоже подтверждает исполненный подъём (crash между FB-успехом
            # и finalize). effect_id один и тот же — дубля в журнале не будет.
            _record_budget_raised(manifest)
            return AdapterPostconditionResult(
                result=ActionResult.CONFIRMED,
                observation=provider,
                reason_code="TARGET_BUDGET_CONFIRMED",
                remote_may_have_changed=True,
            )
        if budget == manifest.current_budget:
            return AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=provider,
                reason_code="SCALE_ORIGINAL_BUDGET_AMBIGUOUS",
                remote_may_have_changed=True,
            )
        return AdapterPostconditionResult(
            result=ActionResult.PARTIAL,
            observation=provider,
            reason_code="SCALE_BUDGET_CONFLICT",
            remote_may_have_changed=True,
        )
