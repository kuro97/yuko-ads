"""
Тесты trailing-окна ДРР до последней прошедшей субботы — services.budget_scaler.

Проверяют §9 спеки ARCH-saturday-drr.md:
- Юнит-тесты compute_drr_window: пн/ср/сб/вс, всегда 7 полных дней, naive-now,
  окно на границе месяца (последняя суббота может лежать в прошлом месяце).
- Интеграционные тесты _run_scaling_inner (через run_budget_scaling):
  юнитка-гейт судит по trailing-выручке (не по плановому окну), выручка=0
  сохраняет fail-closed, headroom остаётся на плановом окне, Telegram-заголовок
  показывает дату субботы окна ДРР.

Паттерны моков — как в tests/test_plan_budget_scaler.py и tests/test_budget_pilot_integration.py
(_frozen_now патчит services.budget_scaler.datetime, конструктор datetime(...) не ломается).
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импортов проекта (как в остальных тестах budget_scaler)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from services.budget_scaler import compute_drr_window  # noqa: E402

_TZ = timezone(timedelta(hours=5))


@pytest.fixture(autouse=True)
def isolated_daily_cap_state(tmp_path, monkeypatch):
    """Перенаправляет state-файл дневного капа во временную папку (изоляция между тестами)."""
    import services.budget_daily_cap as cap_module
    state_file = tmp_path / "budget_daily_cap_state.json"
    monkeypatch.setattr(cap_module, "_CAP_STATE_FILE", state_file)


@pytest.fixture(autouse=True)
def isolated_scale_cooldown_state(tmp_path, monkeypatch):
    """Перенаправляет state-файл кулдауна scaler'а во временную папку (изоляция между тестами)."""
    import services.budget_scaler as scaler_module
    state_file = tmp_path / "budget_scaler_state.json"
    monkeypatch.setattr(scaler_module, "_SCALE_STATE_FILE", state_file)


def _frozen_now(now: datetime):
    """Патчит services.budget_scaler.datetime так, чтобы .now() возвращал фиксированный now,
    а конструктор datetime(...) продолжал работать как обычно (нужен коду внутри модуля).
    Паттерн — как в tests/test_budget_pilot_integration.py L207-213.
    """
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = now
    return patch("services.budget_scaler.datetime", mock_dt)


# ===========================================================================
# Юнит-тесты compute_drr_window
# ===========================================================================

def test_window_saturday_is_today():
    """now=суббота → last_saturday=эта суббота (окно кончается сегодня)."""
    now = datetime(2026, 7, 4, 12, 0, tzinfo=_TZ)  # суббота
    window_start, window_end, last_saturday = compute_drr_window(now)

    assert last_saturday.date() == datetime(2026, 7, 4).date()
    assert window_end == datetime(2026, 7, 4, 23, 59, 59, tzinfo=_TZ)
    assert window_start == datetime(2026, 6, 28, 0, 0, 0, tzinfo=_TZ)


def test_window_sunday_uses_yesterday():
    """now=воскресенье → last_saturday=вчера (1 день назад)."""
    now = datetime(2026, 7, 5, 9, 0, tzinfo=_TZ)  # воскресенье
    window_start, window_end, last_saturday = compute_drr_window(now)

    assert last_saturday.date() == datetime(2026, 7, 4).date()
    assert window_start.date() == datetime(2026, 6, 28).date()
    assert window_end.date() == datetime(2026, 7, 4).date()


def test_window_monday_uses_prev_saturday():
    """now=понедельник → last_saturday=позавчера (2 дня назад)."""
    now = datetime(2026, 7, 6, 8, 0, tzinfo=_TZ)  # понедельник
    _, _, last_saturday = compute_drr_window(now)

    assert last_saturday.date() == datetime(2026, 7, 4).date()


def test_window_wednesday_uses_prev_saturday():
    """now=среда → last_saturday=4 дня назад."""
    now = datetime(2026, 7, 8, 8, 0, tzinfo=_TZ)  # среда
    _, _, last_saturday = compute_drr_window(now)

    assert last_saturday.date() == datetime(2026, 7, 4).date()


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 7, 4, 12, 0, tzinfo=_TZ),   # суббота
        datetime(2026, 7, 5, 9, 0, tzinfo=_TZ),    # воскресенье
        datetime(2026, 7, 6, 8, 0, tzinfo=_TZ),    # понедельник
        datetime(2026, 7, 8, 8, 0, tzinfo=_TZ),    # среда
        datetime(2026, 7, 10, 8, 0, tzinfo=_TZ),   # пятница
    ],
)
def test_window_is_7_full_days(now):
    """Окно всегда охватывает ровно 7 дат включительно, независимо от дня недели now."""
    window_start, window_end, _ = compute_drr_window(now)
    assert (window_end.date() - window_start.date()).days == 6


