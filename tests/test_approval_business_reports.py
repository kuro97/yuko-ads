"""Контрактные тесты checked online/weekly/scorecard отчётов."""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.approval_checker_models import (
    Metric,
    ReportTemplate,
    ReportVerdict,
    SourceSystem,
)


LOCAL_TZ = timezone(timedelta(hours=5))
NOW = datetime(2026, 7, 17, 10, 35, tzinfo=LOCAL_TZ)


def _online_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "report_date": "2026-07-16",
        "city": "Онлайн",
        "ad_spend": 67.5,
        "leads": 12,
        "qleads": 4,
        "pct_qleads": 33.3,
        "drr_new": 4.2,
    }
    row.update(overrides)
    return row


def test_online_request_has_bijection_and_explicit_weighted_mode() -> None:
    from services.online_report import build_online_report_request

    month_rows = [
        _online_row(report_date="2026-07-10", ad_spend=100, drr_new=5),
        _online_row(report_date="2026-07-16", ad_spend=300, drr_new=3),
    ]
    request = build_online_report_request(
        date(2026, 7, 16),
        _online_row(),
        month_rows,
        generated_at=NOW,
    )

    assert request.payload.template is ReportTemplate.ONLINE
    assert {field.field_id for field in request.payload.fields} == {
        claim.field_id for claim in request.claims
    }
    values = {field.field_id: field.value for field in request.payload.fields}
    assert values["online.month.drr"] == Decimal("3.5")
    assert values["online.month.drr_mode"] == "WEIGHTED_FALLBACK"
    assert values["online.day.leads"] == 12


def test_online_request_exact_drr_exposes_revenue_and_mode() -> None:
    from services.online_report import build_online_report_request

    month_rows = [
        _online_row(
            report_date="2026-07-10",
            ad_spend=100,
            drr_new=99,
            usd_rate=100,
            revenue_new=250_000,
        ),
        _online_row(
            report_date="2026-07-16",
            ad_spend=200,
            drr_new=99,
            usd_rate=100,
            revenue_new=500_000,
        ),
    ]
    request = build_online_report_request(
        date(2026, 7, 16),
        _online_row(),
        month_rows,
        generated_at=NOW,
    )
    values = {field.field_id: field.value for field in request.payload.fields}

    assert values["online.month.drr"] == Decimal("4")
    assert values["online.month.drr_mode"] == "EXACT_REVENUE"
    assert values["online.month.revenue"] == Decimal("750000")


@pytest.mark.parametrize("field", ["ad_spend", "leads", "qleads", "pct_qleads", "drr_new"])
def test_online_missing_required_value_never_becomes_zero(field: str) -> None:
    from services.online_report import build_online_report_request

    with pytest.raises(ValueError):
        build_online_report_request(
            date(2026, 7, 16),
            _online_row(**{field: None}),
            [_online_row()],
            generated_at=NOW,
        )


def test_online_run_uses_checked_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import services.online_report as report

    monkeypatch.setattr(report, "_STATE_FILE", tmp_path / "online.json")
    monkeypatch.setattr(report, "get_online_report_config", lambda: {"enabled": True})
    monkeypatch.setattr(
        report.cdp_client,
        "get_daily_report",
        lambda date_from, date_to: [_online_row()],
    )
    checked = MagicMock(return_value=(True, ReportVerdict.VERIFIED))
    monkeypatch.setattr(report, "_check_render_send", checked)

    result = report.run_online_report(NOW)

    assert result["status"] == "sent"
    checked.assert_called_once()


def test_weekly_request_claims_exact_learning_and_product_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.weekly_learning_report as report

    monkeypatch.setattr(
        report,
        "_weekly_source_snapshot",
        lambda now: {
            "hypothesis_total": 4,
            "pattern_total": 3,
            "untagged": 2,
            "products": (("PRODA", Decimal("60")), ("PRODB", Decimal("40"))),
        },
    )
    request = report.build_weekly_report_request(now=NOW)
    by_id = {field.field_id: field for field in request.payload.fields}

    assert request.payload.template is ReportTemplate.WEEKLY_LEARNING
    assert by_id["weekly.hypotheses.total"].source is SourceSystem.HYPOTHESIS_JOURNAL
    assert by_id["weekly.patterns.total"].source is SourceSystem.PATTERN_LEARNINGS
    assert by_id["weekly.product.untagged"].value == 2
    assert by_id["weekly.product.PRODA.share"].value == Decimal("60")


