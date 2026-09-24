"""Неизменяемые модели контура персонального одобрения владельцем."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping


class ProposalKind(StrEnum):
    LAUNCH = "LAUNCH"
    PAUSE = "PAUSE"
    UNPAUSE = "UNPAUSE"
    SCALE = "SCALE"
    ASSET_RECOVERY = "ASSET_RECOVERY"


class ProposalOrigin(StrEnum):
    WEB = "WEB"
    CRON = "CRON"
    AUTOPILOT = "AUTOPILOT"
    TELEGRAM_COMMAND = "TELEGRAM_COMMAND"
    RECOVERY = "RECOVERY"


class OwnerDecisionKind(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    POSTPONE = "POSTPONE"


class SystemRedeliveryReason(StrEnum):
    CALLBACK_SECRET_ROTATION = "CALLBACK_SECRET_ROTATION"
    REDELIVERY = "REDELIVERY"


class PermitRetirementReason(StrEnum):
    PERMIT_EXPIRED = "PERMIT_EXPIRED"
    PERMIT_REVOKED = "PERMIT_REVOKED"


class LifecycleState(StrEnum):
    DELIVERY_PENDING = "DELIVERY_PENDING"
    PENDING_OWNER = "PENDING_OWNER"
    POSTPONED = "POSTPONED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTION_QUEUED = "EXECUTION_QUEUED"
    LIVE_REVIEW = "LIVE_REVIEW"
    EXECUTION_RETRY_WAIT = "EXECUTION_RETRY_WAIT"
    PERMIT_ISSUED = "PERMIT_ISSUED"
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    EXECUTED = "EXECUTED"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    BLOCKED_STALE = "BLOCKED_STALE"
    FAILED_NO_EFFECT = "FAILED_NO_EFFECT"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    CANCELLED = "CANCELLED"


_ACTION_KIND_BY_PROPOSAL = {
    ProposalKind.LAUNCH: "CREATE_AD",
    ProposalKind.PAUSE: "PAUSE_AD",
    ProposalKind.UNPAUSE: "UNPAUSE_AD",
    ProposalKind.SCALE: "SET_ADSET_BUDGET",
    ProposalKind.ASSET_RECOVERY: "RECOVER_AD",
}


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} должен быть непустой строкой")


def _require_sha256(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} должен быть lowercase SHA-256")


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} должен содержать timezone")


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Неконечный Decimal нельзя сериализовать")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _canonical_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _canonical_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        _require_aware(value, "datetime")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Ключи canonical JSON должны быть строками")
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Неконечный float нельзя сериализовать")
        return _decimal_text(Decimal(str(value)))
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError(f"Тип {type(value).__name__} нельзя сериализовать canonical JSON")


def canonical_json(value: object) -> bytes:
    """Канонический UTF-8 JSON для всех durable SHA-256."""

    import json

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _freeze_value(value: object) -> object:
    """Глубоко замораживает JSON-подобное значение без изменения смысла."""

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Ключи payload должны быть строками")
        frozen = {key: _freeze_value(item) for key, item in value.items()}
        return MappingProxyType(frozen)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Payload содержит неконечный Decimal")
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Payload содержит неконечный float")
    if isinstance(value, (str, int, float, bool, type(None), datetime, date, Path, Enum)):
        return value
    raise TypeError(f"Payload содержит неподдерживаемый тип {type(value).__name__}")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("Payload должен быть Mapping")
    frozen = _freeze_value(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - защита инварианта helper
        raise TypeError("Payload должен быть Mapping")
    return frozen


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_kind: str
    source_system: str
    subject_id: str
    observed_at: datetime
    complete: bool
    payload: Mapping[str, object]
    payload_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.evidence_kind, "evidence_kind")
        _require_text(self.source_system, "source_system")
        _require_text(self.subject_id, "subject_id")
        _require_aware(self.observed_at, "observed_at")
        if not isinstance(self.complete, bool):
            raise ValueError("complete должен быть bool")
        frozen_payload = _freeze_mapping(self.payload)
        object.__setattr__(self, "payload", frozen_payload)
        _require_sha256(self.payload_sha256, "payload_sha256")
        if canonical_sha256(frozen_payload) != self.payload_sha256:
            raise ValueError("payload_sha256 не совпадает с evidence payload")


@dataclass(frozen=True, slots=True)
class ProposedTarget:
    claim_id: str
    ordinal: int
    action_kind: str
    account_id: str
    adset_id: str | None
    subject_id: str
    city: str | None
    language: Literal["L2", "L1"] | None
    intended_payload: Mapping[str, object]
    intended_payload_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.claim_id, "claim_id")
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise ValueError("ordinal должен быть целым числом >= 0")
        _require_text(self.action_kind, "action_kind")
        _require_text(self.account_id, "account_id")
        if self.adset_id is not None:
            _require_text(self.adset_id, "adset_id")
        _require_text(self.subject_id, "subject_id")
        if self.city is not None:
            _require_text(self.city, "city")
        if self.language not in (None, "L2", "L1"):
            raise ValueError("language должен быть L2, L1 или None")
        frozen_payload = _freeze_mapping(self.intended_payload)
        object.__setattr__(self, "intended_payload", frozen_payload)
        _require_sha256(self.intended_payload_sha256, "intended_payload_sha256")
        if canonical_sha256(frozen_payload) != self.intended_payload_sha256:
            raise ValueError("intended_payload_sha256 не совпадает с payload")


@dataclass(frozen=True, slots=True)
class ProposedActionPlan:
    proposal_kind: ProposalKind
    origin: ProposalOrigin
    idempotency_key: str
    source_ref: str
    actor: str
    summary: str
    targets: tuple[ProposedTarget, ...]
    evidence: tuple[EvidenceRecord, ...]
    config_version_sha256: str
    valid_until: datetime
    staged_media_root: Path | None

    def __post_init__(self) -> None:
        if not isinstance(self.proposal_kind, ProposalKind):
            raise ValueError("proposal_kind должен быть ProposalKind")
        if not isinstance(self.origin, ProposalOrigin):
            raise ValueError("origin должен быть ProposalOrigin")
        _require_text(self.idempotency_key, "idempotency_key")
        _require_text(self.source_ref, "source_ref")
        _require_text(self.actor, "actor")
        _require_text(self.summary, "summary")
        object.__setattr__(self, "targets", tuple(self.targets))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.targets:
            raise ValueError("Предложение должно содержать хотя бы один target")
        expected_ordinals = list(range(len(self.targets)))
        if [target.ordinal for target in self.targets] != expected_ordinals:
            raise ValueError("Target ordinals должны быть непрерывными от нуля")
        claim_ids = [target.claim_id for target in self.targets]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim_id внутри предложения должны быть уникальными")
        expected_action_kind = _ACTION_KIND_BY_PROPOSAL[self.proposal_kind]
        if any(target.action_kind != expected_action_kind for target in self.targets):
            raise ValueError("action_kind target не соответствует proposal_kind")
        _require_sha256(self.config_version_sha256, "config_version_sha256")
        _require_aware(self.valid_until, "valid_until")
        if self.staged_media_root is not None and not isinstance(self.staged_media_root, Path):
            raise ValueError("staged_media_root должен быть Path или None")


@dataclass(frozen=True, slots=True)
class ProposalReceipt:
    proposal_id: str
    idempotency_key: str
    state: Literal["DELIVERY_PENDING", "PENDING_OWNER"]
    proposal_sha256: str
    created_at: datetime
    valid_until: datetime
    deduplicated: bool


@dataclass(frozen=True, slots=True)
class OwnerDecisionResult:
    proposal_id: str
    decision_id: str | None
    decision: OwnerDecisionKind | None
    state: str
    accepted: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class ActionAttemptAttestation:
    attempt_id: str
    permit_id: str
    proposal_id: str
    decision_id: str
    claim_id: str
    operation_kind: str
    account_id: str
    resource_id: str
    payload_sha256: str
    consumed_at: datetime


@dataclass(frozen=True, slots=True)
class ProposalHashes:
    plan_json: str
    plan_sha256: str
    targets_sha256: str
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class ProposalView:
    proposal_id: str
    plan: ProposedActionPlan
    proposal_sha256: str
    plan_sha256: str
    targets_sha256: str
    evidence_sha256: str
    state: str
    lifecycle_version: int
    delivery_generation: int
    active_decision_id: str | None
    active_job_id: str | None
    latest_reason_code: str | None
    next_action_at: datetime | None
    created_at: datetime
    # Момент APPROVE-решения владельца (recorded_at активного decision).
    # None — предложение ещё не одобрено либо view собран запросом без join
    # на решения. Нужен исполнителю: дедлайн исполнения отсчитывается от
    # одобрения, а не от создания карточки.
    approved_at: datetime | None = None

    @property
    def proposal_kind(self) -> ProposalKind:
        return self.plan.proposal_kind

    @property
    def origin(self) -> ProposalOrigin:
        return self.plan.origin

    @property
    def targets(self) -> tuple[ProposedTarget, ...]:
        return self.plan.targets

    @property
    def valid_until(self) -> datetime:
        return self.plan.valid_until


@dataclass(frozen=True, slots=True)
class ProposalPage:
    items: tuple[ProposalView, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TechnicalPermit:
    permit_id: str
    secret: str
    proposal_id: str
    decision_id: str
    job_id: str
    claim_id: str
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(permit_id={self.permit_id!r}, "
            "secret='<redacted>', "
            f"proposal_id={self.proposal_id!r}, decision_id={self.decision_id!r}, "
            f"job_id={self.job_id!r}, claim_id={self.claim_id!r}, "
            f"expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, slots=True)
class PreparedOwnerRedelivery:
    delivery_id: str
    proposal_id: str
    generation: int
    lifecycle_version: int
    state: Literal["PENDING"]


@dataclass(frozen=True, slots=True)
class ClaimProgress:
    proposal_id: str
    decision_id: str
    job_id: str
    state: Literal[
        "READY",
        "IN_FLIGHT",
        "COMPLETE",
        "FAILED_NO_EFFECT",
        "RECONCILE_REQUIRED",
    ]
    next_claim_id: str | None
    next_ordinal: int | None
    completed_claims: int
    total_claims: int
    lifecycle_version: int


@dataclass(frozen=True, slots=True)
class AttemptTransitionResult:
    attempt_id: str
    claim_id: str
    attempt_state: Literal["CONFIRMED", "FAILED_NO_EFFECT", "RECONCILE_REQUIRED"]
    aggregate_state: str
    next_claim_id: str | None
    lifecycle_version: int


def target_document(target: ProposedTarget) -> Mapping[str, object]:
    return {
        "claim_id": target.claim_id,
        "ordinal": target.ordinal,
        "action_kind": target.action_kind,
        "account_id": target.account_id,
        "adset_id": target.adset_id,
        "subject_id": target.subject_id,
        "city": target.city,
        "language": target.language,
        "intended_payload": target.intended_payload,
        "intended_payload_sha256": target.intended_payload_sha256,
    }


def evidence_document(record: EvidenceRecord) -> Mapping[str, object]:
    return {
        "evidence_kind": record.evidence_kind,
        "source_system": record.source_system,
        "subject_id": record.subject_id,
        "observed_at": record.observed_at,
        "complete": record.complete,
        "payload": record.payload,
        "payload_sha256": record.payload_sha256,
    }


def plan_document(plan: ProposedActionPlan) -> Mapping[str, object]:
    return {
        "proposal_kind": plan.proposal_kind,
        "origin": plan.origin,
        "idempotency_key": plan.idempotency_key,
        "source_ref": plan.source_ref,
        "actor": plan.actor,
        "summary": plan.summary,
        "targets": tuple(target_document(target) for target in plan.targets),
        "evidence": tuple(evidence_document(record) for record in plan.evidence),
        "config_version_sha256": plan.config_version_sha256,
        "valid_until": plan.valid_until,
        "staged_media_root": plan.staged_media_root,
    }


def proposal_hashes(plan: ProposedActionPlan) -> ProposalHashes:
    target_rows = tuple(target_document(target) for target in plan.targets)
    evidence_rows = tuple(evidence_document(record) for record in plan.evidence)
    encoded_plan = canonical_json(plan_document(plan))
    return ProposalHashes(
        plan_json=encoded_plan.decode("utf-8"),
        plan_sha256=hashlib.sha256(encoded_plan).hexdigest(),
        targets_sha256=canonical_sha256(target_rows),
        evidence_sha256=canonical_sha256(evidence_rows),
    )
