"""Тесты для S16: Воронка оплат."""

import pytest
from services.funnel import aggregate_funnel, _calc_conversions


class TestCalcConversions:
    """_calc_conversions добавляет конверсии."""

    def test_normal_conversions(self):
        d = {"leads": 100, "quals": 30, "payments": 10, "spend": 5000.0, "revenue": 50000}
        _calc_conversions(d)
        assert d["lead_to_qual"] == 30.0
        assert d["qual_to_payment"] == 33.3
        assert d["cpl"] == 50.0

    def test_zero_leads(self):
        d = {"leads": 0, "quals": 0, "payments": 0, "spend": 0.0, "revenue": 0}
        _calc_conversions(d)
        assert d["lead_to_qual"] == 0
        assert d["qual_to_payment"] == 0
        assert d["cpl"] == 0

    def test_zero_quals(self):
        d = {"leads": 10, "quals": 0, "payments": 0, "spend": 1000.0, "revenue": 0}
        _calc_conversions(d)
        assert d["lead_to_qual"] == 0
        assert d["qual_to_payment"] == 0
        assert d["cpl"] == 100.0


class TestAggregateFunnel:
    """aggregate_funnel агрегирует по городам."""

    def test_single_city(self):
        ads = [
            {"city": "CityA", "leads": 10, "qual_leads": 3, "payments": 1, "revenue": 50000, "spend": 1000.0},
            {"city": "CityA", "leads": 5, "qual_leads": 2, "payments": 1, "revenue": 30000, "spend": 500.0},
        ]
        result = aggregate_funnel(ads)
        assert "CityA" in result["cities"]
        cta = result["cities"]["CityA"]
        assert cta["leads"] == 15
        assert cta["quals"] == 5
        assert cta["payments"] == 2
        assert cta["revenue"] == 80000
        assert cta["spend"] == 1500.0

    def test_multiple_cities(self):
        ads = [
            {"city": "CityA", "leads": 10, "qual_leads": 3, "payments": 1, "revenue": 50000, "spend": 1000.0},
            {"city": "CityB", "leads": 8, "qual_leads": 2, "payments": 1, "revenue": 40000, "spend": 800.0},
        ]
        result = aggregate_funnel(ads)
        assert len(result["cities"]) == 2
        assert result["total"]["leads"] == 18
        assert result["total"]["quals"] == 5
        assert result["total"]["payments"] == 2

    def test_empty_ads(self):
        result = aggregate_funnel([])
        assert result["cities"] == {}
        assert result["total"]["leads"] == 0

    def test_missing_fields(self):
        """Обрабатывает объявления без некоторых полей."""
        ads = [{"city": "CityA", "spend": 500.0}]
        result = aggregate_funnel(ads)
        assert result["cities"]["CityA"]["leads"] == 0

    def test_none_values(self):
        """None значения обрабатываются как 0."""
        ads = [{"city": "CityB", "leads": None, "qual_leads": None, "payments": None, "revenue": None, "spend": None}]
        result = aggregate_funnel(ads)
        assert result["cities"]["CityB"]["leads"] == 0
        assert result["cities"]["CityB"]["spend"] == 0.0

    def test_total_has_conversions(self):
        ads = [
            {"city": "CityA", "leads": 10, "qual_leads": 5, "payments": 2, "revenue": 100000, "spend": 2000.0},
        ]
        result = aggregate_funnel(ads)
        assert "lead_to_qual" in result["total"]
        assert result["total"]["lead_to_qual"] == 50.0
