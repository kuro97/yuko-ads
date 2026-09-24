from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Iterable, Mapping

from services.approval_checker_models import (
    ActionBatchManifest,
    ActionKind,
    ActionManifest,
    AssetRecoveryManifest,
    ActionReview,
    SafetyDecision,
    CheckIssue,
    ClaimOutcome,
    ClaimState,
    EvidenceBundle,
    EvidenceRecord,
    EvidenceState,
    FactCategory,
    FactClaim,
    LaunchManifest,
    Metric,
    PauseManifest,
    PaymentEvidence,
    ReportCheckRequest,
    ReportCheckResult,
    ReportVerdict,
    ScaleManifest,
    SourceEvidence,
    SourceFreshness,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    UnpauseManifest,
    canonical_json,
    manifest_sha256,
)
from services.approval_report import validate_report_coverage


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ZERO = Decimal("0")

_ACTION_REQUIRED_SOURCES = frozenset(
    {
        SourceSystem.FACEBOOK,
        SourceSystem.TRELLO,
        SourceSystem.MEDIA_BYTES,
        SourceSystem.AMO,
        SourceSystem.CDP_ERP,
    }
)

_RUNTIME_SOURCES = frozenset(
    {
        SourceSystem.FB_ERROR_RING,
        SourceSystem.NOTIFICATION_RUNTIME,
        SourceSystem.CRON_HEARTBEATS,
        SourceSystem.CRON_FAILURE_STATE,
    }
)

_DERIVED_INPUTS: Mapping[Metric, tuple[Metric, ...]] = MappingProxyType(
    {
        Metric.CPL: (Metric.SPEND, Metric.LEADS),
        Metric.CPQL: (Metric.SPEND, Metric.QUALS),
        Metric.QUAL_PCT: (Metric.LEADS, Metric.QUALS),
        Metric.ROMI_PCT: (Metric.SPEND, Metric.REVENUE),
        Metric.DRR_PCT: (Metric.SPEND, Metric.REVENUE),
    }
)

_METRIC_SOURCES: Mapping[Metric, frozenset[SourceSystem]] = MappingProxyType(
    {
        Metric.EFFECTIVE_STATUS: frozenset({SourceSystem.FACEBOOK}),
        Metric.CONFIGURED_STATUS: frozenset({SourceSystem.FACEBOOK}),
        Metric.SPEND: frozenset({SourceSystem.FACEBOOK}),
        # Лиды считаются в ДВУХ несводимых системах, и обе легитимны:
        #  - FACEBOOK — actions[lead|on_facebook_lead] из insights, метрика
        #    рекламы (см. approval_source_facebook._lead_count);
        #  - AMO — канон проекта: воронка «Новые продажи» (config.AMO_PIPELINE_ID
        #    = 3480844, фильтр filter[pipeline_id] в integrations.amo.
        #    get_leads_window:360-361) со служебными сделками «Автосделка»/
        #    «Рассылка Waba» вне счёта (integrations.amo._is_service_lead:563).
        # Отчёты (morning_digest/evening_report) заявляют именно AMO-канон —
        # _leads_quals_payments зовёт get_leads_window, а не FB-каунты. Пока в
        # таблице стоял один FACEBOOK, честный AMO-claim бракавался кодом
        # SOURCE_CONTRACT_MISMATCH; устарела таблица, а не сборщик. Проверка
        # «источник claim == источник записи evidence» этим не ослабляется:
        # запись всё так же обязана прийти ровно из заявленной системы.
        Metric.LEADS: frozenset({SourceSystem.FACEBOOK, SourceSystem.AMO}),
        Metric.QUALS: frozenset({SourceSystem.AMO}),
        Metric.QUAL_PCT: frozenset({SourceSystem.AMO, SourceSystem.CREATIVE_KB}),
        Metric.PAYMENTS: frozenset({SourceSystem.CDP_ERP}),
        Metric.REVENUE: frozenset({SourceSystem.CDP_ERP}),
        Metric.DAILY_BUDGET: frozenset({SourceSystem.FACEBOOK}),
        Metric.CAPACITY: frozenset({SourceSystem.FACEBOOK}),
        Metric.ACCURACY_PCT: frozenset({SourceSystem.CREATIVE_KB}),
        Metric.PRODUCT_SHARE_PCT: frozenset({SourceSystem.CREATIVE_KB}),
        Metric.DECISION_COUNT: frozenset(
            {SourceSystem.DECISIONS_DB, SourceSystem.CREATIVE_KB}
        ),
        Metric.FEEDBACK_COUNT: frozenset({SourceSystem.AUTOPILOT_FEEDBACK}),
        Metric.AGREEMENT_PCT: frozenset({SourceSystem.AUTOPILOT_FEEDBACK}),
        Metric.CHECKER_HEALTH: frozenset({SourceSystem.CHECKER_RUNTIME}),
        Metric.HISTORY_STATE: frozenset({SourceSystem.CHECKER_AUDIT}),
    }
)

_ONLINE_CDP_METRICS = frozenset(
    {
        Metric.DISPLAY_CONTEXT,
        Metric.SPEND,
        Metric.LEADS,
        Metric.QUALS,
        Metric.QUAL_PCT,
        Metric.REVENUE,
        Metric.DRR_PCT,
        Metric.MATCH_STATE,
    }
)

_ONLINE_ONLY_CDP_METRICS = _ONLINE_CDP_METRICS - {Metric.REVENUE}
_ONLINE_DRR_MODES = frozenset({"EXACT_REVENUE", "WEIGHTED_FALLBACK"})

