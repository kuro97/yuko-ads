from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest

import config
from services.approval_checker_models import (
    ActionKind,
    ActionResult,
    ActionRun,
    CheckedDelivery,
    DailyInventorySnapshot,
    DeliveryAuditEvent,
    DeliveryChannel,
    DeliveryKind,
    EvidenceBundle,
    EvidenceRecord,
    EvidenceState,
    FactCategory,
    FactClaim,
    FieldFormat,
    FieldLabelTemplate,
    Metric,
    OperationState,
    OperationItemRecord,
    PermitRevokedAuditEvent,
    PaginationCoverage,
    ReportCheckRequest,
    ReportCheckAuditEvent,
    ReportField,
    ReportSection,
    ReportTemplate,
    ReportVerdict,
    ScaleManifest,
    SectionTemplate,
    ShadowBatchEvaluation,
    ShadowItemEvaluation,
    SourceEvidence,
    SourceFreshness,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    TransportErrorCode,
    TypedReportPayload,
    UnpauseManifest,
    ActionOrigin,
    canonical_json,
    canonical_state_sha256,
    manifest_sha256,
    report_manifest_sha256,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _window() -> TimeWindow:
    return TimeWindow(
        start=NOW - timedelta(days=1),
        end=NOW,
        timezone_name="Etc/GMT-5",
        semantic="PREVIOUS_DAY",
    )


def _unpause(*, ad_id: str = "ad-1") -> UnpauseManifest:
    return UnpauseManifest(
        kind=ActionKind.UNPAUSE,
        manifest_id=f"manifest-{ad_id}",
        origin=ActionOrigin.WEB,
        idempotency_key=str(uuid.uuid4()),
        prepared_at=NOW,
        ad_id=ad_id,
        adset_id="adset-1",
        expected_before_status="PAUSED",
        expected_after_status="ACTIVE",
        pre_inventory_sha256=SHA_A,
    )


def _scale() -> ScaleManifest:
    facebook_window = _window()
    outcome_window = TimeWindow(
        start=NOW - timedelta(days=30),
        end=NOW,
        timezone_name="Etc/GMT-5",
        semantic="OUTCOME_30D",
    )
    subject = SubjectRef(SubjectKind.ADSET, "adset-1")
    facts = tuple(
        FactClaim(
            claim_id=f"claim-{source.value}",
            field_id=None,
            category=FactCategory.BUSINESS_METRIC,
            subject=subject,
            metric=Metric.REVENUE if source is SourceSystem.CDP_ERP else Metric.SPEND,
            value=Decimal("1"),
            source=source,
            window=facebook_window if source is SourceSystem.FACEBOOK else outcome_window,
            currency="USD" if source is SourceSystem.FACEBOOK else "LCY",
        )
        for source in (SourceSystem.FACEBOOK, SourceSystem.AMO, SourceSystem.CDP_ERP)
    )
    return ScaleManifest(
        kind=ActionKind.SCALE,
        manifest_id="manifest-scale-1",
        origin=ActionOrigin.BUDGET_SCALER,
        idempotency_key=str(uuid.uuid4()),
        prepared_at=NOW,
        adset_id="adset-1",
        expected_status="ACTIVE",
        current_budget=Decimal("100"),
        target_budget=Decimal("115"),
        currency="USD",
        candidate_ad_ids=("ad-1", "ad-2"),
        facebook_window=facebook_window,
        outcome_window=outcome_window,
        facts=facts,
    )


def _report_contract() -> tuple[TypedReportPayload, tuple[FactClaim, ...]]:
    subject = SubjectRef(SubjectKind.AD, "ad-1")
    field = ReportField(
        field_id="field-spend",
        section_id="summary",
        category=FactCategory.BUSINESS_METRIC,
        label=FieldLabelTemplate.SPEND,
        format=FieldFormat.USD,
        subject=subject,
        metric=Metric.SPEND,
        value=Decimal("459.00"),
        source=SourceSystem.FACEBOOK,
        window=_window(),
        currency="USD",
    )
    payload = TypedReportPayload(
        template=ReportTemplate.AUTOPILOT,
        sections=(
            ReportSection(
                section_id="summary",
                template=SectionTemplate.SUMMARY,
                field_ids=(field.field_id,),
                window=_window(),
            ),
        ),
        fields=(field,),
        generated_at=NOW,
        mixed_windows_explicit=False,
    )
    claim = FactClaim(
        claim_id="claim-spend",
        field_id=field.field_id,
        category=field.category,
        subject=field.subject,
        metric=field.metric,
        value=field.value,
        source=field.source,
        window=field.window,
        currency=field.currency,
    )
    return payload, (claim,)


def _evidence(*, loaded_at: datetime, observed_at: datetime) -> EvidenceBundle:
    record = EvidenceRecord(
        category=FactCategory.BUSINESS_METRIC,
        subject=SubjectRef(SubjectKind.AD, "ad-1"),
        metric=Metric.SPEND,
        value=Decimal("459"),
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=observed_at,
        window=_window(),
        currency="USD",
        entity_ids=("ad-1",),
    )
    source = SourceEvidence(
        source=SourceSystem.FACEBOOK,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=observed_at,
        data_as_of=observed_at,
        from_cache=False,
        complete=True,
        records=(record,),
    )
    freshness = SourceFreshness(
        source=SourceSystem.FACEBOOK,
        fetched_at=observed_at,
        observed_at=observed_at,
        data_as_of=observed_at,
        max_age_seconds=15,
        from_cache=False,
        complete=True,
        fresh=True,
    )
    return EvidenceBundle(
        loaded_at=loaded_at,
        sources=(source,),
        freshness=(freshness,),
        facebook_sha256=SHA_A,
        trello_sha256=SHA_A,
        media_sha256=SHA_A,
        amo_sha256=SHA_A,
        cdp_sha256=SHA_A,
        final_live_state_sha256=SHA_A,
        local_state_sha256=None,
    )


def _daily_inventory() -> DailyInventorySnapshot:
    pagination = PaginationCoverage(
        endpoint_kind="ACCOUNT_ADS",
        page_count=1,
        item_ids=("ad-1", "ad-2"),
        pagination_complete=True,
        failed_page_index=None,
        response_state_sha256=SHA_A,
    )
    return DailyInventorySnapshot(
        account_id="account-1",
        account_status=1,
        currency="USD",
        timezone_name="Etc/GMT-5",
        target_window=_window(),
        fetched_at=NOW,
        max_age_seconds=900,
        ads_pagination=pagination,
        accessible_ad_ids=("ad-1", "ad-2"),
        eligible_ad_ids=("ad-1", "ad-2"),
        eligible_campaign_ids=("campaign-1", "campaign-2"),
        exact_lookup_ad_ids=(),
        created_time_by_ad_sha256=SHA_A,
        status_by_ad_sha256=SHA_A,
        campaign_by_ad_sha256=SHA_B,
        cache_missing_ad_ids=(),
        cache_mismatched_ad_ids=(),
        fresh=True,
        complete=True,
    )


def test_contracts_are_frozen_and_manifest_hash_changes_with_target() -> None:
    manifest = _unpause(ad_id="ad-1")

    with pytest.raises(FrozenInstanceError):
        manifest.ad_id = "ad-2"  # type: ignore[misc]

    changed = replace(manifest, ad_id="ad-2", manifest_id="manifest-ad-2")
    assert manifest_sha256(manifest) != manifest_sha256(changed)


def test_unpause_hash_binds_pre_inventory_snapshot() -> None:
    manifest = _unpause()

    changed = replace(manifest, pre_inventory_sha256=SHA_B)

    assert manifest_sha256(manifest) != manifest_sha256(changed)


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    (
        ("candidate_ad_ids", ("ad-1", "ad-3")),
        (
            "facebook_window",
            TimeWindow(
                start=NOW - timedelta(days=2),
                end=NOW,
                timezone_name="Etc/GMT-5",
                semantic="FACEBOOK_2D",
            ),
        ),
        (
            "outcome_window",
            TimeWindow(
                start=NOW - timedelta(days=31),
                end=NOW,
                timezone_name="Etc/GMT-5",
                semantic="OUTCOME_31D",
            ),
        ),
    ),
)
def test_scale_hash_binds_candidates_and_exact_windows(
    field_name: str,
    changed_value: object,
) -> None:
    manifest = _scale()
    changes = {field_name: changed_value}
    if field_name == "facebook_window":
        changes["facts"] = tuple(
            replace(claim, window=changed_value)
            if claim.source is SourceSystem.FACEBOOK
            else claim
            for claim in manifest.facts
        )
    elif field_name == "outcome_window":
        changes["facts"] = tuple(
            replace(claim, window=changed_value)
            if claim.source is not SourceSystem.FACEBOOK
            else claim
            for claim in manifest.facts
        )

    changed = replace(manifest, **changes)

    assert manifest_sha256(manifest) != manifest_sha256(changed)


