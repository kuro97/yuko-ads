"""
Тесты прогнозного движка budget-context в services/budget_scaler.py:
compute_engine_pace(now) — доверие движку, план-гейт v3 (forecast_eom vs plan,
НЕ fact_mtd), тормоз перерасхода (ad_spend cost-метрика), fail-closed.

Задача T5 волны 3 спеки ARCH-cdp-budget-context.md (§9).

Моки — на границе: services.cdp_client.get_budget_context. По образцу
tests/test_cdp_unit_economics.py (стиль, _TZ, pytest.approx).

Комментарии на русском.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импортов проекта (как в других тестах бюджет-пилота)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from services.budget_scaler import compute_engine_pace
from services.cdp_client import CdpError

_TZ = timezone(timedelta(hours=5))


def _metric(
    plan=1_000_000.0,
    fact_mtd=100_000.0,
    expected_by_today=100_000.0,
    pace_vs_expected=1.0,
    forecast_eom=1_000_000.0,
    status="on_track",
    is_cost_metric=False,
):
    return {
        "plan": plan,
        "fact_mtd": fact_mtd,
        "expected_by_today": expected_by_today,
        "pace_vs_expected": pace_vs_expected,
        "forecast_eom": forecast_eom,
        "lo": None,
        "hi": None,
        "status": status,
        "is_cost_metric": is_cost_metric,
    }


def _ctx(
    data_as_of="2026-07-03T09:00:00Z",
    is_cold_start=False,
    revenue_new=None,
    ad_spend=None,
    new_sales_wape=0.10,
    revenue_new_wape=0.10,
    include_total=True,
    include_revenue_new=True,
):
    """Собирает валидный ответ /analytics/budget-context (агрегат _total)."""
    metrics = {}
    if include_revenue_new:
        metrics["revenue_new"] = revenue_new if revenue_new is not None else _metric()
    if ad_spend is not None:
        metrics["ad_spend"] = ad_spend

    cities = []
    if include_total:
        cities.append({"city": "_total", "metrics": metrics})

    engine_accuracy = []
    if revenue_new_wape is not None:
        engine_accuracy.append({"metric": "revenue_new", "target_month": "2026-06", "wape": revenue_new_wape})
    if new_sales_wape is not None:
        engine_accuracy.append({"metric": "new_sales", "target_month": "2026-06", "wape": new_sales_wape})

    return {
        "data_as_of": data_as_of,
        "month": "2026-07",
        "is_cold_start": is_cold_start,
        "month_progress": {"day": 3, "days_in_month": 31, "expected_share": 0.0882},
        "engine_accuracy": engine_accuracy,
        "cities": cities,
        "next_months": [],
        "semantics": {},
    }


_NOW = datetime(2026, 7, 3, 14, 0, tzinfo=_TZ)  # локальное 14:00 = UTC 09:00


def _patch_ctx(ctx):
    return patch("services.cdp_client.get_budget_context", return_value=ctx)


def _patch_err(exc):
    return patch("services.cdp_client.get_budget_context", side_effect=exc)


# ===========================================================================
# 1. Пример: темп ниже ожидаемого, но прогноз выше плана (обязательный, §6.6 спеки)
# ===========================================================================


def test_live_case_today_ahead_not_blocked():
    """pace_vs_expected=0.6, forecast_eom=120M >= plan 100M*0.9, status='ahead'
    -> plan_ok=True, plan_reason=None (движку доверяем, НЕ блокирует)."""
    revenue_new = _metric(
        plan=100_000_000.0,
        fact_mtd=6_000_000.0,
        expected_by_today=10_000_000.0,
        pace_vs_expected=0.6,
        forecast_eom=120_000_000.0,
        status="ahead",
    )
    ctx = _ctx(revenue_new=revenue_new, ad_spend=_metric(plan=None, status="no_plan", is_cost_metric=True))
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None
    assert result["forecast_eom"] == pytest.approx(120_000_000.0)
    assert result["pace_vs_expected"] == pytest.approx(0.6)
    assert result["revenue_status"] == "ahead"
    assert result["overspend_block"] is False


# ===========================================================================
# 2. Недоверие движку по каждой причине отдельно -> None (откат на кривую)
# ===========================================================================


def test_distrust_cold_start_returns_none():
    """is_cold_start=True -> compute_engine_pace возвращает None."""
    ctx = _ctx(is_cold_start=True)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is None


def test_distrust_wape_revenue_new_too_high_returns_none():
    """wape(revenue_new)=0.30 > 0.25 -> None."""
    ctx = _ctx(revenue_new_wape=0.30, new_sales_wape=0.10)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is None


def test_distrust_wape_new_sales_too_high_returns_none():
    """wape(new_sales)=0.30 > 0.25 -> None."""
    ctx = _ctx(revenue_new_wape=0.10, new_sales_wape=0.30)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is None


def test_distrust_stale_data_as_of_older_than_36h_returns_none():
    """data_as_of старше 36ч от now -> None."""
    stale_as_of = (_NOW.astimezone(timezone.utc) - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx = _ctx(data_as_of=stale_as_of)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is None


def test_distrust_missing_total_returns_none():
    """Нет агрегата _total в cities -> None (невалидная форма)."""
    ctx = _ctx(include_total=False)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is None


def test_distrust_missing_data_as_of_returns_none():
    """Нет data_as_of (None) -> 'нет отметки свежести' -> None."""
    ctx = _ctx(data_as_of=None)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is None


def test_distrust_missing_revenue_new_returns_none():
    """_total есть, но нет revenue_new в metrics -> None (нет главной метрики темпа)."""
    ctx = _ctx(include_revenue_new=False)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is None


# ===========================================================================
# 3. Реальный недобор: forecast < plan*0.9 И pace < 0.9 -> блок.
#    forecast < plan*0.9 НО pace >= 0.9 -> НЕ блок (нужны оба условия).
# ===========================================================================


def test_real_shortfall_both_conditions_bad_blocks():
    """status='behind', forecast < plan*0.9 И pace < 0.9 -> plan_ok=False."""
    revenue_new = _metric(
        plan=1_000_000.0,
        forecast_eom=800_000.0,       # < 1_000_000*0.9 = 900_000
        pace_vs_expected=0.5,          # < 0.9
        status="behind",
    )
    ctx = _ctx(revenue_new=revenue_new)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is False
    assert result["plan_reason"] is not None
    assert "движок" in result["plan_reason"]


def test_real_shortfall_forecast_bad_but_pace_ok_not_blocked():
    """status='behind', forecast < plan*0.9 НО pace >= 0.9 -> plan_ok=True (OR-послабление)."""
    revenue_new = _metric(
        plan=1_000_000.0,
        forecast_eom=800_000.0,       # < 900_000 (не проходит по forecast)
        pace_vs_expected=0.95,         # >= 0.9 (проходит по pace)
        status="behind",
    )
    ctx = _ctx(revenue_new=revenue_new)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


def test_real_shortfall_pace_bad_but_forecast_ok_not_blocked():
    """status='behind', pace < 0.9 НО forecast >= plan*0.9 -> plan_ok=True (OR-послабление,
    симметричный примеру из §1 — только числа другие, status='behind')."""
    revenue_new = _metric(
        plan=1_000_000.0,
        forecast_eom=950_000.0,        # >= 900_000 (проходит по forecast)
        pace_vs_expected=0.5,          # < 0.9 (не проходит по pace)
        status="behind",
    )
    ctx = _ctx(revenue_new=revenue_new)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


# ===========================================================================
# 4. Все status revenue_new: ahead/on_track -> ok; no_plan -> нейтрально;
#    behind -> формула двух условий (числовая проверка выше, здесь — статусы).
# ===========================================================================


def test_status_ahead_always_ok_even_if_numbers_bad():
    """status='ahead' -> plan_ok=True безусловно, даже если forecast/pace плохие числа."""
    revenue_new = _metric(plan=1_000_000.0, forecast_eom=1.0, pace_vs_expected=0.01, status="ahead")
    ctx = _ctx(revenue_new=revenue_new)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


def test_status_on_track_always_ok_even_if_numbers_bad():
    """status='on_track' -> plan_ok=True безусловно."""
    revenue_new = _metric(plan=1_000_000.0, forecast_eom=1.0, pace_vs_expected=0.01, status="on_track")
    ctx = _ctx(revenue_new=revenue_new)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


def test_status_no_plan_neutral_ok():
    """status='no_plan' (plan=None) -> нейтрально, plan_ok=True, plan_reason=None."""
    revenue_new = _metric(plan=None, forecast_eom=None, pace_vs_expected=None, status="no_plan")
    ctx = _ctx(revenue_new=revenue_new)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


def test_status_behind_blocks_via_two_condition_formula():
    """status='behind' -> решает формула forecast/pace (обе условия плохие -> блок).
    Дублирует §3, но проверяет именно связку status='behind' + дефолтные плохие числа."""
    revenue_new = _metric(
        plan=1_000_000.0,
        forecast_eom=500_000.0,
        pace_vs_expected=0.3,
        status="behind",
    )
    ctx = _ctx(revenue_new=revenue_new)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is False


# ===========================================================================
# 5. Тормоз перерасхода: ad_spend.status='behind' + plan!=null -> блок ДАЖЕ при
#    revenue ahead. ad_spend no_plan -> пропуск.
# ===========================================================================


def test_overspend_brake_blocks_even_if_revenue_ahead():
    """ad_spend.status='behind' (перерасход), plan задан -> overspend_block=True,
    ДАЖЕ когда revenue_new.status='ahead' (тормоз независим от план-темпа выручки)."""
    revenue_new = _metric(
        plan=100_000_000.0, forecast_eom=120_000_000.0, pace_vs_expected=0.6, status="ahead",
    )
    ad_spend = _metric(
        plan=50_000.0, forecast_eom=60_000.0, pace_vs_expected=1.3, status="behind", is_cost_metric=True,
    )
    ctx = _ctx(revenue_new=revenue_new, ad_spend=ad_spend)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is True          # план-темп выручки в порядке
    assert result["overspend_block"] is True  # но тормоз перерасхода блокирует
    assert result["overspend_reason"] is not None
    assert result["spend_status"] == "behind"


def test_overspend_brake_no_plan_skips_block():
    """ad_spend.status='no_plan' (plan=None) -> overspend_block=False (пропускаем,
    нельзя тормозить по отсутствующему плану расхода)."""
    revenue_new = _metric(status="ahead")
    ad_spend = _metric(plan=None, forecast_eom=None, pace_vs_expected=None, status="no_plan", is_cost_metric=True)
    ctx = _ctx(revenue_new=revenue_new, ad_spend=ad_spend)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["overspend_block"] is False
    assert result["overspend_reason"] is None


def test_overspend_brake_missing_ad_spend_metric_skips_block():
    """Нет ad_spend в metrics вообще -> overspend_block=False, spend_status=None."""
    revenue_new = _metric(status="ahead")
    ctx = _ctx(revenue_new=revenue_new, ad_spend=None)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["overspend_block"] is False
    assert result["overspend_reason"] is None
    assert result["spend_status"] is None


def test_overspend_brake_status_ahead_not_blocked():
    """ad_spend.status='ahead' (расход в норме) -> overspend_block=False."""
    revenue_new = _metric(status="on_track")
    ad_spend = _metric(plan=50_000.0, status="ahead", is_cost_metric=True)
    ctx = _ctx(revenue_new=revenue_new, ad_spend=ad_spend)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["overspend_block"] is False


# ===========================================================================
# 6. Граница 36ч: data_as_of ровно на границе и чуть за ней.
# ===========================================================================


def test_staleness_exactly_at_36h_boundary_trusted():
    """age_hours == 36.0 ровно -> НЕ строго больше 36 -> движку доверяем (не None)."""
    as_of_dt = _NOW.astimezone(timezone.utc) - timedelta(hours=36)
    as_of = as_of_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx = _ctx(data_as_of=as_of)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None


def test_staleness_just_over_36h_distrusted():
    """age_hours чуть больше 36 (36ч + 1 минута) -> недоверие -> None."""
    as_of_dt = _NOW.astimezone(timezone.utc) - timedelta(hours=36, minutes=1)
    as_of = as_of_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx = _ctx(data_as_of=as_of)
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is None


# ===========================================================================
# 7. CdpError/исключение из get_budget_context -> None, не падает.
# ===========================================================================


def test_cdp_error_returns_none():
    """cdp_client.get_budget_context бросает CdpError -> compute_engine_pace is None."""
    with _patch_err(CdpError("budget-context недоступен")):
        result = compute_engine_pace(_NOW)

    assert result is None


def test_unexpected_exception_returns_none_never_raises():
    """cdp_client.get_budget_context бросает НЕ-CdpError исключение (баг где-то внутри) —
    двойная страховка try/except всё равно возвращает None, не пробрасывает наружу."""
    with _patch_err(RuntimeError("boom")):
        result = compute_engine_pace(_NOW)

    assert result is None


# ===========================================================================
# Регрессия: CDP может отдавать явные null вместо
# отсутствующих ключей — c.get("metrics", {})/summary.get("month_progress", {})
# и подобные .get(key, {}) не спасают от null (дефолт срабатывает только при
# ОТСУТСТВИИ ключа). Ожидание: не падает, ведёт себя как валидное отсутствие.
# ===========================================================================


def test_total_city_metrics_null_treated_as_missing_revenue_new():
    """cities=[{"city": "_total", "metrics": None}] (явный null metrics) ->
    _engine_metric не падает, revenue_new не найдена -> compute_engine_pace None."""
    ctx = _ctx(revenue_new=_metric(status="ahead"))
    ctx["cities"] = [{"city": "_total", "metrics": None}]  # боевой кейс
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is None


def test_month_progress_null_does_not_crash():
    """month_progress: null в ответе (поле не используется в вычислениях
    compute_engine_pace, но не должно падать при обращении к нему)."""
    revenue_new = _metric(status="ahead", plan=1_000_000.0, forecast_eom=1_100_000.0, pace_vs_expected=1.0)
    ctx = _ctx(revenue_new=revenue_new)
    ctx["month_progress"] = None  # боевой кейс
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is True


def test_cities_none_in_budget_context_treated_as_missing_total():
    """ctx["cities"] = None (не просто пустой список) -> _engine_metric/
    _engine_distrust_reason (нет _total) не падают -> compute_engine_pace None."""
    ctx = _ctx(revenue_new=_metric(status="ahead"))
    ctx["cities"] = None  # боевой кейс
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is None


def test_non_dict_city_element_in_cities_does_not_crash():
    """Элемент cities — не dict (например None среди списка городов) ->
    не должен ронять весь прогон при поиске _total."""
    ctx = _ctx(revenue_new=_metric(status="ahead"))
    ctx["cities"] = [None, {"city": "_total", "metrics": {"revenue_new": _metric(status="ahead")}}]
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is True


def test_engine_accuracy_none_does_not_crash():
    """ctx["engine_accuracy"] = None (явный null) -> _engine_wape не падает,
    ведёт себя как пустой список (wape=None -> недоверие по data_as_of/этому
    условию не срабатывает специально из-за wape, движок доверенный)."""
    revenue_new = _metric(status="ahead", plan=1_000_000.0, forecast_eom=1_100_000.0, pace_vs_expected=1.0)
    ctx = _ctx(revenue_new=revenue_new)
    ctx["engine_accuracy"] = None  # боевой кейс
    with _patch_ctx(ctx):
        result = compute_engine_pace(_NOW)

    assert result is not None
    assert result["plan_ok"] is True
    assert result["engine_wape"] is None
