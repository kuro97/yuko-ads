from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

import config
import pytest
from services import approval_checker, approval_source_amo, approval_sources
from services.approval_audit import find_operation, reserve_operation
from services.approval_audit import issue_item_permit
from services.approval_checker_models import (
    ActionBatchManifest,
    ActionKind,
    ActionOrigin,
    ApprovalDecision,
    ActionObservation,
    CheckerHealth,
    CreativeSpec,
    EvidenceRequest,
    EvidenceRecord,
    EvidenceState,
    FactCategory,
    FactClaim,
    FieldFormat,
    FieldLabelTemplate,
    Metric,
    LaunchDestination,
    LaunchManifest,
    MediaAssetSpec,
    MediaType,
    PlacementRole,
    ReportCheckRequest,
    ReportField,
    ReportSection,
    ReportTemplate,
    ReportVerdict,
    PauseManifest,
    SectionTemplate,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    TrelloPrecondition,
    TypedReportPayload,
    UnpauseManifest,
    canonical_state_sha256,
    manifest_sha256,
    report_manifest_sha256,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
WINDOW = TimeWindow(
    NOW - timedelta(days=7),
    NOW,
    "Etc/GMT-5",
    "trailing_7_days",
)
AD = SubjectRef(SubjectKind.AD, "ad-1", "adset-1")


def _healthy() -> CheckerHealth:
    return CheckerHealth(
        enabled=True,
        enforce_reports=True,
        enforce_actions=True,
        audit_writable=True,
        reconciliation_pending=False,
        last_check_at=None,
    )


def _source(
    source: SourceSystem,
    records: tuple[EvidenceRecord, ...] = (),
    *,
    state: EvidenceState = EvidenceState.FRESH_COMPLETE,
    complete: bool = True,
    fetched_at: datetime = NOW,
) -> SourceEvidence:
    return SourceEvidence(
        source=source,
        state=state,
        fetched_at=fetched_at,
        data_as_of=fetched_at,
        from_cache=False,
        complete=complete,
        records=records,
        error_code=None if complete else "TEST_UNAVAILABLE",
    )


def _claim(
    field_id: str,
    metric: Metric,
    value: int | Decimal,
    *,
    currency: str | None,
) -> FactClaim:
    return FactClaim(
        claim_id=f"claim-{field_id}",
        field_id=field_id,
        category=FactCategory.BUSINESS_METRIC,
        subject=AD,
        metric=metric,
        value=value,
        source=SourceSystem.CDP_ERP,
        window=WINDOW,
        currency=currency,
    )


def _report_request(claims: tuple[FactClaim, ...]) -> ReportCheckRequest:
    fields = tuple(
        ReportField(
            field_id=claim.field_id or "missing",
            section_id="outcomes",
            category=claim.category,
            label=(
                FieldLabelTemplate.PAYMENTS
                if claim.metric is Metric.PAYMENTS
                else FieldLabelTemplate.REVENUE
            ),
            format=(
                FieldFormat.INTEGER
                if claim.metric is Metric.PAYMENTS
                else FieldFormat.LCY
            ),
            subject=claim.subject,
            metric=claim.metric,
            value=claim.value,
            source=claim.source,
            window=claim.window,
            currency=claim.currency,
        )
        for claim in claims
    )
    payload = TypedReportPayload(
        template=ReportTemplate.AUTOPILOT,
        sections=(
            ReportSection(
                section_id="outcomes",
                template=SectionTemplate.OUTCOMES,
                field_ids=tuple(field.field_id for field in fields),
                window=WINDOW,
            ),
        ),
        fields=fields,
        generated_at=NOW,
        mixed_windows_explicit=False,
    )
    return ReportCheckRequest(
        correlation_id="report-checker-test",
        payload=payload,
        claims=claims,
        manifest_sha256=report_manifest_sha256(payload, claims),
    )


def _unpause(ad_id: str, suffix: str, key: str) -> UnpauseManifest:
    return UnpauseManifest(
        kind=ActionKind.UNPAUSE,
        manifest_id=f"unpause-{suffix}",
        origin=ActionOrigin.WEB,
        idempotency_key=key,
        prepared_at=NOW,
        ad_id=ad_id,
        adset_id=f"adset-{suffix}",
        expected_before_status="PAUSED",
        expected_after_status="ACTIVE",
        pre_inventory_sha256="a" * 64,
    )


def _batch(actions: tuple[UnpauseManifest, ...], key: str) -> ActionBatchManifest:
    initial = ActionBatchManifest(
        batch_manifest_id="batch-checker-test",
        correlation_id="checker-test",
        idempotency_key=key,
        prepared_at=NOW,
        subject_ids=tuple(f"ad:{action.ad_id}" for action in actions),
        actions=actions,
        manifest_sha256="0" * 64,
    )
    return replace(initial, manifest_sha256=manifest_sha256(initial))


def test_check_report_validates_coverage_before_any_io(monkeypatch):
    request = _report_request((_claim("payments", Metric.PAYMENTS, 1, currency="LCY"),))
    object.__setattr__(request, "manifest_sha256", "0" * 64)
    monkeypatch.setattr(
        approval_checker,
        "checker_health",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("health I/O")),
    )
    monkeypatch.setattr(
        approval_checker,
        "load_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("source I/O")),
    )
    monkeypatch.setattr(
        approval_checker, "append_report_check", lambda event: event.event_id
    )

    result = approval_checker.check_report(request, NOW)

    assert result.verdict is ReportVerdict.BLOCKED
    assert result.audit_persisted is True
    assert {issue.code for issue in result.issues} == {"REPORT_COVERAGE_INCOMPLETE"}


