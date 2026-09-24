"""
Тесты services/pacing_curve.py — эталонная сезонная pacing-кривая план-гейта.

Проверяем: полноту и монотонность PACING_CURVE, контрольные точки примера
кривой, нейтральные дни 1-4, поведение
на последнем дне короткого/длинного месяца (включая високосный февраль).

Задача T4 волны 2 спеки ARCH-cdp-seasonal-pacing.md.

Без сети, без моков — чистая функция от datetime.

Комментарии на русском.
"""

import calendar
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.pacing_curve import PACING_CURVE, expected_cumulative_share


# --- Дни 1-4: нет статбазы, гейт нейтрален (значение кривой 0.0) ---


@pytest.mark.parametrize("day", [1, 2, 3, 4])
def test_curve_days_1_4_are_zero(day):
    now = datetime(2026, 7, day)
    assert expected_cumulative_share(now) == 0.0


# --- Контрольные точки примера кривой (дни 1-4 = 0.0, дальше +0.03 в день) ---


def test_curve_mid_month_matches_json():
    # 15 июля — 31-дневный месяц, не последний день -> берём PACING_CURVE[15]
    now = datetime(2026, 7, 15)
    assert expected_cumulative_share(now) == 0.33


def test_curve_day_20():
    now = datetime(2026, 7, 20)
    assert expected_cumulative_share(now) == 0.48


# --- Последний день месяца всегда = 1.0 (месяц фактически закрыт) ---


def test_curve_last_day_31_month():
    now = datetime(2026, 7, 31)
    assert expected_cumulative_share(now) == 1.0


def test_curve_last_day_short_month():
    # Февраль 2026 (невисокосный) -> 28 дней, последний день -> 1.0,
    # НЕ PACING_CURVE[28] (0.72)
    now = datetime(2026, 2, 28)
    assert expected_cumulative_share(now) == 1.0


def test_curve_short_month_before_last():
    # 27 февраля — не последний день короткого месяца -> берём значение кривой
    now = datetime(2026, 2, 27)
    assert expected_cumulative_share(now) == PACING_CURVE[27] == 0.69


def test_curve_leap_february_29_is_last_day():
    # 2028 — високосный год, февраль = 29 дней. 29 февраля -> последний день -> 1.0
    assert calendar.isleap(2028)
    assert calendar.monthrange(2028, 2)[1] == 29
    now = datetime(2028, 2, 29)
    assert expected_cumulative_share(now) == 1.0


def test_curve_leap_february_28_before_last():
    # 28 февраля 2028 — НЕ последний день (в этом году их 29) -> значение кривой, не 1.0
    now = datetime(2028, 2, 28)
    assert expected_cumulative_share(now) == PACING_CURVE[28] == 0.72


def test_curve_april_30_last_day_short_month():
    # Апрель — 30 дней. 30 апреля -> последний день -> 1.0, НЕ PACING_CURVE[30] (0.78)
    now = datetime(2026, 4, 30)
    assert expected_cumulative_share(now) == 1.0


# --- Полнота и монотонность кривой ---


def test_curve_all_keys_1_31_present():
    assert set(PACING_CURVE.keys()) == set(range(1, 32))


def test_curve_values_in_unit_interval():
    for day, value in PACING_CURVE.items():
        assert 0.0 <= value <= 1.0, f"день {day}: значение {value} вне [0,1]"


def test_curve_monotonic_non_decreasing():
    days = sorted(PACING_CURVE.keys())
    for prev_day, next_day in zip(days, days[1:]):
        assert PACING_CURVE[prev_day] <= PACING_CURVE[next_day], (
            f"кривая убывает: день {prev_day}={PACING_CURVE[prev_day]} "
            f"> день {next_day}={PACING_CURVE[next_day]}"
        )


def test_curve_expected_share_monotonic_within_month():
    # Проверяем и через саму функцию (не только словарь напрямую) для 31-дневного месяца
    values = [expected_cumulative_share(datetime(2026, 7, d)) for d in range(1, 32)]
    for prev_v, next_v in zip(values, values[1:]):
        assert prev_v <= next_v


# --- Устойчивость: функция не должна бросать исключений ни на одном дне 2026 года ---


def test_curve_no_exception_on_any_day():
    for month in range(1, 13):
        days_in_month = calendar.monthrange(2026, month)[1]
        for day in range(1, days_in_month + 1):
            now = datetime(2026, month, day)
            result = expected_cumulative_share(now)
            assert isinstance(result, float)
            assert 0.0 <= result <= 1.0
