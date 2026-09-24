from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Mapping, TypeAlias


ClaimValue: TypeAlias = int | Decimal | str | None

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EMPTY_CANONICAL_SEQUENCE_SHA256 = hashlib.sha256(b"[]").hexdigest()


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} должен быть непустой строкой")


def _require_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} должен быть lowercase SHA-256")


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} должен содержать timezone")


def _require_uuid(value: str, field_name: str) -> None:
    _require_text(value, field_name)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field_name} должен быть UUID") from exc
    if str(parsed) != value.lower():
        raise ValueError(f"{field_name} должен быть canonical UUID")


def _require_uuid4(value: str, field_name: str) -> None:
    """Launch attempt допускает только lowercase canonical UUID4."""

    _require_text(value, field_name)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field_name} должен быть UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field_name} должен быть canonical UUID4")


def _require_decimal(value: Decimal, field_name: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or isinstance(value, bool) or not value.is_finite():
        raise ValueError(f"{field_name} должен быть конечным Decimal")
    if positive and value <= 0:
        raise ValueError(f"{field_name} должен быть больше нуля")


def _validate_claim_value(value: ClaimValue, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, str, type(None))):
        raise ValueError(f"{field_name} имеет неподдерживаемый тип")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError(f"{field_name} должен быть конечным Decimal")


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Неконечный Decimal нельзя хешировать")
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
        return value.isoformat()
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
            raise ValueError("Неконечный float нельзя хешировать")
        return _decimal_text(Decimal(str(value)))
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError(f"Тип {type(value).__name__} нельзя сериализовать canonical JSON")