# Таблица остаётся публичной: архитектурный тест проверяет, что ни одно правило не потеряно.
RULE_ISSUE_CODES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "R01": ("INVALID_CONTRACT", "REPORT_COVERAGE_INCOMPLETE"),
        "R02": (
            "SOURCE_UNAVAILABLE",
            "SOURCE_STALE",
            "SOURCE_INCOMPLETE",
            "CACHED_ACTION_EVIDENCE",
        ),
        "R03": ("FACT_MISMATCH",),
        "R04": ("REPORT_COVERAGE_INCOMPLETE",),
        "R05": ("NAME_ONLY_ATTRIBUTION",),
        "R06": ("NULL_COERCED_TO_ZERO",),
        "R07": ("PAYMENT_ENTITY_MISSING",),
        "R08": ("DUPLICATE_SOURCE_ENTITY",),
        "R09": ("PAYMENTS_WITHOUT_REVENUE",),
        "R10": ("DERIVED_INPUT_NOT_VERIFIED",),
        "R11": ("LAUNCH_INPUT_DRIFT",),
        "R12": ("LAUNCH_SCOPE_DRIFT",),
        "R13": ("LAUNCH_DUPLICATE",),
        "R14": ("LAUNCH_CAPACITY_INSUFFICIENT",),
        "R15": ("PAUSE_TARGET_CHANGED",),
        "R16": ("LAST_EFFECTIVE_ACTIVE",),
        "R17": ("INVENTORY_CHANGED",),
        "R18": ("PERMIT_INVALID",),
        "R19": ("PERMIT_INVALID", "SEQUENCE_INVALID"),
        "R20": ("ATTEMPT_AUDIT_FAILED",),
        "R21": ("PROVIDER_REJECTED",),
        "R22": ("REMOTE_RESULT_UNKNOWN", "RESULT_AUDIT_FAILED"),
        "R23": ("PARTIAL_LAUNCH",),
        "R24": ("POST_STATE_MISMATCH",),
        "R25": ("POST_STATE_CONFIRMED",),
        "R26": ("CHECKER_UNAVAILABLE", "PENDING_RECONCILIATION"),
        "R27": ("NO_SAFE_FACTS",),
        "R28": ("SEQUENCE_INVALID",),
        "R29": ("ACTION_EVIDENCE_INVALID", "PERMIT_INVALID"),
        "R30": ("SHADOW_FIRST_ITEM_ONLY",),
        "R31": ("SOURCE_CONTRACT_MISMATCH",),
        "R32": ("RUNTIME_EVIDENCE_INCOMPLETE",),
        "R33": ("DAILY_MANIFEST_INCOMPLETE",),
    }
)


def _issue(
    code: str,
    message: str,
    *,
    blocking: bool = True,
    source: SourceSystem | None = None,
    claim_id: str | None = None,
) -> CheckIssue:
    return CheckIssue(
        code=code,
        message=message,
        blocking=blocking,
        source=source,
        claim_id=claim_id,
    )


def _deduplicate_issues(issues: Iterable[CheckIssue]) -> tuple[CheckIssue, ...]:
    unique: list[CheckIssue] = []
    seen: set[tuple[object, ...]] = set()
    for issue in issues:
        key = (issue.code, issue.source, issue.claim_id, issue.blocking)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return tuple(unique)


def _is_zero(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)) == _ZERO
    except (InvalidOperation, TypeError, ValueError):
        return False


def _numeric_equal(left: object, right: object, tolerance: Decimal) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, (int, Decimal)) or not isinstance(right, (int, Decimal)):
        return left == right
    return abs(Decimal(left) - Decimal(right)) <= tolerance


def _identity_tuple(record: EvidenceRecord) -> tuple[object, ...]:
    return (
        record.category,
        record.subject,
        record.metric,
        record.source,
        record.window,
        record.currency,
    )


def _claim_identity_tuple(claim: FactClaim) -> tuple[object, ...]:
    return (
        claim.category,
        claim.subject,
        claim.metric,
        claim.source,
        claim.window,
        claim.currency,
    )


def _uses_name_identity(subject: SubjectRef, entity_ids: tuple[str, ...] = ()) -> bool:
    values = (subject.subject_id, *entity_ids)
    forbidden_prefixes = ("name:", "ad_name:", "adset_name:", "campaign_name:", "utm:")
    return any(value.strip().lower().startswith(forbidden_prefixes) for value in values)


def compare_claim(claim: FactClaim, record: EvidenceRecord | None) -> ClaimOutcome:
    """Сравнивает claim с одной точной записью, не подменяя отсутствие нулём."""

    if record is None:
        if _is_zero(claim.value):
            issue = _issue(
                "NULL_COERCED_TO_ZERO",
                "Отсутствующее значение нельзя считать нулём",
                claim_id=claim.claim_id,
                source=claim.source,
            )
        else:
            issue = _issue(
                "EVIDENCE_MISSING",
                "Для утверждения нет точной записи источника",
                blocking=claim.required,
                claim_id=claim.claim_id,
                source=claim.source,
            )
        return ClaimOutcome(claim, ClaimState.NOT_VERIFIABLE, None, (issue,))

    if _uses_name_identity(record.subject, record.entity_ids):
        issue = _issue(
            "NAME_ONLY_ATTRIBUTION",
            "Название, campaign, adset или UTM нельзя использовать как точную identity",
            claim_id=claim.claim_id,
            source=claim.source,
        )
        return ClaimOutcome(claim, ClaimState.MISMATCH, record.value, (issue,))

    if _identity_tuple(record) != _claim_identity_tuple(claim):
        issue = _issue(
            "FACT_MISMATCH",
            "Источник, subject, период, валюта или метрика не совпадают",
            blocking=claim.required,
            claim_id=claim.claim_id,
            source=claim.source,
        )
        return ClaimOutcome(claim, ClaimState.MISMATCH, record.value, (issue,))

    if record.state is not EvidenceState.FRESH_COMPLETE:
        code = (
            "NULL_COERCED_TO_ZERO"
            if _is_zero(claim.value)
            else "EVIDENCE_NOT_VERIFIABLE"
        )
        issue = _issue(
            code,
            "Неполное или устаревшее значение нельзя подтверждать",
            blocking=True if code == "NULL_COERCED_TO_ZERO" else claim.required,
            claim_id=claim.claim_id,
            source=claim.source,
        )
        return ClaimOutcome(claim, ClaimState.NOT_VERIFIABLE, record.value, (issue,))

    if record.value is None:
        code = "NULL_COERCED_TO_ZERO" if _is_zero(claim.value) else "EVIDENCE_MISSING"
        issue = _issue(
            code,
            "NULL не является подтверждённым числом",
            blocking=True if code == "NULL_COERCED_TO_ZERO" else claim.required,
            claim_id=claim.claim_id,
            source=claim.source,
        )
        return ClaimOutcome(claim, ClaimState.NOT_VERIFIABLE, None, (issue,))

    if not _numeric_equal(claim.value, record.value, claim.tolerance):
        issue = _issue(
            "FACT_MISMATCH",
            "Заявленное значение не совпадает с источником",
            blocking=claim.required,
            claim_id=claim.claim_id,
            source=claim.source,
        )
        return ClaimOutcome(claim, ClaimState.MISMATCH, record.value, (issue,))

    return ClaimOutcome(claim, ClaimState.MATCH, record.value, ())


