"""Интеграция утреннего отчёта с независимым Approval Checker."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from services.approval_checker_models import (
    ActionResult,
    ReportCheckResult,
    ReportTemplate,
    ReportVerdict,
)
from services.approval_report import validate_report_coverage


_TZ = timezone(timedelta(hours=5))
_NOW = datetime(2026, 7, 22, 8, 5, tzinfo=_TZ)


def _metrics() -> dict[str, object]:
    start = datetime(2026, 7, 21, 0, 0, tzinfo=_TZ)
    return {
        "window_start": start,
        "window_end": start + timedelta(days=1),
        "date": "2026-07-21",
        "fb_spend": 90.5,
        "google_spend": 10.0,
        "google_last_known": None,
        "leads": 9,
        "quals": 2,
        "payments_amo_status": 1,
    }


def _request(history=()):
    from services.morning_digest import build_morning_digest_request

    with (
        patch("services.fb_token_provider.get_fb_account_id", return_value="123"),
        patch("services.approval_audit.read_action_history", return_value=history),
    ):
        return build_morning_digest_request(_NOW, _metrics())


def _result(request, *, persisted: bool) -> ReportCheckResult:
    return ReportCheckResult(
        check_id="check-morning",
        verdict=ReportVerdict.BLOCKED,
        checked_at=_NOW,
        outcomes=(),
        issues=(),
        manifest_sha256=request.manifest_sha256,
        audit_persisted=persisted,
    )


def test_morning_request_is_typed_and_mixed_windows_are_explicit():
    request = _request()

    assert request.payload.template is ReportTemplate.MORNING
    assert request.payload.mixed_windows_explicit is True
    assert validate_report_coverage(request).complete is True
    assert {field.field_id for field in request.payload.fields} == {
        claim.field_id for claim in request.claims
    }
    # AMO status «оплата» не попадает в checked report без strict CDP entity.
    assert all(field.metric.value != "PAYMENTS" for field in request.payload.fields)


def test_morning_history_gap_is_pending_and_not_counted_as_success():
    history = (
        SimpleNamespace(
            result=ActionResult.CONFIRMED,
            reconciliation_required=False,
            completed_at=_NOW - timedelta(hours=2),
        ),
        SimpleNamespace(
            result=ActionResult.UNKNOWN,
            reconciliation_required=True,
            completed_at=None,
        ),
    )
    request = _request(history)
    values = {field.field_id: field.value for field in request.payload.fields}

    assert values["actions.confirmed_count"] == 1
    assert values["actions.history_state"] == "PENDING"


def test_morning_unreadable_history_is_not_reported_as_zero():
    from services.morning_digest import build_morning_digest_request

    with (
        patch("services.fb_token_provider.get_fb_account_id", return_value="123"),
        patch(
            "services.approval_audit.read_action_history",
            side_effect=OSError("broken WAL"),
        ),
    ):
        request = build_morning_digest_request(_NOW, _metrics())

    values = {field.field_id: field.value for field in request.payload.fields}
    assert values["actions.confirmed_count"] is None
    assert values["actions.history_state"] == "UNAVAILABLE"


def test_send_morning_uses_checked_pipeline_without_raw_transport():
    from services.morning_digest import send_morning_digest

    request = _request()
    result = _result(request, persisted=True)
    rendered = SimpleNamespace(name="rendered")
    with (
        patch(
            "services.morning_digest.build_morning_digest",
            return_value={"check_request": request},
        ),
        patch("services.approval_checker.check_report", return_value=result) as checker,
        patch(
            "services.approval_report.render_checked_report", return_value=rendered
        ) as renderer,
        patch(
            "services.approval_telegram.send_checked_report",
            return_value=SimpleNamespace(sent=True),
        ) as checked_sender,
        patch("services.notifications.send_telegram") as raw_sender,
    ):
        assert send_morning_digest() is True

    checker.assert_called_once_with(request)
    renderer.assert_called_once_with(request, result)
    checked_sender.assert_called_once_with(rendered, channel="ads")
    raw_sender.assert_not_called()


def test_morning_audit_failure_uses_safe_fact_free_facade():
    from services.morning_digest import send_morning_digest

    request = _request()
    result = _result(request, persisted=False)
    with (
        patch(
            "services.morning_digest.build_morning_digest",
            return_value={"check_request": request},
        ),
        patch("services.approval_checker.check_report", return_value=result),
        patch(
            "services.approval_report.render_checked_report",
            return_value=SimpleNamespace(),
        ),
        patch(
            "services.approval_telegram.send_checked_report",
            return_value=SimpleNamespace(sent=False),
        ),
        patch(
            "services.approval_telegram.send_fact_free",
            return_value=SimpleNamespace(sent=True),
        ) as safe_sender,
    ):
        assert send_morning_digest() is True

    safe_sender.assert_called_once()