def test_unpause_and_scale_reject_unbound_or_ambiguous_source_state() -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(_unpause(), pre_inventory_sha256="stale")

    scale = _scale()
    with pytest.raises(ValueError, match="непустыми и уникальными"):
        replace(scale, candidate_ad_ids=("ad-1", "ad-1"))
    with pytest.raises(ValueError, match="окно вне manifest"):
        replace(scale, facebook_window=replace(scale.facebook_window, semantic="CHANGED"))


def test_permit_revocation_is_immutable_and_contains_only_safe_wal_fields() -> None:
    revoked = PermitRevokedAuditEvent(
        schema_version=1,
        event="permit_revoked",
        event_id="event-1",
        written_at=NOW,
        operation_id="operation-1",
        idempotency_key=str(uuid.uuid4()),
        batch_manifest_id="batch-1",
        batch_manifest_sha256=SHA_A,
        subject_ids=("ad:ad-1",),
        check_id="check-1",
        permit_id="permit-1",
        item_id="item-0",
        item_index=0,
        action_kind=ActionKind.UNPAUSE,
        item_manifest_sha256=SHA_A,
        evidence_state_sha256=SHA_B,
        revoked_at=NOW,
        reason_code="PRECONDITION_MISMATCH",
    )

    with pytest.raises(FrozenInstanceError):
        revoked.reason_code = "CHANGED"  # type: ignore[misc]
    encoded = canonical_json(revoked)
    assert b'"event":"permit_revoked"' in encoded
    assert b'"reason_code":"PRECONDITION_MISMATCH"' in encoded
    assert b"raw_error" not in encoded