def _sources_by_system(
    evidence: EvidenceBundle,
) -> tuple[dict[SourceSystem, SourceEvidence], list[CheckIssue]]:
    grouped: defaultdict[SourceSystem, list[SourceEvidence]] = defaultdict(list)
    for source in evidence.sources:
        grouped[source.source].append(source)
    issues: list[CheckIssue] = []
    for source, values in grouped.items():
        if len(values) > 1:
            issues.append(
                _issue(
                    "DUPLICATE_SOURCE_ENTITY",
                    "Источник присутствует в evidence bundle несколько раз",
                    source=source,
                )
            )
    return {source: values[0] for source, values in grouped.items()}, issues


def _freshness_by_system(
    evidence: EvidenceBundle,
) -> tuple[dict[SourceSystem, SourceFreshness], list[CheckIssue]]:
    grouped: defaultdict[SourceSystem, list[SourceFreshness]] = defaultdict(list)
    for freshness in evidence.freshness:
        grouped[freshness.source].append(freshness)
    issues: list[CheckIssue] = []
    for source, values in grouped.items():
        if len(values) > 1:
            issues.append(
                _issue(
                    "DUPLICATE_SOURCE_ENTITY",
                    "Метаданные свежести источника задублированы",
                    source=source,
                )
            )
    return {source: values[0] for source, values in grouped.items()}, issues


def _source_problem(
    source: SourceSystem,
    source_evidence: SourceEvidence | None,
    freshness: SourceFreshness | None,
    *,
    action: bool,
    blocking: bool,
) -> CheckIssue | None:
    if source is SourceSystem.AD_DAILY_METRICS:
        incomplete_code = "DAILY_MANIFEST_INCOMPLETE"
        incomplete_message = "Daily manifest или его page/chunk/ID coverage неполон"
    elif source in _RUNTIME_SOURCES:
        incomplete_code = "RUNTIME_EVIDENCE_INCOMPLETE"
        incomplete_message = (
            "Runtime evidence получен не из полного same-instance snapshot"
        )
    else:
        incomplete_code = "SOURCE_INCOMPLETE"
        incomplete_message = "Источник прочитан не полностью"
    if source_evidence is None or freshness is None:
        return _issue(
            "SOURCE_UNAVAILABLE",
            "Обязательный источник не предоставил evidence и freshness",
            blocking=blocking,
            source=source,
        )
    if source_evidence.state is EvidenceState.ERROR:
        return _issue(
            "SOURCE_UNAVAILABLE",
            "Источник вернул ошибку",
            blocking=blocking,
            source=source,
        )
    if source_evidence.state is EvidenceState.STALE or not freshness.fresh:
        return _issue(
            "SOURCE_STALE",
            "Данные источника устарели",
            blocking=blocking,
            source=source,
        )
    if (
        source_evidence.state is not EvidenceState.FRESH_COMPLETE
        or not source_evidence.complete
        or not freshness.complete
    ):
        return _issue(
            incomplete_code, incomplete_message, blocking=blocking, source=source
        )
    if action and (source_evidence.from_cache or freshness.from_cache):
        return _issue(
            "CACHED_ACTION_EVIDENCE",
            "Для действия запрещены данные из кеша",
            source=source,
        )
    return None


def _all_records(
    sources: Mapping[SourceSystem, SourceEvidence],
) -> tuple[EvidenceRecord, ...]:
    return tuple(record for source in sources.values() for record in source.records)


def _record_for_claim(
    claim: FactClaim,
    records: tuple[EvidenceRecord, ...],
) -> tuple[EvidenceRecord | None, tuple[CheckIssue, ...]]:
    exact = [
        record
        for record in records
        if _identity_tuple(record) == _claim_identity_tuple(claim)
    ]
    if len(exact) > 1:
        return exact[0], (
            _issue(
                "DUPLICATE_SOURCE_ENTITY",
                "Для одного claim найдено несколько точных записей",
                source=claim.source,
                claim_id=claim.claim_id,
            ),
        )
    if exact:
        return exact[0], ()
    candidates = [
        record
        for record in records
        if record.metric is claim.metric and record.source is claim.source
    ]
    return (candidates[0] if candidates else None), ()


def _source_contract_issue(claim: FactClaim) -> CheckIssue | None:
    if claim.source is SourceSystem.CDP_ERP and claim.metric in _ONLINE_CDP_METRICS:
        is_online = (
            claim.subject.kind is SubjectKind.ACCOUNT
            and claim.subject.subject_id == "ONLINE"
        )
        if is_online:
            currency_valid = (
                claim.currency in {"USD", "LCY"}
                if claim.metric is Metric.SPEND
                else claim.currency == "LCY"
                if claim.metric is Metric.REVENUE
                else claim.currency is None
            )
            category_valid = (
                claim.category is FactCategory.DISPLAY_CONTEXT
                if claim.metric in {Metric.DISPLAY_CONTEXT, Metric.MATCH_STATE}
                else claim.category is FactCategory.BUSINESS_METRIC
            )
            if claim.window is not None and currency_valid and category_valid:
                return None
            return _issue(
                "SOURCE_CONTRACT_MISMATCH",
                "CDP Online требует exact ACCOUNT:ONLINE, период, категорию и валюту",
                blocking=claim.required,
                source=claim.source,
                claim_id=claim.claim_id,
            )
        if claim.metric in _ONLINE_ONLY_CDP_METRICS:
            return _issue(
                "SOURCE_CONTRACT_MISMATCH",
                "CDP разрешён для этой агрегатной метрики только у ACCOUNT:ONLINE",
                blocking=claim.required,
                source=claim.source,
                claim_id=claim.claim_id,
            )
    allowed = _METRIC_SOURCES.get(claim.metric)
    if allowed is None or claim.source in allowed:
        return None
    return _issue(
        "SOURCE_CONTRACT_MISMATCH",
        "Метрика заявлена не тем источником, который закреплён контрактом",
        blocking=claim.required,
        source=claim.source,
        claim_id=claim.claim_id,
    )


