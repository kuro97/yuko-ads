"""Тесты ежедневного онлайн-отчёта из CDP (services/online_report.py).

CDP, Telegram и файловый state изолированы. Фикстура daily-report построена по
образцу JSON строки «Онлайн» (поля ad_spend, leads, qleads, pct_qleads,
drr_new, drr_full, usd_rate — БЕЗ revenue_new). Проверяем дословный формат
сообщения, месячный агрегат (средневзв. и точный ДРР при наличии revenue),
путь «CDP недоступен» (один раз) и выключатель.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

import services.online_report as report
from services.approval_checker_models import ReportVerdict
from services.cdp_client import CdpError

LOCAL_TZ = timezone(timedelta(hours=5))
NOW = datetime(2026, 7, 17, 10, 35, tzinfo=LOCAL_TZ)  # прогон в 10:3x, вчера = 16.07
YESTERDAY = date(2026, 7, 16)
MONTH_START = date(2026, 7, 1)


# Образец строки «Онлайн» за вчера (поля подтверждены прогоном ТЗ)
YST_ONLINE_ROW = {
    "report_date": "2026-07-16",
    "city": "Онлайн",
    "ad_spend": 67.5,
    "leads": 12,
    "qleads": 4,
    "pct_qleads": 33.3,
    "drr_new": 4.2,
    "drr_full": 6.1,
    "usd_rate": 100.0,
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "_STATE_FILE", tmp_path / "online_report_state.json")
    monkeypatch.setattr(report, "get_online_report_config", lambda: {"enabled": True})


def _wire_cdp(monkeypatch, daily_rows: list, month_rows: list):
    """Мокает cdp_client.get_daily_report: daily-окно vs месяц по date_from==date_to."""
    def _fake(date_from, date_to, city=None):
        if date_from == date_to:
            return list(daily_rows)
        return list(month_rows)
    monkeypatch.setattr(report.cdp_client, "get_daily_report", _fake)


def _wire_send(monkeypatch):
    """Мокает только checked delivery и закрытый fact-free fallback."""
    send = MagicMock(return_value=(True, ReportVerdict.VERIFIED))
    unavailable = MagicMock(return_value=True)
    monkeypatch.setattr(report, "_check_render_send", send)
    monkeypatch.setattr(report, "_send_unavailable", unavailable)
    send.unavailable = unavailable
    return send


def _request_values(send: MagicMock) -> dict[str, object]:
    request = send.call_args.args[0]
    return {field.field_id: field.value for field in request.payload.fields}


def test_message_format_on_live_fixture(monkeypatch):
    # Месяц: две строки без revenue → средневзвешенный drr_new по расходу.
    # (5·100 + 3·300)/400 = 3.5 ; Σ расход = 400
    month_rows = [
        {"report_date": "2026-07-10", "city": "Онлайн", "ad_spend": 100.0, "drr_new": 5.0},
        {"report_date": "2026-07-16", "city": "Онлайн", "ad_spend": 300.0, "drr_new": 3.0},
    ]
    _wire_cdp(monkeypatch, [YST_ONLINE_ROW], month_rows)
    send = _wire_send(monkeypatch)

    res = report.run_online_report(NOW)

    assert res["status"] == "sent"
    assert res["sent"] is True
    values = _request_values(send)
    assert values["online.day.spend"] == Decimal("67.5")
    assert values["online.day.leads"] == 12
    assert values["online.day.quals"] == 4
    assert values["online.day.qual_pct"] == Decimal("33.3")
    assert values["online.day.drr"] == Decimal("4.2")
    assert values["online.month.spend"] == Decimal("400")
    assert values["online.month.drr"] == Decimal("3.5")
    assert values["online.month.drr_mode"] == "WEIGHTED_FALLBACK"


def test_monthly_aggregate_exact_drr_when_revenue_present(monkeypatch):
    # Есть revenue_new + usd_rate → строгий ДРР Σ(spend·rate)/Σrevenue, без пометки.
    # (100·100 + 200·100)/750_000 = 30000/750000 = 4%
    month_rows = [
        {"report_date": "2026-07-10", "city": "Онлайн", "ad_spend": 100.0,
         "usd_rate": 100.0, "revenue_new": 250_000.0, "drr_new": 99.0},
        {"report_date": "2026-07-16", "city": "Онлайн", "ad_spend": 200.0,
         "usd_rate": 100.0, "revenue_new": 500_000.0, "drr_new": 99.0},
    ]
    _wire_cdp(monkeypatch, [YST_ONLINE_ROW], month_rows)
    send = _wire_send(monkeypatch)

    res = report.run_online_report(NOW)

    assert res["status"] == "sent"
    values = _request_values(send)
    assert values["online.month.spend"] == Decimal("300")
    assert values["online.month.drr"] == Decimal("4")
    assert values["online.month.drr_mode"] == "EXACT_REVENUE"


def test_month_drr_weighted_ignores_ready_drr_new_when_revenue_present(monkeypatch):
    """drr_new в строках намеренно 99% — при наличии revenue его не используем."""
    month_rows = [
        {"report_date": "2026-07-16", "city": "Онлайн", "ad_spend": 50.0,
         "usd_rate": 100.0, "revenue_new": 100_000.0, "drr_new": 99.0},
    ]
    _wire_cdp(monkeypatch, [YST_ONLINE_ROW], month_rows)
    send = _wire_send(monkeypatch)

    report.run_online_report(NOW)
    # (50·100)/100000 = 5000/100000 = 5% — не 99%
    values = _request_values(send)
    assert values["online.month.drr"] == Decimal("5")


def test_cdp_unavailable_sends_honest_line_once(monkeypatch):
    def _boom(date_from, date_to, city=None):
        raise CdpError("CDP лёг")
    monkeypatch.setattr(report.cdp_client, "get_daily_report", _boom)
    send = _wire_send(monkeypatch)

    first = report.run_online_report(NOW)
    assert first["status"] == "cdp_unavailable"
    assert first["ok"] is False
    send.unavailable.assert_called_once_with("CdpError")

    # Повторный прогон в тот же день — не спамим
    second = report.run_online_report(NOW)
    assert second["status"] == "cdp_unavailable_deduped"
    assert send.unavailable.call_count == 1


def test_disabled_switch_sends_nothing(monkeypatch):
    monkeypatch.setattr(report, "get_online_report_config", lambda: {"enabled": False})
    _wire_cdp(monkeypatch, [YST_ONLINE_ROW], [YST_ONLINE_ROW])
    send = _wire_send(monkeypatch)

    res = report.run_online_report(NOW)
    assert res["status"] == "disabled"
    send.assert_not_called()
    send.unavailable.assert_not_called()


def test_dedup_report_once_per_day(monkeypatch):
    _wire_cdp(monkeypatch, [YST_ONLINE_ROW], [YST_ONLINE_ROW])
    send = _wire_send(monkeypatch)

    first = report.run_online_report(NOW)
    assert first["status"] == "sent"
    second = report.run_online_report(NOW)
    assert second["status"] == "already_sent"
    assert send.call_count == 1


def test_missing_online_row_skips_without_marking(monkeypatch):
    # CDP жив, но строки «Онлайн» за вчера нет — не шлём кривой отчёт, день не помечаем.
    other_city = {"report_date": "2026-07-16", "city": "CityA", "ad_spend": 10.0}
    _wire_cdp(monkeypatch, [other_city], [other_city])
    send = _wire_send(monkeypatch)

    res = report.run_online_report(NOW)
    assert res["status"] == "no_data"
    send.assert_not_called()
