"""
Тесты правила тренда недельных когорт (services/trend_policy.py).

Проверяют дисциплину суждения, а не «числа сошлись»:
- недельный шум ниже порога своего объёма → ПЛАТО, а не ПАДЕНИЕ;
- объём меньше 20 лидов в неделю → МАЛО ДАННЫХ, без исключений;
- comparable=0 в любой из сравниваемых недель → МАЛО ДАННЫХ с причиной и БЕЗ
  единого числа тренда;
- одиночная просадка при стабильных соседних неделях → не action-grade;
- три недели подряд в одну сторону → РОСТ/ПАДЕНИЕ;
- уровень объявления не даёт action-grade НИКОГДА;
- текущая и ещё не дозревшая недели не участвуют в сравнении;
- интервал Уилсона совпадает с известными табличными значениями;
- ROMI (волна 4) считается из СУММ, но остаётся справочным: обвал ROMI при
  ровном квале вердикта не создаёт, потому что порог шума для ROMI не замерен.

Модуль чистый: ни БД, ни сети, ни часов — «сегодня» приходит параметром.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.kill_policy import EvidenceStatus  # noqa: E402
from services.trend_policy import (  # noqa: E402
    MIN_WEEK_AGE_DAYS,
    MIN_WEEK_LEADS,
    NOISE_FLOOR_PP,
    SUSTAIN_MARGIN,
    SustainProof,
    TrendLevel,
    TrendPolicyError,
    TrendStatus,
    WeekPoint,
    aggregate_week_points,
    evaluate_cohort_rows,
    evaluate_trend,
    noise_floor_pp,
    parse_week_point,
    week_age_days,
    wilson_interval,
)

# Опорный календарь. AS_OF — понедельник 27.07.2026.
AS_OF = date(2026, 7, 27)
W1 = date(2026, 7, 13)   # 13–19.07, возраст 8 дней → дозрела
W2 = date(2026, 7, 6)    # 06–12.07, возраст 15 дней
W3 = date(2026, 6, 29)   # 29.06–05.07, возраст 22 дня
W4 = date(2026, 6, 22)   # 22–28.06, возраст 29 дней
W_IMMATURE = date(2026, 7, 20)  # закрыта, но возраст 1 день
W_CURRENT = date(2026, 7, 27)   # текущая неделя
# Курс недели в тестах денежной части (волна 4).
RATE = 500.0


def week(
    week_start: date,
    qual_pct: float,
    leads: int = 100,
    *,
    spend_usd: float | None = 1000.0,
) -> WeekPoint:
    """Полная сравнимая неделя с заданным квал-процентом."""
    return WeekPoint(
        week_start=week_start,
        amo_leads=leads,
        quals=round(qual_pct / 100 * leads),
        comparable=True,
        spend_usd=spend_usd,
    )


def money_week(
    week_start: date,
    qual_pct: float,
    leads: int = 100,
    *,
    spend_usd: float | None = 1000.0,
    revenue_lcy: float | None = 1_000_000.0,
    usd_lcy_rate: float | None = RATE,
    revenue_mature: bool = True,
    horizon_days: int | None = 14,
) -> WeekPoint:
    """Полная неделя с дозревшей когортной выручкой (волна 4)."""
    return WeekPoint(
        week_start=week_start,
        amo_leads=leads,
        quals=round(qual_pct / 100 * leads),
        comparable=True,
        spend_usd=spend_usd,
        revenue_lcy=revenue_lcy,
        usd_lcy_rate=usd_lcy_rate,
        revenue_mature=revenue_mature,
        revenue_horizon_days=horizon_days,
    )


def broken_week(week_start: date, reason: str = "FB_DAYS_MISSING:2026-07-15") -> WeekPoint:
    """Неделя с дырой в данных: расход неизвестен, сравнивать нельзя."""
    return WeekPoint(
        week_start=week_start,
        amo_leads=100,
        quals=20,
        comparable=False,
        not_comparable_reason=reason,
        spend_usd=None,
    )


def judge(points, level=TrendLevel.ADSET, as_of=AS_OF):
    return evaluate_trend(
        points, level=level, entity_id="adset-1", as_of=as_of, entity_name="CityA | PRODA"
    )


# ---------------------------------------------------------------------------
# Интервал Уилсона — сверка с известными значениями
# ---------------------------------------------------------------------------

def test_wilson_matches_known_values_for_half_of_hundred():
    """50 из 100 при 95% → (0.4038, 0.5962) — классическое табличное значение."""
    low, high = wilson_interval(50, 100)
    assert low == pytest.approx(0.4038, abs=1e-4)
    assert high == pytest.approx(0.5962, abs=1e-4)


def test_wilson_does_not_collapse_at_zero_successes():
    """0 из 10 → (0, 0.2775): интервал не схлопывается в точку, как у нормальной."""
    low, high = wilson_interval(0, 10)
    assert low == 0.0
    assert high == pytest.approx(0.2775, abs=1e-4)


def test_wilson_matches_known_values_for_full_success():
    """10 из 10 → (0.7225, 1.0) — зеркало предыдущего случая."""
    low, high = wilson_interval(10, 10)
    assert low == pytest.approx(0.7225, abs=1e-4)
    assert high == pytest.approx(1.0, abs=1e-9)


def test_wilson_narrows_when_sample_grows():
    """Тот же процент на большем объёме — уже интервал."""
    small = wilson_interval(20, 100)
    large = wilson_interval(200, 1000)
    assert (large[1] - large[0]) < (small[1] - small[0])


def test_wilson_rejects_impossible_input():
    """Успехов больше наблюдений или пустая выборка — ошибка, а не догадка."""
    with pytest.raises(TrendPolicyError):
        wilson_interval(11, 10)
    with pytest.raises(TrendPolicyError):
        wilson_interval(0, 0)
    with pytest.raises(TrendPolicyError):
        wilson_interval(-1, 10)


# ---------------------------------------------------------------------------
# Таблица шума
# ---------------------------------------------------------------------------

def test_noise_floor_is_monotone_by_volume():
    """Порог не растёт с объёмом: меньше данных — не мягче требование."""
    for level, table in NOISE_FLOOR_PP.items():
        values = [value for _, value in table]
        assert values == sorted(values, reverse=True), level


def test_noise_floor_never_below_five_points():
    """Порога «5 п.п.» не существует ни на одном объёме."""
    for level in TrendLevel:
        for leads in (20, 40, 80, 160, 1000):
            assert noise_floor_pp(level, leads) > 5.0


def test_noise_floor_on_ad_level_is_stricter_than_on_adset():
    """На объявлении шум выше — порог обязан быть строже при том же объёме."""
    for leads in (20, 40, 100):
        assert noise_floor_pp(TrendLevel.AD, leads) >= noise_floor_pp(
            TrendLevel.ADSET, leads
        )


def test_noise_floor_uses_measured_buckets():
    """Крупный адсет (160+) — 7,1 п.п.; средний (80–159) — 13,8 п.п."""
    assert noise_floor_pp(TrendLevel.ADSET, 200) == pytest.approx(7.1)
    assert noise_floor_pp(TrendLevel.ADSET, 100) == pytest.approx(13.8)


# ---------------------------------------------------------------------------
# Зрелость недель
# ---------------------------------------------------------------------------

def test_week_age_counts_from_sunday():
    """Возраст недели — дни от её воскресенья, у текущей он отрицателен."""
    assert week_age_days(W1, AS_OF) == 8
    assert week_age_days(W_IMMATURE, AS_OF) == 1
    assert week_age_days(W_CURRENT, AS_OF) < 0


def test_current_and_immature_weeks_never_participate():
    """Текущая неделя и закрытая моложе 7 дней в сравнение не входят."""
    verdict = judge([
        week(W3, 40.0),
        week(W2, 28.0),
        week(W1, 16.0),
        week(W_IMMATURE, 90.0),   # закрыта вчера — ещё не дозрела
        week(W_CURRENT, 95.0),    # текущая — не закрыта вовсе
    ])

    assert verdict.recent.week_start == W1
    assert verdict.previous.week_start == W2
    assert verdict.recent.age_days >= MIN_WEEK_AGE_DAYS
    assert verdict.status is TrendStatus.DECLINE


def test_only_immature_weeks_give_insufficient():
    """Есть только незрелые недели — сравнивать нечего, чисел не даём."""
    verdict = judge([week(W_IMMATURE, 30.0), week(W_CURRENT, 8.0)])

    assert verdict.status is TrendStatus.INSUFFICIENT
    assert verdict.evidence is EvidenceStatus.UNKNOWN
    assert verdict.reason.startswith("NO_MATURE_WEEK")
    assert verdict.delta_pp is None


def test_missing_neighbour_week_gives_insufficient():
    """Разрыв в неделях (реклама стояла) — соседа нет, вердикта нет."""
    verdict = judge([week(W4, 40.0), week(W1, 16.0)])

    assert verdict.status is TrendStatus.INSUFFICIENT
    assert verdict.reason.startswith("PREV_WEEK_MISSING")
    assert verdict.delta_pp is None


# ---------------------------------------------------------------------------
# Шум против сигнала
# ---------------------------------------------------------------------------

def test_noise_below_threshold_is_plateau_not_decline():
    """Просадка меньше 90-го процентиля шума — ПЛАТО, а не ПАДЕНИЕ."""
    verdict = judge([week(W3, 20.0), week(W2, 22.0), week(W1, 19.0)])

    assert verdict.status is TrendStatus.PLATEAU
    assert verdict.action_grade is False
    assert verdict.reason.startswith("DELTA_BELOW_NOISE")
    assert abs(verdict.delta_pp) < verdict.noise_floor_pp
    # Наблюдение не теряется — просто не считается трендом.
    assert verdict.observed_direction == "вниз"


def test_five_point_drop_is_not_a_trend_on_any_volume():
    """«5 п.п.» не проходит нигде: порог берётся из таблицы шума."""
    for leads in (40, 100, 300):
        verdict = judge([
            week(W3, 30.0, leads),
            week(W2, 30.0, leads),
            week(W1, 25.0, leads),
        ])
        assert verdict.status is TrendStatus.PLATEAU, leads


def test_wilson_overlap_blocks_verdict_on_thin_sample():
    """Дельта выше порога, но интервалы Уилсона пересеклись — ПЛАТО."""
    verdict = judge([
        week(W3, 50.0, 20),
        week(W2, 50.0, 20),
        week(W1, 25.0, 20),
    ])

    assert verdict.status is TrendStatus.PLATEAU
    assert verdict.wilson_separated is False
    assert verdict.reason == "WILSON_OVERLAP"


# ---------------------------------------------------------------------------
# Устойчивость
# ---------------------------------------------------------------------------

def test_single_dip_between_stable_weeks_is_not_action_grade():
    """Одиночная просадка при стабильных соседях не даёт права действовать."""
    verdict = judge([week(W3, 30.0), week(W2, 30.0), week(W1, 10.0)])

    assert verdict.action_grade is False
    assert verdict.status is TrendStatus.PLATEAU
    assert verdict.reason.startswith("NOT_SUSTAINED")
    assert verdict.sustained_by is None
    # Дельта прошла и порог шума, и Уилсона — отвергла её именно устойчивость.
    assert abs(verdict.delta_pp) >= verdict.noise_floor_pp
    assert verdict.wilson_separated is True


def test_three_weeks_down_in_a_row_is_decline():
    """Три недели подряд вниз — ПАДЕНИЕ с правом действовать на адсете."""
    verdict = judge([week(W3, 40.0), week(W2, 28.0), week(W1, 16.0)])

    assert verdict.status is TrendStatus.DECLINE
    assert verdict.action_grade is True
    assert verdict.sustained_by is SustainProof.THREE_WEEKS
    assert verdict.reason == "TREND_CONFIRMED:three_weeks"
    assert verdict.total_delta_pp == pytest.approx(-24.0)
    assert verdict.evidence is EvidenceStatus.COMPLETE


def test_three_weeks_up_in_a_row_is_growth():
    """Зеркальный случай: разгоняющаяся ракета — РОСТ."""
    verdict = judge([week(W3, 10.0), week(W2, 20.0), week(W1, 32.0)])

    assert verdict.status is TrendStatus.GROWTH
    assert verdict.action_grade is True
    assert verdict.observed_direction == "вверх"


def test_huge_single_jump_passes_by_margin():
    """Третьей недели нет, но дельта вдвое выше порога — вердикт есть."""
    verdict = judge([week(W2, 30.0), week(W1, 0.0)])

    assert verdict.status is TrendStatus.DECLINE
    assert verdict.sustained_by is SustainProof.MARGIN
    assert abs(verdict.delta_pp) >= verdict.noise_floor_pp * SUSTAIN_MARGIN


def test_third_week_below_volume_does_not_confirm_trend():
    """W-3 на 10 лидах не подтверждает направление: её объём ниже минимума."""
    verdict = judge([
        week(W3, 50.0, 10),
        week(W2, 40.0, 100),
        week(W1, 15.0, 100),
    ])

    assert verdict.sustained_by is None
    assert verdict.status is TrendStatus.PLATEAU
    assert verdict.reason.startswith("NOT_SUSTAINED")


def test_incomparable_third_week_does_not_confirm_trend():
    """Дырявая W-3 в подтверждение не годится — но W-1/W-2 сравнить не мешает."""
    verdict = judge([broken_week(W3), week(W2, 30.0), week(W1, 15.0)])

    assert verdict.status is TrendStatus.PLATEAU
    assert verdict.sustained_by is None
    assert verdict.earlier.comparable is False


# ---------------------------------------------------------------------------
# Объём и полнота
# ---------------------------------------------------------------------------

def test_volume_below_minimum_is_insufficient():
    """Меньше 20 лидов в неделю — МАЛО ДАННЫХ, каким бы ни был обвал."""
    verdict = judge([week(W3, 60.0, 19), week(W2, 60.0, 19), week(W1, 0.0, 19)])

    assert verdict.status is TrendStatus.INSUFFICIENT
    assert verdict.reason.startswith("VOLUME_BELOW_MIN")
    assert verdict.min_leads == 19
    assert verdict.delta_pp is None
    assert verdict.action_grade is False


def test_volume_minimum_is_checked_on_both_weeks():
    """Хватает объёма только в одной неделе — этого мало."""
    verdict = judge([week(W2, 40.0, MIN_WEEK_LEADS), week(W1, 0.0, 5)])

    assert verdict.status is TrendStatus.INSUFFICIENT
    assert verdict.min_leads == 5


def test_incomparable_recent_week_gives_insufficient_with_reason():
    """comparable=0 у W-1 → МАЛО ДАННЫХ с причиной и без единого числа."""
    verdict = judge([week(W3, 40.0), week(W2, 30.0), broken_week(W1)])

    assert verdict.status is TrendStatus.INSUFFICIENT
    assert verdict.evidence is EvidenceStatus.INCOMPLETE
    assert verdict.reason.startswith("WEEK_NOT_COMPARABLE")
    assert "FB_DAYS_MISSING" in verdict.reason
    assert verdict.delta_pp is None
    assert verdict.total_delta_pp is None
    assert verdict.noise_floor_pp is None
    assert verdict.wilson_separated is None


def test_incomparable_previous_week_gives_insufficient_with_reason():
    """То же для W-2: неполнота любой из сравниваемых недель отменяет вердикт."""
    verdict = judge([week(W3, 40.0), broken_week(W2), week(W1, 10.0)])

    assert verdict.status is TrendStatus.INSUFFICIENT
    assert verdict.reason.startswith("WEEK_NOT_COMPARABLE")
    assert verdict.delta_pp is None


# ---------------------------------------------------------------------------
# Уровень объявления — только диагностика
# ---------------------------------------------------------------------------

def test_ad_level_never_action_grade_even_on_perfect_trend():
    """Три недели подряд вниз на объявлении — вердикт есть, права действовать нет."""
    verdict = judge(
        [week(W3, 60.0), week(W2, 40.0), week(W1, 20.0)],
        level=TrendLevel.AD,
    )

    assert verdict.status is TrendStatus.DECLINE
    assert verdict.action_grade is False
    assert verdict.reason.startswith("AD_LEVEL_DIAGNOSTIC_ONLY")


def test_ad_level_action_grade_is_false_for_every_status():
    """Ни один статус на уровне объявления не становится action-grade."""
    cases = [
        [week(W3, 40.0), week(W2, 28.0), week(W1, 16.0)],   # тренд
        [week(W3, 20.0), week(W2, 22.0), week(W1, 19.0)],   # шум
        [week(W3, 40.0, 10), week(W2, 40.0, 10), week(W1, 0.0, 10)],  # мало
    ]
    for points in cases:
        assert judge(points, level=TrendLevel.AD).action_grade is False


def test_ad_level_floor_is_never_softer_than_adset():
    """Объявление — часть адсета: тише него оно не бывает, порог не мягче.

    На больших объёмах замер по объявлениям обрывается (7,6 п.п. на 40–79), и
    продлевать его вверх нельзя: на адсетах при 80–159 лидах измерено
    13,8 п.п. Поэтому у объявления порог поднят до адсетного.
    """
    assert noise_floor_pp(TrendLevel.AD, 100) == pytest.approx(13.8)
    assert noise_floor_pp(TrendLevel.AD, 200) > noise_floor_pp(TrendLevel.ADSET, 200)


# ---------------------------------------------------------------------------
# Второй сигнал: расход и CPL. ROMI не считается вовсе
# ---------------------------------------------------------------------------

def test_cpl_is_informational_and_does_not_change_status():
    """Расход вырос втрое — статус тренда от этого не меняется."""
    cheap = judge([week(W3, 20.0), week(W2, 22.0), week(W1, 19.0)])
    pricey = judge([
        week(W3, 20.0, spend_usd=1000.0),
        week(W2, 22.0, spend_usd=1000.0),
        week(W1, 19.0, spend_usd=3000.0),
    ])

    assert cheap.status is pricey.status is TrendStatus.PLATEAU
    assert pricey.cpl_change_pct == pytest.approx(200.0)


def test_romi_is_measured_but_never_action_grade():
    """Волна 4: ROMI считается, но остаётся СПРАВОЧНЫМ.

    Заменяет проверку волны 2 «ROMI не считается вовсе»: выручка стала
    когортной (миграция 025), поэтому число появилось. Порог шума для него не
    замерен, значит на статус и право действовать он влиять не должен.
    """
    verdict = judge([
        money_week(W3, 40.0, revenue_lcy=5_000_000.0),
        money_week(W2, 28.0, revenue_lcy=3_000_000.0),
        money_week(W1, 16.0, revenue_lcy=100_000.0),
    ])

    # ROMI виден…
    assert verdict.romi_recent == pytest.approx(
        100_000.0 / (1000.0 * RATE) * 100
    )
    assert verdict.romi_previous is not None
    assert verdict.romi_change_pp == pytest.approx(
        verdict.romi_recent - verdict.romi_previous
    )
    # …но статус и право действовать по-прежнему определяет только квал.
    assert verdict.status is TrendStatus.DECLINE
    assert verdict.action_grade is True


def test_romi_collapse_alone_does_not_create_verdict():
    """Обвал ROMI при ровном квале — ПЛАТО: порога шума для ROMI нет."""
    verdict = judge([
        money_week(W3, 20.0, revenue_lcy=9_000_000.0),
        money_week(W2, 20.0, revenue_lcy=5_000_000.0),
        money_week(W1, 20.0, revenue_lcy=10_000.0),
    ])

    assert verdict.status is TrendStatus.PLATEAU
    assert verdict.action_grade is False
    assert verdict.romi_change_pp is not None
    assert verdict.romi_change_pp < 0


def test_romi_unknown_while_cohort_is_immature():
    """Недозревшая когорта не даёт ROMI — ни нуля, ни оценки."""
    verdict = judge([
        money_week(W3, 30.0, revenue_lcy=1_000_000.0),
        money_week(W2, 30.0, revenue_lcy=1_000_000.0),
        money_week(W1, 30.0, revenue_lcy=None, revenue_mature=False),
    ])

    assert verdict.romi_recent is None
    assert verdict.romi_change_pp is None
    assert verdict.recent.revenue_mature is False


def test_romi_unknown_without_rate():
    """Нет курса недели — нет ROMI (по сегодняшнему курсу не считаем)."""
    verdict = judge([
        money_week(W3, 30.0, revenue_lcy=1_000_000.0),
        money_week(W2, 30.0, revenue_lcy=1_000_000.0),
        money_week(W1, 30.0, revenue_lcy=1_000_000.0, usd_lcy_rate=None),
    ])

    assert verdict.romi_recent is None
    assert verdict.romi_previous is not None


def test_romi_not_compared_across_different_horizons():
    """ROMI за 14 и за 30 дней — разные метрики, дельта между ними не считается."""
    verdict = judge([
        money_week(W3, 30.0, revenue_lcy=1_000_000.0),
        money_week(W2, 30.0, revenue_lcy=1_000_000.0, horizon_days=30),
        money_week(W1, 30.0, revenue_lcy=1_000_000.0, horizon_days=14),
    ])

    assert verdict.romi_recent is not None
    assert verdict.romi_previous is not None
    assert verdict.romi_change_pp is None


def test_adset_romi_is_computed_from_sums_not_average_of_percents():
    """ROMI адсета считается из сумм: проценты объявлений усреднять нельзя."""
    big = WeekPoint(
        week_start=W1, amo_leads=100, quals=20, comparable=True,
        spend_usd=1000.0, revenue_lcy=1_000_000.0, usd_lcy_rate=RATE,
        revenue_mature=True, revenue_horizon_days=14,
    )
    small = WeekPoint(
        week_start=W1, amo_leads=10, quals=2, comparable=True,
        spend_usd=10.0, revenue_lcy=100_000.0, usd_lcy_rate=RATE,
        revenue_mature=True, revenue_horizon_days=14,
    )

    merged = aggregate_week_points([big, small])

    # Средний ROMI объявлений был бы (200% + 2000%)/2 = 1100%; правильный —
    # 1.1 млн ¤ на 1010 $ расхода.
    assert merged.romi_pct == pytest.approx(
        1_100_000.0 / (1010.0 * RATE) * 100
    )


def test_adset_revenue_unknown_if_one_ad_is_unknown():
    """Одно объявление без выручки делает выручку адсета неизвестной."""
    known = WeekPoint(
        week_start=W1, amo_leads=100, quals=20, comparable=True,
        spend_usd=1000.0, revenue_lcy=1_000_000.0, usd_lcy_rate=RATE,
        revenue_mature=True, revenue_horizon_days=14,
    )
    unknown = WeekPoint(
        week_start=W1, amo_leads=10, quals=2, comparable=True,
        spend_usd=10.0, revenue_lcy=None, usd_lcy_rate=RATE,
        revenue_mature=True, revenue_horizon_days=14,
    )

    merged = aggregate_week_points([known, unknown])

    assert merged.revenue_lcy is None
    assert merged.romi_pct is None


def test_adset_romi_unknown_if_one_ad_is_immature():
    """Пока хоть одна часть адсета зреет, ROMI адсета не считается."""
    mature = WeekPoint(
        week_start=W1, amo_leads=100, quals=20, comparable=True,
        spend_usd=1000.0, revenue_lcy=1_000_000.0, usd_lcy_rate=RATE,
        revenue_mature=True, revenue_horizon_days=14,
    )
    maturing = WeekPoint(
        week_start=W1, amo_leads=10, quals=2, comparable=True,
        spend_usd=10.0, revenue_lcy=0.0, usd_lcy_rate=RATE,
        revenue_mature=False, revenue_horizon_days=14,
    )

    merged = aggregate_week_points([mature, maturing])

    assert merged.revenue_mature is False
    assert merged.romi_pct is None


def test_negative_revenue_is_allowed_and_gives_negative_romi():
    """Возврат больше прихода — выручка в минусе, ROMI отрицательный."""
    point = money_week(W1, 30.0, revenue_lcy=-50_000.0)

    assert point.revenue_lcy == pytest.approx(-50_000.0)
    assert point.romi_pct < 0


def test_rate_must_be_positive():
    """Курс 0 — это не курс, а деление на ноль."""
    with pytest.raises(TrendPolicyError):
        WeekPoint(
            week_start=W1, amo_leads=100, quals=20, comparable=True,
            spend_usd=1000.0, revenue_lcy=1000.0, usd_lcy_rate=0.0,
            revenue_mature=True,
        )


def test_parse_week_point_reads_revenue_columns():
    """Строка когорты с деньгами разбирается целиком."""
    point = parse_week_point({
        "week_start": "2026-07-13",
        "amo_leads": 100,
        "quals": 20,
        "comparable": 1,
        "not_comparable_reason": None,
        "spend_usd": 1000.0,
        "revenue_lcy": 1_000_000.0,
        "usd_lcy_rate": RATE,
        "revenue_mature": 1,
        "revenue_horizon_days": 14,
    })

    assert point.revenue_mature is True
    assert point.revenue_horizon_days == 14
    assert point.romi_pct == pytest.approx(1_000_000.0 / (1000.0 * RATE) * 100)


def test_parse_week_point_without_revenue_columns_is_not_an_error():
    """БД без миграции 025 — просто «выручка неизвестна», а не падение."""
    point = parse_week_point({
        "week_start": "2026-07-13",
        "amo_leads": 100,
        "quals": 20,
        "comparable": 1,
        "spend_usd": 1000.0,
    })

    assert point.revenue_mature is False
    assert point.revenue_lcy is None
    assert point.romi_pct is None


def test_cpl_unknown_when_spend_missing():
    """Расход неизвестен — CPL не выдумывается."""
    verdict = judge([
        week(W3, 40.0, spend_usd=None),
        week(W2, 28.0, spend_usd=None),
        week(W1, 16.0, spend_usd=None),
    ])

    assert verdict.cpl_change_pct is None
    assert verdict.recent.cpl_usd is None
    assert verdict.status is TrendStatus.DECLINE


# ---------------------------------------------------------------------------
# Вход: строгость модели
# ---------------------------------------------------------------------------

def test_week_point_requires_monday():
    """Неделя задаётся понедельником — иначе границы поедут."""
    with pytest.raises(TrendPolicyError):
        WeekPoint(week_start=date(2026, 7, 14), amo_leads=10, quals=1, comparable=True)


def test_week_point_mirrors_cohort_checks():
    """Дисциплина строки ad_weekly_cohorts повторена в модели входа."""
    with pytest.raises(TrendPolicyError):
        WeekPoint(week_start=W1, amo_leads=None, quals=None, comparable=True)
    with pytest.raises(TrendPolicyError):
        WeekPoint(week_start=W1, amo_leads=10, quals=1, comparable=False)
    with pytest.raises(TrendPolicyError):
        WeekPoint(week_start=W1, amo_leads=10, quals=11, comparable=True)


def test_duplicate_week_is_rejected_not_silently_merged():
    """Две строки одной недели — ошибка входа: складывать должен вызывающий."""
    with pytest.raises(TrendPolicyError):
        judge([week(W1, 10.0), week(W1, 20.0)])


def test_parse_week_point_reads_cohort_row():
    """Строка ad_weekly_cohorts разбирается как есть, лишние колонки не мешают."""
    point = parse_week_point({
        "ad_id": "ad-1",
        "week_start": "2026-07-13",
        "adset_id": "adset-1",
        "amo_leads": 100,
        "quals": 17,
        "spend_usd": 1000.0,
        "comparable": 1,
        "not_comparable_reason": None,
        "city": "CityA",
    })

    assert point.week_start == W1
    assert point.comparable is True
    assert point.qual_pct == pytest.approx(17.0)
    assert point.cpl_usd == pytest.approx(10.0)


def test_parse_week_point_rejects_broken_row():
    """Кривая строка — ошибка, а не молчаливый ноль."""
    with pytest.raises(TrendPolicyError):
        parse_week_point({"week_start": "13.07.2026", "comparable": 1,
                          "amo_leads": 1, "quals": 0})
    with pytest.raises(TrendPolicyError):
        parse_week_point({"week_start": "2026-07-13", "comparable": 2,
                          "amo_leads": 1, "quals": 0})
    with pytest.raises(TrendPolicyError):
        parse_week_point({"week_start": "2026-07-13", "comparable": 1,
                          "amo_leads": -5, "quals": 0})


# ---------------------------------------------------------------------------
# Агрегация объявлений в адсет
# ---------------------------------------------------------------------------

def test_aggregate_sums_leads_and_spend():
    """Неделя адсета — сумма его объявлений."""
    merged = aggregate_week_points([
        WeekPoint(W1, 50, 10, True, spend_usd=600.0),
        WeekPoint(W1, 50, 6, True, spend_usd=650.0),
    ])

    assert merged.amo_leads == 100
    assert merged.quals == 16
    assert merged.spend_usd == pytest.approx(1250.0)
    assert merged.comparable is True


def test_aggregate_propagates_unknown_metric():
    """NULL заразителен: неизвестный расход одного объявления рушит сумму."""
    merged = aggregate_week_points([
        WeekPoint(W1, 50, 10, True, spend_usd=600.0),
        WeekPoint(W1, 50, 6, False, "FB_DAYS_MISSING:2026-07-15", None),
    ])

    assert merged.spend_usd is None
    assert merged.comparable is False
    assert "FB_DAYS_MISSING" in merged.not_comparable_reason


def test_aggregate_rejects_mixed_weeks():
    """Складывать разные недели нельзя — это была бы подмена периода."""
    with pytest.raises(TrendPolicyError):
        aggregate_week_points([week(W1, 10.0), week(W2, 10.0)])


def test_evaluate_cohort_rows_groups_ads_into_adsets():
    """Строки когорт → вердикты по адсетам; объявления недели складываются."""
    rows = []
    for week_start, qual_pct in ((W3, 40), (W2, 28), (W1, 16)):
        for ad_id in ("ad-1", "ad-2"):
            rows.append({
                "ad_id": ad_id,
                "ad_name": f"CityA | {ad_id}",
                "adset_id": "adset-A",
                "adset_name": "CityA | PRODA | Онлайн",
                "week_start": week_start.isoformat(),
                "amo_leads": 50,
                "quals": round(qual_pct / 100 * 50),
                "spend_usd": 500.0,
                "comparable": 1,
                "not_comparable_reason": None,
            })

    verdicts = evaluate_cohort_rows(rows, level=TrendLevel.ADSET, as_of=AS_OF)

    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.entity_id == "adset-A"
    assert verdict.entity_name == "CityA | PRODA | Онлайн"
    assert verdict.recent.leads == 100
    assert verdict.status is TrendStatus.DECLINE
    assert verdict.action_grade is True


def test_evaluate_cohort_rows_skips_rows_without_adset():
    """Строка без adset_id не приписывается чужому адсету."""
    rows = [{
        "ad_id": "ad-1",
        "adset_id": None,
        "adset_name": None,
        "week_start": W1.isoformat(),
        "amo_leads": 100,
        "quals": 20,
        "spend_usd": 100.0,
        "comparable": 1,
        "not_comparable_reason": None,
    }]

    assert evaluate_cohort_rows(rows, level=TrendLevel.ADSET, as_of=AS_OF) == []
    assert len(evaluate_cohort_rows(rows, level=TrendLevel.AD, as_of=AS_OF)) == 1


def test_evaluate_trend_is_pure():
    """Правило не мутирует вход: те же точки дают тот же вердикт."""
    points = [week(W3, 40.0), week(W2, 28.0), week(W1, 16.0)]
    snapshot = list(points)

    first = judge(points)
    second = judge(points)

    assert points == snapshot
    assert first == second


def test_verdict_reports_compared_weeks_with_age_and_volume():
    """Вердикт обязан назвать сравниваемые недели, их возраст и объёмы."""
    verdict = judge([week(W3, 40.0, 90), week(W2, 28.0, 110), week(W1, 16.0, 120)])

    assert verdict.previous.week_start == W2
    assert verdict.previous.week_end == W2 + timedelta(days=6)
    assert verdict.recent.week_start == W1
    assert verdict.recent.age_days == 8
    assert verdict.previous.age_days == 15
    assert (verdict.previous.leads, verdict.recent.leads) == (110, 120)
    assert verdict.min_leads == 110
