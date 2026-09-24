"""Отдельный approval adapter для CREATE из existing Facebook creative."""

from __future__ import annotations

import hashlib
import json
from contextlib import AbstractContextManager
from datetime import datetime

from integrations.facebook import (
    AssetRecoveryCreateNotStarted,
    _execute_asset_recovery_manifest_unchecked,
    get_adset_info,
)
from services.action_gateway_core import AdapterMutationResult, AdapterPostconditionResult
from services.action_locks import launch_execution_lease
from services.adset_pause_guard import adset_mutation_locks
from services.approval_checker_models import (
    ActionKind,
    ActionManifest,
    ActionObservation,
    ActionOrigin,
    ActionResult,
    AssetRecoveryManifest,
    OperationItemRecord,
    canonical_json,
    manifest_sha256,
)
from services.owner_action_models import ActionAttemptAttestation
from services.approval_source_facebook import (
    read_action_postcondition,
    read_action_precondition,
)
from services.asset_recovery_repository import (
    get_attempt_state,
    mark_created_id_reconcile_required,
    mark_reconcile_required,
)


def _require_manifest(item: ActionManifest) -> AssetRecoveryManifest:
    if not isinstance(item, AssetRecoveryManifest):
        raise TypeError("AssetRecoveryActionAdapter принимает только AssetRecoveryManifest")
    return item


def _manifest_from_record(item: OperationItemRecord) -> AssetRecoveryManifest:
    if item.action_manifest_json is None:
        raise ValueError("WAL не содержит immutable action manifest")
    payload = json.loads(item.action_manifest_json)
    if not isinstance(payload, dict):
        raise ValueError("WAL action manifest должен быть object")
    payload["kind"] = ActionKind(payload["kind"])
    payload["origin"] = ActionOrigin(payload["origin"])
    payload["prepared_at"] = datetime.fromisoformat(payload["prepared_at"])
    manifest = AssetRecoveryManifest(**payload)
    if manifest_sha256(manifest) != item.item_manifest_sha256:
        raise ValueError("WAL action manifest hash не совпадает с item record")
    return manifest


def _terminal_observation(
    manifest: AssetRecoveryManifest,
    now: datetime,
    state: str,
) -> ActionObservation:
    digest = hashlib.sha256(canonical_json({"state": state})).hexdigest()
    return ActionObservation(
        observed_at=now,
        digest=digest,
        target_state=state,
        subject_ids=(f"adset:{manifest.target_adset_id}",),
        unrelated_state_digest=hashlib.sha256(canonical_json(())).hexdigest(),
    )


class AssetRecoveryActionAdapter:
    """Locks source+target и допускает ровно один durable-claimed POST."""

    def execution_scope(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> AbstractContextManager[None]:
        del now
        manifest = _require_manifest(item)
        return self._locked_scope(manifest)

    @staticmethod
    def _locked_scope(manifest: AssetRecoveryManifest) -> AbstractContextManager[None]:
        lease = launch_execution_lease(manifest.manifest_id)

        class _Scope:
            def __enter__(self):
                lease.__enter__()
                try:
                    self._locks = adset_mutation_locks(
                        tuple(dict.fromkeys((manifest.source_adset_id, manifest.target_adset_id)))
                    )
                    self._locks.__enter__()
                except Exception:
                    lease.__exit__(None, None, None)
                    raise
                return None

            def __exit__(self, exc_type, exc, traceback):
                locks_result = self._locks.__exit__(exc_type, exc, traceback)
                lease_result = lease.__exit__(exc_type, exc, traceback)
                return bool(locks_result or lease_result)

        return _Scope()

    def read_precondition(
        self,
        item: ActionManifest,
        now: datetime,
    ) -> ActionObservation:
        return read_action_precondition(_require_manifest(item), now)

    def mutate(
        self,
        item: ActionManifest,
        now: datetime,
        *,
        attempt: ActionAttemptAttestation,
    ) -> AdapterMutationResult:
        del now
        manifest = _require_manifest(item)
        try:
            ad_id = _execute_asset_recovery_manifest_unchecked(manifest, attempt)
        except AssetRecoveryCreateNotStarted:
            return AdapterMutationResult(
                result=ActionResult.FAILED,
                reason_code="ASSET_RECOVERY_CREATE_NOT_STARTED",
                remote_may_have_changed=False,
            )
        return AdapterMutationResult(result=None, created_ids=(ad_id,))

    def read_postcondition(
        self,
        item: ActionManifest,
        created_ids: tuple[str, ...],
        now: datetime,
    ) -> AdapterPostconditionResult:
        manifest = _require_manifest(item)
        try:
            observation = read_action_postcondition(manifest, created_ids, now)
        except Exception:
            return AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                created_ids=created_ids,
                reason_code="ASSET_RECOVERY_POSTCONDITION_UNKNOWN",
                remote_may_have_changed=True,
            )
        return AdapterPostconditionResult(
            result=ActionResult.CONFIRMED,
            observation=observation,
            created_ids=created_ids,
            reason_code="ASSET_RECOVERY_EXACT_ACTIVE_CONFIRMED",
            remote_may_have_changed=True,
        )

    def finalize_after_scope(
        self,
        item: ActionManifest,
        mutation: AdapterMutationResult,
        post: AdapterPostconditionResult,
        now: datetime,
    ) -> None:
        del mutation
        _require_manifest(item)
        if post.result is not ActionResult.CONFIRMED:
            if len(post.created_ids) == 1 and post.created_ids[0].isdigit():
                mark_created_id_reconcile_required(post.created_ids[0], now)

    def reconcile(
        self,
        item: OperationItemRecord,
        now: datetime,
    ) -> AdapterPostconditionResult:
        manifest = _manifest_from_record(item)
        if item.attempt_id is None:
            raise ValueError("reconciliation требует attempt_id")
        state = get_attempt_state(item.attempt_id)
        if state is None or state["phase"] == "RESERVED":
            reason = (
                "ASSET_RECOVERY_SQL_AUTH_MISSING"
                if state is None
                else "ASSET_RECOVERY_CREATE_NOT_STARTED"
            )
            return AdapterPostconditionResult(
                result=ActionResult.FAILED,
                observation=_terminal_observation(manifest, now, reason),
                reason_code=reason,
                remote_may_have_changed=False,
            )
        created_id = state["created_ad_id"]
        if not created_id:
            try:
                inventory = get_adset_info(manifest.target_adset_id)
                matches = [
                    row
                    for row in inventory.get("ads", [])
                    if isinstance(row, dict)
                    and row.get("name") == manifest.target_ad_name
                ]
                if len(matches) == 1 and str(matches[0].get("id") or "").isdigit():
                    created_id = str(matches[0]["id"])
            except Exception:
                created_id = ""
        if not created_id:
            return AdapterPostconditionResult(
                result=ActionResult.UNKNOWN,
                observation=None,
                reason_code="ASSET_RECOVERY_RECONCILE_AMBIGUOUS",
                remote_may_have_changed=True,
            )
        result = self.read_postcondition(manifest, (created_id,), now)
        if result.result is not ActionResult.CONFIRMED:
            mark_reconcile_required(state["auth_id"], now)
        return result
