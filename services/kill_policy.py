"""Чистая policy-модель отключения рекламы.

Модуль не читает settings, не ходит в сеть и не мутирует данные.
Все денежные значения хранятся в Decimal.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Final, TypeVar


class ZeroLeadPolicy(str, Enum):
    """Взаимоисключающая policy нулевых лидов."""

    AGE_3D = "age_3d"
    SPEND_CPL = "spend_cpl"


class RolloutMode(str, Enum):
    """Режим выкатки контура."""

    OFF = "off"
    SHADOW = "shadow"
    ACTIVE = "active"


class EvidenceStatus(str, Enum):
    """Полнота доказательств по рекламе."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class Decision(str, Enum):
    """Детерминированное решение policy."""

    KEEP = "keep"
    PAUSE = "pause"
    WAIT = "wait"


class KillPolicyConfigError(ValueError):
    """Конфигурация или вход policy небезопасны или неоднозначны."""


EnumT = TypeVar("EnumT", bound=Enum)


@dataclass(frozen=True)
class SegmentTarget:
    """План для одного точного segment-ключа."""

    segment: str
    target_cpl: Decimal | None = None
    planned_qualification_rate: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.segment, str) or not self.segment.strip():
            raise KillPolicyConfigError("target.segment должен быть непустой строкой")
        _validate_optional_positive_decimal(self.target_cpl, "target.target_cpl")
        _validate_optional_rate(
            self.planned_qualification_rate,
            "target.planned_qualification_rate",
        )
        if self.target_cpl is None and self.planned_qualification_rate is None:
            raise KillPolicyConfigError("target должен содержать CPL или план квалификации")


@dataclass(frozen=True)
class KillPolicyConfig:
    """Полная конфигурация чистого evaluator."""

    zero_lead_policy: ZeroLeadPolicy = ZeroLeadPolicy.AGE_3D
    contour_a_rollout: RolloutMode = RolloutMode.SHADOW
    spend_cpl_multiplier: Decimal | None = None
    quality_rollout: RolloutMode = RolloutMode.OFF
    quality_min_mature_leads: int | None = None
    approved_maturity_profiles: tuple[str, ...] = ()
    targets: tuple[SegmentTarget, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.zero_lead_policy, ZeroLeadPolicy):
            raise KillPolicyConfigError("zero_lead_policy должен быть ZeroLeadPolicy")
        if not isinstance(self.contour_a_rollout, RolloutMode):
            raise KillPolicyConfigError("contour_a_rollout должен быть RolloutMode")
        if not isinstance(self.quality_rollout, RolloutMode):
            raise KillPolicyConfigError("quality_rollout должен быть RolloutMode")

        _validate_optional_positive_decimal(
            self.spend_cpl_multiplier,
            "spend_cpl_multiplier",
        )
        _validate_optional_positive_int(
            self.quality_min_mature_leads,
            "quality_min_mature_leads",
        )

        if not isinstance(self.targets, tuple) or not all(
            isinstance(target, SegmentTarget) for target in self.targets
        ):
            raise KillPolicyConfigError("targets должен быть tuple[SegmentTarget, ...]")
        segments = [target.segment for target in self.targets]
        if len(segments) != len(set(segments)):
            raise KillPolicyConfigError("дублирующийся exact target для segment")

        if not isinstance(self.approved_maturity_profiles, tuple) or not all(
            isinstance(profile, str) and profile.strip()
            for profile in self.approved_maturity_profiles
        ):
            raise KillPolicyConfigError("approved_maturity_profiles должен быть tuple[str, ...]")
        if len(self.approved_maturity_profiles) != len(set(self.approved_maturity_profiles)):
            raise KillPolicyConfigError("дублирующийся approved maturity profile")

        if (
            self.contour_a_rollout is RolloutMode.ACTIVE
            and self.zero_lead_policy is ZeroLeadPolicy.SPEND_CPL
        ):
            if self.spend_cpl_multiplier is None:
                raise KillPolicyConfigError("active spend-контур требует multiplier")
            if not any(target.target_cpl is not None for target in self.targets):
                raise KillPolicyConfigError("active spend-контур требует exact CPL target")

        if self.quality_rollout is RolloutMode.ACTIVE:
            if self.quality_min_mature_leads is None:
                raise KillPolicyConfigError("active quality-контур требует min sample")
            if not self.approved_maturity_profiles:
                raise KillPolicyConfigError("active quality-контур требует approved profile")
            if not any(
                target.target_cpl is not None
                and target.planned_qualification_rate is not None
                for target in self.targets
            ):
                raise KillPolicyConfigError("active quality-контур требует CPL и qual plan")


