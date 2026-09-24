from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from services.approval_checker_models import (
    ActionBatchManifest,
    ActionKind,
    ActionOrigin,
    ApprovalDecision,
    ClaimState,
    EvidenceBundle,
    EvidenceRecord,
    EvidenceState,
    FactCategory,
    FactClaim,
    FieldFormat,
    FieldLabelTemplate,
    Metric,
    PaymentEvidence,
    ReportCheckRequest,
    ReportField,
    ReportSection,
    ReportTemplate,
    ReportVerdict,
    CreativeSpec,
    LaunchDestination,
    LaunchManifest,
    MediaAssetSpec,
    MediaType,
    PlacementRole,
    ScaleManifest,
    TrelloPrecondition,
    SectionTemplate,
    SourceEvidence,
    SourceFreshness,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    TypedReportPayload,
    UnpauseManifest,
    canonical_json,
    manifest_sha256,
    report_manifest_sha256,
)
from services.approval_rules import (
    RULE_ISSUE_CODES,
    _launch_issues,
    compare_claim,
    evaluate_action_item,
    evaluate_report,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
AD = SubjectRef(SubjectKind.AD, "ad-1", "adset-1")
ADSET = SubjectRef(SubjectKind.ADSET, "adset-1")
FACEBOOK_STATE_SHA256 = hashlib.sha256(SourceSystem.FACEBOOK.value.encode()).hexdigest()
FACEBOOK_WINDOW = TimeWindow(
    start=NOW - timedelta(days=7),
    end=NOW,
    timezone_name="Etc/GMT-5",
    semantic="trailing_7_closed_days",
)
OUTCOME_WINDOW = TimeWindow(
    start=NOW - timedelta(days=30),
    end=NOW,
    timezone_name="Etc/GMT-5",
    semantic="trailing_30_days",
)
ONLINE_WINDOW = TimeWindow(
    start=datetime(2026, 7, 15, tzinfo=timezone.utc),
    end=datetime(2026, 7, 22, tzinfo=timezone.utc),
    timezone_name="UTC",
    semantic="last_7_closed_days",
)


def _claim(
    claim_id: str,
    metric: Metric,
    value: int | Decimal | str | None,
    source: SourceSystem,
    *,
    subject: SubjectRef = AD,
    currency: str | None = None,
    required: bool = True,
) -> FactClaim:
    return FactClaim(
        claim_id=claim_id,
        field_id=claim_id,
        category=FactCategory.BUSINESS_METRIC,
        subject=subject,
        metric=metric,
        value=value,
        source=source,
        window=None,
        currency=currency,
        required=required,
    )


def _record(claim: FactClaim, value: int | Decimal | str | None) -> EvidenceRecord:
    return EvidenceRecord(
        category=claim.category,
        subject=claim.subject,
        metric=claim.metric,
        value=value,
        source=claim.source,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=NOW,
        window=claim.window,
        currency=claim.currency,
    )


def _report_request(claims: tuple[FactClaim, ...]) -> ReportCheckRequest:
    labels = {
        Metric.PAYMENTS: FieldLabelTemplate.PAYMENTS,
        Metric.REVENUE: FieldLabelTemplate.REVENUE,
        Metric.DRR_PCT: FieldLabelTemplate.DRR,
        Metric.ACCURACY_PCT: FieldLabelTemplate.ACCURACY,
        Metric.PRODUCT_SHARE_PCT: FieldLabelTemplate.PRODUCT_SHARE,
        Metric.DECISION_COUNT: FieldLabelTemplate.ACTION_COUNT,
        Metric.SPEND: FieldLabelTemplate.SPEND,
        Metric.LEADS: FieldLabelTemplate.LEADS,
        Metric.QUALS: FieldLabelTemplate.QUALS,
        Metric.QUAL_PCT: FieldLabelTemplate.QUALS,
        Metric.DISPLAY_CONTEXT: FieldLabelTemplate.DISPLAY_NAME,
        Metric.MATCH_STATE: FieldLabelTemplate.MATCH,
    }
    formats = {
        Metric.PAYMENTS: FieldFormat.INTEGER,
        Metric.DECISION_COUNT: FieldFormat.INTEGER,
        Metric.LEADS: FieldFormat.INTEGER,
        Metric.QUALS: FieldFormat.INTEGER,
        Metric.QUAL_PCT: FieldFormat.PERCENT,
        Metric.SPEND: FieldFormat.USD,
        Metric.REVENUE: FieldFormat.LCY,
        Metric.DRR_PCT: FieldFormat.PERCENT,
        Metric.ACCURACY_PCT: FieldFormat.PERCENT,
        Metric.PRODUCT_SHARE_PCT: FieldFormat.PERCENT,
        Metric.DISPLAY_CONTEXT: FieldFormat.TEXT,
        Metric.MATCH_STATE: FieldFormat.STATUS,
    }
    fields = tuple(
        ReportField(
            field_id=claim.field_id or claim.claim_id,
            section_id="summary",
            category=claim.category,
            label=labels[claim.metric],
            format=formats[claim.metric],
            subject=claim.subject,
            metric=claim.metric,
            value=claim.value,
            source=claim.source,
            window=claim.window,
            currency=claim.currency,
            required=claim.required,
        )
        for claim in claims
    )
    payload = TypedReportPayload(
        template=ReportTemplate.AUTOPILOT,
        sections=(
            ReportSection(
                "summary",
                SectionTemplate.SUMMARY,
                tuple(field.field_id for field in fields),
                None,
            ),
        ),
        fields=fields,
        generated_at=NOW,
        mixed_windows_explicit=False,
    )
    return ReportCheckRequest(
        correlation_id="report-1",
        payload=payload,
        claims=claims,
        manifest_sha256=report_manifest_sha256(payload, claims),
    )


def _source(
    source: SourceSystem,
    records: tuple[EvidenceRecord, ...] = (),
    *,
    payments: tuple[PaymentEvidence, ...] = (),
    state: EvidenceState = EvidenceState.FRESH_COMPLETE,
    from_cache: bool = False,
    complete: bool = True,
) -> SourceEvidence:
    return SourceEvidence(
        source=source,
        state=state,
        fetched_at=NOW,
        data_as_of=NOW,
        from_cache=from_cache,
        complete=complete,
        records=records,
        payments=payments,
    )


def _freshness(
    source: SourceSystem,
    *,
    from_cache: bool = False,
    fresh: bool = True,
    complete: bool = True,
) -> SourceFreshness:
    return SourceFreshness(
        source=source,
        fetched_at=NOW,
        observed_at=NOW,
        data_as_of=NOW,
        max_age_seconds=60,
        from_cache=from_cache,
        complete=complete,
        fresh=fresh,
    )


def _bundle(
    sources: tuple[SourceEvidence, ...], freshness: tuple[SourceFreshness, ...]
) -> EvidenceBundle:
    hashes = tuple(
        hashlib.sha256(source.value.encode()).hexdigest()
        for source in (
            SourceSystem.FACEBOOK,
            SourceSystem.TRELLO,
            SourceSystem.MEDIA_BYTES,
            SourceSystem.AMO,
            SourceSystem.CDP_ERP,
        )
    )
    final_digest = hashlib.sha256(canonical_json(hashes)).hexdigest()
    return EvidenceBundle(
        loaded_at=NOW,
        sources=sources,
        freshness=freshness,
        facebook_sha256=hashes[0],
        trello_sha256=hashes[1],
        media_sha256=hashes[2],
        amo_sha256=hashes[3],
        cdp_sha256=hashes[4],
        final_live_state_sha256=final_digest,
        local_state_sha256=None,
    )


def _all_action_sources(
    facebook_records: tuple[EvidenceRecord, ...],
    *,
    amo_records: tuple[EvidenceRecord, ...] = (),
    cdp_records: tuple[EvidenceRecord, ...] = (),
    payments: tuple[PaymentEvidence, ...] = (),
    cached_source: SourceSystem | None = None,
) -> EvidenceBundle:
    source_systems = (
        SourceSystem.FACEBOOK,
        SourceSystem.TRELLO,
        SourceSystem.MEDIA_BYTES,
        SourceSystem.AMO,
        SourceSystem.CDP_ERP,
    )
    records_by_source = {
        SourceSystem.FACEBOOK: facebook_records,
        SourceSystem.AMO: amo_records,
        SourceSystem.CDP_ERP: cdp_records,
    }
    sources = tuple(
        _source(
            source,
            records_by_source.get(source, ()),
            payments=payments if source is SourceSystem.CDP_ERP else (),
            from_cache=source is cached_source,
        )
        for source in source_systems
    )
    freshness = tuple(
        _freshness(source, from_cache=source is cached_source)
        for source in source_systems
    )
    return _bundle(sources, freshness)


def _batch(item: UnpauseManifest | ScaleManifest) -> ActionBatchManifest:
    subject_id = (
        f"ad:{item.ad_id}"
        if isinstance(item, UnpauseManifest)
        else f"adset:{item.adset_id}"
    )
    batch = ActionBatchManifest(
        batch_manifest_id="batch-1",
        correlation_id="correlation-1",
        idempotency_key=str(uuid.uuid4()),
        prepared_at=item.prepared_at,
        subject_ids=(subject_id,),
        actions=(item,),
        manifest_sha256="0" * 64,
    )
    return replace(batch, manifest_sha256=manifest_sha256(batch))


def test_rule_table_covers_r01_through_r33() -> None:
    assert tuple(RULE_ISSUE_CODES) == tuple(f"R{number:02d}" for number in range(1, 34))
    assert all(RULE_ISSUE_CODES.values())


def test_compare_claim_does_not_coerce_missing_value_to_zero() -> None:
    claim = _claim("leads", Metric.LEADS, 0, SourceSystem.FACEBOOK)

    outcome = compare_claim(claim, None)

    assert outcome.state is ClaimState.NOT_VERIFIABLE
    assert {issue.code for issue in outcome.issues} == {"NULL_COERCED_TO_ZERO"}


def test_compare_claim_rejects_wrong_window_currency_or_subject() -> None:
    claim = _claim(
        "spend", Metric.SPEND, Decimal("10"), SourceSystem.FACEBOOK, currency="USD"
    )
    wrong_record = replace(_record(claim, Decimal("10")), currency="LCY")

    outcome = compare_claim(claim, wrong_record)

    assert outcome.state is ClaimState.MISMATCH
    assert {issue.code for issue in outcome.issues} == {"FACT_MISMATCH"}


def test_payments_without_positive_revenue_blocks_report() -> None:
    payments_claim = _claim(
        "payments", Metric.PAYMENTS, 1, SourceSystem.CDP_ERP, currency="LCY"
    )
    revenue_claim = _claim(
        "revenue", Metric.REVENUE, Decimal("0"), SourceSystem.CDP_ERP, currency="LCY"
    )
    payment = PaymentEvidence(
        payment_id="payment-1",
        contract_number="contract-1",
        amo_lead_id=10,
        fb_ad_id="ad-1",
        amount_lcy=Decimal("100000"),
        direction="INCOME",
        contract_net_lcy=Decimal("100000"),
        document_at=NOW - timedelta(hours=1),
        fetched_at=NOW,
    )
    request = _report_request((payments_claim, revenue_claim))
    evidence = _bundle(
        (
            _source(
                SourceSystem.CDP_ERP,
                (_record(payments_claim, 1), _record(revenue_claim, Decimal("0"))),
                payments=(payment,),
            ),
        ),
        (_freshness(SourceSystem.CDP_ERP),),
    )

    result = evaluate_report(request, evidence, NOW)

    assert result.verdict is ReportVerdict.BLOCKED
    assert "PAYMENTS_WITHOUT_REVENUE" in {issue.code for issue in result.issues}


def test_missing_payment_entity_blocks_report_even_when_count_matches() -> None:
    payments_claim = _claim(
        "payments", Metric.PAYMENTS, 1, SourceSystem.CDP_ERP, currency="LCY"
    )
    revenue_claim = _claim(
        "revenue", Metric.REVENUE, Decimal("100"), SourceSystem.CDP_ERP, currency="LCY"
    )
    request = _report_request((payments_claim, revenue_claim))
    evidence = _bundle(
        (
            _source(
                SourceSystem.CDP_ERP,
                (_record(payments_claim, 1), _record(revenue_claim, Decimal("100"))),
            ),
        ),
        (_freshness(SourceSystem.CDP_ERP),),
    )

    result = evaluate_report(request, evidence, NOW)

    assert result.verdict is ReportVerdict.BLOCKED
    assert "PAYMENT_ENTITY_MISSING" in {issue.code for issue in result.issues}


def test_cdp_online_drr_with_exact_mode_and_coverage_is_verified() -> None:
    subject = SubjectRef(SubjectKind.ACCOUNT, "ONLINE")
    claim = replace(
        _claim(
            "online-drr",
            Metric.DRR_PCT,
            Decimal("10"),
            SourceSystem.CDP_ERP,
            subject=subject,
        ),
        window=ONLINE_WINDOW,
    )
    coverage = "coverage:complete:2026-07-15..2026-07-21"
    drr_record = replace(
        _record(claim, Decimal("10")),
        entity_ids=(coverage, "mode:WEIGHTED_FALLBACK", "rows:7"),
    )
    mode_record = EvidenceRecord(
        category=FactCategory.DISPLAY_CONTEXT,
        subject=subject,
        metric=Metric.MATCH_STATE,
        value="WEIGHTED_FALLBACK",
        source=SourceSystem.CDP_ERP,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=NOW,
        window=ONLINE_WINDOW,
        currency=None,
        entity_ids=(coverage, "mode:WEIGHTED_FALLBACK", "rows:7"),
    )
    evidence = _bundle(
        (_source(SourceSystem.CDP_ERP, (drr_record, mode_record)),),
        (_freshness(SourceSystem.CDP_ERP),),
    )

    result = evaluate_report(_report_request((claim,)), evidence, NOW)

    assert result.verdict is ReportVerdict.VERIFIED
    assert result.issues == ()


def test_cdp_online_drr_without_mode_proof_is_blocked() -> None:
    subject = SubjectRef(SubjectKind.ACCOUNT, "ONLINE")
    drr_claim = replace(
        _claim(
            "online-drr",
            Metric.DRR_PCT,
            Decimal("10"),
            SourceSystem.CDP_ERP,
            subject=subject,
        ),
        window=ONLINE_WINDOW,
    )
    spend_claim = replace(
        _claim(
            "online-spend",
            Metric.SPEND,
            Decimal("100"),
            SourceSystem.CDP_ERP,
            subject=subject,
            currency="USD",
        ),
        window=ONLINE_WINDOW,
    )
    revenue_claim = replace(
        _claim(
            "online-revenue",
            Metric.REVENUE,
            Decimal("500000"),
            SourceSystem.CDP_ERP,
            subject=subject,
            currency="LCY",
        ),
        window=ONLINE_WINDOW,
    )
    claims = (spend_claim, revenue_claim, drr_claim)
    proof_without_mode = (
        "coverage:complete:2026-07-15..2026-07-21",
        "rows:7",
    )
    records = tuple(
        replace(_record(claim, claim.value), entity_ids=proof_without_mode)
        for claim in claims
    )
    evidence = _bundle(
        (_source(SourceSystem.CDP_ERP, records),),
        (_freshness(SourceSystem.CDP_ERP),),
    )

    result = evaluate_report(_report_request(claims), evidence, NOW)

    assert result.verdict is ReportVerdict.BLOCKED
    assert "DERIVED_INPUT_NOT_VERIFIED" in {issue.code for issue in result.issues}


def test_cdp_online_full_exact_aggregate_matches() -> None:
    subject = SubjectRef(SubjectKind.ACCOUNT, "ONLINE")
    claim_specs = (
        (
            "online-context",
            FactCategory.DISPLAY_CONTEXT,
            Metric.DISPLAY_CONTEXT,
            "2026-07-15..2026-07-21",
            None,
        ),
        (
            "online-spend-usd",
            FactCategory.BUSINESS_METRIC,
            Metric.SPEND,
            Decimal("100"),
            "USD",
        ),
        (
            "online-spend-lcy",
            FactCategory.BUSINESS_METRIC,
            Metric.SPEND,
            Decimal("50000"),
            "LCY",
        ),
        ("online-leads", FactCategory.BUSINESS_METRIC, Metric.LEADS, 10, None),
        ("online-quals", FactCategory.BUSINESS_METRIC, Metric.QUALS, 2, None),
        (
            "online-qual-pct",
            FactCategory.BUSINESS_METRIC,
            Metric.QUAL_PCT,
            Decimal("20"),
            None,
        ),
        (
            "online-revenue",
            FactCategory.BUSINESS_METRIC,
            Metric.REVENUE,
            Decimal("500000"),
            "LCY",
        ),
        (
            "online-drr",
            FactCategory.BUSINESS_METRIC,
            Metric.DRR_PCT,
            Decimal("10"),
            None,
        ),
        (
            "online-mode",
            FactCategory.DISPLAY_CONTEXT,
            Metric.MATCH_STATE,
            "EXACT_REVENUE",
            None,
        ),
    )
    claims = tuple(
        replace(
            _claim(
                claim_id,
                metric,
                value,
                SourceSystem.CDP_ERP,
                subject=subject,
                currency=currency,
            ),
            category=category,
            window=ONLINE_WINDOW,
        )
        for claim_id, category, metric, value, currency in claim_specs
    )
    proof = (
        "coverage:complete:2026-07-15..2026-07-21",
        "mode:EXACT_REVENUE",
        "rows:7",
    )
    records = tuple(
        replace(_record(claim, claim.value), entity_ids=proof) for claim in claims
    )
    evidence = _bundle(
        (_source(SourceSystem.CDP_ERP, records),),
        (_freshness(SourceSystem.CDP_ERP),),
    )

    result = evaluate_report(_report_request(claims), evidence, NOW)

    assert result.verdict is ReportVerdict.VERIFIED
    assert all(outcome.state is ClaimState.MATCH for outcome in result.outcomes)


def test_cdp_online_metric_with_wrong_subject_is_blocked() -> None:
    subject = SubjectRef(SubjectKind.ACCOUNT, "BRANCH")
    claim = replace(
        _claim(
            "online-leads",
            Metric.LEADS,
            10,
            SourceSystem.CDP_ERP,
            subject=subject,
        ),
        window=ONLINE_WINDOW,
    )
    record = replace(
        _record(claim, 10),
        entity_ids=("coverage:complete:2026-07-15..2026-07-21",),
    )
    evidence = _bundle(
        (_source(SourceSystem.CDP_ERP, (record,)),),
        (_freshness(SourceSystem.CDP_ERP),),
    )

    result = evaluate_report(_report_request((claim,)), evidence, NOW)

    assert result.verdict is ReportVerdict.BLOCKED
    assert "SOURCE_CONTRACT_MISMATCH" in {issue.code for issue in result.issues}


def test_cdp_online_metric_with_wrong_window_is_blocked() -> None:
    subject = SubjectRef(SubjectKind.ACCOUNT, "ONLINE")
    claim = replace(
        _claim(
            "online-leads",
            Metric.LEADS,
            10,
            SourceSystem.CDP_ERP,
            subject=subject,
        ),
        window=ONLINE_WINDOW,
    )
    other_window = TimeWindow(
        ONLINE_WINDOW.start - timedelta(days=1),
        ONLINE_WINDOW.end - timedelta(days=1),
        ONLINE_WINDOW.timezone_name,
        "different_7d",
    )
    record = replace(
        _record(claim, 10),
        window=other_window,
        entity_ids=("coverage:complete:2026-07-14..2026-07-20",),
    )
    evidence = _bundle(
        (_source(SourceSystem.CDP_ERP, (record,)),),
        (_freshness(SourceSystem.CDP_ERP),),
    )

    result = evaluate_report(_report_request((claim,)), evidence, NOW)

    assert result.verdict is ReportVerdict.BLOCKED
    assert {issue.code for issue in result.issues} & {
        "FACT_MISMATCH",
        "SOURCE_INCOMPLETE",
    }


def test_cdp_online_metric_with_wrong_currency_is_blocked() -> None:
    subject = SubjectRef(SubjectKind.ACCOUNT, "ONLINE")
    claim = replace(
        _claim(
            "online-spend",
            Metric.SPEND,
            Decimal("100"),
            SourceSystem.CDP_ERP,
            subject=subject,
            currency="USD",
        ),
        window=ONLINE_WINDOW,
    )
    record = replace(
        _record(claim, Decimal("100")),
        currency="LCY",
        entity_ids=("coverage:complete:2026-07-15..2026-07-21",),
    )
    evidence = _bundle(
        (_source(SourceSystem.CDP_ERP, (record,)),),
        (_freshness(SourceSystem.CDP_ERP),),
    )

    result = evaluate_report(_report_request((claim,)), evidence, NOW)

    assert result.verdict is ReportVerdict.BLOCKED
    assert "FACT_MISMATCH" in {issue.code for issue in result.issues}


def test_local_accuracy_product_share_and_decisions_match_exact_sources() -> None:
    accuracy = replace(
        _claim(
            "accuracy",
            Metric.ACCURACY_PCT,
            Decimal("33.3"),
            SourceSystem.CREATIVE_KB,
            subject=SubjectRef(SubjectKind.CREATIVE, "scale-accuracy"),
        ),
        window=FACEBOOK_WINDOW,
    )
    product_share = replace(
        _claim(
            "product-share",
            Metric.PRODUCT_SHARE_PCT,
            Decimal("66.7"),
            SourceSystem.CREATIVE_KB,
            subject=SubjectRef(SubjectKind.PRODUCT, "PRODA"),
        ),
        window=FACEBOOK_WINDOW,
    )
    decisions = replace(
        _claim(
            "decisions",
            Metric.DECISION_COUNT,
            2,
            SourceSystem.DECISIONS_DB,
            subject=SubjectRef(SubjectKind.DECISION, "paused"),
        ),
        window=FACEBOOK_WINDOW,
    )
    evidence = _bundle(
        (
            _source(
                SourceSystem.CREATIVE_KB,
                (
                    _record(accuracy, Decimal("33.3")),
                    _record(product_share, Decimal("66.7")),
                ),
            ),
            _source(SourceSystem.DECISIONS_DB, (_record(decisions, 2),)),
        ),
        (
            _freshness(SourceSystem.CREATIVE_KB),
            _freshness(SourceSystem.DECISIONS_DB),
        ),
    )

    result = evaluate_report(
        _report_request((accuracy, product_share, decisions)), evidence, NOW
    )

    assert result.verdict is ReportVerdict.VERIFIED
    assert all(outcome.state is ClaimState.MATCH for outcome in result.outcomes)


def test_unpause_action_with_exact_live_sources_is_approved() -> None:
    item = UnpauseManifest(
        kind=ActionKind.UNPAUSE,
        manifest_id="unpause-1",
        origin=ActionOrigin.WEB,
        idempotency_key=str(uuid.uuid4()),
        prepared_at=NOW - timedelta(seconds=1),
        ad_id="ad-1",
        adset_id="adset-1",
        expected_before_status="PAUSED",
        expected_after_status="ACTIVE",
        pre_inventory_sha256=FACEBOOK_STATE_SHA256,
    )
    status_record = EvidenceRecord(
        category=FactCategory.ACTION_STATE,
        subject=AD,
        metric=Metric.EFFECTIVE_STATUS,
        value="PAUSED",
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=NOW,
        window=None,
        currency=None,
    )
    batch = _batch(item)
    evidence = _all_action_sources((status_record,))

    review = evaluate_action_item(batch, item, 0, evidence, NOW)

    assert review.decision is ApprovalDecision.APPROVED
    assert review.issues == ()
    assert review.expires_at == NOW + timedelta(seconds=120)


def test_action_any_issue_is_denied() -> None:
    item = UnpauseManifest(
        kind=ActionKind.UNPAUSE,
        manifest_id="unpause-1",
        origin=ActionOrigin.WEB,
        idempotency_key=str(uuid.uuid4()),
        prepared_at=NOW - timedelta(seconds=1),
        ad_id="ad-1",
        adset_id="adset-1",
        expected_before_status="PAUSED",
        expected_after_status="ACTIVE",
        pre_inventory_sha256=FACEBOOK_STATE_SHA256,
    )
    status_record = EvidenceRecord(
        category=FactCategory.ACTION_STATE,
        subject=AD,
        metric=Metric.EFFECTIVE_STATUS,
        value="PAUSED",
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=NOW,
        window=None,
        currency=None,
    )
    review = evaluate_action_item(
        _batch(item),
        item,
        0,
        _all_action_sources((status_record,), cached_source=SourceSystem.AMO),
        NOW,
    )

    assert review.decision is ApprovalDecision.DENIED
    assert review.expires_at is None
    assert "CACHED_ACTION_EVIDENCE" in {issue.code for issue in review.issues}


def test_payments_without_revenue_denies_scale_action() -> None:
    facebook_claim = replace(
        _claim(
            "spend",
            Metric.SPEND,
            Decimal("100"),
            SourceSystem.FACEBOOK,
            subject=ADSET,
            currency="USD",
        ),
        window=FACEBOOK_WINDOW,
    )
    amo_claim = replace(
        _claim("quals", Metric.QUALS, 1, SourceSystem.AMO),
        window=OUTCOME_WINDOW,
    )
    payments_claim = replace(
        _claim("payments", Metric.PAYMENTS, 1, SourceSystem.CDP_ERP, currency="LCY"),
        window=OUTCOME_WINDOW,
    )
    revenue_claim = replace(
        _claim(
            "revenue",
            Metric.REVENUE,
            Decimal("0"),
            SourceSystem.CDP_ERP,
            currency="LCY",
        ),
        window=OUTCOME_WINDOW,
    )
    item = ScaleManifest(
        kind=ActionKind.SCALE,
        manifest_id="scale-1",
        origin=ActionOrigin.BUDGET_SCALER,
        idempotency_key=str(uuid.uuid4()),
        prepared_at=NOW - timedelta(seconds=1),
        adset_id="adset-1",
        expected_status="ACTIVE",
        current_budget=Decimal("100"),
        target_budget=Decimal("110"),
        currency="USD",
        candidate_ad_ids=("ad-1",),
        facebook_window=FACEBOOK_WINDOW,
        outcome_window=OUTCOME_WINDOW,
        facts=(facebook_claim, amo_claim, payments_claim, revenue_claim),
    )
    status = EvidenceRecord(
        category=FactCategory.ACTION_STATE,
        subject=ADSET,
        metric=Metric.EFFECTIVE_STATUS,
        value="ACTIVE",
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=NOW,
        window=None,
        currency=None,
    )
    budget = replace(
        status, metric=Metric.DAILY_BUDGET, value=Decimal("100"), currency="USD"
    )
    payment = PaymentEvidence(
        payment_id="payment-1",
        contract_number="contract-1",
        amo_lead_id=10,
        fb_ad_id="ad-1",
        amount_lcy=Decimal("100000"),
        direction="INCOME",
        contract_net_lcy=Decimal("100000"),
        document_at=NOW - timedelta(hours=1),
        fetched_at=NOW,
    )
    evidence = _all_action_sources(
        (status, budget, _record(facebook_claim, Decimal("100"))),
        amo_records=(_record(amo_claim, 1),),
        cdp_records=(_record(payments_claim, 1), _record(revenue_claim, Decimal("0"))),
        payments=(payment,),
    )

    review = evaluate_action_item(_batch(item), item, 0, evidence, NOW)

    assert review.decision is ApprovalDecision.DENIED
    assert "PAYMENTS_WITHOUT_REVENUE" in {issue.code for issue in review.issues}


def test_scale_matches_budget_record_written_with_account_parent() -> None:
    """SCALE находит живой бюджет, записанный источником с parent_id кабинета.

    approval_source_facebook пишет адсет как SubjectRef(ADSET, adset_id,
    account_id), а ScaleManifest несёт только adset_id. SubjectRef сравнивается
    целиком, поэтому поиск по SubjectRef(ADSET, adset_id) не совпадал НИКОГДА:
    любое одобренное изменение бюджета падало на SCALE_BUDGET_DRIFT, не дойдя
    до Facebook. Тесты этого не ловили, потому что складывали записи без
    родителя — не так, как их пишет боевой источник.
    """
    adset_with_parent = SubjectRef(SubjectKind.ADSET, "adset-1", "act-777")
    facebook_claim = replace(
        _claim(
            "budget",
            Metric.DAILY_BUDGET,
            Decimal("100"),
            SourceSystem.FACEBOOK,
            currency="USD",
        ),
        window=FACEBOOK_WINDOW,
    )
    amo_claim = replace(
        _claim("quals", Metric.QUALS, 1, SourceSystem.AMO),
        window=OUTCOME_WINDOW,
    )
    payments_claim = replace(
        _claim("payments", Metric.PAYMENTS, 1, SourceSystem.CDP_ERP, currency="LCY"),
        window=OUTCOME_WINDOW,
    )
    revenue_claim = replace(
        _claim(
            "revenue",
            Metric.REVENUE,
            Decimal("500000"),
            SourceSystem.CDP_ERP,
            currency="LCY",
        ),
        window=OUTCOME_WINDOW,
    )
    item = ScaleManifest(
        kind=ActionKind.SCALE,
        manifest_id="scale-parent",
        origin=ActionOrigin.BUDGET_SCALER,
        idempotency_key=str(uuid.uuid4()),
        prepared_at=NOW - timedelta(seconds=1),
        adset_id="adset-1",
        expected_status="ACTIVE",
        current_budget=Decimal("100"),
        target_budget=Decimal("120"),
        currency="USD",
        candidate_ad_ids=("ad-1",),
        facebook_window=FACEBOOK_WINDOW,
        outcome_window=OUTCOME_WINDOW,
        facts=(facebook_claim, amo_claim, payments_claim, revenue_claim),
    )
    status = EvidenceRecord(
        category=FactCategory.ACTION_STATE,
        subject=adset_with_parent,
        metric=Metric.EFFECTIVE_STATUS,
        value="ACTIVE",
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=NOW,
        window=None,
        currency=None,
    )
    budget = replace(
        status, metric=Metric.DAILY_BUDGET, value=Decimal("100"), currency="USD"
    )
    evidence = _all_action_sources(
        (status, budget, _record(facebook_claim, Decimal("100")))
    )

    review = evaluate_action_item(_batch(item), item, 0, evidence, NOW)

    codes = {issue.code for issue in review.issues}
    assert "SCALE_BUDGET_DRIFT" not in codes
    assert "SCALE_TARGET_CHANGED" not in codes


def test_scale_still_denied_when_live_budget_really_drifted() -> None:
    """Настоящее расхождение бюджета по-прежнему валит SCALE.

    Смягчать проверку нельзя: если в кабинете уже другой бюджет, решение
    принималось по устаревшей картине.
    """
    adset_with_parent = SubjectRef(SubjectKind.ADSET, "adset-1", "act-777")
    facebook_claim = replace(
        _claim(
            "budget",
            Metric.DAILY_BUDGET,
            Decimal("100"),
            SourceSystem.FACEBOOK,
            currency="USD",
        ),
        window=FACEBOOK_WINDOW,
    )
    amo_claim = replace(
        _claim("quals", Metric.QUALS, 1, SourceSystem.AMO),
        window=OUTCOME_WINDOW,
    )
    payments_claim = replace(
        _claim("payments", Metric.PAYMENTS, 1, SourceSystem.CDP_ERP, currency="LCY"),
        window=OUTCOME_WINDOW,
    )
    revenue_claim = replace(
        _claim(
            "revenue",
            Metric.REVENUE,
            Decimal("500000"),
            SourceSystem.CDP_ERP,
            currency="LCY",
        ),
        window=OUTCOME_WINDOW,
    )
    item = ScaleManifest(
        kind=ActionKind.SCALE,
        manifest_id="scale-drift",
        origin=ActionOrigin.BUDGET_SCALER,
        idempotency_key=str(uuid.uuid4()),
        prepared_at=NOW - timedelta(seconds=1),
        adset_id="adset-1",
        expected_status="ACTIVE",
        current_budget=Decimal("100"),
        target_budget=Decimal("120"),
        currency="USD",
        candidate_ad_ids=("ad-1",),
        facebook_window=FACEBOOK_WINDOW,
        outcome_window=OUTCOME_WINDOW,
        facts=(facebook_claim, amo_claim, payments_claim, revenue_claim),
    )
    status = EvidenceRecord(
        category=FactCategory.ACTION_STATE,
        subject=adset_with_parent,
        metric=Metric.EFFECTIVE_STATUS,
        value="ACTIVE",
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=NOW,
        window=None,
        currency=None,
    )
    # В кабинете уже 150, а решение готовилось на 100.
    budget = replace(
        status, metric=Metric.DAILY_BUDGET, value=Decimal("150"), currency="USD"
    )
    evidence = _all_action_sources(
        (status, budget, _record(facebook_claim, Decimal("100")))
    )

    review = evaluate_action_item(_batch(item), item, 0, evidence, NOW)

    assert "SCALE_BUDGET_DRIFT" in {issue.code for issue in review.issues}


def test_scale_denied_when_same_adset_id_in_two_accounts() -> None:
    """Неоднозначность — отказ, а не случайный выбор одной из записей."""
    first = SubjectRef(SubjectKind.ADSET, "adset-1", "act-111")
    second = SubjectRef(SubjectKind.ADSET, "adset-1", "act-222")
    facebook_claim = replace(
        _claim(
            "budget",
            Metric.DAILY_BUDGET,
            Decimal("100"),
            SourceSystem.FACEBOOK,
            currency="USD",
        ),
        window=FACEBOOK_WINDOW,
    )
    amo_claim = replace(
        _claim("quals", Metric.QUALS, 1, SourceSystem.AMO),
        window=OUTCOME_WINDOW,
    )
    payments_claim = replace(
        _claim("payments", Metric.PAYMENTS, 1, SourceSystem.CDP_ERP, currency="LCY"),
        window=OUTCOME_WINDOW,
    )
    revenue_claim = replace(
        _claim(
            "revenue",
            Metric.REVENUE,
            Decimal("500000"),
            SourceSystem.CDP_ERP,
            currency="LCY",
        ),
        window=OUTCOME_WINDOW,
    )
    item = ScaleManifest(
        kind=ActionKind.SCALE,
        manifest_id="scale-ambiguous",
        origin=ActionOrigin.BUDGET_SCALER,
        idempotency_key=str(uuid.uuid4()),
        prepared_at=NOW - timedelta(seconds=1),
        adset_id="adset-1",
        expected_status="ACTIVE",
        current_budget=Decimal("100"),
        target_budget=Decimal("120"),
        currency="USD",
        candidate_ad_ids=("ad-1",),
        facebook_window=FACEBOOK_WINDOW,
        outcome_window=OUTCOME_WINDOW,
        facts=(facebook_claim, amo_claim, payments_claim, revenue_claim),
    )
    status = EvidenceRecord(
        category=FactCategory.ACTION_STATE,
        subject=first,
        metric=Metric.EFFECTIVE_STATUS,
        value="ACTIVE",
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=NOW,
        window=None,
        currency=None,
    )
    budget = replace(
        status, metric=Metric.DAILY_BUDGET, value=Decimal("100"), currency="USD"
    )
    twin = replace(budget, subject=second)
    evidence = _all_action_sources(
        (status, budget, twin, _record(facebook_claim, Decimal("100")))
    )

    review = evaluate_action_item(_batch(item), item, 0, evidence, NOW)

    assert "SCALE_BUDGET_DRIFT" in {issue.code for issue in review.issues}


# --- Live capacity запуска: достаточность вместо равенства -------------------

LAUNCH_NOW = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
_LAUNCH_SHA = "a" * 64


def _launch_item(
    *,
    capacity_available: int = 10,
    hard_reserve: int = 1,
) -> LaunchManifest:
    media = MediaAssetSpec(
        asset_id="asset-1",
        order_index=0,
        media_type=MediaType.VIDEO,
        placement_group_id=None,
        placement_role=PlacementRole.DEFAULT,
        staged_relative_path="media/video.mp4",
        original_attachment_id="attachment-1",
        mime_type="video/mp4",
        size_bytes=10,
        content_sha256=_LAUNCH_SHA,
    )
    creative = CreativeSpec(
        creative_id="creative-1",
        order_index=0,
        ad_name="ad-name",
        media_asset_ids=(media.asset_id,),
        body_staged_relative_path="text/body.txt",
        body_sha256="b" * 64,
        product="PRODA",
        page_id="page-1",
        lead_form_id="form-1",
        call_to_action="LEARN_MORE",
        instagram_actor_id="instagram-1",
        title="title",
        link_url=None,
        expected_configured_status="ACTIVE",
    )
    destination = LaunchDestination(
        city="CityB",
        account_id="account-1",
        adset_id="adset-1",
        adset_type="L1",
        current_daily_budget=Decimal("10"),
        currency="USD",
        capacity_available=capacity_available,
        hard_reserve_slots=hard_reserve,
        creatives=(creative,),
        duplicate_signature="c" * 64,
    )
    trello = TrelloPrecondition(
        card_id="card-1",
        board_id="board-1",
        ready_list_id="ready-1",
        expected_list_id="ready-1",
        expected_due_complete=False,
        expected_closed=False,
        date_last_activity=LAUNCH_NOW,
        attachment_ids=("attachment-1",),
        attachment_manifest_sha256="d" * 64,
        labels_sha256="e" * 64,
        card_content_sha256="f" * 64,
    )
    return LaunchManifest(
        kind=ActionKind.LAUNCH,
        manifest_id="launch-manifest",
        origin=ActionOrigin.AUTO_LAUNCH,
        idempotency_key=str(uuid.uuid4()),
        prepared_at=LAUNCH_NOW,
        config_version_sha256=_LAUNCH_SHA,
        staging_root="/tmp/launch-staging",
        staging_directory="/tmp/launch-staging/launch-manifest",
        trello=trello,
        card_name_sha256=_LAUNCH_SHA,
        media_manifest_sha256=_LAUNCH_SHA,
        media_assets=(media,),
        campaign_type="PRODA",
        destinations=(destination,),
    )


def _launch_records(*, live_capacity: object = 10) -> tuple[EvidenceRecord, ...]:
    adset = SubjectRef(SubjectKind.ADSET, "adset-1", "account-1")

    def _fb(metric: Metric, value, currency: str | None = None) -> EvidenceRecord:
        return EvidenceRecord(
            FactCategory.MATCH,
            adset,
            metric,
            value,
            SourceSystem.FACEBOOK,
            EvidenceState.FRESH_COMPLETE,
            LAUNCH_NOW,
            None,
            currency,
        )

    records = [
        EvidenceRecord(
            FactCategory.MATCH,
            SubjectRef(SubjectKind.CARD, "card-1"),
            Metric.MATCH_STATE,
            "MATCH",
            SourceSystem.TRELLO,
            EvidenceState.FRESH_COMPLETE,
            LAUNCH_NOW,
            None,
            None,
        ),
        EvidenceRecord(
            FactCategory.MATCH,
            SubjectRef(SubjectKind.MEDIA, "asset-1", "launch-manifest"),
            Metric.MATCH_STATE,
            "MATCH",
            SourceSystem.MEDIA_BYTES,
            EvidenceState.FRESH_COMPLETE,
            LAUNCH_NOW,
            None,
            None,
        ),
        _fb(Metric.DAILY_BUDGET, Decimal("10"), "USD"),
        _fb(Metric.MATCH_STATE, "NO_DUPLICATE"),
    ]
    if live_capacity is not None:
        records.append(_fb(Metric.CAPACITY, live_capacity))
    return tuple(records)


def test_launch_card_drift_record_is_terminal_issue():
    """Запись MATCH_STATE=DRIFT от approval_sources (карточка изменилась) даёт LAUNCH_INPUT_DRIFT."""
    item = _launch_item(capacity_available=10, hard_reserve=1)
    records = tuple(
        replace(row, value="DRIFT") if row.source is SourceSystem.TRELLO else row
        for row in _launch_records(live_capacity=10)
    )
    issues = _launch_issues(item, records)
    assert [issue.code for issue in issues] == ["LAUNCH_INPUT_DRIFT"]


def test_launch_capacity_sufficiency_tolerates_neighbour_claim():
    # Манифест заморозил capacity=10, сосед-claim занял слот → live=9.
    # Слотов на запуск (1) и резерв (1) хватает — отказа быть не должно.
    item = _launch_item(capacity_available=10, hard_reserve=1)
    issues = _launch_issues(item, _launch_records(live_capacity=9))
    assert issues == ()


def test_launch_capacity_shortage_is_insufficient_not_drift():
    item = _launch_item(capacity_available=10, hard_reserve=1)
    issues = _launch_issues(item, _launch_records(live_capacity=1))
    codes = [issue.code for issue in issues]
    assert codes == ["LAUNCH_CAPACITY_INSUFFICIENT"]


def test_launch_capacity_missing_record_is_scope_drift():
    item = _launch_item()
    issues = _launch_issues(item, _launch_records(live_capacity=None))
    assert [issue.code for issue in issues] == ["LAUNCH_SCOPE_DRIFT"]


def test_launch_capacity_unreadable_record_is_scope_drift():
    item = _launch_item()
    issues = _launch_issues(item, _launch_records(live_capacity="сломано"))
    assert [issue.code for issue in issues] == ["LAUNCH_SCOPE_DRIFT"]


def test_launch_capacity_manifest_shortage_still_flagged():
    # Заморозка в манифесте меньше требуемого — статическая проверка манифеста
    # остаётся, даже когда live-слотов достаточно.
    item = _launch_item(capacity_available=1, hard_reserve=1)
    issues = _launch_issues(item, _launch_records(live_capacity=10))
    assert [issue.code for issue in issues] == ["LAUNCH_CAPACITY_INSUFFICIENT"]