def test_report_check_audit_is_immutable_redacted_and_strict() -> None:
    event = ReportCheckAuditEvent(
        schema_version=1,
        event="report_check",
        event_id="event-report-1",
        written_at=NOW,
        correlation_id="correlation-1",
        check_id="check-report-1",
        report_template=ReportTemplate.AUTOPILOT,
        verdict=ReportVerdict.BLOCKED,
        manifest_sha256=SHA_A,
        evidence_state_sha256=SHA_B,
        issue_codes=("PAYMENT_REVENUE_CONTRADICTION",),
        checked_at=NOW,
    )

    with pytest.raises(FrozenInstanceError):
        event.verdict = ReportVerdict.VERIFIED  # type: ignore[misc]
    encoded = canonical_json(event)
    assert b'"event":"report_check"' in encoded
    assert b'"verdict":"BLOCKED"' in encoded
    for forbidden_field in (b'"value"', b'"text"', b'"payment_id"', b'"raw_error"'):
        assert forbidden_field not in encoded

    with pytest.raises(ValueError, match="sorted и unique"):
        replace(event, issue_codes=("Z", "A", "A"))
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(event, evidence_state_sha256="invalid")


def test_delivery_audit_is_typed_redacted_and_mutually_exclusive() -> None:
    event = DeliveryAuditEvent(
        schema_version=1,
        event="delivery",
        event_id="event-delivery-1",
        written_at=NOW,
        delivery_id="delivery-1",
        delivery_kind=DeliveryKind.REPORT,
        reference_id="check-1",
        payload_sha256=SHA_A,
        buttons_sha256=SHA_B,
        manifest_sha256=SHA_A,
        report_verdict=ReportVerdict.BLOCKED,
        action_result=None,
        channel=DeliveryChannel.ADS,
        sent=False,
        fallback_sent=True,
        transport_error_code=TransportErrorCode.NETWORK_ERROR,
        delivered_at=NOW,
    )

    with pytest.raises(FrozenInstanceError):
        event.channel = DeliveryChannel.HEALTH  # type: ignore[misc]
    encoded = canonical_json(event)
    assert b'"delivery_kind":"REPORT"' in encoded
    assert b'"channel":"ads"' in encoded
    assert b'"payload_sha256"' in encoded
    assert b'"buttons_sha256"' in encoded
    for forbidden_field in (
        b'"text"',
        b'"buttons"',
        b'"value"',
        b'"subject_ids"',
        b'"payment_id"',
    ):
        assert forbidden_field not in encoded

    with pytest.raises(ValueError, match="только report_verdict"):
        replace(event, action_result=ActionResult.CONFIRMED)
    with pytest.raises(ValueError, match="закрытым TransportErrorCode"):
        replace(event, transport_error_code="connection refused")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(event, payload_sha256="raw delivered text")