@dataclass(frozen=True)
class KillCandidate:
    """Доказательства по одной рекламе в точном segment."""

    segment: str
    lifetime_evidence: EvidenceStatus
    qualification_evidence: EvidenceStatus
    age_days: int | None = None
    lifetime_leads: int | None = None
    lifetime_spend: Decimal | None = None
    mature_leads: int | None = None
    qualified_mature_leads: int | None = None
    maturity_profile: str | None = None
    maturity_event_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.segment, str) or not self.segment.strip():
            raise KillPolicyConfigError("candidate.segment должен быть непустой строкой")
        if not isinstance(self.lifetime_evidence, EvidenceStatus):
            raise KillPolicyConfigError("candidate.lifetime_evidence должен быть EvidenceStatus")
        if not isinstance(self.qualification_evidence, EvidenceStatus):
            raise KillPolicyConfigError(
                "candidate.qualification_evidence должен быть EvidenceStatus"
            )
        _validate_optional_nonnegative_int(self.age_days, "candidate.age_days")
        _validate_optional_nonnegative_int(self.lifetime_leads, "candidate.lifetime_leads")
        _validate_optional_nonnegative_decimal(self.lifetime_spend, "candidate.lifetime_spend")
        _validate_optional_nonnegative_int(self.mature_leads, "candidate.mature_leads")
        _validate_optional_nonnegative_int(
            self.qualified_mature_leads,
            "candidate.qualified_mature_leads",
        )
        if (
            self.mature_leads is not None
            and self.qualified_mature_leads is not None
            and self.qualified_mature_leads > self.mature_leads
        ):
            raise KillPolicyConfigError(
                "candidate.qualified_mature_leads не может превышать mature_leads"
            )
        if self.maturity_profile is not None and (
            not isinstance(self.maturity_profile, str) or not self.maturity_profile.strip()
        ):
            raise KillPolicyConfigError("candidate.maturity_profile должен быть непустой строкой")
        _validate_optional_timestamp(self.maturity_event_at, "candidate.maturity_event_at")


@dataclass(frozen=True)
class ContourEvaluation:
    """Результат одного контура без побочных эффектов."""

    contour: str
    mode: RolloutMode
    decision: Decision
    eligible: bool
    mutation_allowed: bool
    reason: str
    threshold: Decimal | None = None


@dataclass(frozen=True)
class KillPlan:
    """Сводный план: только active-контур может разрешить мутацию."""

    segment: str
    zero_lead_policy: ZeroLeadPolicy
    zero_lead: ContourEvaluation
    quality: ContourEvaluation
    decision: Decision
    mutation_allowed: bool


_CONFIG_KEYS: Final = frozenset(
    {
        "zero_lead_policy",
        "contour_a_rollout",
        "spend_cpl_multiplier",
        "quality_rollout",
        "quality_min_mature_leads",
        "approved_maturity_profiles",
        "targets",
    }
)
_TARGET_KEYS: Final = frozenset(
    {"segment", "target_cpl", "planned_qualification_rate"}
)
_CANDIDATE_KEYS: Final = frozenset(
    {
        "segment",
        "lifetime_evidence",
        "qualification_evidence",
        "age_days",
        "lifetime_leads",
        "lifetime_spend",
        "mature_leads",
        "qualified_mature_leads",
        "maturity_profile",
        "maturity_event_at",
    }
)
_QUALITY_SPEND_MULTIPLIER: Final = Decimal("3")


