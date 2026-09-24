"""Тесты для S22: Разбивка L1/L2."""

import pytest
from services.language_split import filter_by_lang, split_by_lang, aggregate_by_lang


# --- Тестовые данные ---
SAMPLE_ADS = [
    {"city": "CityA", "adset_type": "L2", "spend": 1000.0, "leads": 10, "qual_leads": 3, "payments": 1, "revenue": 50000},
    {"city": "CityA", "adset_type": "L1", "spend": 800.0, "leads": 8, "qual_leads": 2, "payments": 1, "revenue": 40000},
    {"city": "CityB", "adset_type": "L2", "spend": 600.0, "leads": 6, "qual_leads": 2, "payments": 0, "revenue": 0},
    {"city": "CityB", "adset_type": "L1", "spend": 500.0, "leads": 5, "qual_leads": 1, "payments": 1, "revenue": 30000},
    {"city": "CityC", "adset_type": "L2", "spend": 400.0, "leads": 4, "qual_leads": 1, "payments": 0, "revenue": 0},
]


class TestFilterByLang:
    """filter_by_lang фильтрует по L2/L1."""

    def test_filter_l2(self):
        result = filter_by_lang(SAMPLE_ADS, "L2")
        assert len(result) == 3
        assert all(ad["adset_type"] == "L2" for ad in result)

    def test_filter_l1(self):
        result = filter_by_lang(SAMPLE_ADS, "L1")
        assert len(result) == 2
        assert all(ad["adset_type"] == "L1" for ad in result)

    def test_filter_all(self):
        result = filter_by_lang(SAMPLE_ADS, "all")
        assert len(result) == 5

    def test_filter_none(self):
        result = filter_by_lang(SAMPLE_ADS, None)
        assert len(result) == 5

    def test_filter_case_insensitive(self):
        result = filter_by_lang(SAMPLE_ADS, "l2")
        assert len(result) == 3

    def test_filter_empty(self):
        result = filter_by_lang([], "L2")
        assert result == []

    def test_filter_no_match(self):
        ads = [{"adset_type": "L2", "spend": 100}]
        result = filter_by_lang(ads, "L1")
        assert result == []


class TestSplitByLang:
    """split_by_lang разделяет на L2/L1/other."""

    def test_split_normal(self):
        result = split_by_lang(SAMPLE_ADS)
        assert len(result["L2"]) == 3
        assert len(result["L1"]) == 2
        assert len(result["other"]) == 0

    def test_split_with_unknown_type(self):
        ads = SAMPLE_ADS + [{"adset_type": "XX", "spend": 100}]
        result = split_by_lang(ads)
        assert len(result["other"]) == 1

    def test_split_empty(self):
        result = split_by_lang([])
        assert result == {"L2": [], "L1": [], "other": []}

    def test_split_no_adset_type(self):
        ads = [{"city": "CityA", "spend": 100}]
        result = split_by_lang(ads)
        assert len(result["other"]) == 1


class TestAggregateByLang:
    """aggregate_by_lang агрегирует метрики по L2/L1."""

    def test_aggregate_normal(self):
        result = aggregate_by_lang(SAMPLE_ADS)
        assert result["L2"]["spend"] == 2000.0
        assert result["L2"]["leads"] == 20
        assert result["L1"]["spend"] == 1300.0
        assert result["L1"]["leads"] == 13

    def test_aggregate_total(self):
        result = aggregate_by_lang(SAMPLE_ADS)
        assert result["total"]["spend"] == 3300.0
        assert result["total"]["leads"] == 33
        assert result["total"]["count"] == 5

    def test_aggregate_cpl(self):
        result = aggregate_by_lang(SAMPLE_ADS)
        assert result["L2"]["cpl"] == 100.0  # 2000/20
        assert result["L1"]["cpl"] == 100.0  # 1300/13

    def test_aggregate_empty(self):
        result = aggregate_by_lang([])
        assert result["L2"]["leads"] == 0
        assert result["L1"]["leads"] == 0
        assert result["total"]["leads"] == 0

    def test_aggregate_quals_payments(self):
        result = aggregate_by_lang(SAMPLE_ADS)
        assert result["L2"]["quals"] == 6
        assert result["L2"]["payments"] == 1
        assert result["L1"]["quals"] == 3
        assert result["L1"]["payments"] == 2

    def test_aggregate_none_values(self):
        ads = [{"adset_type": "L2", "spend": None, "leads": None, "qual_leads": None, "payments": None, "revenue": None}]
        result = aggregate_by_lang(ads)
        assert result["L2"]["spend"] == 0.0
        assert result["L2"]["leads"] == 0
