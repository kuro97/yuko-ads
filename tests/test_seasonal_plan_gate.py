"""
Тесты сезонного план-гейта в services/budget_scaler.compute_cdp_unit_economics
(ARCH-cdp-seasonal-pacing §6.2, §9 — tests/test_seasonal_plan_gate.py, T5 волны 3).

Формула: fact_share = факт/план выручки месяца (сумма по городам CDP),
expected_share = pacing_curve.expected_cumulative_share(now) — историческая
консервативная кумулятивная доля. Гейт: plan_ok = fact_share >= expected_share
* _PLAN_GATE_BUFFER (буфер = 1.0). Дни 1-4 (expected_share=0.0) — нейтрально.
Заменяет линейный гейт fact_pace >= time_pct*0.9 (Шаг A) — тот ложно блокировал
"нормально низкий" факт в середине месяца (жалоба владельца, см. спеку §1).

Моки — на границе: services.cdp_client.get_daily_report/get_plan_fact_summary,
по образцу tests/test_cdp_unit_economics.py. now патчится ЛОКАЛЬНО в каждом
тесте (передаётся напрямую в compute_cdp_unit_economics(now) — модуль не
патчит datetime.now() внутри самой функции, поэтому отдельный _frozen_now не
нужен для юнит-тестов compute_cdp_unit_economics; он нужен только для полного
прогона run_budget_scaling, который здесь не тестируется — это уже покрыто
в test_cdp_unit_economics.py::test_cdp_plan_pace_behind_blocks_in_full_run).

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

from services.budget_scaler import compute_cdp_unit_economics
from services.pacing_curve import PACING_CURVE

_TZ = timezone(timedelta(hours=5))


def _daily_items():
    """Минимальный валидный набор для ДРР-части (не в фокусе этих тестов,
    но compute_cdp_unit_economics считает ДРР и план-гейт вместе)."""
    return [
        {"report_date": "2026-07-01", "city": "CityA", "usd_rate": 100.0,
         "ad_spend": 100.0, "revenue_new": 200_000.0, "drr_new": 999.0},
    ]


def _plan_fact_summary(time_pct=0.5, plan_rev=1_000_000.0, fact_rev=600_000.0, cities_override=None):
    """Строит cdp_client.get_plan_fact_summary(...)-подобный ответ.

    cities_override=[] эмулирует отсутствие планового знаменателя (§9
    test_no_plan_denominator_plan_ok). plan_rev=0 через cities с plan=0 даёт
    тот же эффект (fact_share=None), поэтому проверяем оба варианта.
    """
    if cities_override is not None:
        cities = cities_override
    else:
        cities = [
            {"city": "CityA", "plan": {"revenue_new": plan_rev * 0.6}, "fact": {"revenue_new": fact_rev * 0.6}},
            {"city": "CityB", "plan": {"revenue_new": plan_rev * 0.4}, "fact": {"revenue_new": fact_rev * 0.4}},
        ]
    return {"month": "2026-07-01", "time_pct": time_pct, "days_in_month": 31, "days_elapsed": 15, "cities": cities}


def _compute(now, *, time_pct=0.5, plan_rev=1_000_000.0, fact_rev=600_000.0, cities_override=None,
             daily_items=None):
    """Хелпер: мокает cdp_client на границе и вызывает compute_cdp_unit_economics(now)."""
    with patch("services.cdp_client.get_daily_report", return_value=daily_items or _daily_items()), \
         patch("services.cdp_client.get_plan_fact_summary",
               return_value=_plan_fact_summary(
                   time_pct=time_pct, plan_rev=plan_rev, fact_rev=fact_rev, cities_override=cities_override,
               )):
        return compute_cdp_unit_economics(now)


# ===========================================================================
# 1. ГЛАВНЫЙ КЕЙС: середина месяца, "нормально низкий" факт НЕ блокирует
# ===========================================================================


def test_owner_case_mid_month_low_fact_not_blocked():
    """15-е число, fact_share=0.35 >= expected_share=PACING_CURVE[15]=0.33 -> plan_ok=True.

    Это ровно жалоба владельца из спеки §1: старый линейный гейт (time_pct*0.9)
    в середине месяца видел "нормально низкий" факт и ошибочно блокировал подъём
    бюджета, потому что ожидал РАВНОМЕРНОГО накопления выручки по дням месяца.
    Но если оплаты смещены на вторую половину месяца (так задаёт кривая
    PACING_CURVE), к 15-му закрыто лишь ~33% (консервативный
    минимум), поэтому факт 35% на 15-е число — это НЕ отставание, а норма.
    Сезонная кривая это знает и НЕ блокирует подъём бюджета.
    """
    assert PACING_CURVE[15] == pytest.approx(0.33)
    now = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)
    # plan_rev=1_000_000, fact_rev=350_000 -> fact_share=0.35
    result = _compute(now, plan_rev=1_000_000.0, fact_rev=350_000.0)

    assert result is not None
    assert result["fact_share"] == pytest.approx(0.35)
    assert result["expected_share"] == pytest.approx(0.33)
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


# ===========================================================================
# 2. Реальное отставание в конце месяца — блокирует
# ===========================================================================


def test_end_of_month_real_lag_blocks():
    """25-е число, expected_share=PACING_CURVE[25]=0.63, fact_share=0.40 < 0.63
    -> plan_ok=False, plan_reason содержит "сезонно" (реальное отставание, не
    ложное срабатывание — конец месяца, консервативная кривая уже занижена
    (минимум по истории), а факт всё равно ниже неё)."""
    assert PACING_CURVE[25] == pytest.approx(0.63)  # sanity: сверим со спекой ниже
    now = datetime(2026, 7, 25, 10, 0, tzinfo=_TZ)
    # plan_rev=1_000_000, fact_rev=400_000 -> fact_share=0.40 < expected_share
    result = _compute(now, plan_rev=1_000_000.0, fact_rev=400_000.0)

    assert result is not None
    assert result["fact_share"] == pytest.approx(0.40)
    assert result["expected_share"] == pytest.approx(0.63)
    assert result["plan_ok"] is False
    assert result["plan_reason"] is not None
    assert "сезонно" in result["plan_reason"]


# ===========================================================================
# 3. Дни 1-4: expected_share=0.0 -> нейтрально (plan_ok=True) при любом fact_share
# ===========================================================================


@pytest.mark.parametrize("day", [1, 2, 3, 4])
@pytest.mark.parametrize("fact_rev", [0.0, 10_000.0, 999_999.0])
def test_days_1_4_neutral_regardless_of_fact_share(day, fact_rev):
    """Дни 1-4: expected_share=0.0 -> гейт нейтрален независимо от fact_share
    (нет статбазы для алертов, защита остаётся на ДРР-гейте и дневном капе)."""
    now = datetime(2026, 7, day, 10, 0, tzinfo=_TZ)
    result = _compute(now, plan_rev=1_000_000.0, fact_rev=fact_rev)

    assert result is not None
    assert result["expected_share"] == pytest.approx(0.0)
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


# ===========================================================================
# 4. Перевыполнение fact_share > 1 -> plan_ok=True
# ===========================================================================


def test_plan_overachieved():
    """20-е число, fact_share=1.3 (план перевыполнен на 30%) -> plan_ok=True,
    plan_reason=None. Перевыполнение — не повод блокировать подъём (§8.1)."""
    now = datetime(2026, 7, 20, 10, 0, tzinfo=_TZ)
    # plan_rev=1_000_000, fact_rev=1_300_000 -> fact_share=1.3
    result = _compute(now, plan_rev=1_000_000.0, fact_rev=1_300_000.0)

    assert result is not None
    assert result["fact_share"] == pytest.approx(1.3)
    assert result["expected_share"] == pytest.approx(0.48)
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


# ===========================================================================
# 5. Нет плана (plan_rev=0 -> fact_share None) -> plan_ok=True
# ===========================================================================


def test_no_plan_denominator_empty_cities_plan_ok():
    """cities=[] -> plan_rev=0 -> fact_share=None -> нет планового знаменателя,
    не выдумываем блокировку по темпу (как в Шаге A). ДРР-гейт остаётся главным
    предохранителем."""
    now = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)
    result = _compute(now, cities_override=[])

    assert result is not None
    assert result["fact_share"] is None
    assert result["fact_pace"] is None  # обратная совместимость = fact_share
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


def test_no_plan_denominator_zero_plan_rev_plan_ok():
    """Явный plan_rev=0 через cities с нулевым планом (тот же эффект, что
    cities=[]) -> fact_share=None -> plan_ok=True."""
    now = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)
    cities = [
        {"city": "CityA", "plan": {"revenue_new": 0.0}, "fact": {"revenue_new": 500_000.0}},
        {"city": "CityB", "plan": {"revenue_new": 0.0}, "fact": {"revenue_new": 0.0}},
    ]
    result = _compute(now, cities_override=cities)

    assert result is not None
    assert result["fact_share"] is None
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


# ===========================================================================
# Граница: fact_share == expected_share (буфер 1.0, >=) -> проходит
# ===========================================================================


def test_exactly_on_border_passes():
    """fact_share == expected_share*1.0 -> plan_ok=True (гейт использует >=,
    не строгое >). На 20-е число expected_share=0.48 -> fact_rev=480_000
    при plan_rev=1_000_000 даёт fact_share=0.48 ровно."""
    now = datetime(2026, 7, 20, 10, 0, tzinfo=_TZ)
    result = _compute(now, plan_rev=1_000_000.0, fact_rev=480_000.0)

    assert result is not None
    assert result["fact_share"] == pytest.approx(0.48)
    assert result["expected_share"] == pytest.approx(0.48)
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None


# ===========================================================================
# 6. Сравнение со старой формулой: линейный гейт блокировал бы, сезонный пропускает
# ===========================================================================


def test_seasonal_gate_replaces_linear_gate_day_10():
    """День 10: time_pct=0.32 (10/31≈0.3226), expected_share=PACING_CURVE[10]=0.18.

    СТАРАЯ формула (Шаг A, УСТАРЕЛА): fact_pace >= time_pct*0.9 = 0.32*0.9=0.288
    -> при fact_share=0.20 старая формула БЛОКИРОВАЛА бы (0.20 < 0.288).

    НОВАЯ сезонная формула: fact_share >= expected_share*1.0 = 0.18
    -> при том же fact_share=0.20 новая формула ПРОПУСКАЕТ (0.20 >= 0.18).

    Это и есть замена: линейный гейт наивно ждал равномерного накопления
    выручки (32% к 10-му дню), сезонная кривая знает, что к
    10-му дню закрыто лишь ~18% (консервативный минимум) — 20% факта это
    НЕ отставание. Фиксируем смысл замены числами.
    """
    assert PACING_CURVE[10] == pytest.approx(0.18)
    now = datetime(2026, 7, 10, 10, 0, tzinfo=_TZ)
    time_pct = 0.32
    old_linear_threshold = time_pct * 0.9  # = 0.288, старая формула (УСТАРЕЛА)
    fact_share = 0.20

    # Сверяем предпосылку: старая формула действительно заблокировала бы.
    assert fact_share < old_linear_threshold

    result = _compute(now, time_pct=time_pct, plan_rev=1_000_000.0, fact_rev=200_000.0)

    assert result is not None
    assert result["fact_share"] == pytest.approx(0.20)
    assert result["expected_share"] == pytest.approx(0.18)
    # Новая (сезонная) формула пропускает — в отличие от гипотетической старой.
    assert result["plan_ok"] is True
    assert result["plan_reason"] is None
    # time_pct сохранён в результате справочно, но НЕ определяет решение.
    assert result["time_pct"] == pytest.approx(0.32)


# ===========================================================================
# 7. Телеметрия: plan_gate_mode="seasonal", expected_share/fact_share в результате
# ===========================================================================


def test_result_has_seasonal_telemetry():
    """Любой валидный вход -> результат содержит plan_gate_mode="seasonal",
    expected_share (float) и fact_share (float) в дополнение к существующим
    ключам (fact_pace/time_pct сохранены для обратной совместимости)."""
    now = datetime(2026, 7, 18, 10, 0, tzinfo=_TZ)
    result = _compute(now, plan_rev=1_000_000.0, fact_rev=500_000.0)

    assert result is not None
    assert result["plan_gate_mode"] == "seasonal"
    assert isinstance(result["expected_share"], float)
    assert isinstance(result["fact_share"], float)
    assert result["expected_share"] == pytest.approx(PACING_CURVE[18])
    assert result["fact_share"] == pytest.approx(0.5)
    # Обратная совместимость: старые ключи никуда не делись.
    assert "fact_pace" in result
    assert "time_pct" in result
    assert result["fact_pace"] == pytest.approx(result["fact_share"])


def test_time_pct_ignored_for_decision_but_normalized():
    """time_pct=90.0 (проценты, нормализуется в 0.9) — старый линейный гейт с
    таким time_pct блокировал бы почти любой fact_pace<0.81, но НОВЫЙ сезонный
    гейт его игнорирует: на 15-е число fact_share=0.35 >= expected_share=0.33
    -> plan_ok=True несмотря на "высокий" time_pct."""
    now = datetime(2026, 7, 15, 10, 0, tzinfo=_TZ)
    result = _compute(now, time_pct=90.0, plan_rev=1_000_000.0, fact_rev=350_000.0)

    assert result is not None
    assert result["time_pct"] == pytest.approx(0.9)  # нормализация из процентов сохранена
    assert result["plan_ok"] is True  # решение приняла сезонная формула, не time_pct
    assert result["plan_reason"] is None