def parse_kill_policy_config(raw: Mapping[str, object]) -> KillPolicyConfig:
    """Strict-парсер JSON-совместимого mapping без fallback-догадок."""
    data = _require_mapping(raw, "config")
    _reject_unknown_keys(data, _CONFIG_KEYS, "config")

    targets_raw = data.get("targets", [])
    if not isinstance(targets_raw, list):
        raise KillPolicyConfigError("config.targets должен быть JSON-списком")

    targets: list[SegmentTarget] = []
    for index, target_raw in enumerate(targets_raw):
        target_data = _require_mapping(target_raw, f"config.targets[{index}]")
        _reject_unknown_keys(target_data, _TARGET_KEYS, f"config.targets[{index}]")
        segment = target_data.get("segment")
        if not isinstance(segment, str):
            raise KillPolicyConfigError(f"config.targets[{index}].segment должен быть строкой")
        targets.append(
            SegmentTarget(
                segment=segment,
                target_cpl=_parse_optional_decimal(
                    target_data.get("target_cpl"),
                    f"config.targets[{index}].target_cpl",
                ),
                planned_qualification_rate=_parse_optional_decimal(
                    target_data.get("planned_qualification_rate"),
                    f"config.targets[{index}].planned_qualification_rate",
                ),
            )
        )

    profiles_raw = data.get("approved_maturity_profiles", [])
    if not isinstance(profiles_raw, list) or not all(
        isinstance(profile, str) for profile in profiles_raw
    ):
        raise KillPolicyConfigError(
            "config.approved_maturity_profiles должен быть JSON-списком строк"
        )

    return KillPolicyConfig(
        zero_lead_policy=_parse_enum(
            data.get("zero_lead_policy", ZeroLeadPolicy.AGE_3D.value),
            ZeroLeadPolicy,
            "config.zero_lead_policy",
        ),
        contour_a_rollout=_parse_enum(
            data.get("contour_a_rollout", RolloutMode.SHADOW.value),
            RolloutMode,
            "config.contour_a_rollout",
        ),
        spend_cpl_multiplier=_parse_optional_decimal(
            data.get("spend_cpl_multiplier"),
            "config.spend_cpl_multiplier",
        ),
        quality_rollout=_parse_enum(
            data.get("quality_rollout", RolloutMode.OFF.value),
            RolloutMode,
            "config.quality_rollout",
        ),
        quality_min_mature_leads=_parse_optional_int(
            data.get("quality_min_mature_leads"),
            "config.quality_min_mature_leads",
        ),
        approved_maturity_profiles=tuple(profiles_raw),
        targets=tuple(targets),
    )


def parse_kill_candidate(raw: Mapping[str, object]) -> KillCandidate:
    """Strict-парсер кандидата с теми же numeric-гарантиями."""
    data = _require_mapping(raw, "candidate")
    _reject_unknown_keys(data, _CANDIDATE_KEYS, "candidate")

    segment = data.get("segment")
    if not isinstance(segment, str):
        raise KillPolicyConfigError("candidate.segment должен быть строкой")
    for required_field in ("lifetime_evidence", "qualification_evidence"):
        if required_field not in data:
            raise KillPolicyConfigError(f"candidate.{required_field} обязателен")

    return KillCandidate(
        segment=segment,
        lifetime_evidence=_parse_enum(
            data["lifetime_evidence"],
            EvidenceStatus,
            "candidate.lifetime_evidence",
        ),
        qualification_evidence=_parse_enum(
            data["qualification_evidence"],
            EvidenceStatus,
            "candidate.qualification_evidence",
        ),
        age_days=_parse_optional_int(data.get("age_days"), "candidate.age_days"),
        lifetime_leads=_parse_optional_int(
            data.get("lifetime_leads"),
            "candidate.lifetime_leads",
        ),
        lifetime_spend=_parse_optional_decimal(
            data.get("lifetime_spend"),
            "candidate.lifetime_spend",
        ),
        mature_leads=_parse_optional_int(
            data.get("mature_leads"),
            "candidate.mature_leads",
        ),
        qualified_mature_leads=_parse_optional_int(
            data.get("qualified_mature_leads"),
            "candidate.qualified_mature_leads",
        ),
        maturity_profile=data.get("maturity_profile"),
        maturity_event_at=data.get("maturity_event_at"),
    )


def evaluate_kill_policy(config: KillPolicyConfig, candidate: KillCandidate) -> KillPlan:
    """Строит детерминированный план без I/O и мутаций."""
    target = _find_exact_target(config.targets, candidate.segment)
    if config.zero_lead_policy is ZeroLeadPolicy.AGE_3D:
        zero_lead = _evaluate_age3(config, candidate)
    else:
        zero_lead = _evaluate_spend_cpl(config, candidate, target)
    quality = _evaluate_quality(config, candidate, target)

    active_pause = any(
        evaluation.decision is Decision.PAUSE and evaluation.mutation_allowed
        for evaluation in (zero_lead, quality)
    )
    shadow_pause = any(
        evaluation.decision is Decision.PAUSE
        for evaluation in (zero_lead, quality)
    )
    if active_pause:
        decision = Decision.PAUSE
    elif shadow_pause:
        decision = Decision.WAIT
    elif any(evaluation.decision is Decision.KEEP for evaluation in (zero_lead, quality)):
        decision = Decision.KEEP
    else:
        decision = Decision.WAIT

    return KillPlan(
        segment=candidate.segment,
        zero_lead_policy=config.zero_lead_policy,
        zero_lead=zero_lead,
        quality=quality,
        decision=decision,
        mutation_allowed=active_pause,
    )