def test_check_report_blocks_payment_without_revenue(monkeypatch):
    payments = _claim("payments", Metric.PAYMENTS, 1, currency="LCY")
    revenue = _claim("revenue", Metric.REVENUE, Decimal("0"), currency="LCY")
    request = _report_request((payments, revenue))
    records = tuple(
        EvidenceRecord(
            category=claim.category,
            subject=claim.subject,
            metric=claim.metric,
            value=claim.value,
            source=claim.source,
            state=EvidenceState.FRESH_COMPLETE,
            observed_at=NOW,
            window=claim.window,
            currency=claim.currency,
        )
        for claim in request.claims
    )
    bundle = approval_sources._assemble_bundle(
        (_source(SourceSystem.CDP_ERP, records),),
        NOW,
        300,
    )
    monkeypatch.setattr(approval_checker, "checker_health", lambda _now: _healthy())
    monkeypatch.setattr(approval_checker, "load_evidence", lambda *_args: bundle)
    monkeypatch.setattr(
        approval_checker, "append_report_check", lambda event: event.event_id
    )

    result = approval_checker.check_report(request, NOW)

    assert result.verdict is ReportVerdict.BLOCKED
    assert result.audit_persisted is True
    assert "PAYMENTS_WITHOUT_REVENUE" in {issue.code for issue in result.issues}


def test_check_report_fsyncs_sanitized_verdict_and_binds_evidence(
    monkeypatch, tmp_path
):
    claim = _claim("revenue", Metric.REVENUE, Decimal("987654"), currency="LCY")
    request = replace(
        _report_request((claim,)),
        correlation_id="client@example.com Денис",
    )
    record = EvidenceRecord(
        category=claim.category,
        subject=claim.subject,
        metric=claim.metric,
        value=claim.value,
        source=claim.source,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=NOW,
        window=claim.window,
        currency=claim.currency,
    )
    bundle = approval_sources._assemble_bundle(
        (_source(SourceSystem.CDP_ERP, (record,)),), NOW, 300
    )
    audit_path = tmp_path / "approval.jsonl"
    monkeypatch.setattr(config, "REPORT_CHECKER_AUDIT_PATH", audit_path)
    monkeypatch.setattr(approval_checker, "checker_health", lambda _now: _healthy())
    monkeypatch.setattr(approval_checker, "load_evidence", lambda *_args: bundle)

    result = approval_checker.check_report(request, NOW)
    raw = audit_path.read_text(encoding="utf-8")
    event = json.loads(raw)

    assert result.verdict is ReportVerdict.VERIFIED
    assert result.audit_persisted is True
    assert event["event"] == "report_check"
    assert event["evidence_state_sha256"] == canonical_state_sha256(bundle)
    assert event["correlation_id"].startswith("sha256:")
    assert "client@example.com" not in raw
    assert "Денис" not in raw
    assert "987654" not in raw
    assert "ad-1" not in raw