def _online_coverage_token(claim: FactClaim) -> str | None:
    if claim.window is None:
        return None
    first = claim.window.start.date()
    last = (claim.window.end - timedelta(microseconds=1)).date()
    return f"coverage:complete:{first.isoformat()}..{last.isoformat()}"


def _online_cdp_proof_issue(
    claim: FactClaim,
    record: EvidenceRecord | None,
    records: tuple[EvidenceRecord, ...],
) -> CheckIssue | None:
    is_online_claim = (
        claim.source is SourceSystem.CDP_ERP
        and claim.metric in _ONLINE_CDP_METRICS
        and claim.subject.kind is SubjectKind.ACCOUNT
        and claim.subject.subject_id == "ONLINE"
    )
    if not is_online_claim or record is None:
        return None
    coverage_token = _online_coverage_token(claim)
    if coverage_token is None or coverage_token not in record.entity_ids:
        return _issue(
            "SOURCE_INCOMPLETE",
            "CDP Online record не содержит полного покрытия exact window",
            blocking=claim.required,
            source=claim.source,
            claim_id=claim.claim_id,
        )
    if claim.metric is not Metric.DRR_PCT:
        return None

    record_modes = {
        entity.removeprefix("mode:")
        for entity in record.entity_ids
        if entity.startswith("mode:")
    }
    mode_records = [
        item
        for item in records
        if item.source is SourceSystem.CDP_ERP
        and item.subject == claim.subject
        and item.metric is Metric.MATCH_STATE
        and item.window == claim.window
        and item.currency is None
        and item.state is EvidenceState.FRESH_COMPLETE
        and item.value in _ONLINE_DRR_MODES
        and coverage_token in item.entity_ids
        and f"mode:{item.value}" in item.entity_ids
    ]
    if len(mode_records) != 1 or mode_records[0].value not in record_modes:
        return _issue(
            "DERIVED_INPUT_NOT_VERIFIED",
            "CDP Online DRR не содержит единственного mode/coverage proof",
            blocking=claim.required,
            source=claim.source,
            claim_id=claim.claim_id,
        )
    return None


def _payment_entity_issues(
    claim: FactClaim,
    payments: tuple[PaymentEvidence, ...],
) -> tuple[CheckIssue, ...]:
    if (
        claim.metric is not Metric.PAYMENTS
        or not isinstance(claim.value, int)
        or claim.value <= 0
    ):
        return ()

    issues: list[CheckIssue] = []
    by_id: defaultdict[str, list[PaymentEvidence]] = defaultdict(list)
    for payment in payments:
        by_id[payment.payment_id].append(payment)
    if any(len(items) > 1 and len(set(items)) > 1 for items in by_id.values()):
        issues.append(
            _issue(
                "DUPLICATE_SOURCE_ENTITY",
                "Один payment_id содержит конфликтующие сущности",
                source=SourceSystem.CDP_ERP,
                claim_id=claim.claim_id,
            )
        )

    valid_ids: set[str] = set()
    for payment_id, items in by_id.items():
        payment = items[0]
        exact_ad = (
            claim.subject.kind is not SubjectKind.AD
            or payment.fb_ad_id == claim.subject.subject_id
        )
        in_window = (
            claim.window is None
            or claim.window.start <= payment.document_at < claim.window.end
        )
        entity_complete = (
            bool(payment.payment_id.strip())
            and bool(payment.contract_number.strip())
            and payment.amo_lead_id > 0
            and exact_ad
            and in_window
            and payment.direction in {"INCOME", "REFUND"}
            and payment.amount_lcy.is_finite()
            and payment.contract_net_lcy.is_finite()
            and payment.contract_net_lcy > 0
        )
        if entity_complete:
            valid_ids.add(payment_id)
    if len(valid_ids) != claim.value:
        issues.append(
            _issue(
                "PAYMENT_ENTITY_MISSING",
                "Количество оплат не подтверждено полными payment entities",
                source=SourceSystem.CDP_ERP,
                claim_id=claim.claim_id,
            )
        )
    return tuple(issues)


def _payments_without_revenue(claims: tuple[FactClaim, ...]) -> tuple[CheckIssue, ...]:
    issues: list[CheckIssue] = []
    revenue_claims = [claim for claim in claims if claim.metric is Metric.REVENUE]
    for payment_claim in claims:
        if (
            payment_claim.metric is not Metric.PAYMENTS
            or not isinstance(payment_claim.value, int)
            or payment_claim.value <= 0
        ):
            continue
        matching_revenue = [
            claim
            for claim in revenue_claims
            if claim.subject == payment_claim.subject
            and claim.window == payment_claim.window
        ]
        revenue_is_positive = any(
            isinstance(claim.value, (int, Decimal))
            and not isinstance(claim.value, bool)
            and Decimal(claim.value) > 0
            for claim in matching_revenue
        )
        if not revenue_is_positive:
            issues.append(
                _issue(
                    "PAYMENTS_WITHOUT_REVENUE",
                    "Оплаты больше нуля, но выручка отсутствует или не положительна",
                    source=SourceSystem.CDP_ERP,
                    claim_id=payment_claim.claim_id,
                )
            )
    return tuple(issues)