def test_fact_free_delivery_cannot_carry_business_result_or_manifest() -> None:
    fact_free = DeliveryAuditEvent(
        schema_version=1,
        event="delivery",
        event_id="event-delivery-2",
        written_at=NOW,
        delivery_id="delivery-2",
        delivery_kind=DeliveryKind.FACT_FREE,
        reference_id="fact-free-1",
        payload_sha256=SHA_A,
        buttons_sha256=None,
        manifest_sha256=None,
        report_verdict=None,
        action_result=None,
        channel=DeliveryChannel.HEALTH,
        sent=True,
        fallback_sent=False,
        transport_error_code=None,
        delivered_at=NOW,
    )
    assert fact_free.delivery_kind is DeliveryKind.FACT_FREE

    with pytest.raises(ValueError, match="не может ссылаться"):
        replace(fact_free, manifest_sha256=SHA_A)
    with pytest.raises(ValueError, match="не может ссылаться"):
        replace(fact_free, buttons_sha256=SHA_B)


def test_checked_delivery_requires_explicit_audit_result() -> None:
    delivery = CheckedDelivery(
        sent=True,
        fallback_sent=False,
        check_id="check-1",
        report_verdict=ReportVerdict.VERIFIED,
        action_result=None,
        audit_persisted=True,
    )
    assert delivery.audit_persisted is True

    with pytest.raises(ValueError, match="audit_persisted"):
        replace(delivery, audit_persisted=1)  # type: ignore[arg-type]


def test_operation_item_requires_complete_permit_revocation_pair() -> None:
    item = OperationItemRecord(
        item_id="item-0",
        item_index=0,
        action_kind=ActionKind.UNPAUSE,
        item_manifest_sha256=SHA_A,
        subject_ids=("ad:ad-1",),
        check_id="check-1",
        decision=None,
        evidence_state_sha256=SHA_A,
        shadow_evaluation=None,
        permit_id="permit-1",
        attempt_id=None,
        result=None,
        exact_created_ids=(),
        attempted_at=None,
        completed_at=None,
        reconciliation_required=False,
        permit_revoked_at=NOW,
        permit_revocation_reason_code="PRECONDITION_MISMATCH",
    )
    assert item.permit_revoked_at == NOW

    with pytest.raises(ValueError, match="задаются вместе"):
        replace(item, permit_revocation_reason_code=None)


def test_canonical_json_normalizes_decimal_and_sorts_mapping() -> None:
    first = canonical_json({"b": Decimal("1.00"), "a": "Город"})
    second = canonical_json({"a": "Город", "b": Decimal("1")})

    assert first == second == b'{"a":"\xd0\x93\xd0\xbe\xd1\x80\xd0\xbe\xd0\xb4","b":"1"}'


def test_report_contract_requires_exact_field_claim_bijection_and_hash() -> None:
    payload, claims = _report_contract()
    digest = report_manifest_sha256(payload, claims)

    request = ReportCheckRequest("corr-1", payload, claims, digest)
    assert request.manifest_sha256 == digest

    with pytest.raises(ValueError, match="биекцию"):
        ReportCheckRequest("corr-2", payload, (), report_manifest_sha256(payload, ()))
    with pytest.raises(ValueError, match="не совпадает"):
        ReportCheckRequest("corr-3", payload, claims, SHA_B)


def test_report_rejects_arbitrary_label_and_bool_as_number() -> None:
    subject = SubjectRef(SubjectKind.AD, "ad-1")
    with pytest.raises(ValueError, match="FieldLabelTemplate"):
        ReportField(
            "f1",
            "s1",
            FactCategory.BUSINESS_METRIC,
            "Произвольный текст",  # type: ignore[arg-type]
            FieldFormat.INTEGER,
            subject,
            Metric.LEADS,
            1,
            SourceSystem.FACEBOOK,
            _window(),
        )
    with pytest.raises(ValueError, match="неподдерживаемый тип"):
        FactClaim(
            "c1",
            None,
            FactCategory.BUSINESS_METRIC,
            subject,
            Metric.LEADS,
            True,  # type: ignore[arg-type]
            SourceSystem.FACEBOOK,
            _window(),
        )


def test_time_window_rejects_naive_or_empty_interval() -> None:
    with pytest.raises(ValueError, match="timezone"):
        TimeWindow(
            datetime(2026, 7, 22),
            datetime(2026, 7, 23),
            "Etc/GMT-5",
            "DAY",
        )
    with pytest.raises(ValueError, match="полуинтервал"):
        TimeWindow(NOW, NOW, "UTC", "EMPTY")


def test_action_kind_cannot_represent_delete_or_archive() -> None:
    with pytest.raises(ValueError):
        ActionKind("DELETE")
    with pytest.raises(ValueError):
        ActionKind("ARCHIVE")


