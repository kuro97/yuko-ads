"""Тесты для S17: Сравнение периодов."""

import pytest
from services.comparison import (
    compare_periods,
    get_period_ranges,
    _aggregate_by_city,
    _calc_delta,
    _empty_metrics,
)


class TestGetPeriodRanges:
    """get_period_ranges возвращает два диапазона."""

    def test_week_returns_two_ranges(self):
        cur, prev = get_period_ranges("week")
        assert len(cur) == 2
        assert len(prev) == 2
        # Текущий период заканчивается сегодня
        # Предыдущий заканчивается где текущий начинается
        assert prev[1] == cur[0]

    def test_month_returns_two_ranges(self):
        cur, prev = get_period_ranges("month")
        assert prev[1] == cur[0]


class TestAggregateByCity:
    """_aggregate_by_city группирует метрики по городам."""

    def test_single_city(self):
        ads = [
            {"city": "CityA", "spend": 1000.0, "leads": 10, "payments": 2, "revenue": 50000},
            {"city": "CityA", "spend": 500.0, "leads": 5, "payments": 1, "revenue": 30000},
        ]
        result = _aggregate_by_city(ads)
        assert result["CityA"]["spend"] == 1500.0
        assert result["CityA"]["leads"] == 15
        assert result["CityA"]["cpl"] == 100.0

    def test_multiple_cities(self):
        ads = [
            {"city": "CityA", "spend": 1000.0, "leads": 10, "payments": 2, "revenue": 50000},
            {"city": "CityB", "spend": 800.0, "leads": 8, "payments": 1, "revenue": 40000},
        ]
        result = _aggregate_by_city(ads)
        assert len(result) == 2

    def test_empty_ads(self):
        result = _aggregate_by_city([])
        assert result == {}

    def test_none_values(self):
        ads = [{"city": "CityA", "spend": None, "leads": None, "payments": None, "revenue": None}]
        result = _aggregate_by_city(ads)
        assert result["CityA"]["spend"] == 0.0
        assert result["CityA"]["leads"] == 0


class TestCalcDelta:
    """_calc_delta считает процент изменения."""

    def test_positive_delta(self):
        cur = {"spend": 1500, "leads": 15, "cpl": 100, "payments": 3, "revenue": 90000}
        prev = {"spend": 1000, "leads": 10, "cpl": 100, "payments": 2, "revenue": 60000}
        delta = _calc_delta(cur, prev)
        assert delta["spend"] == 50.0
        assert delta["leads"] == 50.0
        assert delta["payments"] == 50.0

    def test_negative_delta(self):
        cur = {"spend": 500, "leads": 5, "cpl": 100, "payments": 1, "revenue": 30000}
        prev = {"spend": 1000, "leads": 10, "cpl": 100, "payments": 2, "revenue": 60000}
        delta = _calc_delta(cur, prev)
        assert delta["spend"] == -50.0

    def test_zero_previous(self):
        cur = {"spend": 1000, "leads": 10, "cpl": 100, "payments": 2, "revenue": 50000}
        prev = {"spend": 0, "leads": 0, "cpl": 0, "payments": 0, "revenue": 0}
        delta = _calc_delta(cur, prev)
        assert delta["spend"] is None
        assert delta["leads"] is None


class TestComparePeriods:
    """compare_periods возвращает полное сравнение."""

    def test_full_comparison(self):
        cur_ads = [
            {"city": "CityA", "spend": 1500.0, "leads": 15, "payments": 3, "revenue": 90000},
        ]
        prev_ads = [
            {"city": "CityA", "spend": 1000.0, "leads": 10, "payments": 2, "revenue": 60000},
        ]
        result = compare_periods(cur_ads, prev_ads, ("2025-03-05", "2025-03-12"), ("2025-02-26", "2025-03-05"))
        assert "CityA" in result["cities"]
        assert result["cities"]["CityA"]["delta"]["spend"] == 50.0
        assert result["total"]["delta"]["spend"] == 50.0

    def test_new_city_in_current(self):
        cur_ads = [{"city": "CityE", "spend": 500.0, "leads": 5, "payments": 1, "revenue": 30000}]
        prev_ads = []
        result = compare_periods(cur_ads, prev_ads, ("2025-03-05", "2025-03-12"), ("2025-02-26", "2025-03-05"))
        assert "CityE" in result["cities"]
        assert result["cities"]["CityE"]["previous"] == _empty_metrics()

    def test_empty_both(self):
        result = compare_periods([], [], ("2025-03-05", "2025-03-12"), ("2025-02-26", "2025-03-05"))
        assert result["cities"] == {}
        assert result["total"]["current"]["leads"] == 0