def _evaluate_age3(
    config: KillPolicyConfig,
    candidate: KillCandidate,
) -> ContourEvaluation:
    mode = config.contour_a_rollout
    if mode is RolloutMode.OFF:
        return _wait("zero_lead_age_3d", mode, "contour_off")
    if candidate.lifetime_evidence is not EvidenceStatus.COMPLETE:
        return _wait("zero_lead_age_3d", mode, "insufficient_lifetime_evidence")
    if candidate.lifetime_leads is None:
        return _wait("zero_lead_age_3d", mode, "insufficient_lifetime_leads")
    if candidate.lifetime_leads > 0:
        return _keep("zero_lead_age_3d", mode, "lifetime_leads_present")
    if candidate.age_days is None:
        return _wait("zero_lead_age_3d", mode, "insufficient_age")
    if candidate.age_days < 3:
        return _wait("zero_lead_age_3d", mode, "age_below_3d")
    return ContourEvaluation(
        contour="zero_lead_age_3d",
        mode=mode,
        decision=Decision.PAUSE,
        eligible=True,
        mutation_allowed=mode is RolloutMode.ACTIVE,
        reason="age_3d_complete_zero_lifetime_leads",
    )


def _evaluate_spend_cpl(
    config: KillPolicyConfig,
    candidate: KillCandidate,
    target: SegmentTarget | None,
) -> ContourEvaluation:
    mode = config.contour_a_rollout
    if mode is RolloutMode.OFF:
        return _wait("zero_lead_spend_cpl", mode, "contour_off")
    if candidate.lifetime_evidence is not EvidenceStatus.COMPLETE:
        return _wait("zero_lead_spend_cpl", mode, "insufficient_lifetime_evidence")
    if candidate.lifetime_leads is None:
        return _wait("zero_lead_spend_cpl", mode, "insufficient_lifetime_leads")
    if candidate.lifetime_leads > 0:
        return _keep("zero_lead_spend_cpl", mode, "lifetime_leads_present")
    if config.spend_cpl_multiplier is None:
        return _wait("zero_lead_spend_cpl", mode, "insufficient_multiplier")
    if target is None or target.target_cpl is None:
        return _wait("zero_lead_spend_cpl", mode, "insufficient_exact_target")
    if candidate.lifetime_spend is None:
        return _wait("zero_lead_spend_cpl", mode, "insufficient_spend")

    threshold = target.target_cpl * config.spend_cpl_multiplier
    if candidate.lifetime_spend < threshold:
        return ContourEvaluation(
            contour="zero_lead_spend_cpl",
            mode=mode,
            decision=Decision.WAIT,
            eligible=False,
            mutation_allowed=False,
            reason="spend_below_threshold",
            threshold=threshold,
        )
    return ContourEvaluation(
        contour="zero_lead_spend_cpl",
        mode=mode,
        decision=Decision.PAUSE,
        eligible=True,
        mutation_allowed=mode is RolloutMode.ACTIVE,
        reason="spend_cpl_threshold_reached",
        threshold=threshold,
    )


def _evaluate_quality(
    config: KillPolicyConfig,
    candidate: KillCandidate,
    target: SegmentTarget | None,
) -> ContourEvaluation:
    mode = config.quality_rollout
    if mode is RolloutMode.OFF:
        return _wait("quality", mode, "contour_off")
    if candidate.qualification_evidence is not EvidenceStatus.COMPLETE:
        return _wait("quality", mode, "insufficient_qualification_evidence")
    if target is None:
        return _wait("quality", mode, "insufficient_exact_target")
    if not config.approved_maturity_profiles:
        return _wait("quality", mode, "insufficient_approved_profiles")
    if candidate.maturity_profile is None:
        return _wait("quality", mode, "insufficient_maturity_profile")
    if candidate.maturity_profile not in config.approved_maturity_profiles:
        return _wait("quality", mode, "unapproved_maturity_profile")
    if candidate.maturity_event_at is None:
        return _wait("quality", mode, "insufficient_maturity_event")
    if config.quality_min_mature_leads is None:
        return _wait("quality", mode, "insufficient_min_sample")
    if candidate.mature_leads is None:
        return _wait("quality", mode, "insufficient_mature_sample")
    if candidate.mature_leads < config.quality_min_mature_leads:
        return _wait("quality", mode, "mature_sample_below_minimum")
    if candidate.qualified_mature_leads is None:
        return _wait("quality", mode, "insufficient_qualified_mature_leads")
    if candidate.qualified_mature_leads > 0:
        return _keep("quality", mode, "qualified_mature_leads_present")
    if target.target_cpl is None:
        return _wait("quality", mode, "insufficient_target_cpl")
    if target.planned_qualification_rate is None:
        return _wait("quality", mode, "insufficient_qualification_plan")
    if candidate.lifetime_spend is None:
        return _wait("quality", mode, "insufficient_spend")

    cpq = target.target_cpl / target.planned_qualification_rate
    threshold = _QUALITY_SPEND_MULTIPLIER * cpq
    if candidate.lifetime_spend < threshold:
        return ContourEvaluation(
            contour="quality",
            mode=mode,
            decision=Decision.WAIT,
            eligible=False,
            mutation_allowed=False,
            reason="spend_below_quality_threshold",
            threshold=threshold,
        )
    return ContourEvaluation(
        contour="quality",
        mode=mode,
        decision=Decision.PAUSE,
        eligible=True,
        mutation_allowed=mode is RolloutMode.ACTIVE,
        reason="quality_spend_threshold_reached_with_zero_qualifications",
        threshold=threshold,
    )


