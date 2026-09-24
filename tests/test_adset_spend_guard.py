"""Тесты стража трат адсетов (services/adset_spend_guard.py).

FB, Telegram и файловый state изолированы моками/tmp_path. Проверяем:
$0-расход → алерт «не тратит», перерасход 1.26× → алерт, 1.24× → тишина,
антиспам (тот же день дедуплен, следующий день снова алертит), FB-ошибка не роняет.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import services.adset_spend_guard as guard

LOCAL_TZ = timezone(timedelta(hours=5))
DAY1 = datetime(2026, 7, 17, 9, 5, tzinfo=LOCAL_TZ)   # вчера = 16.07
DAY2 = datetime(2026, 7, 18, 9, 5, tzinfo=LOCAL_TZ)   # вчера = 17.07


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """State во временный файл + дефолтный конфиг стража."""
    monkeypatch.setattr(guard, "_STATE_FILE", tmp_path / "adset_spend_guard_state.json")
    monkeypatch.setattr(
        guard, "get_spend_guard_config",
        lambda: {"enabled": True, "overspend_mult": 1.25},
    )


def _wire(monkeypatch, budgets: dict, spend_by_id: dict):
    """Мокает discover + budgets + insights + Telegram. Возвращает mock send_telegram."""
    from unittest.mock import MagicMock

    monkeypatch.setattr(guard, "_discover_leadgen_adset_ids", lambda: list(budgets.keys()))
    monkeypatch.setattr(guard, "_fetch_adset_budgets", lambda ids: budgets)
    monkeypatch.setattr(guard, "_fetch_yesterday_spend", lambda aid, day: spend_by_id.get(aid))
    send = MagicMock(return_value=True)
    monkeypatch.setattr(guard, "send_telegram", send)
    return send


def _budget(name: str, usd: float, status: str = "ACTIVE") -> dict:
    return {"daily_budget_usd": usd, "effective_status": status, "name": name}


def test_zero_spend_triggers_not_spending_alert(monkeypatch):
    send = _wire(
        monkeypatch,
        budgets={"A1": _budget("CityA | L1 | LeadGen", 30.0)},
        spend_by_id={"A1": 0.0},
    )
    res = guard.run_adset_spend_guard(DAY1)

    assert res["status"] == "sent"
    assert res["not_spending"] == 1
    assert res["overspending"] == 0
    assert res["alerts_sent"] == 1
    text = send.call_args.args[0]
    assert "⚠️ Адсет не тратит: CityA | L1 | LeadGen — бюджет $30/день, вчера $0" in text
    assert "вчера 16.07" in text


def test_overspend_126_triggers_alert(monkeypatch):
    send = _wire(
        monkeypatch,
        budgets={"A1": _budget("CityB | L2 | LeadGen", 100.0)},
        spend_by_id={"A1": 126.0},  # 1.26× > 1.25×
    )
    res = guard.run_adset_spend_guard(DAY1)

    assert res["status"] == "sent"
    assert res["overspending"] == 1
    assert res["not_spending"] == 0
    text = send.call_args.args[0]
    assert (
        "⚠️ Адсет перетрачивает: CityB | L2 | LeadGen — бюджет $100, "
        "вчера потрачено $126 (+26%)" in text
    )


def test_overspend_124_no_alert(monkeypatch):
    send = _wire(
        monkeypatch,
        budgets={"A1": _budget("CityC | L1 | LeadGen", 100.0)},
        spend_by_id={"A1": 124.0},  # 1.24× < 1.25× → не перерасход, spend>0 → не «не тратит»
    )
    res = guard.run_adset_spend_guard(DAY1)

    assert res["status"] == "no_findings"
    assert res["overspending"] == 0
    assert res["not_spending"] == 0
    assert res["alerts_sent"] == 0
    send.assert_not_called()


def test_antispam_same_day_deduped_next_day_alerts(monkeypatch):
    send = _wire(
        monkeypatch,
        budgets={"A1": _budget("CityA | L1 | LeadGen", 30.0)},
        spend_by_id={"A1": 0.0},
    )

    first = guard.run_adset_spend_guard(DAY1)
    assert first["status"] == "sent"
    assert first["alerts_sent"] == 1

    # Тот же день, второй прогон — дедуплен, повторно не шлём
    second = guard.run_adset_spend_guard(DAY1)
    assert second["status"] == "deduped"
    assert second["alerts_sent"] == 0
    assert second["alerts_skipped"] == 1

    # Следующий день — антиспам сбрасывается, снова алертим
    third = guard.run_adset_spend_guard(DAY2)
    assert third["status"] == "sent"
    assert third["alerts_sent"] == 1

    assert send.call_count == 2  # день1 + день2, но НЕ второй прогон дня1


def test_fb_error_does_not_crash(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr(guard, "_discover_leadgen_adset_ids", lambda: ["A1"])

    def _boom(ids):
        raise RuntimeError("FB adset GET 500: rate limited")

    monkeypatch.setattr(guard, "_fetch_adset_budgets", _boom)
    send = MagicMock(return_value=True)
    monkeypatch.setattr(guard, "send_telegram", send)

    res = guard.run_adset_spend_guard(DAY1)  # не должно бросить

    assert res["ok"] is False
    assert res["status"] == "error"
    send.assert_not_called()


def test_inactive_and_zero_budget_adsets_skipped(monkeypatch):
    send = _wire(
        monkeypatch,
        budgets={
            "PAUSED": _budget("Пауза | L1", 50.0, status="ADSET_PAUSED"),
            "CBO": _budget("CBO | L2", 0.0),  # бюджет на уровне кампании → daily_budget=0
        },
        spend_by_id={"PAUSED": 0.0, "CBO": 0.0},
    )
    res = guard.run_adset_spend_guard(DAY1)

    assert res["checked"] == 0  # оба отфильтрованы до insights
    assert res["status"] == "no_findings"
    send.assert_not_called()


def test_disabled_switch_sends_nothing(monkeypatch):
    monkeypatch.setattr(
        guard, "get_spend_guard_config",
        lambda: {"enabled": False, "overspend_mult": 1.25},
    )
    send = _wire(
        monkeypatch,
        budgets={"A1": _budget("CityA | L1", 30.0)},
        spend_by_id={"A1": 0.0},
    )
    res = guard.run_adset_spend_guard(DAY1)

    assert res["status"] == "disabled"
    send.assert_not_called()


def test_send_failure_not_marked_and_ok_false(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr(guard, "_discover_leadgen_adset_ids", lambda: ["A1"])
    monkeypatch.setattr(
        guard, "_fetch_adset_budgets",
        lambda ids: {"A1": _budget("CityA | L1", 30.0)},
    )
    monkeypatch.setattr(guard, "_fetch_yesterday_spend", lambda aid, day: 0.0)
    send = MagicMock(return_value=False)  # Telegram не отправил
    monkeypatch.setattr(guard, "send_telegram", send)

    res = guard.run_adset_spend_guard(DAY1)
    assert res["ok"] is False
    assert res["status"] == "send_failed"

    # Отметку не поставили → следующий прогон снова попытается отправить
    send2 = MagicMock(return_value=True)
    monkeypatch.setattr(guard, "send_telegram", send2)
    res2 = guard.run_adset_spend_guard(DAY1)
    assert res2["status"] == "sent"
    send2.assert_called_once()
