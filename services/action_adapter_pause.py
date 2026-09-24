"""Закрытые PAUSE/UNPAUSE adapters для sealed approval gateway."""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime

import hashlib
import json

from integrations.facebook_ads_mutation_transport import (
    AttestationRejected,
    MutationOutcomeUnknown,
    MutationRejected,
    set_ad_status,
)
from services.action_gateway_core import (
    AdapterMutationResult,
    AdapterPostconditionResult,
)
from services.action_locks import adset_locks
from services.action_manifests import manifest_from_operation_record
from services.adset_pause_guard import (
    _validate_pause_locked,
    fetch_pause_inventory,
    unrelated_inventory_baseline,
)
from services.approval_checker_models import (
    ActionBatchManifest,
    ActionManifest,
    ActionObservation,
    ActionResult,
    OperationItemRecord,
    PauseManifest,
    SourceSystem,
    UnpauseManifest,
    canonical_json,
    manifest_sha256,
)
from services.owner_action_models import ActionAttemptAttestation
from services.approval_source_facebook import (
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


@dataclass(slots=True)
class _PauseScopeState:
    manifest_id: str
    provider_precondition: ActionObservation | None = None


_SCOPE_STATE: ContextVar[_PauseScopeState | None] = ContextVar(
    "approval_pause_scope_state",
    default=None,
)


def _require_pause(item: ActionManifest) -> PauseManifest:
    if not isinstance(item, PauseManifest):
        raise TypeError("PauseActionAdapter принимает только PauseManifest")
    return item


def _require_unpause(item: ActionManifest) -> UnpauseManifest:
    if not isinstance(item, UnpauseManifest):
        raise TypeError("UnpauseActionAdapter принимает только UnpauseManifest")
    return item


def _subject_id(item: PauseManifest | UnpauseManifest) -> str:
    return f"ad:{item.ad_id}"


def _single_item_batch(
    item: PauseManifest | UnpauseManifest,
) -> ActionBatchManifest:
    draft = ActionBatchManifest(
        batch_manifest_id=f"precondition:{item.manifest_id}",
        correlation_id=f"precondition:{item.manifest_id}",
        idempotency_key=item.idempotency_key,
        prepared_at=item.prepared_at,
        subject_ids=(_subject_id(item),),
        actions=(item,),
        manifest_sha256="0" * 64,
    )
    return replace(draft, manifest_sha256=manifest_sha256(draft))


def _five_source_observation(
    item: PauseManifest | UnpauseManifest,
    now: datetime,
    provider: ActionObservation,
) -> ActionObservation:
    bundle = load_action_item_evidence(_single_item_batch(item), item, 0, now)
    sources = {source.source for source in bundle.sources}
    if sources != _ACTION_SOURCES or any(
        not source.complete or source.from_cache for source in bundle.sources
    ):
        raise RuntimeError("ACTION_FIVE_SOURCE_INCOMPLETE")
    if len(bundle.freshness) != len(_ACTION_SOURCES) or any(
        not freshness.fresh or not freshness.complete or freshness.from_cache
        for freshness in bundle.freshness
    ):
        raise RuntimeError("ACTION_FIVE_SOURCE_STALE")
    return ActionObservation(
        observed_at=now,
        digest=bundle.final_live_state_sha256,
        target_state=provider.target_state,
        subject_ids=(_subject_id(item),),
        unrelated_state_digest=provider.unrelated_state_digest,
    )


def _target_statuses(observation: ActionObservation) -> tuple[str, ...]:
    return tuple(part for part in observation.target_state.split("|") if part)


def _inventory_matches_sibling_baseline(
    manifest: PauseManifest | UnpauseManifest,
    inventory: dict,
) -> tuple[bool, str]:
    """Сверяет весь target-excluded inventory с immutable WAL baseline."""

    digest, snapshot = unrelated_inventory_baseline(
        inventory,
        target_ad_id=manifest.ad_id,
        expected_adset_id=manifest.adset_id,
    )
    matches = (
        digest == manifest.pre_unrelated_inventory_sha256
        and snapshot == manifest.sibling_status_snapshot
    )
    return matches, digest


def _scope_state(item: PauseManifest | UnpauseManifest) -> _PauseScopeState:
    state = _SCOPE_STATE.get()
    if state is None or state.manifest_id != item.manifest_id:
        raise RuntimeError("PAUSE_EXECUTION_SCOPE_REQUIRED")
    return state


@contextmanager
def _one_adset_scope(item: PauseManifest | UnpauseManifest):
    with adset_locks((item.adset_id,)):
        token = _SCOPE_STATE.set(_PauseScopeState(manifest_id=item.manifest_id))
        try:
            yield
        finally:
            _SCOPE_STATE.reset(token)


class PauseActionAdapter:
    """Разрешает только exact ACTIVE -> PAUSED под одним adset lock."""

    def execution_scope(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> AbstractContextManager[None]:
        del now
        return _one_adset_scope(_require_pause(item))

    def read_precondition(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> ActionObservation:
        manifest = _require_pause(item)
        state = _scope_state(manifest)
        locked = _validate_pause_locked(manifest)
        if not locked.allowed:
            code = (
                locked.check_reason.code
                if locked.check_reason is not None
                else "PAUSE_GUARD_DENIED"
            )
            raise RuntimeError(code)
        if locked.inventory_sha256 != manifest.pre_inventory_sha256:
            raise RuntimeError("INVENTORY_CHANGED")
        provider = read_facebook_precondition(manifest, now)
        statuses = _target_statuses(provider)
        if len(statuses) < 2 or statuses[0] != "ACTIVE" or statuses[1] != "ACTIVE":
            raise RuntimeError("PAUSE_TARGET_CHANGED")
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
        manifest = _require_pause(item)
        _scope_state(manifest)
        try:
            accepted = set_ad_status(
                attempt,
                account_id=attempt.account_id,
                ad_id=manifest.ad_id,
                status="PAUSED",
                payload_sha256=attempt.payload_sha256,
            )
        except (AttestationRejected, MutationRejected):
            return AdapterMutationResult(
                result=ActionResult.FAILED,
                reason_code="PAUSE_PROVIDER_REJECTED",
                remote_may_have_changed=False,
            )
        except MutationOutcomeUnknown:
            return AdapterMutationResult(
                result=ActionResult.UNKNOWN,
                reason_code="PAUSE_PROVIDER_OUTCOME_UNKNOWN",
                remote_may_have_changed=True,
            )
        if not accepted:
            return AdapterMutationResult(
                result=ActionResult.FAILED,
                reason_code="PAUSE_PROVIDER_REJECTED",
                remote_may_have_changed=False,
            )
        return AdapterMutationResult(result=None)

    def read_postcondition(
        self,
        item: ActionManifest,
        created_ids: tuple[str, ...],
        now: datetime,
    ) -> AdapterPostconditionResult:
        del created_ids
        manifest = _require_pause(item)
        state = _scope_state(manifest)
        provider = read_facebook_postcondition(manifest, (), now)
        statuses = _target_statuses(provider)
        if len(statuses) < 2 or statuses[0] != "PAUSED" or statuses[1] == "ACTIVE":
            return AdapterPostconditionResult(
                result=ActionResult.PARTIAL,
                observation=provider,
                reason_code="PAUSE_TARGET_NOT_CONFIRMED",
                remote_may_have_changed=True,
            )
        baseline = state.provider_precondition
        if baseline is None or provider.unrelated_state_digest != baseline.unrelated_state_digest:
            return AdapterPostconditionResult(
                result=ActionResult.PARTIAL,
                observation=provider,
                reason_code="PAUSE_SIBLING_DRIFT",
                remote_may_have_changed=True,
            )
        inventory_ids = tuple(manifest.sibling_active_ids)
        if manifest.replacement_ad_id is not None:
            inventory_ids = (*inventory_ids, manifest.replacement_ad_id)
        inventories = fetch_pause_inventory(list(dict.fromkeys(inventory_ids)))
        inventory = inventories.get(manifest.adset_id)
        if inventory is None or not inventory.get("complete"):
            return AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=provider,
                reason_code="PAUSE_POST_INVENTORY_UNAVAILABLE",
                remote_may_have_changed=True,
            )
        siblings_match, _sibling_digest = _inventory_matches_sibling_baseline(
            manifest,
            inventory,
        )
        if not siblings_match:
            return AdapterPostconditionResult(
                result=ActionResult.PARTIAL,
                observation=provider,
                reason_code="PAUSE_SIBLING_DRIFT",
                remote_may_have_changed=True,
            )
        active_ids = set(inventory.get("active_ids") or set())
        if manifest.ad_id in active_ids or active_ids != set(manifest.sibling_active_ids):
            return AdapterPostconditionResult(
                result=ActionResult.PARTIAL,
                observation=provider,
                reason_code="PAUSE_ACTIVE_GROUP_DRIFT",
                remote_may_have_changed=True,
            )
        if not active_ids:
            return AdapterPostconditionResult(
                result=ActionResult.PARTIAL,
                observation=provider,
                reason_code="LAST_EFFECTIVE_ACTIVE",
                remote_may_have_changed=True,
            )
        return AdapterPostconditionResult(
            result=ActionResult.CONFIRMED,
            observation=provider,
            reason_code="TARGET_PAUSED",
            remote_may_have_changed=True,
        )

    def finalize_after_scope(
        self,
        item: ActionManifest,
        mutation: AdapterMutationResult,
        post: AdapterPostconditionResult,
        now: datetime,
    ) -> None:
        del mutation, post, now
        _require_pause(item)

    def reconcile(
        self,
        item: OperationItemRecord,
        now: datetime,
    ) -> AdapterPostconditionResult:
        return _reconcile_ad_status(item, now, manifest_type=PauseManifest)


def _reconcile_ad_status(
    item: OperationItemRecord,
    now: datetime,
    *,
    manifest_type,
) -> AdapterPostconditionResult:
    try:
        manifest = manifest_from_operation_record(item, manifest_type)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return AdapterPostconditionResult(
            result=ActionResult.UNKNOWN,
            observation=None,
            reason_code="WAL_MANIFEST_INVALID",
            remote_may_have_changed=True,
        )
    try:
        with adset_locks((manifest.adset_id,)):
            inventories = fetch_pause_inventory([manifest.ad_id])
    except Exception:
        inventories = {}
    inventory = inventories.get(manifest.adset_id)
    if inventory is None or inventory.get("complete") is not True:
        return AdapterPostconditionResult(
            result=ActionResult.UNKNOWN,
            observation=None,
            reason_code="AD_STATUS_RECONCILIATION_UNAVAILABLE",
            remote_may_have_changed=True,
        )
    context = (inventory.get("inventory_context") or {}).get(manifest.ad_id)
    if not isinstance(context, dict):
        return AdapterPostconditionResult(
            result=ActionResult.PARTIAL,
            observation=None,
            reason_code="AD_TARGET_MISSING",
            remote_may_have_changed=True,
        )
    configured = str(context.get("configured_status") or "")
    effective = str(context.get("effective_status") or "")
    try:
        siblings_match, unrelated_digest = _inventory_matches_sibling_baseline(
            manifest,
            inventory,
        )
    except ValueError:
        return AdapterPostconditionResult(
            result=ActionResult.UNKNOWN,
            observation=None,
            reason_code="AD_STATUS_RECONCILIATION_INVALID",
            remote_may_have_changed=True,
        )
    state = {"ad_id": manifest.ad_id, "configured_status": configured, "effective_status": effective}
    observation = ActionObservation(
        observed_at=now,
        digest=hashlib.sha256(canonical_json(state)).hexdigest(),
        target_state=f"{configured}|{effective}",
        subject_ids=item.subject_ids,
        unrelated_state_digest=unrelated_digest,
    )
    if isinstance(manifest, PauseManifest):
        active_ids = set(inventory.get("active_ids") or set())
        confirmed = (
            configured == "PAUSED"
            and effective != "ACTIVE"
            and manifest.ad_id not in active_ids
            and active_ids == set(manifest.sibling_active_ids)
            and bool(active_ids)
            and siblings_match
        )
        reason = "TARGET_PAUSED" if confirmed else "PAUSE_RECONCILIATION_DRIFT"
    else:
        confirmed = configured == "ACTIVE" and effective == "ACTIVE" and siblings_match
        reason = "TARGET_ACTIVE" if confirmed else "UNPAUSE_RECONCILIATION_DRIFT"
    return AdapterPostconditionResult(
        result=ActionResult.CONFIRMED if confirmed else ActionResult.PARTIAL,
        observation=observation,
        reason_code=reason,
        remote_may_have_changed=True,
    )


class UnpauseActionAdapter:
    """Разрешает только exact declared non-ACTIVE -> ACTIVE."""

    def execution_scope(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> AbstractContextManager[None]:
        del now
        return _one_adset_scope(_require_unpause(item))

    def read_precondition(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> ActionObservation:
        manifest = _require_unpause(item)
        state = _scope_state(manifest)
        provider = read_facebook_precondition(manifest, now)
        statuses = _target_statuses(provider)
        if (
            len(statuses) < 2
            or statuses[0] != manifest.expected_before_status
            or statuses[1] == "ACTIVE"
        ):
            raise RuntimeError("UNPAUSE_TARGET_CHANGED")
        inventories = fetch_pause_inventory([manifest.ad_id])
        inventory = inventories.get(manifest.adset_id)
        if inventory is None or inventory.get("complete") is not True:
            raise RuntimeError("INVENTORY_CHANGED")
        if inventory.get("state_sha256") != manifest.pre_inventory_sha256:
            raise RuntimeError("INVENTORY_CHANGED")
        siblings_match, _sibling_digest = _inventory_matches_sibling_baseline(
            manifest,
            inventory,
        )
        if not siblings_match:
            raise RuntimeError("INVENTORY_CHANGED")
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
        manifest = _require_unpause(item)
        _scope_state(manifest)
        try:
            accepted = set_ad_status(
                attempt,
                account_id=attempt.account_id,
                ad_id=manifest.ad_id,
                status="ACTIVE",
                payload_sha256=attempt.payload_sha256,
            )
        except (AttestationRejected, MutationRejected):
            return AdapterMutationResult(
                result=ActionResult.FAILED,
                reason_code="UNPAUSE_PROVIDER_REJECTED",
                remote_may_have_changed=False,
            )
        except MutationOutcomeUnknown:
            return AdapterMutationResult(
                result=ActionResult.UNKNOWN,
                reason_code="UNPAUSE_PROVIDER_OUTCOME_UNKNOWN",
                remote_may_have_changed=True,
            )
        if not accepted:
            return AdapterMutationResult(
                result=ActionResult.FAILED,
                reason_code="UNPAUSE_PROVIDER_REJECTED",
                remote_may_have_changed=False,
            )
        return AdapterMutationResult(result=None)

    def read_postcondition(
        self,
        item: ActionManifest,
        created_ids: tuple[str, ...],
        now: datetime,
    ) -> AdapterPostconditionResult:
        del created_ids
        manifest = _require_unpause(item)
        state = _scope_state(manifest)
        provider = read_facebook_postcondition(manifest, (), now)
        statuses = _target_statuses(provider)
        baseline = state.provider_precondition
        if len(statuses) < 2 or statuses[0] != "ACTIVE" or statuses[1] != "ACTIVE":
            result = ActionResult.PARTIAL
            reason = "UNPAUSE_TARGET_NOT_CONFIRMED"
        elif baseline is None or provider.unrelated_state_digest != baseline.unrelated_state_digest:
            result = ActionResult.PARTIAL
            reason = "UNPAUSE_SIBLING_DRIFT"
        else:
            inventories = fetch_pause_inventory([manifest.ad_id])
            inventory = inventories.get(manifest.adset_id)
            if inventory is None or inventory.get("complete") is not True:
                result = ActionResult.UNKNOWN
                reason = "UNPAUSE_POST_INVENTORY_UNAVAILABLE"
            else:
                siblings_match, _sibling_digest = _inventory_matches_sibling_baseline(
                    manifest,
                    inventory,
                )
                result = ActionResult.CONFIRMED if siblings_match else ActionResult.PARTIAL
                reason = "TARGET_ACTIVE" if siblings_match else "UNPAUSE_SIBLING_DRIFT"
        return AdapterPostconditionResult(
            result=result,
            observation=provider,
            reason_code=reason,
            remote_may_have_changed=True,
        )

    def finalize_after_scope(
        self,
        item: ActionManifest,
        mutation: AdapterMutationResult,
        post: AdapterPostconditionResult,
        now: datetime,
    ) -> None:
        del mutation, post, now
        _require_unpause(item)

    def reconcile(
        self,
        item: OperationItemRecord,
        now: datetime,
    ) -> AdapterPostconditionResult:
        return _reconcile_ad_status(item, now, manifest_type=UnpauseManifest)