def test_business_state_hash_ignores_transport_metadata() -> None:
    first = _evidence(loaded_at=NOW, observed_at=NOW - timedelta(seconds=1))
    later = _evidence(
        loaded_at=NOW + timedelta(seconds=20),
        observed_at=NOW + timedelta(seconds=10),
    )

    assert canonical_state_sha256(first) == canonical_state_sha256(later)
    changed_record = replace(
        later.sources[0].records[0],
        value=Decimal("460"),
    )
    changed_source = replace(later.sources[0], records=(changed_record,))
    changed = replace(later, sources=(changed_source,))
    assert canonical_state_sha256(first) != canonical_state_sha256(changed)


def test_daily_inventory_binds_exact_campaign_universe() -> None:
    inventory = _daily_inventory()

    changed_campaigns = replace(
        inventory,
        eligible_campaign_ids=("campaign-1", "campaign-3"),
    )
    changed_mapping = replace(inventory, campaign_by_ad_sha256=SHA_A)

    assert canonical_json(inventory) != canonical_json(changed_campaigns)
    assert canonical_json(inventory) != canonical_json(changed_mapping)


def test_daily_inventory_rejects_ambiguous_campaign_coverage() -> None:
    inventory = _daily_inventory()

    with pytest.raises(ValueError, match="sorted и unique"):
        replace(
            inventory,
            eligible_campaign_ids=("campaign-2", "campaign-1", "campaign-1"),
        )
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(inventory, campaign_by_ad_sha256="missing")


def test_shadow_contract_is_item_zero_only_and_incomplete() -> None:
    run = ActionRun(
        operation_id="operation-1",
        idempotency_key=str(uuid.uuid4()),
        batch_manifest_id="batch-1",
        batch_manifest_sha256=SHA_A,
        state=OperationState.SHADOW_FIRST_ITEM_ONLY,
        reviews=(),
        executions=(),
        result=None,
        shadow_evaluation=ShadowBatchEvaluation.INCOMPLETE,
        shadow_items=(
            ShadowItemEvaluation.FIRST_ITEM_WOULD_APPROVE,
            ShadowItemEvaluation.NOT_EVALUATED_REQUIRES_LIVE_SEQUENCE,
        ),
        dry_run=True,
        provider_mutation_count=0,
        first_unprocessed_index=1,
        stop_reason_code="SHADOW_FIRST_ITEM_ONLY",
        reconciliation_required=False,
    )
    assert run.result is None
    assert run.provider_mutation_count == 0

    with pytest.raises(ValueError, match="только item 0"):
        replace(
            run,
            shadow_items=(
                ShadowItemEvaluation.FIRST_ITEM_WOULD_APPROVE,
                ShadowItemEvaluation.FIRST_ITEM_WOULD_DENY,
            ),
        )


def test_checker_enforcement_defaults_true_and_invalid_env_is_fail_safe(monkeypatch) -> None:
    assert config.REPORT_CHECKER_ENABLED is True
    assert config.REPORT_CHECKER_ENFORCE_REPORTS is True
    assert config.REPORT_CHECKER_ENFORCE_ACTIONS is True

    monkeypatch.setenv("REPORT_CHECKER_ENFORCE_ACTIONS", "maybe")
    assert config._checker_bool("REPORT_CHECKER_ENFORCE_ACTIONS", True) is True


def test_checker_exact_ttls_roots_and_limits() -> None:
    assert config.REPORT_CHECKER_LAUNCH_TTL_SECONDS == 300
    assert config.REPORT_CHECKER_PAUSE_TTL_SECONDS == 120
    assert config.REPORT_CHECKER_MAX_BATCH_ACTIONS == 20
    assert config.REPORT_CHECKER_MAX_LAUNCH_DESTINATIONS == 10
    assert config.REPORT_CHECKER_FB_OBJECT_CHUNK_SIZE == 50
    assert config.REPORT_CHECKER_DAILY_MANIFEST_RETENTION_DAYS == 32
    assert config.REPORT_CHECKER_STAGING_DIR_MODE == 0o700
    assert config.REPORT_CHECKER_STAGING_FILE_MODE == 0o600
    assert config.REPORT_CHECKER_STAGING_ROOT.parent == config.REPORT_CHECKER_DATA_ROOT
    assert config.REPORT_CHECKER_METRICS_MANIFEST_PATH.name == "metrics_snapshot_state.json"
    assert set(config.REPORT_CHECKER_MONITOR_STATE_PATHS) == {
        "GUARDIAN_STATE",
        "BRIEF_GENERATOR_STATE",
        "ANOMALY_ALERT_STATE",
        "EXPIRED_OFFER_STATE",
        "COVERAGE_STATE",
        "ADS_WATCHDOG_STATE",
        "ADSET_SPEND_GUARD_STATE",
        "CDP_SPEND_ALERT_STATE",
    }