def _derived_metric_issues(
    claims: tuple[FactClaim, ...],
    outcomes: tuple[ClaimOutcome, ...],
    trusted_derived_claim_ids: frozenset[str] = frozenset(),
) -> tuple[CheckIssue, ...]:
    outcome_by_claim_id = {outcome.claim.claim_id: outcome for outcome in outcomes}
    issues: list[CheckIssue] = []
    for claim in claims:
        if claim.claim_id in trusted_derived_claim_ids:
            continue
        dependencies = _DERIVED_INPUTS.get(claim.metric)
        if dependencies is None:
            continue
        matching_inputs: list[FactClaim] = []
        valid = True
        for metric in dependencies:
            candidates = [
                item
                for item in claims
                if item.metric is metric and item.subject == claim.subject
            ]
            exact = [item for item in candidates if item.window == claim.window]
            if len(exact) != 1:
                valid = False
                continue
            dependency = exact[0]
            matching_inputs.append(dependency)
            if outcome_by_claim_id.get(dependency.claim_id, None) is None:
                valid = False
            elif outcome_by_claim_id[dependency.claim_id].state is not ClaimState.MATCH:
                valid = False
        currencies = {
            item.currency for item in matching_inputs if item.currency is not None
        }
        if len(currencies) > 1:
            valid = False
        if not valid:
            issues.append(
                _issue(
                    "DERIVED_INPUT_NOT_VERIFIED",
                    "Производная метрика использует неподтверждённые или разные периоды/валюты",
                    blocking=claim.required,
                    source=claim.source,
                    claim_id=claim.claim_id,
                )
            )
    return tuple(issues)