def canonical_json(value: object) -> bytes:
    """Возвращает стабильный UTF-8 JSON без пробелов и неоднозначных чисел."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ReportVerdict(str, Enum):
    VERIFIED = "VERIFIED"
    VERIFIED_WITH_LIMITATIONS = "VERIFIED_WITH_LIMITATIONS"
    BLOCKED = "BLOCKED"
    CHECKER_UNAVAILABLE = "CHECKER_UNAVAILABLE"


class SafetyDecision(str, Enum):
    """Результат автоматической проверки, не являющийся согласием владельца."""

    SAFE = "SAFE"
    DENIED = "DENIED"

    # Совместимость старого sealed WAL: имя не сериализуется как APPROVED и не
    # создаёт owner consent. Новые проверки обязаны использовать SAFE.
    APPROVED = "SAFE"

    @classmethod
    def _missing_(cls, value: object) -> SafetyDecision | None:
        # Старые append-only записи физически содержат APPROVED. При чтении это
        # только технический SAFE verdict, но журнал переписывать нельзя.
        return cls.SAFE if value == "APPROVED" else None


# Старое имя оставлено только для чтения существующего WAL и адаптеров.
ApprovalDecision = SafetyDecision


class ActionResult(str, Enum):
    CONFIRMED = "CONFIRMED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class DeliveryKind(str, Enum):
    REPORT = "REPORT"
    ACTION = "ACTION"
    FACT_FREE = "FACT_FREE"


class DeliveryChannel(str, Enum):
    ADS = "ads"
    HEALTH = "health"


class TransportErrorCode(str, Enum):
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    REMOTE_ERROR = "REMOTE_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    UNKNOWN = "UNKNOWN"


class ShadowBatchEvaluation(str, Enum):
    INCOMPLETE = "INCOMPLETE"


class ShadowItemEvaluation(str, Enum):
    FIRST_ITEM_WOULD_APPROVE = "FIRST_ITEM_WOULD_APPROVE"
    FIRST_ITEM_WOULD_DENY = "FIRST_ITEM_WOULD_DENY"
    NOT_EVALUATED_REQUIRES_LIVE_SEQUENCE = "NOT_EVALUATED_REQUIRES_LIVE_SEQUENCE"


class ActionKind(str, Enum):
    LAUNCH = "LAUNCH"
    ASSET_RECOVERY = "ASSET_RECOVERY"
    PAUSE = "PAUSE"
    UNPAUSE = "UNPAUSE"
    SCALE = "SCALE"


class ActionOrigin(str, Enum):
    AUTO_LAUNCH = "AUTO_LAUNCH"
    WEB = "WEB"
    TELEGRAM = "TELEGRAM"
    AUTOPILOT_CLASSIC = "AUTOPILOT_CLASSIC"
    AUTOPILOT_LIVE = "AUTOPILOT_LIVE"
    AUTOPILOT_MANUAL = "AUTOPILOT_MANUAL"
    LEGACY_SCHEDULER = "LEGACY_SCHEDULER"
    LEGACY_LAUNCHER = "LEGACY_LAUNCHER"
    REPLACEMENT = "REPLACEMENT"
    LAUNCH_RECOVERY = "LAUNCH_RECOVERY"
    ASSET_RECOVERY = "ASSET_RECOVERY"
    BUDGET_SCALER = "BUDGET_SCALER"


class SubjectKind(str, Enum):
    ACCOUNT = "ACCOUNT"
    CARD = "CARD"
    MEDIA = "MEDIA"
    ADSET = "ADSET"
    AD = "AD"
    PAYMENT = "PAYMENT"
    CHECKER = "CHECKER"
    DECISION = "DECISION"
    FEEDBACK = "FEEDBACK"
    CREATIVE = "CREATIVE"
    PRODUCT = "PRODUCT"
    PATTERN = "PATTERN"
    HYPOTHESIS = "HYPOTHESIS"
    DAILY_METRIC = "DAILY_METRIC"
    BRIEF = "BRIEF"
    RUNTIME = "RUNTIME"
    MONITOR = "MONITOR"
    CRON = "CRON"


class FactCategory(str, Enum):
    BUSINESS_METRIC = "BUSINESS_METRIC"
    ACTION_STATE = "ACTION_STATE"
    CHECKER_HEALTH = "CHECKER_HEALTH"
    HISTORY_AUDIT = "HISTORY_AUDIT"
    DISPLAY_CONTEXT = "DISPLAY_CONTEXT"
    MATCH = "MATCH"
    WINDOW_BOUND = "WINDOW_BOUND"
    THRESHOLD = "THRESHOLD"


class Metric(str, Enum):
    EFFECTIVE_STATUS = "EFFECTIVE_STATUS"
    CONFIGURED_STATUS = "CONFIGURED_STATUS"
    SPEND = "SPEND"
    LEADS = "LEADS"
    QUALS = "QUALS"
    PAYMENTS = "PAYMENTS"
    REVENUE = "REVENUE"
    CPL = "CPL"
    CPQL = "CPQL"
    QUAL_PCT = "QUAL_PCT"
    ROMI_PCT = "ROMI_PCT"
    DAILY_BUDGET = "DAILY_BUDGET"
    CAPACITY = "CAPACITY"
    ACTION_STATE = "ACTION_STATE"
    ACTION_COUNT = "ACTION_COUNT"
    CHECKER_HEALTH = "CHECKER_HEALTH"
    HISTORY_STATE = "HISTORY_STATE"
    DISPLAY_CONTEXT = "DISPLAY_CONTEXT"
    MATCH_STATE = "MATCH_STATE"
    WINDOW_START = "WINDOW_START"
    WINDOW_END = "WINDOW_END"
    THRESHOLD = "THRESHOLD"
    DRR_PCT = "DRR_PCT"
    AGREEMENT_PCT = "AGREEMENT_PCT"
    ACCURACY_PCT = "ACCURACY_PCT"
    PRODUCT_SHARE_PCT = "PRODUCT_SHARE_PCT"
    DECISION_COUNT = "DECISION_COUNT"
    FEEDBACK_COUNT = "FEEDBACK_COUNT"
    PATTERN_COUNT = "PATTERN_COUNT"
    HYPOTHESIS_COUNT = "HYPOTHESIS_COUNT"
    CPL_RATIO = "CPL_RATIO"
    ERROR_COUNT = "ERROR_COUNT"
    RUNTIME_COUNT = "RUNTIME_COUNT"
    RECORD_STATUS = "RECORD_STATUS"


class SourceSystem(str, Enum):
    FACEBOOK = "FACEBOOK"
    AMO = "AMO"
    CDP_ERP = "CDP_ERP"
    TRELLO = "TRELLO"
    MEDIA_BYTES = "MEDIA_BYTES"
    CHECKER_AUDIT = "CHECKER_AUDIT"
    CHECKER_RUNTIME = "CHECKER_RUNTIME"
    DECISIONS_DB = "DECISIONS_DB"
    CREATIVE_KB = "CREATIVE_KB"
    AUTOPILOT_FEEDBACK = "AUTOPILOT_FEEDBACK"
    PATTERN_LEARNINGS = "PATTERN_LEARNINGS"
    HYPOTHESIS_JOURNAL = "HYPOTHESIS_JOURNAL"
    AD_DAILY_METRICS = "AD_DAILY_METRICS"
    METRICS_SNAPSHOT_STATE = "METRICS_SNAPSHOT_STATE"
    PENDING_BRIEFS = "PENDING_BRIEFS"
    SETTINGS_FILE = "SETTINGS_FILE"
    FB_ERROR_RING = "FB_ERROR_RING"
    NOTIFICATION_RUNTIME = "NOTIFICATION_RUNTIME"
    GUARDIAN_STATE = "GUARDIAN_STATE"
    BRIEF_GENERATOR_STATE = "BRIEF_GENERATOR_STATE"
    ANOMALY_ALERT_STATE = "ANOMALY_ALERT_STATE"
    EXPIRED_OFFER_STATE = "EXPIRED_OFFER_STATE"
    COVERAGE_STATE = "COVERAGE_STATE"
    ADS_WATCHDOG_STATE = "ADS_WATCHDOG_STATE"
    ADSET_SPEND_GUARD_STATE = "ADSET_SPEND_GUARD_STATE"
    CDP_SPEND_ALERT_STATE = "CDP_SPEND_ALERT_STATE"
    CRON_HEARTBEATS = "CRON_HEARTBEATS"
    CRON_FAILURE_STATE = "CRON_FAILURE_STATE"


class EvidenceState(str, Enum):
    FRESH_COMPLETE = "FRESH_COMPLETE"
    MISSING = "MISSING"
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"
    ERROR = "ERROR"


class ClaimState(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


class ReportTemplate(str, Enum):
    AUTOPILOT = "AUTOPILOT"
    LAUNCH = "LAUNCH"
    SCALE = "SCALE"
    EVENING = "EVENING"
    MORNING = "MORNING"
    STATUS = "STATUS"
    ADS = "ADS"
    ACTION_RESULT = "ACTION_RESULT"
    ONLINE = "ONLINE"
    WEEKLY_LEARNING = "WEEKLY_LEARNING"
    SCORECARD = "SCORECARD"
    SPEND_ALERT = "SPEND_ALERT"
    COVERAGE = "COVERAGE"
    GUARDIAN = "GUARDIAN"
    ANOMALY = "ANOMALY"
    CLEANER = "CLEANER"
    BRIEF = "BRIEF"
    HEALTH = "HEALTH"


class SectionTemplate(str, Enum):
    SUMMARY = "SUMMARY"
    FACEBOOK = "FACEBOOK"
    OUTCOMES = "OUTCOMES"
    ACTIONS = "ACTIONS"
    MATCHES = "MATCHES"
    LIMITATIONS = "LIMITATIONS"


class FieldLabelTemplate(str, Enum):
    DISPLAY_NAME = "DISPLAY_NAME"
    MATCH = "MATCH"
    STATUS = "STATUS"
    SPEND = "SPEND"
    LEADS = "LEADS"
    QUALS = "QUALS"
    PAYMENTS = "PAYMENTS"
    REVENUE = "REVENUE"
    CPL = "CPL"
    ROMI = "ROMI"
    BUDGET = "BUDGET"
    CAPACITY = "CAPACITY"
    ACTION_STATE = "ACTION_STATE"
    ACTION_COUNT = "ACTION_COUNT"
    CHECKER_HEALTH = "CHECKER_HEALTH"
    HISTORY_STATE = "HISTORY_STATE"
    WINDOW_START = "WINDOW_START"
    WINDOW_END = "WINDOW_END"
    THRESHOLD = "THRESHOLD"
    DRR = "DRR"
    AGREEMENT = "AGREEMENT"
    ACCURACY = "ACCURACY"
    PRODUCT_SHARE = "PRODUCT_SHARE"


class FieldFormat(str, Enum):
    TEXT = "TEXT"
    INTEGER = "INTEGER"
    USD = "USD"
    LCY = "LCY"
    PERCENT = "PERCENT"
    STATUS = "STATUS"
    DATETIME = "DATETIME"


class MediaType(str, Enum):
    VIDEO = "VIDEO"
    IMAGE = "IMAGE"
    CAROUSEL_ITEM = "CAROUSEL_ITEM"
    PLACEMENT_PAIR = "PLACEMENT_PAIR"


class PlacementRole(str, Enum):
    DEFAULT = "DEFAULT"
    FEED = "FEED"
    STORY = "STORY"


class OperationState(str, Enum):
    RESERVED = "RESERVED"
    DENIED = "DENIED"
    APPROVED = "APPROVED"
    SHADOW_FIRST_ITEM_ONLY = "SHADOW_FIRST_ITEM_ONLY"
    EXECUTING = "EXECUTING"
    CONFIRMED = "CONFIRMED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class FactFreeTemplate(str, Enum):
    ACTION_CHECK_STARTED = "ACTION_CHECK_STARTED"
    REPORT_BUILD_STARTED = "REPORT_BUILD_STARTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    CRON_CRASHED = "CRON_CRASHED"
    BACKUP_FAILED = "BACKUP_FAILED"
    CHECKER_INTERNAL_ERROR = "CHECKER_INTERNAL_ERROR"
    TRANSPORT_FAILED = "TRANSPORT_FAILED"


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: datetime
    end: datetime
    timezone_name: str
    semantic: str

    def __post_init__(self) -> None:
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        if self.start >= self.end:
            raise ValueError("TimeWindow должен быть непустым полуинтервалом [start,end)")
        _require_text(self.timezone_name, "timezone_name")
        _require_text(self.semantic, "semantic")


@dataclass(frozen=True, slots=True)
class SubjectRef:
    kind: SubjectKind
    subject_id: str
    parent_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SubjectKind):
            raise ValueError("kind должен быть SubjectKind")
        _require_text(self.subject_id, "subject_id")
        if self.parent_id is not None:
            _require_text(self.parent_id, "parent_id")


@dataclass(frozen=True, slots=True)
class FactClaim:
    claim_id: str
    field_id: str | None
    category: FactCategory
    subject: SubjectRef
    metric: Metric
    value: ClaimValue
    source: SourceSystem
    window: TimeWindow | None
    currency: str | None = None
    required: bool = True
    tolerance: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not isinstance(self.category, FactCategory):
            raise ValueError("category должен быть FactCategory")
        if not isinstance(self.metric, Metric):
            raise ValueError("metric должен быть Metric")
        if not isinstance(self.source, SourceSystem):
            raise ValueError("source должен быть SourceSystem")
        _require_text(self.claim_id, "claim_id")
        if self.field_id is not None:
            _require_text(self.field_id, "field_id")
        _validate_claim_value(self.value, "value")
        if not isinstance(self.required, bool):
            raise ValueError("required должен быть bool")
        _require_decimal(self.tolerance, "tolerance")
        if self.tolerance < 0:
            raise ValueError("tolerance не может быть отрицательным")


@dataclass(frozen=True, slots=True)
class ReportField:
    field_id: str
    section_id: str
    category: FactCategory
    label: FieldLabelTemplate
    format: FieldFormat
    subject: SubjectRef
    metric: Metric
    value: ClaimValue
    source: SourceSystem
    window: TimeWindow | None
    currency: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.category, FactCategory):
            raise ValueError("category должен быть FactCategory")
        if not isinstance(self.label, FieldLabelTemplate):
            raise ValueError("label должен быть FieldLabelTemplate")
        if not isinstance(self.format, FieldFormat):
            raise ValueError("format должен быть FieldFormat")
        if not isinstance(self.metric, Metric):
            raise ValueError("metric должен быть Metric")
        if not isinstance(self.source, SourceSystem):
            raise ValueError("source должен быть SourceSystem")
        _require_text(self.field_id, "field_id")
        _require_text(self.section_id, "section_id")
        _validate_claim_value(self.value, "value")
        if not isinstance(self.required, bool):
            raise ValueError("required должен быть bool")


@dataclass(frozen=True, slots=True)
class ReportSection:
    section_id: str
    template: SectionTemplate
    field_ids: tuple[str, ...]
    window: TimeWindow | None

    def __post_init__(self) -> None:
        if not isinstance(self.template, SectionTemplate):
            raise ValueError("template должен быть SectionTemplate")
        _require_text(self.section_id, "section_id")
        if len(self.field_ids) != len(set(self.field_ids)):
            raise ValueError("field_ids внутри section должны быть уникальными")
        for field_id in self.field_ids:
            _require_text(field_id, "field_id")


@dataclass(frozen=True, slots=True)
class TypedReportPayload:
    template: ReportTemplate
    sections: tuple[ReportSection, ...]
    fields: tuple[ReportField, ...]
    generated_at: datetime
    mixed_windows_explicit: bool

    def __post_init__(self) -> None:
        if not isinstance(self.template, ReportTemplate):
            raise ValueError("template должен быть ReportTemplate")
        _require_aware(self.generated_at, "generated_at")
        section_ids = [section.section_id for section in self.sections]
        field_ids = [item.field_id for item in self.fields]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("section_id должны быть уникальными")
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("field_id должны быть уникальными")
        declared = [field_id for section in self.sections for field_id in section.field_ids]
        if len(declared) != len(set(declared)) or set(declared) != set(field_ids):
            raise ValueError("Каждое поле должно входить ровно в одну section")
        by_id = {item.field_id: item for item in self.fields}
        if any(by_id[field_id].section_id != section.section_id for section in self.sections for field_id in section.field_ids):
            raise ValueError("ReportField ссылается на другую section")
        if not isinstance(self.mixed_windows_explicit, bool):
            raise ValueError("mixed_windows_explicit должен быть bool")


@dataclass(frozen=True, slots=True)
class ReportCheckRequest:
    correlation_id: str
    payload: TypedReportPayload
    claims: tuple[FactClaim, ...]
    manifest_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.correlation_id, "correlation_id")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim_id должны быть уникальными")
        fields_by_id = {field.field_id: field for field in self.payload.fields}
        claims_by_field = {claim.field_id: claim for claim in self.claims if claim.field_id is not None}
        if len(claims_by_field) != len(self.claims) or set(claims_by_field) != set(fields_by_id):
            raise ValueError("Report fields и claims должны образовывать точную биекцию")
        for field_id, field in fields_by_id.items():
            claim = claims_by_field[field_id]
            if (
                claim.category != field.category
                or claim.subject != field.subject
                or claim.metric != field.metric
                or claim.value != field.value
                or claim.source != field.source
                or claim.window != field.window
                or claim.currency != field.currency
                or claim.required != field.required
            ):
                raise ValueError(f"Claim не совпадает с report field {field_id}")
        if self.manifest_sha256 != report_manifest_sha256(self.payload, self.claims):
            raise ValueError("manifest_sha256 не совпадает с report payload/claims")


@dataclass(frozen=True, slots=True)
class CdpResponse:
    payload: object
    status_code: int
    fetched_at: datetime
    data_as_of: datetime | None
    from_cache: bool
    cache_age_seconds: float
    response_headers_sha256: str


@dataclass(frozen=True, slots=True)
class PaginationCoverage:
    endpoint_kind: str
    page_count: int
    item_ids: tuple[str, ...]
    pagination_complete: bool
    failed_page_index: int | None
    response_state_sha256: str


@dataclass(frozen=True, slots=True)
class CampaignChunkCoverage:
    chunk_index: int
    campaign_ids: tuple[str, ...]
    pagination: PaginationCoverage
    returned_ad_ids: tuple[str, ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class DailyInventorySnapshot:
    account_id: str
    account_status: int
    currency: str
    timezone_name: str
    target_window: TimeWindow
    fetched_at: datetime
    max_age_seconds: int
    ads_pagination: PaginationCoverage
    accessible_ad_ids: tuple[str, ...]
    eligible_ad_ids: tuple[str, ...]
    eligible_campaign_ids: tuple[str, ...]
    exact_lookup_ad_ids: tuple[str, ...]
    created_time_by_ad_sha256: str
    status_by_ad_sha256: str
    campaign_by_ad_sha256: str
    cache_missing_ad_ids: tuple[str, ...]
    cache_mismatched_ad_ids: tuple[str, ...]
    fresh: bool
    complete: bool

    def __post_init__(self) -> None:
        _require_text(self.account_id, "account_id")
        _require_text(self.currency, "currency")
        _require_text(self.timezone_name, "timezone_name")
        _require_aware(self.fetched_at, "fetched_at")
        if isinstance(self.max_age_seconds, bool) or self.max_age_seconds <= 0:
            raise ValueError("max_age_seconds должен быть положительным int")
        if self.eligible_campaign_ids != tuple(sorted(set(self.eligible_campaign_ids))):
            raise ValueError("eligible_campaign_ids должны быть sorted и unique")
        for campaign_id in self.eligible_campaign_ids:
            _require_text(campaign_id, "eligible_campaign_id")
        for field_name in (
            "created_time_by_ad_sha256",
            "status_by_ad_sha256",
            "campaign_by_ad_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class DailyMetricsSnapshotResult:
    target_date: date
    inventory: DailyInventorySnapshot
    fetch_path: str
    account_insights: PaginationCoverage | None
    campaign_pagination: PaginationCoverage | None
    campaign_chunks: tuple[CampaignChunkCoverage, ...]
    requested_ad_ids: tuple[str, ...]
    insights_returned_ad_ids: tuple[str, ...]
    verified_zero_ad_ids: tuple[str, ...]
    fetched_ad_ids: tuple[str, ...]
    upserted_ad_ids: tuple[str, ...]
    started_at: datetime
    completed_at: datetime
    complete: bool
    incomplete_reason_codes: tuple[str, ...]
    state_sha256: str


@dataclass(frozen=True, slots=True)
class DailyMetricsRunManifest:
    result: DailyMetricsSnapshotResult
    persisted_at: datetime
    db_rows_state_sha256: str


@dataclass(frozen=True, slots=True)
class PendingBriefRecord:
    brief_id: str
    name: str
    desc: str
    product: str | None
    signature: str
    status: str
    created_at: datetime
    decided_at: datetime | None
    card_id: str | None
    card_url: str | None
    record_sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeBufferSnapshot:
    source_instance_id: str
    observed_at: datetime
    window: TimeWindow
    event_timestamps: tuple[datetime, ...]
    count_in_window: int
    buffer_size: int
    capacity: int
    truncated_in_window: bool
    complete: bool


@dataclass(frozen=True, slots=True)
class StableJsonSnapshot:
    source: SourceSystem
    absolute_path: Path
    fetched_at: datetime
    content_sha256: str
    schema_sha256: str
    size_bytes: int
    inode: int
    complete: bool
    high_watermark: str | None


@dataclass(frozen=True, slots=True)
class TrelloPrecondition:
    card_id: str
    board_id: str
    ready_list_id: str
    expected_list_id: str
    expected_due_complete: bool
    expected_closed: bool
    date_last_activity: datetime
    attachment_ids: tuple[str, ...]
    attachment_manifest_sha256: str
    labels_sha256: str
    card_content_sha256: str


@dataclass(frozen=True, slots=True)
class MediaAssetSpec:
    asset_id: str
    order_index: int
    media_type: MediaType
    placement_group_id: str | None
    placement_role: PlacementRole
    staged_relative_path: str
    original_attachment_id: str
    mime_type: str
    size_bytes: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class CreativeSpec:
    creative_id: str
    order_index: int
    ad_name: str
    media_asset_ids: tuple[str, ...]
    body_staged_relative_path: str
    body_sha256: str
    product: str
    page_id: str
    lead_form_id: str | None
    call_to_action: str
    instagram_actor_id: str
    title: str
    link_url: str | None
    expected_configured_status: str


@dataclass(frozen=True, slots=True)
class LaunchDestination:
    city: str
    account_id: str
    adset_id: str
    adset_type: str
    current_daily_budget: Decimal
    currency: str
    capacity_available: int
    hard_reserve_slots: int
    creatives: tuple[CreativeSpec, ...]
    duplicate_signature: str
    replacement_workflow_id: str | None = None


@dataclass(frozen=True, slots=True)
class LaunchManifest:
    kind: ActionKind
    manifest_id: str
    origin: ActionOrigin
    idempotency_key: str
    prepared_at: datetime
    config_version_sha256: str
    staging_root: str
    staging_directory: str
    trello: TrelloPrecondition
    card_name_sha256: str
    media_manifest_sha256: str
    media_assets: tuple[MediaAssetSpec, ...]
    campaign_type: str
    destinations: tuple[LaunchDestination, ...]

    def __post_init__(self) -> None:
        if self.kind is not ActionKind.LAUNCH:
            raise ValueError("LaunchManifest.kind должен быть LAUNCH")
        _validate_action_common(self.manifest_id, self.idempotency_key, self.prepared_at)
        _require_uuid4(self.idempotency_key, "idempotency_key")
        for field_name in ("config_version_sha256", "card_name_sha256", "media_manifest_sha256"):
            _require_sha256(getattr(self, field_name), field_name)
        if not 1 <= len(self.destinations) <= 10:
            raise ValueError("LAUNCH должен содержать 1..10 destinations")
        if not self.media_assets:
            raise ValueError("LAUNCH должен содержать media assets")
        asset_ids = [asset.asset_id for asset in self.media_assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("media asset IDs должны быть уникальными")
        destination_keys = [(item.account_id, item.adset_id, item.city) for item in self.destinations]
        if len(destination_keys) != len(set(destination_keys)):
            raise ValueError("Launch destinations должны быть уникальными")
        for asset in self.media_assets:
            _require_text(asset.asset_id, "asset_id")
            if asset.size_bytes <= 0 or isinstance(asset.size_bytes, bool):
                raise ValueError("media size_bytes должен быть положительным")
            _require_sha256(asset.content_sha256, "content_sha256")
            _require_safe_relative_path(asset.staged_relative_path)
        for destination in self.destinations:
            _require_decimal(destination.current_daily_budget, "current_daily_budget", positive=True)
            if isinstance(destination.capacity_available, bool) or destination.capacity_available < 0:
                raise ValueError("capacity_available должен быть целым неотрицательным")
            if isinstance(destination.hard_reserve_slots, bool) or destination.hard_reserve_slots < 0:
                raise ValueError("hard_reserve_slots должен быть целым неотрицательным")
            if destination.replacement_workflow_id is not None:
                _require_text(
                    destination.replacement_workflow_id,
                    "replacement_workflow_id",
                )


@dataclass(frozen=True, slots=True)
class AssetRecoveryManifest:
    """Один exact CREATE из уже существующего Facebook creative."""

    kind: ActionKind
    manifest_id: str
    origin: ActionOrigin
    idempotency_key: str
    prepared_at: datetime
    account_kind: str
    account_id: str
    campaign_type: str
    source: str
    city: str
    source_ad_id: str
    source_adset_id: str
    source_adset_name: str
    adset_type: str
    source_ad_name: str
    source_creative_id: str
    source_identity_sha256: str
    target_adset_id: str
    target_adset_name: str
    target_ad_name: str
    target_identity_key: str
    pre_inventory_sha256: str
    capacity_available: int
    hard_reserve_slots: int

    def __post_init__(self) -> None:
        if self.kind is not ActionKind.ASSET_RECOVERY:
            raise ValueError("AssetRecoveryManifest.kind должен быть ASSET_RECOVERY")
        _validate_action_common(self.manifest_id, self.idempotency_key, self.prepared_at)
        _require_uuid4(self.idempotency_key, "idempotency_key")
        if self.origin is not ActionOrigin.ASSET_RECOVERY:
            raise ValueError("asset recovery origin должен быть ASSET_RECOVERY")
        if self.account_kind != "offline":
            raise ValueError("asset recovery разрешён только для offline account")
        if self.campaign_type != "asset_recovery" or self.source != "RECOVERY":
            raise ValueError("asset recovery campaign/source contract неверен")
        for field_name in (
            "account_id",
            "source_ad_id",
            "source_adset_id",
            "source_creative_id",
            "target_adset_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.isdigit():
                raise ValueError(f"{field_name} должен быть numeric Meta ID")
        for field_name in (
            "city",
            "source_adset_name",
            "source_ad_name",
            "target_adset_name",
            "target_ad_name",
            "target_identity_key",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.adset_type not in {"L2", "L1"}:
            raise ValueError("adset_type должен быть L2 или L1")
        for field_name in ("source_identity_sha256", "pre_inventory_sha256"):
            _require_sha256(getattr(self, field_name), field_name)
        if isinstance(self.capacity_available, bool) or self.capacity_available < 0:
            raise ValueError("capacity_available должен быть целым неотрицательным")
        if isinstance(self.hard_reserve_slots, bool) or self.hard_reserve_slots < 0:
            raise ValueError("hard_reserve_slots должен быть целым неотрицательным")
        if self.capacity_available < 1 + self.hard_reserve_slots:
            raise ValueError("capacity не покрывает CREATE и обязательный резерв")
        from services.product_tags import PRODUCTS

        normalized_target = unicodedata.normalize("NFKC", self.target_ad_name)
        tag_names = "|".join(
            re.escape(str(metadata["tag"])[1:-1])
            for metadata in PRODUCTS.values()
        )
        normalized_target = re.sub(
            rf"\s*\[(?:{tag_names})\]\s*$",
            "",
            normalized_target,
            flags=re.IGNORECASE,
        )
        normalized_target = " ".join(normalized_target.split()).casefold()
        if self.target_identity_key != normalized_target:
            raise ValueError("target_identity_key не совпадает с exact target name")
        source_parts = self.source_ad_name.split(" | ", 1)
        target_parts = self.target_ad_name.split(" | ", 1)
        if (
            len(source_parts) != 2
            or len(target_parts) != 2
            or " ".join(source_parts[1].split()).casefold()
            != " ".join(target_parts[1].split()).casefold()
        ):
            raise ValueError("source и target должны иметь одну creative identity")


@dataclass(frozen=True, slots=True)
class PreparedLaunch:
    manifest_id: str
    staged_at: datetime
    staging_root: Path
    staging_directory: Path
    trello: TrelloPrecondition
    card_name: str
    card_name_sha256: str
    campaign_type: str
    config_version_sha256: str
    media_assets: tuple[MediaAssetSpec, ...]
    destinations: tuple[LaunchDestination, ...]
    media_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class AdStatusSnapshot:
    """Immutable exact provider-состояние одного объявления в adset."""

    ad_id: str
    adset_id: str
    configured_status: str
    effective_status: str

    def __post_init__(self) -> None:
        for field_name in (
            "ad_id",
            "adset_id",
            "configured_status",
            "effective_status",
        ):
            _require_text(getattr(self, field_name), field_name)


def _validate_sibling_snapshot(
    target_ad_id: str,
    adset_id: str,
    expected_sha256: str,
    snapshot: tuple[AdStatusSnapshot, ...],
) -> None:
    """Проверяет полный target-excluded baseline и его canonical digest."""

    _require_sha256(expected_sha256, "pre_unrelated_inventory_sha256")
    if not isinstance(snapshot, tuple):
        raise ValueError("sibling_status_snapshot должен быть tuple")
    sibling_ids = tuple(item.ad_id for item in snapshot)
    if sibling_ids != tuple(sorted(sibling_ids)) or len(sibling_ids) != len(set(sibling_ids)):
        raise ValueError("sibling_status_snapshot должен быть уникальным и отсортированным")
    if target_ad_id in sibling_ids:
        raise ValueError("sibling_status_snapshot не должен содержать target ad")
    if any(item.adset_id != adset_id for item in snapshot):
        raise ValueError("sibling_status_snapshot содержит другой adset")
    actual_sha256 = hashlib.sha256(canonical_json(snapshot)).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError("pre_unrelated_inventory_sha256 не совпадает со snapshot")


@dataclass(frozen=True, slots=True)
class PauseManifest:
    kind: ActionKind
    manifest_id: str
    origin: ActionOrigin
    idempotency_key: str
    prepared_at: datetime
    ad_id: str
    adset_id: str
    reason_code: str
    expected_before_status: str
    expected_after_status: str
    decision_window: TimeWindow
    facts: tuple[FactClaim, ...]
    pre_inventory_sha256: str
    sibling_active_ids: tuple[str, ...]
    pre_unrelated_inventory_sha256: str = _EMPTY_CANONICAL_SEQUENCE_SHA256
    sibling_status_snapshot: tuple[AdStatusSnapshot, ...] = ()
    replacement_ad_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind is not ActionKind.PAUSE:
            raise ValueError("PauseManifest.kind должен быть PAUSE")
        _validate_action_common(self.manifest_id, self.idempotency_key, self.prepared_at)
        if self.expected_before_status != "ACTIVE" or self.expected_after_status != "PAUSED":
            raise ValueError("PAUSE разрешает только переход ACTIVE -> PAUSED")
        _require_text(self.ad_id, "ad_id")
        _require_text(self.adset_id, "adset_id")
        _require_text(self.reason_code, "reason_code")
        _require_sha256(self.pre_inventory_sha256, "pre_inventory_sha256")
        _validate_sibling_snapshot(
            self.ad_id,
            self.adset_id,
            self.pre_unrelated_inventory_sha256,
            self.sibling_status_snapshot,
        )
        sources = {claim.source for claim in self.facts}
        if not {SourceSystem.FACEBOOK, SourceSystem.AMO, SourceSystem.CDP_ERP}.issubset(sources):
            raise ValueError("PAUSE facts должны включать Facebook, AMO и CDP")


@dataclass(frozen=True, slots=True)
class UnpauseManifest:
    kind: ActionKind
    manifest_id: str
    origin: ActionOrigin
    idempotency_key: str
    prepared_at: datetime
    ad_id: str
    adset_id: str
    expected_before_status: str
    expected_after_status: str
    pre_inventory_sha256: str
    pre_unrelated_inventory_sha256: str = _EMPTY_CANONICAL_SEQUENCE_SHA256
    sibling_status_snapshot: tuple[AdStatusSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if self.kind is not ActionKind.UNPAUSE:
            raise ValueError("UnpauseManifest.kind должен быть UNPAUSE")
        _validate_action_common(self.manifest_id, self.idempotency_key, self.prepared_at)
        if self.expected_before_status == "ACTIVE" or self.expected_after_status != "ACTIVE":
            raise ValueError("UNPAUSE разрешает только переход non-ACTIVE -> ACTIVE")
        _require_text(self.ad_id, "ad_id")
        _require_text(self.adset_id, "adset_id")
        _require_sha256(self.pre_inventory_sha256, "pre_inventory_sha256")
        _validate_sibling_snapshot(
            self.ad_id,
            self.adset_id,
            self.pre_unrelated_inventory_sha256,
            self.sibling_status_snapshot,
        )


@dataclass(frozen=True, slots=True)
class ScaleManifest:
    kind: ActionKind
    manifest_id: str
    origin: ActionOrigin
    idempotency_key: str
    prepared_at: datetime
    adset_id: str
    expected_status: str
    current_budget: Decimal
    target_budget: Decimal
    currency: str
    candidate_ad_ids: tuple[str, ...]
    facebook_window: TimeWindow
    outcome_window: TimeWindow
    facts: tuple[FactClaim, ...]

    def __post_init__(self) -> None:
        if self.kind is not ActionKind.SCALE:
            raise ValueError("ScaleManifest.kind должен быть SCALE")
        _validate_action_common(self.manifest_id, self.idempotency_key, self.prepared_at)
        if self.expected_status != "ACTIVE":
            raise ValueError("SCALE разрешён только для ACTIVE adset")
        _require_decimal(self.current_budget, "current_budget", positive=True)
        _require_decimal(self.target_budget, "target_budget", positive=True)
        if self.target_budget <= self.current_budget:
            raise ValueError("target_budget должен быть больше current_budget")
        _require_text(self.adset_id, "adset_id")
        _require_text(self.currency, "currency")
        if not self.candidate_ad_ids or len(self.candidate_ad_ids) != len(
            set(self.candidate_ad_ids)
        ):
            raise ValueError("candidate_ad_ids должны быть непустыми и уникальными")
        for ad_id in self.candidate_ad_ids:
            _require_text(ad_id, "candidate_ad_id")
        required_sources = {
            SourceSystem.FACEBOOK,
            SourceSystem.AMO,
            SourceSystem.CDP_ERP,
        }
        if not required_sources.issubset({claim.source for claim in self.facts}):
            raise ValueError("SCALE facts должны включать Facebook, AMO и CDP")
        for claim in self.facts:
            expected_window = (
                self.facebook_window
                if claim.source is SourceSystem.FACEBOOK
                else self.outcome_window
            )
            if claim.window is not None and claim.window != expected_window:
                raise ValueError("SCALE fact использует окно вне manifest contract")


@dataclass(frozen=True, slots=True)
class PaymentEvidence:
    payment_id: str
    contract_number: str
    amo_lead_id: int
    fb_ad_id: str
    amount_lcy: Decimal
    direction: str
    contract_net_lcy: Decimal
    document_at: datetime
    fetched_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.payment_id, "payment_id")
        _require_text(self.contract_number, "contract_number")
        _require_text(self.fb_ad_id, "fb_ad_id")
        if isinstance(self.amo_lead_id, bool) or not isinstance(self.amo_lead_id, int):
            raise ValueError("amo_lead_id должен быть int")
        _require_decimal(self.amount_lcy, "amount_lcy")
        _require_decimal(self.contract_net_lcy, "contract_net_lcy")
        if self.direction not in {"INCOME", "REFUND"}:
            raise ValueError("direction должен быть INCOME или REFUND")
        _require_aware(self.document_at, "document_at")
        _require_aware(self.fetched_at, "fetched_at")


@dataclass(frozen=True, slots=True)
class LaunchSourceInput:
    card_id: str
    campaign_type: str
    requested_cities: tuple[str, ...]
    as_carousel: bool
    origin_reference: str
    # Дозапуск недостающих городов карточки, уже отмеченной галочкой.
    allow_checked: bool = False


@dataclass(frozen=True, slots=True)
class PauseCandidate:
    ad_id: str
    adset_id: str
    display_name: str
    reason_code: str
    decision_window: TimeWindow
    expected_before_status: str
    expected_after_status: str
    spend: Decimal
    spend_currency: str
    leads: int
    quals: int
    payments: tuple[PaymentEvidence, ...]
    revenue_lcy: Decimal
    pre_inventory_sha256: str
    sibling_active_ids: tuple[str, ...]
    replacement_ad_id: str | None = None
    pre_unrelated_inventory_sha256: str = _EMPTY_CANONICAL_SEQUENCE_SHA256
    sibling_status_snapshot: tuple[AdStatusSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class UnpauseCandidate:
    ad_id: str
    adset_id: str
    display_name: str
    expected_before_status: str
    expected_after_status: str
    pre_inventory_sha256: str
    pre_unrelated_inventory_sha256: str = _EMPTY_CANONICAL_SEQUENCE_SHA256
    sibling_status_snapshot: tuple[AdStatusSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class ScaleCandidate:
    adset_id: str
    expected_status: str
    current_budget: Decimal
    target_budget: Decimal
    currency: str
    facebook_window: TimeWindow
    outcome_window: TimeWindow
    candidate_ad_ids: tuple[str, ...]
    facts: tuple[FactClaim, ...]


ActionManifest: TypeAlias = (
    LaunchManifest | AssetRecoveryManifest | PauseManifest | UnpauseManifest | ScaleManifest
)


def _validate_action_common(manifest_id: str, idempotency_key: str, prepared_at: datetime) -> None:
    _require_text(manifest_id, "manifest_id")
    _require_uuid(idempotency_key, "idempotency_key")
    _require_aware(prepared_at, "prepared_at")


def _require_safe_relative_path(value: str) -> None:
    _require_text(value, "staged_relative_path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("staged path должен быть безопасным относительным путём")


def _action_subject_id(action: ActionManifest) -> str:
    if isinstance(action, LaunchManifest):
        return f"card:{action.trello.card_id}"
    if isinstance(action, AssetRecoveryManifest):
        return f"adset:{action.target_adset_id}"
    if isinstance(action, (PauseManifest, UnpauseManifest)):
        return f"ad:{action.ad_id}"
    return f"adset:{action.adset_id}"


@dataclass(frozen=True, slots=True)
class ActionBatchManifest:
    batch_manifest_id: str
    correlation_id: str
    idempotency_key: str
    prepared_at: datetime
    subject_ids: tuple[str, ...]
    actions: tuple[ActionManifest, ...]
    manifest_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.batch_manifest_id, "batch_manifest_id")
        _require_text(self.correlation_id, "correlation_id")
        _require_uuid(self.idempotency_key, "idempotency_key")
        _require_aware(self.prepared_at, "prepared_at")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        if not 1 <= len(self.actions) <= 20:
            raise ValueError("Action batch должен содержать 1..20 actions")
        if len({action.kind for action in self.actions}) != 1:
            raise ValueError("Action batch может содержать только один ActionKind")
        manifest_ids = [action.manifest_id for action in self.actions]
        if len(manifest_ids) != len(set(manifest_ids)):
            raise ValueError("manifest_id внутри batch должны быть уникальными")
        action_subjects = [_action_subject_id(action) for action in self.actions]
        if len(action_subjects) != len(set(action_subjects)):
            raise ValueError("Точные action subjects внутри batch должны быть уникальными")
        if len(self.subject_ids) != len(set(self.subject_ids)):
            raise ValueError("subject_ids внутри batch должны быть уникальными")


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    category: FactCategory
    subject: SubjectRef
    metric: Metric
    value: ClaimValue
    source: SourceSystem
    state: EvidenceState
    observed_at: datetime
    window: TimeWindow | None
    currency: str | None
    entity_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    source: SourceSystem
    state: EvidenceState
    fetched_at: datetime
    data_as_of: datetime | None
    from_cache: bool
    complete: bool
    records: tuple[EvidenceRecord, ...]
    payments: tuple[PaymentEvidence, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SourceFreshness:
    source: SourceSystem
    fetched_at: datetime
    observed_at: datetime | None
    data_as_of: datetime | None
    max_age_seconds: int
    from_cache: bool
    complete: bool
    fresh: bool
    schema_sha256: str | None = None
    high_watermark: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    request_id: str
    purpose: str
    action_kind: ActionKind | None
    generated_at: datetime
    subjects: tuple[SubjectRef, ...]
    claims: tuple[FactClaim, ...]
    required_sources: tuple[SourceSystem, ...]
    windows: tuple[TimeWindow, ...]
    account_ids: tuple[str, ...]
    adset_ids: tuple[str, ...]
    ad_ids: tuple[str, ...]
    card_ids: tuple[str, ...]
    staged_relative_paths: tuple[str, ...]
    include_full_inventory: bool
    force_live: bool
    max_age_seconds: int


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    loaded_at: datetime
    sources: tuple[SourceEvidence, ...]
    freshness: tuple[SourceFreshness, ...]
    facebook_sha256: str
    trello_sha256: str
    media_sha256: str
    amo_sha256: str
    cdp_sha256: str
    final_live_state_sha256: str
    local_state_sha256: str | None


@dataclass(frozen=True, slots=True)
class CheckIssue:
    code: str
    message: str
    blocking: bool
    source: SourceSystem | None = None
    claim_id: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    claim: FactClaim
    state: ClaimState
    observed: ClaimValue
    issues: tuple[CheckIssue, ...]


@dataclass(frozen=True, slots=True)
class ReportCheckResult:
    check_id: str
    verdict: ReportVerdict
    checked_at: datetime
    outcomes: tuple[ClaimOutcome, ...]
    issues: tuple[CheckIssue, ...]
    manifest_sha256: str
    audit_persisted: bool


@dataclass(frozen=True, slots=True)
class ActionReview:
    check_id: str
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    item_id: str
    item_index: int
    action_kind: ActionKind
    decision: SafetyDecision
    checked_at: datetime
    expires_at: datetime | None
    batch_manifest_sha256: str
    item_manifest_sha256: str
    evidence_state_sha256: str | None
    subject_ids: tuple[str, ...]
    issues: tuple[CheckIssue, ...]
    audit_persisted: bool


@dataclass(frozen=True, slots=True)
class ActionPermit:
    permit_id: str
    check_id: str
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    item_id: str
    item_index: int
    action_kind: ActionKind
    item_manifest_sha256: str
    subject_ids: tuple[str, ...]
    evidence_state_sha256: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ActionObservation:
    observed_at: datetime
    digest: str
    target_state: str
    subject_ids: tuple[str, ...]
    unrelated_state_digest: str


@dataclass(frozen=True, slots=True)
class ActionExecution:
    attempt_id: str
    item_id: str
    item_index: int
    action_manifest_id: str
    result: ActionResult
    started_at: datetime | None
    completed_at: datetime | None
    created_ids: tuple[str, ...]
    reason_code: str
    remote_may_have_changed: bool


@dataclass(frozen=True, slots=True)
class ActionRun:
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    state: OperationState
    reviews: tuple[ActionReview, ...]
    executions: tuple[ActionExecution, ...]
    result: ActionResult | None
    shadow_evaluation: ShadowBatchEvaluation | None
    shadow_items: tuple[ShadowItemEvaluation, ...]
    dry_run: bool
    provider_mutation_count: int
    first_unprocessed_index: int | None
    stop_reason_code: str | None
    reconciliation_required: bool

    def __post_init__(self) -> None:
        if self.state is OperationState.SHADOW_FIRST_ITEM_ONLY:
            if self.shadow_evaluation is not ShadowBatchEvaluation.INCOMPLETE or not self.dry_run:
                raise ValueError("Shadow batch всегда INCOMPLETE и dry_run")
            if self.provider_mutation_count != 0 or self.result is not None:
                raise ValueError("Shadow batch не может иметь mutation или terminal result")
            if not self.shadow_items:
                raise ValueError("Shadow batch должен содержать item 0")
            if any(
                item is not ShadowItemEvaluation.NOT_EVALUATED_REQUIRES_LIVE_SEQUENCE
                for item in self.shadow_items[1:]
            ):
                raise ValueError("В shadow только item 0 может быть проверен")


@dataclass(frozen=True, slots=True)
class OperationItemRecord:
    item_id: str
    item_index: int
    action_kind: ActionKind
    item_manifest_sha256: str
    subject_ids: tuple[str, ...]
    check_id: str | None
    decision: SafetyDecision | None
    evidence_state_sha256: str | None
    shadow_evaluation: ShadowItemEvaluation | None
    permit_id: str | None
    attempt_id: str | None
    result: ActionResult | None
    exact_created_ids: tuple[str, ...]
    attempted_at: datetime | None
    completed_at: datetime | None
    reconciliation_required: bool
    permit_revoked_at: datetime | None = None
    permit_revocation_reason_code: str | None = None
    action_manifest_json: str | None = None
    action_manifest_payload_sha256: str | None = None

    def __post_init__(self) -> None:
        if (self.permit_revoked_at is None) != (
            self.permit_revocation_reason_code is None
        ):
            raise ValueError("Время и причина отзыва permit задаются вместе")
        if self.permit_revoked_at is not None:
            _require_aware(self.permit_revoked_at, "permit_revoked_at")
            _require_text(
                self.permit_revocation_reason_code or "",
                "permit_revocation_reason_code",
            )
        if (self.action_manifest_json is None) != (
            self.action_manifest_payload_sha256 is None
        ):
            raise ValueError("manifest JSON и digest задаются вместе")
        if self.action_manifest_payload_sha256 is not None:
            _require_sha256(
                self.action_manifest_payload_sha256,
                "action_manifest_payload_sha256",
            )


@dataclass(frozen=True, slots=True)
class OperationAttemptAttestation:
    """Проверяемая ссылка на fsync-сохранённый action_attempt батч-операции.

    Не owner-аттестация: у owner_action_models.ActionAttemptAttestation другой
    состав полей (в т.ч. account_id), и provider-границы принимают только её.
    Повторное слияние имён валит tests/test_action_adapters_attestation.py.
    """

    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    permit_id: str
    attempt_id: str
    item_id: str
    item_index: int
    action_kind: ActionKind
    item_manifest_sha256: str
    precondition_sha256: str
    subject_ids: tuple[str, ...]
    attempted_at: datetime


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    correlation_id: str
    subject_ids: tuple[str, ...]
    state: OperationState
    items: tuple[OperationItemRecord, ...]
    reserved_at: datetime
    updated_at: datetime
    reconciliation_required: bool


@dataclass(frozen=True, slots=True)
class ActionHistoryRecord:
    operation_id: str
    batch_manifest_id: str
    item_id: str
    item_index: int
    action_kind: ActionKind
    subject_ids: tuple[str, ...]
    attempt_id: str
    result: ActionResult
    exact_created_ids: tuple[str, ...]
    attempted_at: datetime
    completed_at: datetime | None
    reconciliation_required: bool


@dataclass(frozen=True, slots=True)
class OperationReservedAuditEvent:
    schema_version: int
    event: str
    event_id: str
    written_at: datetime
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    correlation_id: str
    subject_ids: tuple[str, ...]
    item_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportCheckAuditEvent:
    schema_version: int
    event: str
    event_id: str
    written_at: datetime
    correlation_id: str
    check_id: str
    report_template: ReportTemplate
    verdict: ReportVerdict
    manifest_sha256: str
    evidence_state_sha256: str | None
    issue_codes: tuple[str, ...]
    checked_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("ReportCheckAuditEvent поддерживает только schema_version=1")
        if self.event != "report_check":
            raise ValueError("ReportCheckAuditEvent.event должен быть report_check")
        for field_name in ("event_id", "correlation_id", "check_id"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.report_template, ReportTemplate):
            raise ValueError("report_template должен быть ReportTemplate")
        if not isinstance(self.verdict, ReportVerdict):
            raise ValueError("verdict должен быть ReportVerdict")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        if self.evidence_state_sha256 is not None:
            _require_sha256(self.evidence_state_sha256, "evidence_state_sha256")
        if self.issue_codes != tuple(sorted(set(self.issue_codes))):
            raise ValueError("issue_codes должны быть sorted и unique")
        for issue_code in self.issue_codes:
            _require_text(issue_code, "issue_code")
        _require_aware(self.written_at, "written_at")
        _require_aware(self.checked_at, "checked_at")


@dataclass(frozen=True, slots=True)
class DeliveryAuditEvent:
    schema_version: int
    event: str
    event_id: str
    written_at: datetime
    delivery_id: str
    delivery_kind: DeliveryKind
    reference_id: str
    payload_sha256: str
    buttons_sha256: str | None
    manifest_sha256: str | None
    report_verdict: ReportVerdict | None
    action_result: ActionResult | None
    channel: DeliveryChannel
    sent: bool
    fallback_sent: bool
    transport_error_code: TransportErrorCode | None
    delivered_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("DeliveryAuditEvent поддерживает только schema_version=1")
        if self.event != "delivery":
            raise ValueError("DeliveryAuditEvent.event должен быть delivery")
        for field_name in ("event_id", "delivery_id", "reference_id"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.delivery_kind, DeliveryKind):
            raise ValueError("delivery_kind должен быть DeliveryKind")
        if not isinstance(self.channel, DeliveryChannel):
            raise ValueError("channel должен быть DeliveryChannel")
        if not isinstance(self.sent, bool) or not isinstance(self.fallback_sent, bool):
            raise ValueError("sent/fallback_sent должны быть bool")
        if self.sent and self.fallback_sent:
            raise ValueError("Primary и fallback delivery не могут быть успешны одновременно")
        if self.transport_error_code is not None and not isinstance(
            self.transport_error_code, TransportErrorCode
        ):
            raise ValueError("transport_error_code должен быть закрытым TransportErrorCode")
        if not self.sent and not self.fallback_sent and self.transport_error_code is None:
            raise ValueError("Неуспешная доставка должна иметь sanitized transport error code")
        if self.sent and self.transport_error_code is not None:
            raise ValueError("Успешная primary доставка не должна иметь transport error")
        _require_aware(self.written_at, "written_at")
        _require_aware(self.delivered_at, "delivered_at")
        _require_sha256(self.payload_sha256, "payload_sha256")
        if self.buttons_sha256 is not None:
            _require_sha256(self.buttons_sha256, "buttons_sha256")

        if self.delivery_kind is DeliveryKind.REPORT:
            if self.report_verdict is None or self.action_result is not None:
                raise ValueError("REPORT delivery требует только report_verdict")
            if self.manifest_sha256 is None:
                raise ValueError("REPORT delivery требует manifest_sha256")
        elif self.delivery_kind is DeliveryKind.ACTION:
            if self.action_result is None or self.report_verdict is not None:
                raise ValueError("ACTION delivery требует только action_result")
            if self.manifest_sha256 is None:
                raise ValueError("ACTION delivery требует manifest_sha256")
            if self.buttons_sha256 is not None:
                raise ValueError("ACTION delivery не может содержать buttons_sha256")
        elif (
            self.report_verdict is not None
            or self.action_result is not None
            or self.manifest_sha256 is not None
            or self.buttons_sha256 is not None
        ):
            raise ValueError(
                "FACT_FREE delivery не может ссылаться на business result/manifest/buttons"
            )

        if self.manifest_sha256 is not None:
            _require_sha256(self.manifest_sha256, "manifest_sha256")


@dataclass(frozen=True, slots=True)
class CheckAuditEvent:
    schema_version: int
    event: str
    event_id: str
    written_at: datetime
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    subject_ids: tuple[str, ...]
    check_id: str
    item_id: str
    item_index: int
    action_kind: ActionKind
    item_manifest_sha256: str
    decision: ApprovalDecision
    checked_at: datetime
    expires_at: datetime | None
    final_live_state_sha256: str | None
    issue_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PermitIssuedAuditEvent:
    schema_version: int
    event: str
    event_id: str
    written_at: datetime
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    subject_ids: tuple[str, ...]
    check_id: str
    permit_id: str
    item_id: str
    item_index: int
    action_kind: ActionKind
    item_manifest_sha256: str
    final_live_state_sha256: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PermitRevokedAuditEvent:
    schema_version: int
    event: str
    event_id: str
    written_at: datetime
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    subject_ids: tuple[str, ...]
    check_id: str
    permit_id: str
    item_id: str
    item_index: int
    action_kind: ActionKind
    item_manifest_sha256: str
    evidence_state_sha256: str
    revoked_at: datetime
    reason_code: str

    def __post_init__(self) -> None:
        if self.event != "permit_revoked":
            raise ValueError("PermitRevokedAuditEvent.event должен быть permit_revoked")
        for field_name in (
            "event_id",
            "operation_id",
            "idempotency_key",
            "batch_manifest_id",
            "check_id",
            "permit_id",
            "item_id",
            "reason_code",
        ):
            _require_text(getattr(self, field_name), field_name)
        for field_name in (
            "batch_manifest_sha256",
            "item_manifest_sha256",
            "evidence_state_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        _require_aware(self.written_at, "written_at")
        _require_aware(self.revoked_at, "revoked_at")
        if isinstance(self.item_index, bool) or self.item_index < 0:
            raise ValueError("item_index должен быть неотрицательным int")


@dataclass(frozen=True, slots=True)
class ActionAttemptAuditEvent:
    schema_version: int
    event: str
    event_id: str
    written_at: datetime
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    subject_ids: tuple[str, ...]
    permit_id: str
    attempt_id: str
    item_id: str
    item_index: int
    action_kind: ActionKind
    item_manifest_sha256: str
    precondition_sha256: str
    attempted_at: datetime


@dataclass(frozen=True, slots=True)
class ActionResultAuditEvent:
    schema_version: int
    event: str
    event_id: str
    written_at: datetime
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    subject_ids: tuple[str, ...]
    permit_id: str
    attempt_id: str
    item_id: str
    item_index: int
    action_kind: ActionKind
    result: ActionResult
    exact_created_ids: tuple[str, ...]
    postcondition_sha256: str | None
    reason_code: str
    remote_may_have_changed: bool
    reconciliation_required: bool
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationAuditEvent:
    schema_version: int
    event: str
    event_id: str
    written_at: datetime
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    subject_ids: tuple[str, ...]
    item_id: str
    item_index: int
    prior_attempt_id: str
    final_result: ActionResult
    exact_created_ids: tuple[str, ...]
    postcondition_sha256: str
    reconciled_at: datetime


AuditEvent: TypeAlias = (
    OperationReservedAuditEvent
    | ReportCheckAuditEvent
    | DeliveryAuditEvent
    | CheckAuditEvent
    | PermitIssuedAuditEvent
    | PermitRevokedAuditEvent
    | ActionAttemptAuditEvent
    | ActionResultAuditEvent
    | ReconciliationAuditEvent
)


@dataclass(frozen=True, slots=True)
class CoverageResult:
    complete: bool
    manifest_sha256: str
    issues: tuple[CheckIssue, ...]


@dataclass(frozen=True, slots=True)
class RenderedReport:
    text: str
    text_sha256: str
    rendered_field_ids: tuple[str, ...]
    manifest_sha256: str
    verdict: ReportVerdict
    check_id: str


@dataclass(frozen=True, slots=True)
class TelegramButton:
    text: str
    callback_data: str
    action_kind: ActionKind | None
    subject_id: str | None
    check_id: str
    permit_seed_id: str | None


@dataclass(frozen=True, slots=True)
class CheckerHealth:
    enabled: bool
    enforce_reports: bool
    enforce_actions: bool
    audit_writable: bool
    reconciliation_pending: bool
    last_check_at: datetime | None


@dataclass(frozen=True, slots=True)
class CheckedDelivery:
    sent: bool
    fallback_sent: bool
    check_id: str
    report_verdict: ReportVerdict | None
    action_result: ActionResult | None
    audit_persisted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.sent, bool) or not isinstance(self.fallback_sent, bool):
            raise ValueError("sent/fallback_sent должны быть bool")
        if not isinstance(self.audit_persisted, bool):
            raise ValueError("audit_persisted должен быть bool")
        _require_text(self.check_id, "check_id")


def manifest_sha256(manifest: ActionManifest | ActionBatchManifest) -> str:
    """Хеширует точный immutable manifest, исключая его собственное поле digest."""

    if isinstance(manifest, ActionBatchManifest):
        payload = {
            item.name: getattr(manifest, item.name)
            for item in fields(manifest)
            if item.name != "manifest_sha256"
        }
    else:
        payload = manifest
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def report_manifest_sha256(payload: TypedReportPayload, claims: tuple[FactClaim, ...]) -> str:
    return hashlib.sha256(canonical_json({"payload": payload, "claims": claims})).hexdigest()


def _record_business_state(record: EvidenceRecord) -> dict[str, object]:
    return {
        "category": record.category,
        "subject": record.subject,
        "metric": record.metric,
        "value": record.value,
        "source": record.source,
        "state": record.state,
        "window": record.window,
        "currency": record.currency,
        "entity_ids": record.entity_ids,
    }


def _payment_business_state(payment: PaymentEvidence) -> dict[str, object]:
    return {
        "payment_id": payment.payment_id,
        "contract_number": payment.contract_number,
        "amo_lead_id": payment.amo_lead_id,
        "fb_ad_id": payment.fb_ad_id,
        "amount_lcy": payment.amount_lcy,
        "direction": payment.direction,
        "contract_net_lcy": payment.contract_net_lcy,
        "document_at": payment.document_at,
    }


def canonical_state_sha256(evidence: EvidenceBundle) -> str:
    """Хеширует только business/provider state, без transport timestamps и cache metadata."""

    state = {
        "sources": [
            {
                "source": source.source,
                "records": tuple(_record_business_state(record) for record in source.records),
                "payments": tuple(_payment_business_state(payment) for payment in source.payments),
            }
            for source in evidence.sources
        ],
        "facebook_sha256": evidence.facebook_sha256,
        "trello_sha256": evidence.trello_sha256,
        "media_sha256": evidence.media_sha256,
        "amo_sha256": evidence.amo_sha256,
        "cdp_sha256": evidence.cdp_sha256,
        "final_live_state_sha256": evidence.final_live_state_sha256,
        "local_state_sha256": evidence.local_state_sha256,
    }
    return hashlib.sha256(canonical_json(state)).hexdigest()
