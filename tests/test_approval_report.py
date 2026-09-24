from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from services.approval_checker_models import (
    CheckIssue,
    ClaimOutcome,
    ClaimState,
    FactCategory,
    FactClaim,
    FieldFormat,
    FieldLabelTemplate,
    Metric,
    RenderedReport,
    ReportCheckRequest,
    ReportCheckResult,
    ReportField,
    ReportSection,
    ReportTemplate,
    ReportVerdict,
    SectionTemplate,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TypedReportPayload,
    report_manifest_sha256,
)
from services.approval_report import (
    render_checked_report,
    render_safe_notice,
    validate_report_coverage,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
AD = SubjectRef(SubjectKind.AD, "ad-1", "adset-1")


def _request(
    specs: tuple[
        tuple[str, FactCategory, FieldLabelTemplate, FieldFormat, Metric, object, bool],
        ...,
    ],
) -> ReportCheckRequest:
    fields = tuple(
        ReportField(
            field_id=field_id,
            section_id="summary",
            category=category,
            label=label,
            format=field_format,
            subject=AD,
            metric=metric,
            value=value,
            source=SourceSystem.FACEBOOK,
            window=None,
            required=required,
        )
        for field_id, category, label, field_format, metric, value, required in specs
    )
    claims = tuple(
        FactClaim(
            claim_id=f"claim-{field.field_id}",
            field_id=field.field_id,
            category=field.category,
            subject=field.subject,
            metric=field.metric,
            value=field.value,
            source=field.source,
            window=field.window,
            currency=field.currency,
            required=field.required,
        )
        for field in fields
    )
    payload = TypedReportPayload(
        template=ReportTemplate.AUTOPILOT,
        sections=(
            ReportSection(
                section_id="summary",
                template=SectionTemplate.SUMMARY,
                field_ids=tuple(field.field_id for field in fields),
                window=None,
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


def _result(
    request: ReportCheckRequest,
    verdict: ReportVerdict,
    states: tuple[ClaimState, ...],
) -> ReportCheckResult:
    outcomes = tuple(
        ClaimOutcome(
            claim, state, claim.value if state is ClaimState.MATCH else None, ()
        )
        for claim, state in zip(request.claims, states, strict=True)
    )
    return ReportCheckResult(
        check_id="check-1",
        verdict=verdict,
        checked_at=NOW,
        outcomes=outcomes,
        issues=(),
        manifest_sha256=request.manifest_sha256,
        audit_persisted=True,
    )


def test_validate_report_coverage_accepts_exact_bijection() -> None:
    request = _request(
        (
            (
                "spend",
                FactCategory.BUSINESS_METRIC,
                FieldLabelTemplate.SPEND,
                FieldFormat.USD,
                Metric.SPEND,
                Decimal("10.5"),
                True,
            ),
        )
    )

    coverage = validate_report_coverage(request)

    assert coverage.complete is True
    assert coverage.issues == ()


def test_validate_report_coverage_rejects_orphan_after_runtime_tamper() -> None:
    request = _request(
        (
            (
                "spend",
                FactCategory.BUSINESS_METRIC,
                FieldLabelTemplate.SPEND,
                FieldFormat.USD,
                Metric.SPEND,
                Decimal("10"),
                True,
            ),
        )
    )
    object.__setattr__(request, "claims", ())

    coverage = validate_report_coverage(request)

    assert coverage.complete is False
    assert {issue.code for issue in coverage.issues} == {"REPORT_COVERAGE_INCOMPLETE"}


def test_validate_report_coverage_rejects_arbitrary_label() -> None:
    request = _request(
        (
            (
                "name",
                FactCategory.DISPLAY_CONTEXT,
                FieldLabelTemplate.DISPLAY_NAME,
                FieldFormat.TEXT,
                Metric.DISPLAY_CONTEXT,
                "Креатив",
                True,
            ),
        )
    )
    object.__setattr__(request.payload.fields[0], "label", "Произвольный label")

    coverage = validate_report_coverage(request)

    assert coverage.complete is False
    assert any("label" in issue.message for issue in coverage.issues)


def test_verified_renderer_outputs_every_field_in_declared_order() -> None:
    request = _request(
        (
            (
                "name",
                FactCategory.DISPLAY_CONTEXT,
                FieldLabelTemplate.DISPLAY_NAME,
                FieldFormat.TEXT,
                Metric.DISPLAY_CONTEXT,
                "Креатив 1",
                True,
            ),
            (
                "spend",
                FactCategory.BUSINESS_METRIC,
                FieldLabelTemplate.SPEND,
                FieldFormat.USD,
                Metric.SPEND,
                Decimal("459"),
                True,
            ),
        )
    )
    result = _result(
        request, ReportVerdict.VERIFIED, (ClaimState.MATCH, ClaimState.MATCH)
    )

    rendered = render_checked_report(request, result)

    assert rendered.verdict is ReportVerdict.VERIFIED
    assert rendered.rendered_field_ids == ("name", "spend")
    assert "Название: Креатив 1" in rendered.text
    assert "Расход: $459" in rendered.text


def test_limited_renderer_outputs_all_and_only_match_fields() -> None:
    request = _request(
        (
            (
                "name",
                FactCategory.DISPLAY_CONTEXT,
                FieldLabelTemplate.DISPLAY_NAME,
                FieldFormat.TEXT,
                Metric.DISPLAY_CONTEXT,
                "Креатив 1",
                True,
            ),
            (
                "match",
                FactCategory.MATCH,
                FieldLabelTemplate.MATCH,
                FieldFormat.STATUS,
                Metric.MATCH_STATE,
                "EXACT",
                True,
            ),
            (
                "spend",
                FactCategory.BUSINESS_METRIC,
                FieldLabelTemplate.SPEND,
                FieldFormat.USD,
                Metric.SPEND,
                Decimal("459"),
                False,
            ),
        )
    )
    result = _result(
        request,
        ReportVerdict.VERIFIED_WITH_LIMITATIONS,
        (ClaimState.MATCH, ClaimState.MATCH, ClaimState.MISMATCH),
    )

    rendered = render_checked_report(request, result)

    assert rendered.verdict is ReportVerdict.VERIFIED_WITH_LIMITATIONS
    assert rendered.rendered_field_ids == ("name", "match")
    assert "Креатив 1" in rendered.text
    assert "EXACT" in rendered.text
    assert "$459" not in rendered.text


def test_limited_renderer_without_safe_match_returns_blocked_notice() -> None:
    request = _request(
        (
            (
                "spend",
                FactCategory.BUSINESS_METRIC,
                FieldLabelTemplate.SPEND,
                FieldFormat.USD,
                Metric.SPEND,
                Decimal("459"),
                True,
            ),
        )
    )
    result = _result(
        request, ReportVerdict.VERIFIED_WITH_LIMITATIONS, (ClaimState.NOT_VERIFIABLE,)
    )

    rendered = render_checked_report(request, result)

    assert rendered.verdict is ReportVerdict.BLOCKED
    assert rendered.rendered_field_ids == ()
    assert "459" not in rendered.text


def test_renderer_rejects_missing_outcome_instead_of_omitting_field() -> None:
    request = _request(
        (
            (
                "spend",
                FactCategory.BUSINESS_METRIC,
                FieldLabelTemplate.SPEND,
                FieldFormat.USD,
                Metric.SPEND,
                Decimal("459"),
                True,
            ),
        )
    )
    result = _result(request, ReportVerdict.VERIFIED, (ClaimState.MATCH,))
    object.__setattr__(result, "outcomes", ())

    rendered = render_checked_report(request, result)

    assert rendered.verdict is ReportVerdict.BLOCKED
    assert rendered.rendered_field_ids == ()


def test_safe_notice_never_contains_issue_message_or_disputed_value() -> None:
    request = _request(
        (
            (
                "spend",
                FactCategory.BUSINESS_METRIC,
                FieldLabelTemplate.SPEND,
                FieldFormat.USD,
                Metric.SPEND,
                Decimal("459"),
                True,
            ),
        )
    )
    result = _result(request, ReportVerdict.BLOCKED, (ClaimState.MISMATCH,))
    object.__setattr__(
        result,
        "issues",
        (CheckIssue("SECRET", "Креатив 459 и спорное имя", True),),
    )

    rendered: RenderedReport = render_safe_notice(result)

    assert "459" not in rendered.text
    assert "спорное имя" not in rendered.text
    assert rendered.rendered_field_ids == ()