def test_check_report_write_failure_removes_business_outcomes(monkeypatch):
    claim = _claim("revenue", Metric.REVENUE, Decimal("100"), currency="LCY")
    request = _report_request((claim,))
    record = EvidenceRecord(
        claim.category,
        claim.subject,
        claim.metric,
        claim.value,
        claim.source,
        EvidenceState.FRESH_COMPLETE,
        NOW,
        claim.window,
        claim.currency,
    )
    bundle = approval_sources._assemble_bundle(
        (_source(SourceSystem.CDP_ERP, (record,)),), NOW, 300
    )
    monkeypatch.setattr(approval_checker, "checker_health", lambda _now: _healthy())
    monkeypatch.setattr(approval_checker, "load_evidence", lambda *_args: bundle)
    monkeypatch.setattr(
        approval_checker,
        "append_report_check",
        lambda _event: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = approval_checker.check_report(request, NOW)

    assert result.verdict is ReportVerdict.CHECKER_UNAVAILABLE
    assert result.audit_persisted is False
    assert result.outcomes == ()
    assert {issue.code for issue in result.issues} == {"AUDIT_WRITE_FAILED"}


def test_check_report_corrupt_audit_fails_closed_without_source_io(
    monkeypatch, tmp_path
):
    request = _report_request(
        (_claim("revenue", Metric.REVENUE, Decimal("100"), currency="LCY"),)
    )
    audit_path = tmp_path / "approval.jsonl"
    audit_path.write_text("corrupt\n", encoding="utf-8")
    monkeypatch.setattr(config, "REPORT_CHECKER_AUDIT_PATH", audit_path)
    monkeypatch.setattr(
        config,
        "REPORT_CHECKER_RECONCILIATION_PATH",
        tmp_path / "reconciliation.json",
    )
    monkeypatch.setattr(
        approval_checker,
        "load_evidence",
        lambda *_args: (_ for _ in ()).throw(AssertionError("source I/O")),
    )

    result = approval_checker.check_report(request, NOW)

    assert result.verdict is ReportVerdict.CHECKER_UNAVAILABLE
    assert result.audit_persisted is False
    assert result.outcomes == ()
    assert {issue.code for issue in result.issues} == {"AUDIT_WRITE_FAILED"}


def test_review_action_denies_unavailable_source_and_persists_check(
    monkeypatch, tmp_path
):
    key = str(uuid.uuid4())
    item = _unpause("ad-1", "1", key)
    batch = _batch((item,), key)
    audit_path = tmp_path / "approval.jsonl"
    monkeypatch.setattr(config, "REPORT_CHECKER_AUDIT_PATH", audit_path)
    reserve_operation(batch, NOW)
    facebook_record = EvidenceRecord(
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
    bundle = approval_sources._assemble_bundle(
        (
            _source(SourceSystem.FACEBOOK, (facebook_record,)),
            _source(SourceSystem.TRELLO),
            _source(SourceSystem.MEDIA_BYTES),
            _source(SourceSystem.AMO),
            _source(SourceSystem.CDP_ERP, state=EvidenceState.ERROR, complete=False),
        ),
        NOW,
        1,
    )
    monkeypatch.setattr(approval_checker, "checker_health", lambda _now: _healthy())
    monkeypatch.setattr(
        approval_checker, "load_action_item_evidence", lambda *_args: bundle
    )

    review = approval_checker.review_action_item(batch, item, 0, NOW)
    operation = find_operation(key)

    assert review.decision is ApprovalDecision.DENIED
    assert review.audit_persisted is True
    assert operation is not None
    assert operation.items[0].decision is ApprovalDecision.DENIED


def test_review_successor_before_confirmed_performs_no_source_io(monkeypatch, tmp_path):
    key = str(uuid.uuid4())
    first = _unpause("ad-1", "1", key)
    second = _unpause("ad-2", "2", key)
    batch = _batch((first, second), key)
    monkeypatch.setattr(
        config, "REPORT_CHECKER_AUDIT_PATH", tmp_path / "approval.jsonl"
    )
    reserve_operation(batch, NOW)
    source_calls = 0

    def unexpected_source(*_args):
        nonlocal source_calls
        source_calls += 1
        raise AssertionError("successor source I/O")

    monkeypatch.setattr(
        approval_checker, "load_action_item_evidence", unexpected_source
    )

    review = approval_checker.review_action_item(batch, second, 1, NOW)

    assert review.decision is ApprovalDecision.DENIED
    assert review.audit_persisted is False
    assert source_calls == 0
    assert "SEQUENCE_INVALID" in {issue.code for issue in review.issues}


def test_denied_review_audit_failure_is_logged_without_source_or_mutation(
    monkeypatch,
    tmp_path,
    caplog,
):
    key = str(uuid.uuid4())
    item = _unpause("ad-1", "1", key)
    batch = _batch((item,), key)
    monkeypatch.setattr(
        config, "REPORT_CHECKER_AUDIT_PATH", tmp_path / "approval.jsonl"
    )
    reserve_operation(batch, NOW)
    tampered = replace(batch, manifest_sha256="0" * 64)
    source = Mock(side_effect=AssertionError("source/provider I/O запрещён"))
    monkeypatch.setattr(approval_checker, "load_action_item_evidence", source)
    monkeypatch.setattr(
        approval_checker,
        "_persist_review",
        Mock(side_effect=OSError("disk unavailable")),
    )

    with caplog.at_level("ERROR", logger="services.approval_checker"):
        review = approval_checker.review_action_item(tampered, item, 0, NOW)

    assert review.decision is ApprovalDecision.DENIED
    assert review.audit_persisted is False
    assert source.call_count == 0
    assert "audit persist failed: OSError" in caplog.text
    assert "disk unavailable" not in caplog.text


def test_state_digest_excludes_transport_timestamps():
    first = _source(SourceSystem.FACEBOOK, fetched_at=NOW)
    second = _source(SourceSystem.FACEBOOK, fetched_at=NOW + timedelta(seconds=30))

    first_bundle = approval_sources._assemble_bundle((first,), NOW, 60)
    second_bundle = approval_sources._assemble_bundle(
        (second,), NOW + timedelta(seconds=30), 60
    )

    assert first_bundle.facebook_sha256 == second_bundle.facebook_sha256
    assert first_bundle.final_live_state_sha256 == second_bundle.final_live_state_sha256


def test_approved_review_can_receive_permit_only_from_persisted_audit(
    monkeypatch, tmp_path
):
    key = str(uuid.uuid4())
    item = _unpause("ad-1", "1", key)
    batch = _batch((item,), key)
    monkeypatch.setattr(
        config, "REPORT_CHECKER_AUDIT_PATH", tmp_path / "approval.jsonl"
    )
    reserve_operation(batch, NOW)
    facebook_record = EvidenceRecord(
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
    bundle = approval_sources._assemble_bundle(
        (
            _source(SourceSystem.FACEBOOK, (facebook_record,)),
            _source(SourceSystem.TRELLO),
            _source(SourceSystem.MEDIA_BYTES),
            _source(SourceSystem.AMO),
            _source(SourceSystem.CDP_ERP),
        ),
        NOW,
        1,
    )
    monkeypatch.setattr(approval_checker, "checker_health", lambda _now: _healthy())
    monkeypatch.setattr(
        approval_checker, "load_action_item_evidence", lambda *_args: bundle
    )

    review = approval_checker.review_action_item(batch, item, 0, NOW)
    permit = issue_item_permit(review, batch, item, NOW + timedelta(microseconds=1))

    assert review.decision is ApprovalDecision.APPROVED
    assert review.audit_persisted is True
    assert permit.check_id == review.check_id
    assert permit.evidence_state_sha256 == review.evidence_state_sha256


def test_review_pause_denies_payments_without_revenue(monkeypatch, tmp_path):
    key = str(uuid.uuid4())
    sibling = SubjectRef(SubjectKind.AD, "ad-2", "adset-1")
    facts = (
        FactClaim(
            "fb-status",
            None,
            FactCategory.ACTION_STATE,
            AD,
            Metric.EFFECTIVE_STATUS,
            "ACTIVE",
            SourceSystem.FACEBOOK,
            None,
        ),
        FactClaim(
            "fb-spend",
            None,
            FactCategory.BUSINESS_METRIC,
            AD,
            Metric.SPEND,
            Decimal("100"),
            SourceSystem.FACEBOOK,
            WINDOW,
            "USD",
        ),
        FactClaim(
            "amo-quals",
            None,
            FactCategory.BUSINESS_METRIC,
            AD,
            Metric.QUALS,
            1,
            SourceSystem.AMO,
            WINDOW,
        ),
        FactClaim(
            "cdp-payments",
            None,
            FactCategory.BUSINESS_METRIC,
            AD,
            Metric.PAYMENTS,
            1,
            SourceSystem.CDP_ERP,
            WINDOW,
            "LCY",
        ),
        FactClaim(
            "cdp-revenue",
            None,
            FactCategory.BUSINESS_METRIC,
            AD,
            Metric.REVENUE,
            Decimal("0"),
            SourceSystem.CDP_ERP,
            WINDOW,
            "LCY",
        ),
    )
    records_by_source = {
        source: tuple(
            EvidenceRecord(
                category=claim.category,
                subject=claim.subject,
                metric=claim.metric,
                value=claim.value,
                source=claim.source,
                state=EvidenceState.FRESH_COMPLETE,
                observed_at=NOW,
                window=claim.window,
                currency=claim.currency,
            )
            for claim in facts
            if claim.source is source
        )
        for source in (SourceSystem.FACEBOOK, SourceSystem.AMO, SourceSystem.CDP_ERP)
    }
    records_by_source[SourceSystem.FACEBOOK] = (
        *records_by_source[SourceSystem.FACEBOOK],
        EvidenceRecord(
            FactCategory.ACTION_STATE,
            sibling,
            Metric.EFFECTIVE_STATUS,
            "ACTIVE",
            SourceSystem.FACEBOOK,
            EvidenceState.FRESH_COMPLETE,
            NOW,
            None,
            None,
        ),
    )
    provisional = approval_sources._assemble_bundle(
        (
            _source(SourceSystem.FACEBOOK, records_by_source[SourceSystem.FACEBOOK]),
            _source(SourceSystem.TRELLO),
            _source(SourceSystem.MEDIA_BYTES),
            _source(SourceSystem.AMO, records_by_source[SourceSystem.AMO]),
            _source(SourceSystem.CDP_ERP, records_by_source[SourceSystem.CDP_ERP]),
        ),
        NOW,
        1,
    )
    item = PauseManifest(
        kind=ActionKind.PAUSE,
        manifest_id="pause-1",
        origin=ActionOrigin.AUTOPILOT_CLASSIC,
        idempotency_key=key,
        prepared_at=NOW,
        ad_id="ad-1",
        adset_id="adset-1",
        reason_code="LOW_ROMI",
        expected_before_status="ACTIVE",
        expected_after_status="PAUSED",
        decision_window=WINDOW,
        facts=facts,
        pre_inventory_sha256=provisional.facebook_sha256,
        sibling_active_ids=("ad-2",),
    )
    initial_batch = ActionBatchManifest(
        batch_manifest_id="pause-batch",
        correlation_id="pause-checker-test",
        idempotency_key=key,
        prepared_at=NOW,
        subject_ids=("ad:ad-1",),
        actions=(item,),
        manifest_sha256="0" * 64,
    )
    batch = replace(initial_batch, manifest_sha256=manifest_sha256(initial_batch))
    monkeypatch.setattr(
        config, "REPORT_CHECKER_AUDIT_PATH", tmp_path / "approval.jsonl"
    )
    reserve_operation(batch, NOW)
    monkeypatch.setattr(approval_checker, "checker_health", lambda _now: _healthy())
    monkeypatch.setattr(
        approval_checker, "load_action_item_evidence", lambda *_args: provisional
    )

    review = approval_checker.review_action_item(batch, item, 0, NOW)

    assert review.decision is ApprovalDecision.DENIED
    assert review.audit_persisted is True
    assert "PAYMENTS_WITHOUT_REVENUE" in {issue.code for issue in review.issues}


def test_checker_health_fails_closed_on_corrupt_audit(monkeypatch, tmp_path):
    audit_path = tmp_path / "approval.jsonl"
    audit_path.write_text("not-json\n", encoding="utf-8")
    monkeypatch.setattr(config, "REPORT_CHECKER_AUDIT_PATH", audit_path)
    monkeypatch.setattr(
        config,
        "REPORT_CHECKER_RECONCILIATION_PATH",
        tmp_path / "reconciliation.json",
    )

    health = approval_checker.checker_health(NOW)

    assert health.audit_writable is False
    assert health.reconciliation_pending is True


def test_load_evidence_uses_exact_local_source_route(monkeypatch):
    calls: list[SourceSystem] = []

    def load_decisions(request, now):
        calls.append(request.required_sources[0])
        return _source(SourceSystem.DECISIONS_DB, fetched_at=now)

    monkeypatch.setattr(approval_sources, "load_decisions_evidence", load_decisions)
    request = EvidenceRequest(
        request_id="local-route",
        purpose="REPORT",
        action_kind=None,
        generated_at=NOW,
        subjects=(),
        claims=(),
        required_sources=(SourceSystem.DECISIONS_DB,),
        windows=(WINDOW,),
        account_ids=(),
        adset_ids=(),
        ad_ids=(),
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=False,
        force_live=False,
        max_age_seconds=300,
    )

    bundle = approval_sources.load_evidence(request, NOW)

    assert calls == [SourceSystem.DECISIONS_DB]
    assert tuple(source.source for source in bundle.sources) == (
        SourceSystem.DECISIONS_DB,
    )
    assert bundle.local_state_sha256 is not None


def _account_report_evidence_request(
    account_ids: tuple[str, ...] = ("account-1",),
) -> EvidenceRequest:
    facebook_claims = tuple(
        FactClaim(
            claim_id=f"fb-spend-{account_id}",
            field_id=f"fb-spend-{account_id}",
            category=FactCategory.BUSINESS_METRIC,
            subject=SubjectRef(SubjectKind.ACCOUNT, account_id),
            metric=Metric.SPEND,
            value=Decimal("100"),
            source=SourceSystem.FACEBOOK,
            window=WINDOW,
            currency="USD",
        )
        for account_id in account_ids
    )
    outcome_claims = (
        FactClaim(
            claim_id="amo-quals",
            field_id="amo-quals",
            category=FactCategory.BUSINESS_METRIC,
            subject=SubjectRef(SubjectKind.ACCOUNT, account_ids[0]),
            metric=Metric.QUALS,
            value=1,
            source=SourceSystem.AMO,
            window=WINDOW,
        ),
        FactClaim(
            claim_id="cdp-revenue",
            field_id="cdp-revenue",
            category=FactCategory.BUSINESS_METRIC,
            subject=SubjectRef(SubjectKind.ACCOUNT, account_ids[0]),
            metric=Metric.REVENUE,
            value=Decimal("1000"),
            source=SourceSystem.CDP_ERP,
            window=WINDOW,
            currency="LCY",
        ),
    )
    return EvidenceRequest(
        request_id="account-outcomes",
        purpose="REPORT",
        action_kind=None,
        generated_at=NOW,
        subjects=tuple(claim.subject for claim in (*facebook_claims, *outcome_claims)),
        claims=(*facebook_claims, *outcome_claims),
        required_sources=(
            SourceSystem.AMO,
            SourceSystem.CDP_ERP,
            SourceSystem.FACEBOOK,
        ),
        windows=(WINDOW,),
        account_ids=account_ids,
        adset_ids=(),
        ad_ids=(),
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=True,
        force_live=False,
        max_age_seconds=300,
    )


def _facebook_account_live(account_id: str = "account-1") -> SourceEvidence:
    """Свежее живое чтение FB по кабинету — без полного инвентаря объявлений."""

    return _source(
        SourceSystem.FACEBOOK,
        (
            EvidenceRecord(
                category=FactCategory.ACTION_STATE,
                subject=SubjectRef(SubjectKind.ACCOUNT, account_id),
                metric=Metric.CONFIGURED_STATUS,
                value="1",
                source=SourceSystem.FACEBOOK,
                state=EvidenceState.FRESH_COMPLETE,
                observed_at=NOW,
                window=None,
                currency=None,
            ),
        ),
    )


def _patch_outcome_universe(
    monkeypatch,
    *,
    amo_ids: tuple[str, ...] = (),
    cdp_ids: tuple[str, ...] = (),
    owned: frozenset[str] | None = None,
    ownership_calls: list[tuple[str, tuple[str, ...]]] | None = None,
) -> None:
    monkeypatch.setattr(
        approval_sources, "load_amo_window_ad_ids", lambda _windows: amo_ids
    )
    monkeypatch.setattr(
        approval_sources,
        "load_cdp_window_ad_ids",
        lambda _windows, *, force_live: cdp_ids,
    )

    def ownership(account_id, ad_ids):
        if ownership_calls is not None:
            ownership_calls.append((account_id, ad_ids))
        return frozenset(ad_ids) if owned is None else owned

    monkeypatch.setattr(approval_sources, "load_account_ad_ownership", ownership)


def test_account_report_checks_only_observed_ad_ids_for_ownership(monkeypatch):
    """Проверяются ровно ad_id окна, а не весь инвентарь кабинета."""

    request = _account_report_evidence_request()
    call_order: list[str] = []
    outcome_ad_ids: list[tuple[str, ...]] = []
    ownership_calls: list[tuple[str, tuple[str, ...]]] = []

    def load_facebook(_request, now, *, force_live):
        del now, force_live
        call_order.append("FACEBOOK")
        return _facebook_account_live()

    def load_amo(enriched, now, *, force_live):
        del force_live
        call_order.append("AMO")
        outcome_ad_ids.append(enriched.ad_ids)
        return _source(SourceSystem.AMO, fetched_at=now)

    def load_cdp(enriched, now, *, force_live):
        del force_live
        call_order.append("CDP")
        outcome_ad_ids.append(enriched.ad_ids)
        return _source(SourceSystem.CDP_ERP, fetched_at=now)

    monkeypatch.setattr(approval_sources, "load_facebook_evidence", load_facebook)
    monkeypatch.setattr(approval_sources, "load_amo_evidence", load_amo)
    monkeypatch.setattr(approval_sources, "load_cdp_evidence", load_cdp)
    _patch_outcome_universe(
        monkeypatch,
        amo_ids=("ad-1", "name:creative", "ad-1"),
        cdp_ids=("ad-2",),
        owned=frozenset({"ad-1", "ad-2"}),
        ownership_calls=ownership_calls,
    )

    bundle = approval_sources.load_evidence(request, NOW)

    assert call_order == ["FACEBOOK", "AMO", "CDP"]
    assert ownership_calls == [("account-1", ("ad-1", "ad-2"))]
    assert outcome_ad_ids == [("ad-1", "ad-2"), ("ad-1", "ad-2")]
    assert tuple(source.source for source in bundle.sources) == request.required_sources


def test_account_report_drops_foreign_ad_ids_without_breaking_outcomes(monkeypatch):
    """Чужой/несуществующий ad_id — минус один id, а не отказ источника."""

    request = _account_report_evidence_request()
    outcome_ad_ids: list[tuple[str, ...]] = []

    def load_outcome(source):
        def loader(enriched, now, *, force_live):
            del force_live
            outcome_ad_ids.append(enriched.ad_ids)
            return _source(source, fetched_at=now)

        return loader

    monkeypatch.setattr(
        approval_sources,
        "load_facebook_evidence",
        lambda *_args, **_kwargs: _facebook_account_live(),
    )
    monkeypatch.setattr(
        approval_sources, "load_amo_evidence", load_outcome(SourceSystem.AMO)
    )
    monkeypatch.setattr(
        approval_sources, "load_cdp_evidence", load_outcome(SourceSystem.CDP_ERP)
    )
    _patch_outcome_universe(
        monkeypatch,
        amo_ids=("ad-1", "ad-foreign", "ad-deleted"),
        owned=frozenset({"ad-1"}),
    )

    bundle = approval_sources.load_evidence(request, NOW)
    by_source = {source.source: source for source in bundle.sources}

    assert outcome_ad_ids == [("ad-1",), ("ad-1",)]
    assert by_source[SourceSystem.AMO].state is EvidenceState.FRESH_COMPLETE
    assert by_source[SourceSystem.CDP_ERP].state is EvidenceState.FRESH_COMPLETE


def test_account_report_amo_aggregate_survives_account_with_huge_inventory(
    monkeypatch,
):
    """Регресс: кабинет на 14k объявлений больше не роняет amo_quals.

    Раньше universe тянулся из полного инвентаря и упирался в потолок 2000 —
    AMO деградировал в INCOMPLETE. Теперь проверяются только ad_id из лидов.
    """

    account = SubjectRef(SubjectKind.ACCOUNT, "account-1")
    base = _account_report_evidence_request()
    claims = tuple(
        claim for claim in base.claims if claim.source is not SourceSystem.CDP_ERP
    )
    request = replace(
        base,
        claims=claims,
        subjects=tuple(claim.subject for claim in claims),
        required_sources=(SourceSystem.AMO, SourceSystem.FACEBOOK),
    )
    created_at = int((NOW - timedelta(days=1)).timestamp())

    def _lead(lead_id: int, ad_id: str) -> dict[str, object]:
        return {
            "id": lead_id,
            "created_at": created_at,
            "updated_at": created_at,
            "custom_fields_values": [
                {
                    "field_id": 902422,
                    "field_name": "fb_ad_id",
                    "values": [{"value": ad_id}],
                }
            ],
        }

    leads = [_lead(1, "ad-1"), _lead(2, "ad-1"), _lead(3, "ad-foreign")]
    checked: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        approval_sources,
        "load_facebook_evidence",
        lambda *_args, **_kwargs: _facebook_account_live(),
    )
    monkeypatch.setattr(approval_source_amo, "_raw_leads_page", lambda _params: leads)
    monkeypatch.setattr(
        approval_source_amo.amo, "_is_qualified", lambda lead: lead["id"] == 1
    )

    def ownership(account_id, ad_ids):
        assert account_id == "account-1"
        checked.append(ad_ids)
        return frozenset({"ad-1"})

    monkeypatch.setattr(approval_sources, "load_account_ad_ownership", ownership)

    bundle = approval_sources.load_evidence(request, NOW)
    amo = next(
        source for source in bundle.sources if source.source is SourceSystem.AMO
    )
    aggregate = {
        record.metric: record.value
        for record in amo.records
        if record.subject == account
    }

    assert checked == [("ad-1", "ad-foreign")]
    assert amo.state is EvidenceState.FRESH_COMPLETE
    assert amo.complete is True
    assert aggregate == {Metric.QUALS: 1}


@pytest.mark.parametrize(
    "case",
    [
        "incomplete",
        "cached",
        "multiple_accounts",
        "no_candidates",
        "all_foreign",
        "candidates_unreadable",
        "ownership_unreadable",
        "too_many_candidates",
    ],
)
def test_account_report_unproven_ad_universe_blocks_outcome_sources(monkeypatch, case):
    account_ids = (
        ("account-1", "account-2") if case == "multiple_accounts" else ("account-1",)
    )
    request = _account_report_evidence_request(account_ids)
    facebook = _facebook_account_live()
    if case == "incomplete":
        facebook = _source(
            SourceSystem.FACEBOOK,
            state=EvidenceState.INCOMPLETE,
            complete=False,
        )
    elif case == "cached":
        facebook = replace(facebook, from_cache=True)
    elif case == "multiple_accounts":
        facebook = _source(
            SourceSystem.FACEBOOK,
            (
                *_facebook_account_live("account-1").records,
                *_facebook_account_live("account-2").records,
            ),
        )
    expected_code = {
        "incomplete": "FB_ACCOUNT_OWNERSHIP_SOURCE_INCOMPLETE",
        "cached": "FB_ACCOUNT_OWNERSHIP_SOURCE_INCOMPLETE",
        "multiple_accounts": "FB_ACCOUNT_OWNERSHIP_ACCOUNT_MISMATCH",
        "no_candidates": "FB_ACCOUNT_OWNERSHIP_NO_CANDIDATES",
        "all_foreign": "FB_ACCOUNT_OWNERSHIP_EMPTY",
        "candidates_unreadable": "OUTCOME_CANDIDATES_AMO_AMO_PAGE_LIMIT_EXCEEDED",
        "ownership_unreadable": "FB_ACCOUNT_OWNERSHIP_UNRESOLVED_FB_HTTP_500",
        "too_many_candidates": "FB_ACCOUNT_OWNERSHIP_TOO_MANY_CANDIDATES",
    }[case]

    candidates: tuple[str, ...] = ("ad-1",)
    if case == "no_candidates":
        candidates = ()
    elif case == "too_many_candidates":
        candidates = tuple(
            f"ad-{index}"
            for index in range(approval_sources._MAX_OWNERSHIP_CANDIDATE_IDS + 1)
        )
    _patch_outcome_universe(
        monkeypatch,
        amo_ids=candidates,
        owned=frozenset() if case == "all_foreign" else None,
    )
    if case == "candidates_unreadable":

        def broken_candidates(_windows):
            raise approval_sources.AmoEvidenceError("AMO_PAGE_LIMIT_EXCEEDED")

        monkeypatch.setattr(
            approval_sources, "load_amo_window_ad_ids", broken_candidates
        )
    if case == "ownership_unreadable":

        def broken_ownership(_account_id, _ad_ids):
            raise approval_sources.FacebookEvidenceError("FB_HTTP_500")

        monkeypatch.setattr(
            approval_sources, "load_account_ad_ownership", broken_ownership
        )
    if case == "too_many_candidates":

        def unexpected_ownership(_account_id, _ad_ids):
            raise AssertionError("потолок обязан отсекать до запросов в Graph")

        monkeypatch.setattr(
            approval_sources, "load_account_ad_ownership", unexpected_ownership
        )
    dependent_calls = 0

    def unexpected_outcome(*_args, **_kwargs):
        nonlocal dependent_calls
        dependent_calls += 1
        raise AssertionError("AMO/CDP не должны читаться без exact FB universe")

    monkeypatch.setattr(
        approval_sources,
        "load_facebook_evidence",
        lambda *_args, **_kwargs: facebook,
    )
    monkeypatch.setattr(approval_sources, "load_amo_evidence", unexpected_outcome)
    monkeypatch.setattr(approval_sources, "load_cdp_evidence", unexpected_outcome)

    bundle = approval_sources.load_evidence(request, NOW)
    by_source = {source.source: source for source in bundle.sources}

    assert dependent_calls == 0
    assert by_source[SourceSystem.AMO].state is EvidenceState.INCOMPLETE
    assert by_source[SourceSystem.CDP_ERP].state is EvidenceState.INCOMPLETE
    assert by_source[SourceSystem.AMO].complete is False
    assert by_source[SourceSystem.CDP_ERP].complete is False
    assert by_source[SourceSystem.AMO].error_code == expected_code
    assert by_source[SourceSystem.CDP_ERP].error_code == expected_code


def _launch_bundle_for_trello_state(monkeypatch, tmp_path, *, entity_ids, record_value=None):
    sha = "a" * 64
    body_sha = "b" * 64
    manifest_id = "launch-manifest"
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
        content_sha256=sha,
    )
    creative = CreativeSpec(
        creative_id="creative-1",
        order_index=0,
        ad_name="exact-name",
        media_asset_ids=(media.asset_id,),
        body_staged_relative_path="text/body.txt",
        body_sha256=body_sha,
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
        capacity_available=10,
        hard_reserve_slots=1,
        creatives=(creative,),
        duplicate_signature="c" * 64,
    )
    media_manifest_sha = approval_sources.hashlib.sha256(
        approval_sources.canonical_json(
            {
                "assets": (media,),
                "body_relative_path": creative.body_staged_relative_path,
                "body_sha256": body_sha,
            }
        )
    ).hexdigest()
    trello = TrelloPrecondition(
        card_id="card-1",
        board_id="board-1",
        ready_list_id="ready-1",
        expected_list_id="ready-1",
        expected_due_complete=False,
        expected_closed=False,
        date_last_activity=NOW,
        attachment_ids=("attachment-1",),
        attachment_manifest_sha256="d" * 64,
        labels_sha256="e" * 64,
        card_content_sha256="f" * 64,
    )
    key = str(uuid.uuid4())
    root = tmp_path / "staging"
    monkeypatch.setattr(config, "REPORT_CHECKER_STAGING_ROOT", root)
    item = LaunchManifest(
        kind=ActionKind.LAUNCH,
        manifest_id=manifest_id,
        origin=ActionOrigin.AUTO_LAUNCH,
        idempotency_key=key,
        prepared_at=NOW,
        config_version_sha256=sha,
        staging_root=str(root),
        staging_directory=str(root / manifest_id),
        trello=trello,
        card_name_sha256=sha,
        media_manifest_sha256=media_manifest_sha,
        media_assets=(media,),
        campaign_type="PRODA",
        destinations=(destination,),
    )
    initial_batch = ActionBatchManifest(
        batch_manifest_id="launch-batch",
        correlation_id="launch-checker-test",
        idempotency_key=key,
        prepared_at=NOW,
        subject_ids=("card:card-1",),
        actions=(item,),
        manifest_sha256="0" * 64,
    )
    batch = replace(initial_batch, manifest_sha256=manifest_sha256(initial_batch))
    trello_record = EvidenceRecord(
        FactCategory.MATCH,
        SubjectRef(SubjectKind.CARD, "card-1"),
        Metric.MATCH_STATE,
        record_value if record_value is not None else trello.card_content_sha256,
        SourceSystem.TRELLO,
        EvidenceState.FRESH_COMPLETE,
        NOW,
        None,
        None,
        entity_ids(trello),
    )
    media_records = (
        EvidenceRecord(
            FactCategory.MATCH,
            SubjectRef(SubjectKind.MEDIA, "asset-1", manifest_id),
            Metric.MATCH_STATE,
            sha,
            SourceSystem.MEDIA_BYTES,
            EvidenceState.FRESH_COMPLETE,
            NOW,
            None,
            None,
        ),
        EvidenceRecord(
            FactCategory.MATCH,
            SubjectRef(SubjectKind.MEDIA, f"body:{body_sha}", manifest_id),
            Metric.DISPLAY_CONTEXT,
            body_sha,
            SourceSystem.MEDIA_BYTES,
            EvidenceState.FRESH_COMPLETE,
            NOW,
            None,
            None,
        ),
    )
    generic_bundle = approval_sources._assemble_bundle(
        (
            _source(SourceSystem.FACEBOOK),
            _source(SourceSystem.TRELLO, (trello_record,)),
            _source(SourceSystem.MEDIA_BYTES, media_records),
            _source(SourceSystem.AMO),
            _source(SourceSystem.CDP_ERP),
        ),
        NOW,
        1,
    )
    manifest_reads = 0

    def manifest_precondition(_item, observed_at):
        nonlocal manifest_reads
        manifest_reads += 1
        return ActionObservation(
            observed_at=observed_at,
            digest="1" * 64,
            target_state="ACTIVE",
            subject_ids=("adset-1",),
            unrelated_state_digest="2" * 64,
        )

    monkeypatch.setattr(
        approval_sources, "load_evidence", lambda *_args: generic_bundle
    )
    monkeypatch.setattr(
        approval_sources, "read_action_precondition", manifest_precondition
    )

    return approval_sources.load_action_item_evidence(batch, item, 0, NOW)


def _trello_record(bundle):
    trello = next(source for source in bundle.sources if source.source is SourceSystem.TRELLO)
    return trello, next(
        (row for row in trello.records if row.metric is Metric.MATCH_STATE and row.subject.kind is SubjectKind.CARD),
        None,
    )


def _base_entities(trello, **overrides):
    values = {
        "board": "board-1", "list": "ready-1", "closed": "false",
        "attachments": trello.attachment_manifest_sha256, "labels": trello.labels_sha256,
    }
    values.update(overrides)
    return tuple(f"{key}:{value}" for key, value in values.items())


def test_checkmark_and_activity_are_not_launch_drift(monkeypatch, tmp_path):
    """Галочка после первого города и свежая дата активности не ломают дозапуск остальных городов.

    Разбор: сверщик ставил dueComplete → precondition видел дрейф → источник INCOMPLETE →
    исполнитель молча крутил задание до истечения TTL.
    """

    bundle = _launch_bundle_for_trello_state(
        monkeypatch, tmp_path,
        entity_ids=lambda t: (*_base_entities(t), "due-complete:true", "activity:2026-09-22T09:00:00+00:00"),
    )
    trello, record = _trello_record(bundle)
    assert trello.complete and record.value == "f" * 64


def test_real_card_drift_is_terminal_not_source_unavailable(monkeypatch, tmp_path):
    """Изменились вложения или текст — источник остаётся полным, а запись MATCH_STATE говорит DRIFT.

    Полный источник с DRIFT доходит до правил (LAUNCH_INPUT_DRIFT) и закрывает задание с сообщением,
    а не уходит в транзиентный повтор как «Trello недоступен».
    """

    bundle = _launch_bundle_for_trello_state(
        monkeypatch, tmp_path, entity_ids=lambda t: _base_entities(t, attachments="9" * 64),
    )
    trello, record = _trello_record(bundle)
    assert trello.complete and trello.error_code is None
    assert record.value == "DRIFT"

    changed_text = _launch_bundle_for_trello_state(
        monkeypatch, tmp_path, entity_ids=_base_entities, record_value="0" * 64,
    )
    trello2, record2 = _trello_record(changed_text)
    assert trello2.complete and record2.value == "DRIFT"


def test_legacy_manifest_fingerprint_still_matches(monkeypatch, tmp_path):
    """Манифест, собранный по старой схеме отпечатка, сверяется через content-legacy."""

    bundle = _launch_bundle_for_trello_state(
        monkeypatch, tmp_path,
        entity_ids=lambda t: (*_base_entities(t), "content-legacy:" + "f" * 64),
        record_value="1" * 64,  # новый отпечаток живой карточки другой, но legacy совпал
    )
    trello, record = _trello_record(bundle)
    assert trello.complete and record.value == "1" * 64


def test_launch_duplicate_match_requires_manifest_aware_precondition(
    monkeypatch, tmp_path
):
    sha = "a" * 64
    body_sha = "b" * 64
    manifest_id = "launch-manifest"
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
        content_sha256=sha,
    )
    creative = CreativeSpec(
        creative_id="creative-1",
        order_index=0,
        ad_name="exact-name",
        media_asset_ids=(media.asset_id,),
        body_staged_relative_path="text/body.txt",
        body_sha256=body_sha,
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
        capacity_available=10,
        hard_reserve_slots=1,
        creatives=(creative,),
        duplicate_signature="c" * 64,
    )
    media_manifest_sha = approval_sources.hashlib.sha256(
        approval_sources.canonical_json(
            {
                "assets": (media,),
                "body_relative_path": creative.body_staged_relative_path,
                "body_sha256": body_sha,
            }
        )
    ).hexdigest()
    trello = TrelloPrecondition(
        card_id="card-1",
        board_id="board-1",
        ready_list_id="ready-1",
        expected_list_id="ready-1",
        expected_due_complete=False,
        expected_closed=False,
        date_last_activity=NOW,
        attachment_ids=("attachment-1",),
        attachment_manifest_sha256="d" * 64,
        labels_sha256="e" * 64,
        card_content_sha256="f" * 64,
    )
    key = str(uuid.uuid4())
    root = tmp_path / "staging"
    monkeypatch.setattr(config, "REPORT_CHECKER_STAGING_ROOT", root)
    item = LaunchManifest(
        kind=ActionKind.LAUNCH,
        manifest_id=manifest_id,
        origin=ActionOrigin.AUTO_LAUNCH,
        idempotency_key=key,
        prepared_at=NOW,
        config_version_sha256=sha,
        staging_root=str(root),
        staging_directory=str(root / manifest_id),
        trello=trello,
        card_name_sha256=sha,
        media_manifest_sha256=media_manifest_sha,
        media_assets=(media,),
        campaign_type="PRODA",
        destinations=(destination,),
    )
    initial_batch = ActionBatchManifest(
        batch_manifest_id="launch-batch",
        correlation_id="launch-checker-test",
        idempotency_key=key,
        prepared_at=NOW,
        subject_ids=("card:card-1",),
        actions=(item,),
        manifest_sha256="0" * 64,
    )
    batch = replace(initial_batch, manifest_sha256=manifest_sha256(initial_batch))
    trello_record = EvidenceRecord(
        FactCategory.MATCH,
        SubjectRef(SubjectKind.CARD, "card-1"),
        Metric.MATCH_STATE,
        trello.card_content_sha256,
        SourceSystem.TRELLO,
        EvidenceState.FRESH_COMPLETE,
        NOW,
        None,
        None,
        (
            "board:board-1",
            "list:ready-1",
            "due-complete:false",
            "closed:false",
            f"activity:{NOW.isoformat()}",
            f"attachments:{trello.attachment_manifest_sha256}",
            f"labels:{trello.labels_sha256}",
        ),
    )
    media_records = (
        EvidenceRecord(
            FactCategory.MATCH,
            SubjectRef(SubjectKind.MEDIA, "asset-1", manifest_id),
            Metric.MATCH_STATE,
            sha,
            SourceSystem.MEDIA_BYTES,
            EvidenceState.FRESH_COMPLETE,
            NOW,
            None,
            None,
        ),
        EvidenceRecord(
            FactCategory.MATCH,
            SubjectRef(SubjectKind.MEDIA, f"body:{body_sha}", manifest_id),
            Metric.DISPLAY_CONTEXT,
            body_sha,
            SourceSystem.MEDIA_BYTES,
            EvidenceState.FRESH_COMPLETE,
            NOW,
            None,
            None,
        ),
    )
    generic_bundle = approval_sources._assemble_bundle(
        (
            _source(SourceSystem.FACEBOOK),
            _source(SourceSystem.TRELLO, (trello_record,)),
            _source(SourceSystem.MEDIA_BYTES, media_records),
            _source(SourceSystem.AMO),
            _source(SourceSystem.CDP_ERP),
        ),
        NOW,
        1,
    )
    manifest_reads = 0

    def manifest_precondition(_item, observed_at):
        nonlocal manifest_reads
        manifest_reads += 1
        return ActionObservation(
            observed_at=observed_at,
            digest="1" * 64,
            target_state="ACTIVE",
            subject_ids=("adset-1",),
            unrelated_state_digest="2" * 64,
        )

    monkeypatch.setattr(
        approval_sources, "load_evidence", lambda *_args: generic_bundle
    )
    monkeypatch.setattr(
        approval_sources, "read_action_precondition", manifest_precondition
    )

    bundle = approval_sources.load_action_item_evidence(batch, item, 0, NOW)

    facebook = next(
        source for source in bundle.sources if source.source is SourceSystem.FACEBOOK
    )
    assert manifest_reads == 2
    assert bundle.facebook_sha256 == "1" * 64
    assert any(
        record.metric is Metric.MATCH_STATE and record.value == "NO_DUPLICATE"
        for record in facebook.records
    )
