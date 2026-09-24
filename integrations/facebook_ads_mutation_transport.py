"""Единственная attested-граница записей в Facebook Ads Graph API."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Mapping

from agent import database
from agent.fb_common import API, FBApiError, _do_throttled_request, session
from services.fb_token_provider import get_fb_token
from services.owner_action_models import ActionAttemptAttestation
from services.owner_action_repository import OwnerActionRepository

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
# LAUNCH-клеймы несут составной id «launch:<uuid манифеста>:<индекс>» — c
# двоеточиями, которые общий идентификаторный регэксп не пропускает. Подлинность
# клейма держит не формат, а сверка с persisted attempt/permit ниже по коду.
_CLAIM_ID_RE = re.compile(
    r"(?:[A-Za-z0-9_-]{1,100}"
    r"|launch:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:\d{1,4})\Z"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_STATUS_BY_OPERATION = {
    "PAUSE_AD": "PAUSED",
    "UNPAUSE_AD": "ACTIVE",
}
_CREATE_OPERATIONS = frozenset({"CREATE_AD", "RECOVER_AD"})
_BUDGET_OPERATION = "SET_ADSET_BUDGET"


class FacebookMutationError(RuntimeError):
    """Базовая безопасная ошибка typed mutation transport."""


class AttestationRejected(FacebookMutationError):
    """Аттестация не совпала с durable owner-action attempt."""


class MutationRejected(FacebookMutationError):
    """Facebook однозначно отклонил запрос без подтверждённого эффекта."""


class MutationOutcomeUnknown(FacebookMutationError):
    """Результат запроса неоднозначен и требует только live reconciliation."""


class ForbiddenMutation(PermissionError):
    """Операция не имеет owner-approved typed boundary."""


def _access_token() -> str:
    try:
        return get_fb_token()
    except Exception as exc:
        logger.error("Facebook credentials unavailable: %s", type(exc).__name__)
        raise MutationRejected("FACEBOOK_CREDENTIALS_UNAVAILABLE") from None


def _normalize_account_id(account_id: str) -> str:
    value = account_id.removeprefix("act_").strip() if isinstance(account_id, str) else ""
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise AttestationRejected("ATTESTATION_ACCOUNT_INVALID")
    return value


def _require_identifier(value: str, code: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise AttestationRejected(code)
    return value


def _read_persisted_attempt(attestation: ActionAttemptAttestation) -> Mapping[str, object]:
    """Читает exact lineage через repository connection без изменения данных."""

    if database.DB_PATH is None:
        raise AttestationRejected("ATTESTATION_STORE_UNAVAILABLE")
    repository = OwnerActionRepository(database.DB_PATH)
    connection = repository._connect()
    try:
        row = connection.execute(
            """
            SELECT a.attempt_id, a.permit_id, a.proposal_id, a.decision_id,
                   a.claim_id, a.operation_kind, a.account_id, a.resource_id,
                   a.exact_payload_sha256, a.state, a.started_at,
                   p.phase AS permit_phase, p.consumed_at,
                   t.intended_payload_sha256
            FROM owner_action_attempts a
            JOIN owner_technical_permits p
              ON p.permit_id = a.permit_id
             AND p.proposal_id = a.proposal_id
             AND p.decision_id = a.decision_id
             AND p.claim_id = a.claim_id
            JOIN owner_action_proposal_targets t
              ON t.proposal_id = a.proposal_id
             AND t.claim_id = a.claim_id
            WHERE a.attempt_id = ?
            """,
            (attestation.attempt_id,),
        ).fetchone()
    except Exception as exc:
        logger.error("Owner attestation lookup failed: %s", type(exc).__name__)
        raise AttestationRejected("ATTESTATION_STORE_UNAVAILABLE") from None
    finally:
        connection.close()
    if row is None:
        raise AttestationRejected("ATTESTATION_NOT_PERSISTED")
    return dict(row)


def _validate_attestation(
    attestation: ActionAttemptAttestation,
    *,
    operation_kind: str,
    account_id: str,
    resource_id: str,
    payload_sha256: str,
) -> None:
    if type(attestation) is not ActionAttemptAttestation:
        raise AttestationRejected("ATTESTATION_TYPE_INVALID")
    if (
        not isinstance(attestation.claim_id, str)
        or _CLAIM_ID_RE.fullmatch(attestation.claim_id) is None
    ):
        raise AttestationRejected("ATTESTATION_CLAIM_INVALID")
    _normalize_account_id(account_id)
    _normalize_account_id(attestation.account_id)
    exact_resource_id = _require_identifier(resource_id, "ATTESTATION_RESOURCE_INVALID")
    if _SHA256_RE.fullmatch(payload_sha256) is None:
        raise AttestationRejected("ATTESTATION_PAYLOAD_HASH_INVALID")
    if (
        attestation.operation_kind != operation_kind
        or attestation.account_id != account_id
        or attestation.resource_id != exact_resource_id
        or attestation.payload_sha256 != payload_sha256
    ):
        raise AttestationRejected("ATTESTATION_SCOPE_MISMATCH")
    consumed_at = attestation.consumed_at
    if (
        not isinstance(consumed_at, datetime)
        or consumed_at.tzinfo is None
        or consumed_at.utcoffset() is None
    ):
        raise AttestationRejected("ATTESTATION_CONSUMED_AT_INVALID")

    persisted = _read_persisted_attempt(attestation)
    exact = (
        str(persisted["attempt_id"]) == attestation.attempt_id,
        str(persisted["permit_id"]) == attestation.permit_id,
        str(persisted["proposal_id"]) == attestation.proposal_id,
        str(persisted["decision_id"]) == attestation.decision_id,
        str(persisted["claim_id"]) == attestation.claim_id,
        str(persisted["operation_kind"]) == operation_kind,
        str(persisted["account_id"]) == account_id,
        str(persisted["resource_id"]) == exact_resource_id,
        str(persisted["exact_payload_sha256"]) == payload_sha256,
        str(persisted["intended_payload_sha256"]) == payload_sha256,
        str(persisted["state"]) == "ATTEMPT_STARTED",
        str(persisted["permit_phase"]) == "CONSUMED",
        str(persisted["started_at"]) == consumed_at.astimezone(timezone.utc).isoformat(),
        str(persisted["consumed_at"]) == consumed_at.astimezone(timezone.utc).isoformat(),
    )
    if not all(exact):
        raise AttestationRejected("ATTESTATION_DURABLE_LINEAGE_MISMATCH")


def _post(url: str, data: Mapping[str, object]):
    try:
        response = _do_throttled_request(session.post, url, data=dict(data))
    except FBApiError:
        raise MutationOutcomeUnknown("FACEBOOK_MUTATION_OUTCOME_UNKNOWN") from None
    except Exception as exc:
        logger.error("Facebook mutation outcome unknown: %s", type(exc).__name__)
        raise MutationOutcomeUnknown("FACEBOOK_MUTATION_OUTCOME_UNKNOWN") from None
    status_code = getattr(response, "status_code", 0)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        if 200 <= status_code < 300:
            return response
        if 400 <= status_code < 500 and status_code not in {408, 425, 429}:
            raise MutationRejected("FACEBOOK_MUTATION_REJECTED")
    raise MutationOutcomeUnknown("FACEBOOK_MUTATION_OUTCOME_UNKNOWN")


def _create_launch_asset(
    attestation: ActionAttemptAttestation,
    *,
    account_id: str,
    url: str,
    data: Mapping[str, object],
    files: Mapping[str, object] | None = None,
    timeout: object = None,
):
    """Private attested transport для upload/creative шагов LAUNCH."""

    normalized_account_id = _normalize_account_id(account_id)
    _validate_attestation(
        attestation,
        operation_kind=attestation.operation_kind,
        account_id=attestation.account_id,
        resource_id=attestation.resource_id,
        payload_sha256=attestation.payload_sha256,
    )
    if attestation.operation_kind not in _CREATE_OPERATIONS:
        raise ForbiddenMutation("CREATE_OPERATION_FORBIDDEN")
    allowed_prefixes = (
        f"{API}/act_{normalized_account_id}/",
        f"https://graph-video.facebook.com/v21.0/act_{normalized_account_id}/",
    )
    if not url.startswith(allowed_prefixes):
        raise ForbiddenMutation("CREATE_ENDPOINT_FORBIDDEN")
    request_data = dict(data)
    request_data["access_token"] = _access_token()
    kwargs: dict[str, object] = {"data": request_data}
    if files is not None:
        kwargs["files"] = dict(files)
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        response = _do_throttled_request(session.post, url, **kwargs)
    except FBApiError:
        raise MutationOutcomeUnknown("FACEBOOK_MUTATION_OUTCOME_UNKNOWN") from None
    except Exception as exc:
        logger.error("Facebook launch asset outcome unknown: %s", type(exc).__name__)
        raise MutationOutcomeUnknown("FACEBOOK_MUTATION_OUTCOME_UNKNOWN") from None
    status_code = getattr(response, "status_code", 0)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        if 200 <= status_code < 300:
            return response
        if 400 <= status_code < 500 and status_code not in {408, 425, 429}:
            raise MutationRejected("FACEBOOK_MUTATION_REJECTED")
    raise MutationOutcomeUnknown("FACEBOOK_MUTATION_OUTCOME_UNKNOWN")


def set_ad_status(
    attestation: ActionAttemptAttestation,
    *,
    account_id: str,
    ad_id: str,
    status: str,
    payload_sha256: str,
) -> bool:
    """Меняет только PAUSED/ACTIVE для exact approved ad."""

    operation_kind = next(
        (kind for kind, expected in _STATUS_BY_OPERATION.items() if expected == status),
        None,
    )
    if operation_kind is None:
        raise ForbiddenMutation("AD_STATUS_OPERATION_FORBIDDEN")
    _validate_attestation(
        attestation,
        operation_kind=operation_kind,
        account_id=account_id,
        resource_id=ad_id,
        payload_sha256=payload_sha256,
    )
    _post(
        f"{API}/{ad_id}",
        {"access_token": _access_token(), "status": status},
    )
    return True


def set_adset_budget(
    attestation: ActionAttemptAttestation,
    *,
    account_id: str,
    adset_id: str,
    daily_budget_minor_units: int,
    payload_sha256: str,
) -> bool:
    """Меняет только exact daily budget approved adset."""

    if (
        not isinstance(daily_budget_minor_units, int)
        or isinstance(daily_budget_minor_units, bool)
        or daily_budget_minor_units <= 0
    ):
        raise ValueError("daily_budget_minor_units_invalid")
    _validate_attestation(
        attestation,
        operation_kind=_BUDGET_OPERATION,
        account_id=account_id,
        resource_id=adset_id,
        payload_sha256=payload_sha256,
    )
    _post(
        f"{API}/{adset_id}",
        {
            "access_token": _access_token(),
            "daily_budget": daily_budget_minor_units,
        },
    )
    return True


def create_ad(
    attestation: ActionAttemptAttestation,
    *,
    account_id: str,
    adset_id: str,
    name: str,
    creative: Mapping[str, object],
    payload_sha256: str,
    status: str = "ACTIVE",
    url_tags: str | None = None,
) -> str:
    """Создаёт один exact ad; creative/resource creation сюда не входит."""

    if attestation.operation_kind not in _CREATE_OPERATIONS:
        raise ForbiddenMutation("CREATE_OPERATION_FORBIDDEN")
    _validate_attestation(
        attestation,
        operation_kind=attestation.operation_kind,
        account_id=account_id,
        resource_id=adset_id,
        payload_sha256=payload_sha256,
    )
    if not isinstance(name, str) or not name.strip():
        raise ValueError("ad_name_invalid")
    if status != "ACTIVE" or not isinstance(creative, Mapping) or not creative:
        raise ForbiddenMutation("CREATE_PAYLOAD_FORBIDDEN")
    request_data: dict[str, object] = {
        "access_token": _access_token(),
        "name": name,
        "adset_id": adset_id,
        "status": status,
        "creative": json.dumps(dict(creative), ensure_ascii=False),
    }
    if url_tags is not None:
        request_data["url_tags"] = url_tags
    response = _post(f"{API}/act_{_normalize_account_id(account_id)}/ads", request_data)
    try:
        created_id = str(response.json()["id"]).strip()
    except (AttributeError, KeyError, TypeError, ValueError):
        raise MutationOutcomeUnknown("FACEBOOK_CREATE_ID_UNKNOWN") from None
    if _IDENTIFIER_RE.fullmatch(created_id) is None:
        raise MutationOutcomeUnknown("FACEBOOK_CREATE_ID_UNKNOWN")
    return created_id


__all__ = [
    "AttestationRejected",
    "FacebookMutationError",
    "ForbiddenMutation",
    "MutationOutcomeUnknown",
    "MutationRejected",
    "create_ad",
    "set_ad_status",
    "set_adset_budget",
]