def _find_exact_target(
    targets: tuple[SegmentTarget, ...],
    segment: str,
) -> SegmentTarget | None:
    return next((target for target in targets if target.segment == segment), None)


def _wait(contour: str, mode: RolloutMode, reason: str) -> ContourEvaluation:
    return ContourEvaluation(
        contour=contour,
        mode=mode,
        decision=Decision.WAIT,
        eligible=False,
        mutation_allowed=False,
        reason=reason,
    )


def _keep(contour: str, mode: RolloutMode, reason: str) -> ContourEvaluation:
    return ContourEvaluation(
        contour=contour,
        mode=mode,
        decision=Decision.KEEP,
        eligible=False,
        mutation_allowed=False,
        reason=reason,
    )


def _require_mapping(raw: object, field: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise KillPolicyConfigError(f"{field} должен быть JSON-объектом")
    if not all(isinstance(key, str) for key in raw):
        raise KillPolicyConfigError(f"{field} содержит нестроковый ключ")
    return raw


def _reject_unknown_keys(
    data: Mapping[str, object],
    allowed: frozenset[str],
    field: str,
) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise KillPolicyConfigError(f"{field}: неизвестные ключи {sorted(unknown)}")


def _parse_enum(value: object, enum_type: type[EnumT], field: str) -> EnumT:
    if not isinstance(value, str):
        raise KillPolicyConfigError(f"{field} должен быть строкой")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise KillPolicyConfigError(f"{field}: неизвестное значение {value!r}") from exc


def _parse_optional_decimal(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise KillPolicyConfigError(f"{field} должен быть JSON-числом или numeric-строкой")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise KillPolicyConfigError(f"{field} содержит невалидное число") from exc
    if not parsed.is_finite() or parsed < 0:
        raise KillPolicyConfigError(f"{field} должен быть конечным неотрицательным числом")
    return parsed


def _parse_optional_int(value: object, field: str) -> int | None:
    parsed = _parse_optional_decimal(value, field)
    if parsed is None:
        return None
    if parsed != parsed.to_integral_value():
        raise KillPolicyConfigError(f"{field} должен быть целым числом")
    return int(parsed)


def _validate_optional_positive_decimal(value: Decimal | None, field: str) -> None:
    _validate_optional_nonnegative_decimal(value, field)
    if value is not None and value <= 0:
        raise KillPolicyConfigError(f"{field} должен быть > 0")


def _validate_optional_nonnegative_decimal(value: Decimal | None, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise KillPolicyConfigError(f"{field} должен быть конечным Decimal >= 0")


def _validate_optional_rate(value: Decimal | None, field: str) -> None:
    _validate_optional_nonnegative_decimal(value, field)
    if value is not None and (value <= 0 or value > 1):
        raise KillPolicyConfigError(f"{field} должен быть > 0 и <= 1")


def _validate_optional_positive_int(value: int | None, field: str) -> None:
    _validate_optional_nonnegative_int(value, field)
    if value is not None and value <= 0:
        raise KillPolicyConfigError(f"{field} должен быть > 0")


def _validate_optional_nonnegative_int(value: int | None, field: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise KillPolicyConfigError(f"{field} должен быть int >= 0")


def _validate_optional_timestamp(value: str | None, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise KillPolicyConfigError(f"{field} должен быть непустой строкой")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KillPolicyConfigError(f"{field} должен быть ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise KillPolicyConfigError(f"{field} должен содержать timezone")