def test_window_naive_now_treated_as_local():
    """naive now (без tzinfo) не падает — трактуется как TZ CityA."""
    now = datetime(2026, 7, 6, 8, 0)  # понедельник, без tzinfo
    window_start, window_end, last_saturday = compute_drr_window(now)

    assert last_saturday.tzinfo == _TZ
    assert window_start.tzinfo == _TZ
    assert window_end.tzinfo == _TZ
    assert last_saturday.date() == datetime(2026, 7, 4).date()


def test_window_start_of_month_prev_month_saturday():
    """Начало месяца: суббота может лежать в предыдущем месяце — окно пересекает границу."""
    now = datetime(2026, 7, 1, 8, 0, tzinfo=_TZ)  # среда, первый день июля
    window_start, window_end, last_saturday = compute_drr_window(now)

    assert last_saturday.date() == datetime(2026, 6, 27).date()  # прошлый месяц
    assert window_start.date() == datetime(2026, 6, 21).date()
    assert window_end.date() == datetime(2026, 6, 27).date()


# ===========================================================================
# Интеграционные тесты: юнитка-гейт использует trailing-окно, а не плановое
# ===========================================================================

def _base_scale_cfg_with_plan() -> dict:
    """Базовый конфиг с plan_sheet_id (как в test_plan_budget_scaler.py)."""
    return {
        "enabled": True,
        "kill_switch": False,
        "scale_enabled": False,
        "max_budget_increase_pct": 20,
        "max_adset_budget_mult": 2.0,
        "max_adset_daily_budget": 300,
        "max_total_daily_budget": 4000,
        "max_scales_per_run": 2,
        "plan_sheet_id": "test_sheet_id",
    }


def _make_local_ad_win(ad_id="ad1") -> dict:
    """Объявление-кандидат (payments>0 — иначе не пройдёт отбор до план-гейта)."""
    return {
        "ad_id": ad_id, "ad_name": "Победитель", "city": "CityA",
        "adset_type": "L2", "adset_id": None,
        "spend": 80.0, "leads": 8, "qual_pct": 20.0, "romi": 150.0,
        "cpl": 10.0, "ctr": 2.0, "hook_rate": 30.0, "impressions": 5000,
        "video_p25": 0, "video_p100": 0, "video_views_3s": 0,
        "payments": 3, "outcomes_matched_at": None,
        "days_running": 10, "effective_status": "ACTIVE",
        "recommendation": "ЖДАТЬ", "reason": "",
    }


def _make_scale_dec(ad_id="ad1") -> dict:
    return {
        "ad_id": ad_id, "ad_name": "Победитель", "adset_id": "",
        "action": "SCALE", "score": 7, "reasons": [],
    }


def test_drr_uses_trailing_window_not_plan_window():
    """Понедельник, плановое окно только начинается (мало выручки по числам месяца),
    но trailing-окно захватило денежную субботу — гейт НЕ должен блокировать по юнитке.
    """
    plan_data = {
        "week_label": "неделя 1 (2026-07-01–07)",
        "budget_usd": 2000.0,       # headroom положительный
        "revenue_plan_lcy": 10_000_000.0,
        "unit_target": 0.10,        # 10% ДРР
    }
    # fb=300, google=0 → total=300; drr = 300*100/8_000_000 ≈ 0.0037 << 0.10
    all_budgets = {"adset1": {"daily_budget_usd": 100.0, "effective_status": "ACTIVE", "name": "Тест"}}
    now = datetime(2026, 7, 6, 10, 0, tzinfo=_TZ)  # понедельник

    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg_with_plan()), \
         patch("services.plan_reader.read_general_plan", return_value=plan_data), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=300.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=8_000_000.0) as mock_revenue, \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=[_make_local_ad_win()]), \
         patch("services.decision_policy.score_and_decide", return_value=[_make_scale_dec()]), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler.set_adset_budget"), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         _frozen_now(now):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is True
    assert "юнитка превышена" not in str(result.get("skipped_reason", ""))
    assert "plan_gate" not in str(result.get("skipped_reason", ""))

    # get_amo_week_revenue вызывается с ДРР-датами (trailing-окно до субботы),
    # а не с плановыми датами недели (числа месяца 01-07).
    mock_revenue.assert_called_once()
    called_start, called_end = mock_revenue.call_args[0]
    assert called_start.date() == datetime(2026, 6, 28).date()  # last_saturday - 6 дней
    assert called_end.date() == datetime(2026, 7, 4).date()     # последняя прошедшая суббота