def evaluate_report(
    request: ReportCheckRequest,
    evidence: EvidenceBundle,
    now: datetime,
) -> ReportCheckResult:
    """Применяет детерминированные правила к критическому отчёту."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")

    coverage = validate_report_coverage(request)
    issues: list[CheckIssue] = list(coverage.issues)
    sources, source_issues = _sources_by_system(evidence)
    freshness, freshness_issues = _freshness_by_system(evidence)
    issues.extend(source_issues)
    issues.extend(freshness_issues)
    records = _all_records(sources)
    outcomes: list[ClaimOutcome] = []
    trusted_derived_claim_ids: set[str] = set()

    required_by_source: defaultdict[SourceSystem, bool] = defaultdict(bool)
    for claim in request.claims:
        required_by_source[claim.source] = (
            required_by_source[claim.source] or claim.required
        )

    source_problems: dict[SourceSystem, CheckIssue] = {}
    for source, blocking in required_by_source.items():
        problem = _source_problem(
            source,
            sources.get(source),
            freshness.get(source),
            action=False,
            blocking=blocking,
        )
        if problem is not None:
            source_problems[source] = problem
            issues.append(problem)

    for claim in request.claims:
        record: EvidenceRecord | None = None
        contract_issue = _source_contract_issue(claim)
        if contract_issue is not None:
            issues.append(contract_issue)
            outcome = ClaimOutcome(
                claim=claim,
                state=ClaimState.NOT_VERIFIABLE,
                observed=None,
                issues=(contract_issue,),
            )
        else:
            source_problem = source_problems.get(claim.source)
        if contract_issue is None and source_problem is not None:
            outcome = ClaimOutcome(
                claim=claim,
                state=ClaimState.NOT_VERIFIABLE,
                observed=None,
                issues=(replace_issue_for_claim(source_problem, claim),),
            )
        elif contract_issue is None:
            record, duplicate_issues = _record_for_claim(claim, records)
            issues.extend(duplicate_issues)
            outcome = compare_claim(claim, record)
        proof_issue = _online_cdp_proof_issue(claim, record, records)
        if proof_issue is not None:
            issues.append(proof_issue)
            outcome = ClaimOutcome(
                claim=claim,
                state=ClaimState.NOT_VERIFIABLE,
                observed=outcome.observed,
                issues=outcome.issues + (proof_issue,),
            )
        elif (
            claim.metric is Metric.DRR_PCT
            and claim.source is SourceSystem.CDP_ERP
            and outcome.state is ClaimState.MATCH
        ):
            trusted_derived_claim_ids.add(claim.claim_id)
        outcomes.append(outcome)
        issues.extend(outcome.issues)

        source_evidence = sources.get(SourceSystem.CDP_ERP)
        payments = source_evidence.payments if source_evidence is not None else ()
        issues.extend(_payment_entity_issues(claim, payments))

    outcome_tuple = tuple(outcomes)
    issues.extend(_payments_without_revenue(request.claims))
    derived_issues = _derived_metric_issues(
        request.claims,
        outcome_tuple,
        frozenset(trusted_derived_claim_ids),
    )
    issues.extend(derived_issues)
    invalid_derived_claim_ids = {
        issue.claim_id for issue in derived_issues if issue.claim_id is not None
    }
    if invalid_derived_claim_ids:
        outcome_tuple = tuple(
            ClaimOutcome(
                claim=outcome.claim,
                state=ClaimState.NOT_VERIFIABLE,
                observed=outcome.observed,
                issues=outcome.issues
                + tuple(
                    issue
                    for issue in derived_issues
                    if issue.claim_id == outcome.claim.claim_id
                ),
            )
            if outcome.claim.claim_id in invalid_derived_claim_ids
            else outcome
            for outcome in outcome_tuple
        )

    if not any(outcome.state is ClaimState.MATCH for outcome in outcome_tuple):
        issues.append(
            _issue(
                "NO_SAFE_FACTS",
                "В отчёте нет ни одного безопасного подтверждённого поля",
            )
        )

    final_issues = _deduplicate_issues(issues)
    unavailable_codes = {"CHECKER_INTERNAL_ERROR", "AUDIT_WRITE_FAILED"}
    if any(issue.code in unavailable_codes for issue in final_issues):
        verdict = ReportVerdict.CHECKER_UNAVAILABLE
    elif any(issue.blocking for issue in final_issues):
        verdict = ReportVerdict.BLOCKED
    elif final_issues or any(
        outcome.state is not ClaimState.MATCH for outcome in outcome_tuple
    ):
        verdict = ReportVerdict.VERIFIED_WITH_LIMITATIONS
    else:
        verdict = ReportVerdict.VERIFIED

    return ReportCheckResult(
        check_id=str(uuid.uuid4()),
        verdict=verdict,
        checked_at=now,
        outcomes=outcome_tuple,
        issues=final_issues,
        manifest_sha256=request.manifest_sha256,
        audit_persisted=False,
    )


def replace_issue_for_claim(issue: CheckIssue, claim: FactClaim) -> CheckIssue:
    return CheckIssue(
        code=issue.code,
        message=issue.message,
        blocking=claim.required,
        source=issue.source,
        claim_id=claim.claim_id,
    )


def _is_sha256(value: str) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _bundle_state_digest(evidence: EvidenceBundle) -> str:
    return hashlib.sha256(
        canonical_json(
            (
                evidence.facebook_sha256,
                evidence.trello_sha256,
                evidence.media_sha256,
                evidence.amo_sha256,
                evidence.cdp_sha256,
            )
        )
    ).hexdigest()


def _find_record(
    records: tuple[EvidenceRecord, ...],
    *,
    source: SourceSystem,
    subject: SubjectRef,
    metric: Metric,
) -> EvidenceRecord | None:
    matches = [
        record
        for record in records
        if record.source is source
        and record.subject == subject
        and record.metric is metric
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _find_entity_record(
    records: tuple[EvidenceRecord, ...],
    *,
    source: SourceSystem,
    kind: SubjectKind,
    subject_id: str,
    metric: Metric,
) -> EvidenceRecord | None:
    """Запись по СУЩНОСТИ — без сверки родителя, но с требованием единственности.

    Нужен там, где манифест не несёт родителя, а источник его пишет: SubjectRef
    сравнивается целиком, включая parent_id, поэтому SubjectRef(ADSET, id) НИКОГДА
    не совпадёт с записанным SubjectRef(ADSET, id, account_id). Из-за этого любое
    одобренное изменение бюджета падало на SCALE_BUDGET_DRIFT ещё до того, как
    дойти до Facebook.

    Строгость сохранена: совпасть должна РОВНО одна запись. Два кабинета с одним
    adset_id дадут неоднозначность → None → отказ, а не случайный выбор.
    """
    matches = [
        record
        for record in records
        if record.source is source
        and record.subject.kind is kind
        and record.subject.subject_id == subject_id
        and record.metric is metric
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _action_subject_ids(item: ActionManifest) -> tuple[str, ...]:
    if isinstance(item, LaunchManifest):
        return (f"card:{item.trello.card_id}",)
    if isinstance(item, (PauseManifest, UnpauseManifest)):
        return (f"ad:{item.ad_id}",)
    if isinstance(item, AssetRecoveryManifest):
        return (f"adset:{item.target_adset_id}",)
    return (f"adset:{item.adset_id}",)


def _append_status_issue(
    issues: list[CheckIssue],
    record: EvidenceRecord | None,
    expected: str,
    code: str,
) -> None:
    if (
        record is None
        or record.state is not EvidenceState.FRESH_COMPLETE
        or record.value != expected
    ):
        issues.append(
            _issue(
                code,
                f"Live статус не равен ожидаемому {expected}",
                source=SourceSystem.FACEBOOK,
            )
        )


def _launch_issues(
    item: LaunchManifest, records: tuple[EvidenceRecord, ...]
) -> tuple[CheckIssue, ...]:
    issues: list[CheckIssue] = []
    card_record = _find_record(
        records,
        source=SourceSystem.TRELLO,
        subject=SubjectRef(SubjectKind.CARD, item.trello.card_id),
        metric=Metric.MATCH_STATE,
    )
    if card_record is None or card_record.value not in {
        "MATCH",
        "EXACT",
        item.trello.card_content_sha256,
    }:
        issues.append(
            _issue(
                "LAUNCH_INPUT_DRIFT",
                "Карточка Trello изменилась",
                source=SourceSystem.TRELLO,
            )
        )

    for asset in item.media_assets:
        # Сводка подписывает медиа-субъекты родителем-манифестом
        # (approval_sources._action_subjects: SubjectRef(MEDIA, asset_id,
        # item.manifest_id)), а равенство SubjectRef включает parent_id.
        # Поиск без родителя не находил запись НИКОГДА — каждый запуск падал
        # «Media bytes изменились» при идеально совпадающих хешах.
        media_record = _find_record(
            records,
            source=SourceSystem.MEDIA_BYTES,
            subject=SubjectRef(SubjectKind.MEDIA, asset.asset_id, item.manifest_id),
            metric=Metric.MATCH_STATE,
        )
        if media_record is None or media_record.value not in {
            "MATCH",
            "EXACT",
            asset.content_sha256,
        }:
            issues.append(
                _issue(
                    "LAUNCH_INPUT_DRIFT",
                    "Media bytes изменились",
                    source=SourceSystem.MEDIA_BYTES,
                )
            )

    for destination in item.destinations:
        subject = SubjectRef(
            SubjectKind.ADSET, destination.adset_id, destination.account_id
        )
        budget_record = _find_record(
            records,
            source=SourceSystem.FACEBOOK,
            subject=subject,
            metric=Metric.DAILY_BUDGET,
        )
        capacity_record = _find_record(
            records,
            source=SourceSystem.FACEBOOK,
            subject=subject,
            metric=Metric.CAPACITY,
        )
        duplicate_record = _find_record(
            records,
            source=SourceSystem.FACEBOOK,
            subject=subject,
            metric=Metric.MATCH_STATE,
        )
        if (
            budget_record is None
            or budget_record.value != destination.current_daily_budget
            or budget_record.currency != destination.currency
        ):
            issues.append(
                _issue(
                    "LAUNCH_SCOPE_DRIFT",
                    "Бюджет или валюта adset изменились",
                    source=SourceSystem.FACEBOOK,
                )
            )
        expected_count = len(destination.creatives)
        required_slots = expected_count + destination.hard_reserve_slots
        # Live capacity сверяется предикатом достаточности, а не равенством с
        # манифестом: сосед-claim того же прогона легитимно занимает слот, и
        # равенство превращало каждый следующий claim адсета в терминальный
        # отказ (однажды из сотен предложений до запуска дошли единицы). Проверка «слотов
        # хватает на запуск и резерв» и есть вся защита, которую давало
        # равенство; отсутствие или нечитаемость live-снимка — по-прежнему
        # отказ (fail-closed), нехватка живых слотов — отдельный код, который
        # executor возвращает в повтор, а не сжигает одобрение.
        if capacity_record is None:
            issues.append(
                _issue(
                    "LAUNCH_SCOPE_DRIFT",
                    "Нет live-снимка capacity adset",
                    source=SourceSystem.FACEBOOK,
                )
            )
        else:
            try:
                live_capacity = int(capacity_record.value)
            except (TypeError, ValueError):
                live_capacity = None
            if live_capacity is None:
                issues.append(
                    _issue(
                        "LAUNCH_SCOPE_DRIFT",
                        "Live capacity adset нечитаема",
                        source=SourceSystem.FACEBOOK,
                    )
                )
            elif live_capacity < required_slots:
                issues.append(
                    _issue(
                        "LAUNCH_CAPACITY_INSUFFICIENT",
                        "Живых слотов не хватает на запуск и обязательный резерв",
                        source=SourceSystem.FACEBOOK,
                    )
                )
        if destination.capacity_available < required_slots:
            issues.append(
                _issue(
                    "LAUNCH_CAPACITY_INSUFFICIENT",
                    "Capacity не покрывает запуск и обязательный резерв",
                    source=SourceSystem.FACEBOOK,
                )
            )
        if duplicate_record is None or duplicate_record.value not in {
            "NO_DUPLICATE",
            "CLEAR",
        }:
            issues.append(
                _issue(
                    "LAUNCH_DUPLICATE",
                    "Не доказано отсутствие полного или частичного дубля",
                    source=SourceSystem.FACEBOOK,
                )
            )
    return _deduplicate_issues(issues)


def _pause_issues(
    item: PauseManifest,
    records: tuple[EvidenceRecord, ...],
    evidence: EvidenceBundle,
) -> tuple[CheckIssue, ...]:
    issues: list[CheckIssue] = []
    target_subject = SubjectRef(SubjectKind.AD, item.ad_id, item.adset_id)
    target_status = _find_record(
        records,
        source=SourceSystem.FACEBOOK,
        subject=target_subject,
        metric=Metric.EFFECTIVE_STATUS,
    )
    _append_status_issue(issues, target_status, "ACTIVE", "PAUSE_TARGET_CHANGED")
    if item.pre_inventory_sha256 != evidence.facebook_sha256:
        issues.append(
            _issue(
                "INVENTORY_CHANGED",
                "Полный inventory изменился",
                source=SourceSystem.FACEBOOK,
            )
        )

    active_sibling_ids = {
        record.subject.subject_id
        for record in records
        if (
            record.source is SourceSystem.FACEBOOK
            and record.metric is Metric.EFFECTIVE_STATUS
            and record.subject.kind is SubjectKind.AD
            and record.subject.parent_id == item.adset_id
            and record.subject.subject_id != item.ad_id
            and record.state is EvidenceState.FRESH_COMPLETE
            and record.value == "ACTIVE"
        )
    }
    replacement_active = (
        item.replacement_ad_id is not None
        and item.replacement_ad_id in active_sibling_ids
    )
    if not active_sibling_ids and not replacement_active:
        issues.append(
            _issue(
                "LAST_EFFECTIVE_ACTIVE",
                "После паузы не останется ACTIVE рекламы",
                source=SourceSystem.FACEBOOK,
            )
        )
    if item.sibling_active_ids and set(item.sibling_active_ids) != active_sibling_ids:
        issues.append(
            _issue(
                "INVENTORY_CHANGED",
                "Список ACTIVE siblings изменился",
                source=SourceSystem.FACEBOOK,
            )
        )
    return _deduplicate_issues(issues)


def _unpause_issues(
    item: UnpauseManifest, records: tuple[EvidenceRecord, ...]
) -> tuple[CheckIssue, ...]:
    issues: list[CheckIssue] = []
    status = _find_record(
        records,
        source=SourceSystem.FACEBOOK,
        subject=SubjectRef(SubjectKind.AD, item.ad_id, item.adset_id),
        metric=Metric.EFFECTIVE_STATUS,
    )
    _append_status_issue(
        issues, status, item.expected_before_status, "UNPAUSE_TARGET_CHANGED"
    )
    return tuple(issues)


def _scale_issues(
    item: ScaleManifest, records: tuple[EvidenceRecord, ...]
) -> tuple[CheckIssue, ...]:
    issues: list[CheckIssue] = []
    # По сущности, а не по полному SubjectRef: ScaleManifest не несёт account_id,
    # а источник пишет адсет с ним в parent_id (см. _find_entity_record).
    status = _find_entity_record(
        records,
        source=SourceSystem.FACEBOOK,
        kind=SubjectKind.ADSET,
        subject_id=item.adset_id,
        metric=Metric.EFFECTIVE_STATUS,
    )
    budget = _find_entity_record(
        records,
        source=SourceSystem.FACEBOOK,
        kind=SubjectKind.ADSET,
        subject_id=item.adset_id,
        metric=Metric.DAILY_BUDGET,
    )
    _append_status_issue(issues, status, "ACTIVE", "SCALE_TARGET_CHANGED")
    if (
        budget is None
        or budget.value != item.current_budget
        or budget.currency != item.currency
    ):
        issues.append(
            _issue(
                "SCALE_BUDGET_DRIFT",
                "Live бюджет или валюта изменились",
                source=SourceSystem.FACEBOOK,
            )
        )
    return tuple(issues)


def _checker_state_issues(
    records: tuple[EvidenceRecord, ...],
) -> tuple[CheckIssue, ...]:
    issues: list[CheckIssue] = []
    healthy_values = {"OK", "HEALTHY", "ENABLED", "CLEAR", "NO_PENDING"}
    for record in records:
        if (
            record.metric is Metric.CHECKER_HEALTH
            and str(record.value).upper() not in healthy_values
        ):
            issues.append(
                _issue(
                    "CHECKER_UNAVAILABLE",
                    "Checker отключён или нездоров",
                    source=record.source,
                )
            )
        if (
            record.metric is Metric.HISTORY_STATE
            and str(record.value).upper() not in healthy_values
        ):
            issues.append(
                _issue(
                    "PENDING_RECONCILIATION",
                    "Есть незавершённая reconciliation",
                    source=record.source,
                )
            )
    return _deduplicate_issues(issues)


def evaluate_action_item(
    batch: ActionBatchManifest,
    item: ActionManifest,
    item_index: int,
    evidence: EvidenceBundle,
    now: datetime,
) -> ActionReview:
    """Возвращает только SAFE/DENIED; это не является согласием владельца."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")

    issues: list[CheckIssue] = []
    sources, source_issues = _sources_by_system(evidence)
    freshness, freshness_issues = _freshness_by_system(evidence)
    issues.extend(source_issues)
    issues.extend(freshness_issues)
    records = _all_records(sources)

    if (
        item_index < 0
        or item_index >= len(batch.actions)
        or batch.actions[item_index] != item
    ):
        issues.append(
            _issue("SEQUENCE_INVALID", "Item не находится на заявленной позиции batch")
        )
    if batch.manifest_sha256 != manifest_sha256(batch):
        issues.append(_issue("PERMIT_INVALID", "Batch manifest hash не совпадает"))
    if not _action_subject_ids(item) or not set(_action_subject_ids(item)).issubset(
        batch.subject_ids
    ):
        issues.append(
            _issue("INVALID_CONTRACT", "Batch не содержит exact subject текущего item")
        )

    ttl_seconds = 300 if item.kind is ActionKind.LAUNCH else 120
    age_seconds = (now - item.prepared_at).total_seconds()
    if age_seconds < 0 or age_seconds > ttl_seconds:
        issues.append(
            _issue("PERMIT_INVALID", "Action manifest просрочен или создан в будущем")
        )

    required_sources = (
        frozenset({SourceSystem.FACEBOOK})
        if isinstance(item, AssetRecoveryManifest)
        else _ACTION_REQUIRED_SOURCES
    )
    for source in sorted(required_sources, key=lambda value: value.value):
        problem = _source_problem(
            source,
            sources.get(source),
            freshness.get(source),
            action=True,
            blocking=True,
        )
        if problem is not None:
            issues.append(problem)

    component_hashes = (
        evidence.facebook_sha256,
        evidence.trello_sha256,
        evidence.media_sha256,
        evidence.amo_sha256,
        evidence.cdp_sha256,
    )
    if not all(_is_sha256(value) for value in component_hashes):
        issues.append(
            _issue("ACTION_EVIDENCE_INVALID", "Один из пяти source digest неверен")
        )
    elif evidence.final_live_state_sha256 != _bundle_state_digest(evidence):
        issues.append(
            _issue("ACTION_EVIDENCE_INVALID", "Итоговый live digest не совпадает")
        )

    fact_claims: tuple[FactClaim, ...] = ()
    if isinstance(item, PauseManifest):
        fact_claims = item.facts
    elif isinstance(item, ScaleManifest):
        fact_claims = item.facts
    claim_outcomes: list[ClaimOutcome] = []
    for claim in fact_claims:
        contract_issue = _source_contract_issue(claim)
        if contract_issue is not None:
            issues.append(contract_issue)
        record, duplicate_issues = _record_for_claim(claim, records)
        issues.extend(duplicate_issues)
        outcome = compare_claim(claim, record)
        claim_outcomes.append(outcome)
        issues.extend(outcome.issues)
        cdp = sources.get(SourceSystem.CDP_ERP)
        issues.extend(
            _payment_entity_issues(claim, cdp.payments if cdp is not None else ())
        )
    issues.extend(_payments_without_revenue(fact_claims))
    issues.extend(_derived_metric_issues(fact_claims, tuple(claim_outcomes)))

    if isinstance(item, LaunchManifest):
        issues.extend(_launch_issues(item, records))
    elif isinstance(item, PauseManifest):
        issues.extend(_pause_issues(item, records, evidence))
    elif isinstance(item, UnpauseManifest):
        issues.extend(_unpause_issues(item, records))
    elif isinstance(item, ScaleManifest):
        issues.extend(_scale_issues(item, records))
    elif isinstance(item, AssetRecoveryManifest):
        # Exact source/target/capacity validation выполнена двумя live reads
        # approval_source_facebook; digest связывает permit с этим snapshot.
        pass
    else:  # pragma: no cover - union закрыт, ветка защищает runtime от чужого объекта
        issues.append(_issue("INVALID_CONTRACT", "Неизвестный ActionManifest"))
    issues.extend(_checker_state_issues(records))

    final_issues = _deduplicate_issues(issues)
    safe = not final_issues
    checked_at = now
    expires_at = checked_at + timedelta(seconds=ttl_seconds) if safe else None
    return ActionReview(
        check_id=str(uuid.uuid4()),
        operation_id=batch.idempotency_key,
        idempotency_key=batch.idempotency_key,
        batch_manifest_id=batch.batch_manifest_id,
        item_id=item.manifest_id,
        item_index=item_index,
        action_kind=item.kind,
        decision=SafetyDecision.SAFE if safe else SafetyDecision.DENIED,
        checked_at=checked_at,
        expires_at=expires_at,
        batch_manifest_sha256=batch.manifest_sha256,
        item_manifest_sha256=manifest_sha256(item),
        evidence_state_sha256=evidence.final_live_state_sha256 if safe else None,
        subject_ids=_action_subject_ids(item),
        issues=final_issues,
        audit_persisted=False,
    )
