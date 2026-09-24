"""Одноразовая capability для одного workflow-bound DELETE."""

from __future__ import annotations

import threading
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from services.cleanup_repository import (
    CleanupDeleteClaim,
    CleanupClaimError,
    create_cleanup_delete_authorization,
    mark_cleanup_delete_http_started,
    revoke_cleanup_delete_authorization,
)


class CleanupAuthorizationError(RuntimeError):
    """Authorization отсутствует, не совпадает, истекла или уже потреблена."""


@dataclass(frozen=True, slots=True, weakref_slot=True)
class CleanupDeleteAuthorization:
    """Неподделываемое разрешение на один exact ad DELETE."""

    token_id: str
    claim_id: str
    workflow_id: str
    adset_id: str
    ad_id: str
    purpose: Literal["REPLACEMENT_SLOT"]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _AuthorizationRecord:
    reference: weakref.ReferenceType[CleanupDeleteAuthorization]
    token_id: str
    claim_id: str
    workflow_id: str
    adset_id: str
    ad_id: str
    purpose: Literal["REPLACEMENT_SLOT"]
    expires_at: datetime


_AUTHORIZATION_LOCK = threading.Lock()
_AUTHORIZATIONS: dict[str, _AuthorizationRecord] = {}
_ISSUED_CLAIM_IDS: set[str] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required_exact_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise CleanupAuthorizationError(f"{field_name}_invalid")
    return value


def issue_delete_authorization(
    claim: CleanupDeleteClaim,
    ttl: timedelta = timedelta(minutes=5),
) -> CleanupDeleteAuthorization:
    """Выдаёт capability ровно один раз для committed CLAIMED claim."""
    if type(claim) is not CleanupDeleteClaim:
        raise CleanupAuthorizationError("cleanup_claim_required")
    if claim.purpose != "REPLACEMENT_SLOT" or claim.state != "CLAIMED":
        raise CleanupAuthorizationError("cleanup_claim_not_authorizable")
    claim_id = _required_exact_text(claim.claim_id, "claim_id")
    workflow_id = _required_exact_text(claim.workflow_id, "workflow_id")
    adset_id = _required_exact_text(claim.adset_id, "adset_id")
    ad_id = _required_exact_text(claim.ad_id, "ad_id")
    if type(ttl) is not timedelta or ttl <= timedelta(0):
        raise CleanupAuthorizationError("cleanup_authorization_ttl_invalid")
    expires_at = _now() + ttl
    token_id = f"cleanup-auth-{uuid.uuid4().hex}"
    authorization = CleanupDeleteAuthorization(
        token_id=token_id,
        claim_id=claim_id,
        workflow_id=workflow_id,
        adset_id=adset_id,
        ad_id=ad_id,
        purpose="REPLACEMENT_SLOT",
        expires_at=expires_at,
    )

    def unregister(
        reference: weakref.ReferenceType[CleanupDeleteAuthorization],
    ) -> None:
        with _AUTHORIZATION_LOCK:
            record = _AUTHORIZATIONS.get(token_id)
            if record is not None and record.reference is reference:
                _AUTHORIZATIONS.pop(token_id, None)

    reference = weakref.ref(authorization, unregister)
    record = _AuthorizationRecord(
        reference=reference,
        token_id=token_id,
        claim_id=claim_id,
        workflow_id=workflow_id,
        adset_id=adset_id,
        ad_id=ad_id,
        purpose="REPLACEMENT_SLOT",
        expires_at=expires_at,
    )
    with _AUTHORIZATION_LOCK:
        if claim_id in _ISSUED_CLAIM_IDS:
            raise CleanupAuthorizationError("cleanup_authorization_already_issued")
        try:
            create_cleanup_delete_authorization(
                token_id=token_id,
                claim=claim,
                expires_at=expires_at,
            )
        except CleanupClaimError as exc:
            raise CleanupAuthorizationError(
                "cleanup_durable_claim_not_authorizable"
            ) from exc
        _ISSUED_CLAIM_IDS.add(claim_id)
        _AUTHORIZATIONS[token_id] = record
    return authorization


def consume_delete_authorization(
    authorization: CleanupDeleteAuthorization,
    *,
    claim_id: str,
    workflow_id: str,
    adset_id: str,
    ad_id: str,
    live_capacity_before: int,
) -> None:
    """Фиксирует durable HTTP-start и потребляет exact capability."""
    if not isinstance(authorization, CleanupDeleteAuthorization):
        raise CleanupAuthorizationError("cleanup_authorization_required")
    with _AUTHORIZATION_LOCK:
        record = _AUTHORIZATIONS.pop(authorization.token_id, None)
    if record is None or record.reference() is not authorization:
        raise CleanupAuthorizationError("cleanup_authorization_consumed_or_forged")

    authorization_state = (
        authorization.token_id,
        authorization.claim_id,
        authorization.workflow_id,
        authorization.adset_id,
        authorization.ad_id,
        authorization.purpose,
        authorization.expires_at,
    )
    record_state = (
        record.token_id,
        record.claim_id,
        record.workflow_id,
        record.adset_id,
        record.ad_id,
        record.purpose,
        record.expires_at,
    )
    if authorization_state != record_state:
        raise CleanupAuthorizationError("cleanup_authorization_tampered")
    if _now() >= record.expires_at:
        raise CleanupAuthorizationError("cleanup_authorization_expired")
    expected_binding = (
        _required_exact_text(claim_id, "claim_id"),
        _required_exact_text(workflow_id, "workflow_id"),
        _required_exact_text(adset_id, "adset_id"),
        _required_exact_text(ad_id, "ad_id"),
    )
    if expected_binding != (
        record.claim_id,
        record.workflow_id,
        record.adset_id,
        record.ad_id,
    ):
        raise CleanupAuthorizationError("cleanup_authorization_binding_mismatch")
    try:
        mark_cleanup_delete_http_started(
            token_id=record.token_id,
            claim_id=record.claim_id,
            workflow_id=record.workflow_id,
            adset_id=record.adset_id,
            ad_id=record.ad_id,
            live_capacity_before=live_capacity_before,
        )
    except (CleanupClaimError, TypeError, ValueError) as exc:
        raise CleanupAuthorizationError("cleanup_http_start_not_authorized") from exc


def revoke_delete_authorization(claim_id: str) -> None:
    """Отзывает активную capability claim без возможности повторной выдачи."""
    normalized_claim_id = _required_exact_text(claim_id, "claim_id")
    with _AUTHORIZATION_LOCK:
        token_ids = [
            token_id
            for token_id, record in _AUTHORIZATIONS.items()
            if record.claim_id == normalized_claim_id
        ]
        for token_id in token_ids:
            _AUTHORIZATIONS.pop(token_id, None)
    try:
        revoke_cleanup_delete_authorization(normalized_claim_id)
    except CleanupClaimError as exc:
        raise CleanupAuthorizationError("cleanup_authorization_revoke_failed") from exc