def test_drr_zero_revenue_in_trailing_window_blocks():
    """Выручка trailing-окна = 0 → fail-closed сохранён (блок с текстом про выручку)."""
    plan_data = {
        "week_label": "неделя 1 (2026-07-01–07)",
        "budget_usd": 2000.0,
        "revenue_plan_lcy": 10_000_000.0,
        "unit_target": 0.10,
    }
    now = datetime(2026, 7, 6, 10, 0, tzinfo=_TZ)  # понедельник

    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg_with_plan()), \
         patch("services.plan_reader.read_general_plan", return_value=plan_data), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=300.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=0.0), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         _frozen_now(now):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is True
    assert "plan_gate" in str(result.get("skipped_reason", ""))
    assert "выручка" in str(result.get("skipped_reason", ""))


def test_headroom_still_uses_plan_window():
    """Headroom остаётся на плановом окне: даже с большой trailing-выручкой,
    если расход планового окна превысил план — гейт блокирует по запасу.
    """
    plan_data = {
        "week_label": "неделя 1 (2026-07-01–07)",
        "budget_usd": 500.0,   # план всего $500
        "revenue_plan_lcy": 10_000_000.0,
        "unit_target": 0.10,
    }
    # fb=300 + google=300 = 600 > 500 → headroom=-100 < 0 (плановое окно превышено)
    now = datetime(2026, 7, 6, 10, 0, tzinfo=_TZ)  # понедельник

    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg_with_plan()), \
         patch("services.plan_reader.read_general_plan", return_value=plan_data), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=300.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=300.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=8_000_000.0), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.notifications.send_telegram"), \
         patch("services.notifications.send_critical_alert"), \
         _frozen_now(now):

        from services.budget_scaler import run_budget_scaling
        result = run_budget_scaling(mode="dry_run")

    assert result["ran"] is True
    assert "plan_gate" in str(result.get("skipped_reason", ""))
    assert "запас исчерпан" in str(result.get("skipped_reason", ""))


def test_telegram_header_shows_drr_window_saturday():
    """dry_run с валидным планом (гейт разрешает) → сообщение содержит подпись
    ДРР с окном (trailing-7дн до последней субботы), включая дату этой субботы.

    РЕДИЗАЙН по решению владельца: dry_run больше не собирает
    заголовок через _build_plan_header (фраза "ДРР за окно до субботы X" ушла) —
    теперь окно ДРР показывается через _fmt_drr_signature в формате
    "окно 28.06–04.07" (см. docs/specs/ARCH-scaler-report-redesign.md §6.2).
    Дата субботы (конец окна) по-прежнему обязана присутствовать в сообщении.
    """
    plan_data = {
        "week_label": "неделя 1 (2026-07-01–07)",
        "budget_usd": 2000.0,
        "revenue_plan_lcy": 10_000_000.0,
        "unit_target": 0.10,
    }
    all_budgets = {"adset1": {"daily_budget_usd": 100.0, "effective_status": "ACTIVE", "name": "Тест"}}
    now = datetime(2026, 7, 6, 10, 0, tzinfo=_TZ)  # понедельник

    with patch("services.budget_scaler.get_scale_config", return_value=_base_scale_cfg_with_plan()), \
         patch("services.plan_reader.read_general_plan", return_value=plan_data), \
         patch("services.budget_scaler.get_fb_week_spend", return_value=300.0), \
         patch("services.budget_scaler.get_google_week_spend", return_value=0.0), \
         patch("services.budget_scaler.get_amo_week_revenue", return_value=8_000_000.0), \
         patch("services.exchange_rate.get_usd_to_lcy", return_value=100.0), \
         patch("services.shadow_report._fetch_ads_from_local_db", return_value=[_make_local_ad_win()]), \
         patch("services.decision_policy.score_and_decide", return_value=[_make_scale_dec()]), \
         patch("agent.scheduler.load_settings", return_value={"thresholds": {}}), \
         patch("services.autopilot._fetch_candidate_fb_info",
               return_value={"ad1": {"adset_id": "adset1", "effective_status": "ACTIVE"}}), \
         patch("services.budget_scaler._fetch_all_account_adset_budgets", return_value=all_budgets), \
         patch("services.budget_scaler._fetch_adset_budgets", return_value={}), \
         patch("services.budget_scaler.set_adset_budget"), \
         patch("services.notifications.send_telegram") as mock_send, \
         patch("services.notifications.send_critical_alert"), \
         _frozen_now(now):

        from services.budget_scaler import run_budget_scaling
        run_budget_scaling(mode="dry_run")

    mock_send.assert_called_once()
    sent_text = mock_send.call_args[0][0]
    assert "окно" in sent_text  # подпись ДРР (_fmt_drr_signature) присутствует
    assert "04.07" in sent_text  # last_saturday для понедельника 2026-07-06 (конец окна)