def test_scorecard_request_checks_agreement_and_accuracy_formula() -> None:
    from services.scorecard import build_scorecard_request

    request = build_scorecard_request(
        {
            "period_days": 7,
            "feedback": {"up": 3, "down": 1, "total": 4, "agreement_pct": 75},
            "decisions": {"paused": 2, "scaled": 1},
            "scale_accuracy": {"total": 2, "winners": 1, "accuracy_pct": 50},
        },
        generated_at=NOW,
    )
    values = {field.field_id: field.value for field in request.payload.fields}

    assert request.payload.template is ReportTemplate.SCORECARD
    assert values["scorecard.feedback.agreement"] == Decimal("75")
    assert values["scorecard.accuracy.pct"] == Decimal("50")
    assert next(
        claim for claim in request.claims if claim.metric is Metric.AGREEMENT_PCT
    ).source is SourceSystem.AUTOPILOT_FEEDBACK


@pytest.mark.parametrize(
    "feedback,accuracy",
    [
        ({"up": 3, "down": 1, "total": 5, "agreement_pct": 60}, {"total": 0, "winners": 0, "accuracy_pct": None}),
        ({"up": 3, "down": 1, "total": 4, "agreement_pct": 75}, {"total": 2, "winners": 1, "accuracy_pct": 90}),
        ({"up": 0, "down": 0, "total": 0, "agreement_pct": None}, {"total": 0, "winners": 0, "accuracy_pct": 0}),
    ],
)
def test_scorecard_inconsistent_aggregates_block_before_check(
    feedback: dict[str, object],
    accuracy: dict[str, object],
) -> None:
    from services.scorecard import build_scorecard_request

    with pytest.raises(ValueError):
        build_scorecard_request(
            {
                "period_days": 7,
                "feedback": feedback,
                "decisions": {"paused": 0, "scaled": 0},
                "scale_accuracy": accuracy,
            },
            generated_at=NOW,
        )


def test_producers_have_no_raw_telegram_call() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "services/online_report.py",
        "services/weekly_learning_report.py",
        "services/scorecard.py",
    ):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        raw_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id == "send_telegram"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "send_telegram"
            )
        ]
        assert raw_calls == [], relative


def test_weekly_sender_calls_check_render_checked_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.weekly_learning_report as report

    request = SimpleNamespace(payload=SimpleNamespace(generated_at=NOW))
    result = object()
    rendered = object()
    monkeypatch.setattr(
        "services.autopilot.get_autopilot_config",
        lambda: {"hypothesist": {"enabled": True}},
    )
    monkeypatch.setattr(report, "build_weekly_report_request", lambda now=None: request)
    check = MagicMock(return_value=result)
    render = MagicMock(return_value=rendered)
    send = MagicMock(return_value=SimpleNamespace(sent=True))
    monkeypatch.setattr(report, "check_report", check)
    monkeypatch.setattr(report, "render_checked_report", render)
    monkeypatch.setattr(report, "send_checked_report", send)

    assert report.send_weekly_learning_report(now=NOW) is True
    check.assert_called_once_with(request, now=NOW)
    render.assert_called_once_with(request, result)
    send.assert_called_once_with(rendered, channel="ads")


def test_scorecard_sender_calls_check_render_checked_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.scorecard as report

    request = object()
    result = object()
    rendered = object()
    monkeypatch.setattr(report, "build_scorecard", lambda days, now: {"snapshot": True})
    build_request = MagicMock(return_value=request)
    check = MagicMock(return_value=result)
    render = MagicMock(return_value=rendered)
    send = MagicMock(return_value=SimpleNamespace(sent=True))
    monkeypatch.setattr(report, "build_scorecard_request", build_request)
    monkeypatch.setattr(report, "check_report", check)
    monkeypatch.setattr(report, "render_checked_report", render)
    monkeypatch.setattr(report, "send_checked_report", send)

    report.send_scorecard()

    generated_at = build_request.call_args.kwargs["generated_at"]
    check.assert_called_once_with(request, now=generated_at)
    render.assert_called_once_with(request, result)
    send.assert_called_once_with(rendered, channel="ads")
