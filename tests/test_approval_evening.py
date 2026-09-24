"""Интеграция вечернего отчёта с независимым Approval Checker."""

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
_NOW = datetime(2026, 7, 22, 21, 0, tzinfo=_TZ)


def _windows() -> dict:
    return {
        "today": {"fb_spend": 120.5, "leads": 10, "quals": 2, "payments": 1},
        "yesterday": {"fb_spend": 100.0, "leads": 8, "quals": 1, "payments": 0},
    }


def _request():
    from services.evening_report import build_evening_report_request

    with (
        patch("services.fb_token_provider.get_fb_account_id", return_value="123"),
        patch("services.approval_audit.read_action_history", return_value=()),
    ):
        return build_evening_report_request(_NOW, _windows())


def _result(request, *, persisted: bool) -> ReportCheckResult:
    return ReportCheckResult(
        check_id="check-evening",
        verdict=ReportVerdict.BLOCKED,
        checked_at=_NOW,
        outcomes=(),
        issues=(),
        manifest_sha256=request.manifest_sha256,
        audit_persisted=persisted,
    )


def test_evening_request_has_exact_bijection_and_explicit_windows():
    request = _request()

    assert request.payload.template is ReportTemplate.EVENING
    assert request.payload.mixed_windows_explicit is True
    assert validate_report_coverage(request).complete is True
    assert {field.field_id for field in request.payload.fields} == {
        claim.field_id for claim in request.claims
    }
    # Пять несводимых периодов: сутки кабинета FB (открытые и закрытые), окна
    # AMO-лидов (с 12:00 локальных суток) и окно действий. Расход FB
    # больше не подписывается окном лидов — он меряется сутками кабинета.
    sections_by_id = {
        section.section_id: section for section in request.payload.sections
    }
    assert len({section.window for section in request.payload.sections}) == 5
    assert sections_by_id["facebook_today"].window.semantic == "fb_account_open_day"
    assert (
        sections_by_id["facebook_yesterday"].window.semantic
        == "fb_account_closed_day"
    )
    assert sections_by_id["outcomes_yesterday"].window.semantic == "closed_local_day"
    # Статус «оплата» из AMO не является доказательством платежа и не отправляется.
    assert all(field.metric.value != "PAYMENTS" for field in request.payload.fields)


def test_evening_history_counts_only_confirmed_wal_results():
    from services.evening_report import build_evening_report_request

    history = (
        SimpleNamespace(
            result=ActionResult.CONFIRMED,
            reconciliation_required=False,
            completed_at=_NOW - timedelta(hours=1),
        ),
        SimpleNamespace(
            result=ActionResult.UNKNOWN,
            reconciliation_required=True,
            completed_at=None,
        ),
    )
    with (
        patch("services.fb_token_provider.get_fb_account_id", return_value="123"),
        patch("services.approval_audit.read_action_history", return_value=history),
    ):
        request = build_evening_report_request(_NOW, _windows())

    values = {field.field_id: field.value for field in request.payload.fields}
    assert values["actions.confirmed_count"] == 1
    assert values["actions.history_state"] == "PENDING"


def test_send_evening_uses_checked_pipeline_without_raw_transport():
    from services.evening_report import send_evening_report

    request = _request()
    result = _result(request, persisted=True)
    rendered = SimpleNamespace(name="rendered")
    with (
        patch(
            "services.evening_report.build_evening_report",
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
        patch("services.telegram_bot.send_with_buttons") as raw_buttons,
    ):
        assert send_evening_report() is True

    checker.assert_called_once_with(request)
    renderer.assert_called_once_with(request, result)
    checked_sender.assert_called_once_with(rendered, channel="ads")
    raw_sender.assert_not_called()
    raw_buttons.assert_not_called()


def test_evening_audit_failure_uses_only_safe_fact_free_facade():
    from services.evening_report import send_evening_report

    request = _request()
    result = _result(request, persisted=False)
    with (
        patch(
            "services.evening_report.build_evening_report",
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
        assert send_evening_report() is True

    safe_sender.assert_called_once()
