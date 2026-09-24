"""
Тесты D4: services/budget_scaler.py — fail-closed без валидного медиаплана.

Проверяем:
1. plan_sheet_id пуст → plan_gate_reason задан, set_adset_budget НЕ вызывается,
   "plan_gate" в skipped_reason, Telegram отправлен.
2. read_general_plan бросает исключение → тот же fail-closed эффект.
3. Валидный план с запасом и ДРР в норме → подъём разрешён (plan_gate НЕ блокирует).

Все внешние границы (plan_reader, FB/Google spend, AMO revenue, exchange_rate,
Telegram, локальная БД) замокано — без сети.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())

import pytest


def _base_scale_cfg(plan_sheet_id: str = "") -> dict:
    """Базовый конфиг скейлера. plan_sheet_id='' по умолчанию — имитирует
    «медиаплан не настроен»."""
    return {
        "enabled": True,
        "kill_switch": False,
        "scale_enabled": False,
        "max_budget_increase_pct": 20,
        "max_adset_budget_mult": 2.0,
        "max_adset_daily_budget": 300,
        "max_total_daily_budget": 4000,
        "max_scales_per_run": 2,
        "plan_sheet_id": plan_sheet_id,
    }


def _make_local_ad_win(ad_id="ad1") -> dict:
    return {
        "ad_id": ad_id, "ad_name": "Победитель", "city": "CityA",
        "adset_type": "L2", "adset_id": None,
        "spend": 80.0, "leads": 8, "qual_pct": 20.0, "romi": 150.0,
        "cpl": 10.0, "ctr": 2.0, "hook_rate": 30.0, "impressions": 5000,
        "video_p25": 0, "video_p100": 0, "video_views_3s": 0,
        "days_running": 10, "effective_status": "ACTIVE",
        "recommendation": "ЖДАТЬ", "reason": "",
    }


def _make_scale_dec(ad_id="ad1") -> dict:
    return {
        "ad_id": ad_id, "ad_name": "Победитель", "adset_id": "",
        "action": "SCALE", "score": 7, "reasons": [],
    }


# ---------------------------------------------------------------------------
# Тест 1: нет plan_sheet_id → fail-closed, set_adset_budget не вызван
# ---------------------------------------------------------------------------

def test_no_sheet_id_blocks_raise():
    """plan_sheet_id='' → plan_data остаётся None → plan_gate_reason задан
    (fail-closed), подъёма бюджетов нет, set_adset_budget не вызывается,
    Telegram-уведомление отправлено."""
    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg(plan_sheet_id="")), \
         patch("services.budget_scaler.set_adset_budget") as mock_set_budget, \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=[_make_local_ad_win()]), \
         patch("services.decision_policy.score_and_decide", return_value=[_make_scale_dec()]), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.notifications.send_telegram") as mock_telegram, \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is True
    assert "plan_gate" in str(result.get("skipped_reason", "")), \
        f"Ожидали plan_gate в skipped_reason (fail-closed), got: {result}"
    assert "медиаплан недоступен" in str(result["skipped_reason"])
    mock_set_budget.assert_not_called()
    assert result["scaled"] == []
    mock_telegram.assert_called_once()
    # редизайн: "Не поднимаем" -> "НЕ поднимаю" (единое число, честный тон)
    assert "❌ НЕ поднимаю" in mock_telegram.call_args.args[0]


# ---------------------------------------------------------------------------
# Тест 2: read_general_plan бросает исключение → fail-closed
# ---------------------------------------------------------------------------

def test_plan_read_error_blocks_raise():
    """sheet_id задан, но read_general_plan бросает исключение (сеть/парсинг упали)
    → plan_data остаётся None → fail-closed, set_adset_budget не вызван."""
    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg(plan_sheet_id="real_sheet_id")), \
         patch("services.plan_reader.read_general_plan", side_effect=RuntimeError("Google Sheets недоступен")), \
         patch("services.budget_scaler.set_adset_budget") as mock_set_budget, \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=[_make_local_ad_win()]), \
         patch("services.decision_policy.score_and_decide", return_value=[_make_scale_dec()]), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.notifications.send_telegram") as mock_telegram, \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is True
    assert "plan_gate" in str(result.get("skipped_reason", "")), \
        f"Ожидали fail-closed при упавшем read_general_plan, got: {result}"
    assert "медиаплан недоступен" in str(result["skipped_reason"])
    mock_set_budget.assert_not_called()
    assert result["scaled"] == []
    mock_telegram.assert_called_once()


# ---------------------------------------------------------------------------
# Тест 3: валидный план, запас есть, ДРР в норме → подъём разрешён
# ---------------------------------------------------------------------------

def test_valid_plan_allows_raise():
    """Валидный медиаплан с запасом бюджета и ДРР в норме → plan_gate_reason
    отсутствует, дальнейшая логика скейлера (не блокированная планом) работает."""
    plan_data = {
        "week_label": "неделя 1 (2026-07-01–07)",
        "budget_usd": 2000.0,
        "revenue_plan_lcy": 10_000_000.0,
        "unit_target": 0.10,
    }
    all_budgets = {"adset1": {"daily_budget_usd": 100.0, "effective_status": "ACTIVE", "name": "Тест"}}

    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg(plan_sheet_id="real_sheet_id")), \
         patch("services.plan_reader.read_general_plan", return_value=plan_data), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=500.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=300.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=8_000_000.0), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=[_make_local_ad_win()]), \
         patch("services.decision_policy.score_and_decide", return_value=[_make_scale_dec()]), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler.set_adset_budget") as mock_set_budget, \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is True
    assert "plan_gate" not in str(result.get("skipped_reason") or ""), \
        f"План валиден — plan_gate НЕ должен блокировать, got: {result}"
    # dry_run — set_adset_budget не обязан вызываться (это только режим active),
    # но сама план-проверка не заблокировала прогон дальше шага 2.
