"""
Тесты services/topic_selector.py — приоритизация тем (empty > thin > teardown),
фильтр городов вне fact_sheet.cities, блок testimonial-тем при disabled режиме,
max_topics, пустой случай, проброс FactSheetError.

Мокаем источники данных на месте их определения (ленивые импорты внутри
topic_selector делают `from module import name` при каждом вызове функции,
поэтому патчим services.coverage_monitor.analyze_coverage,
services.brief_generator._get_fresh_ads, services.creative_briefs.analyze_winners
/select_winner_teardowns и services.fact_sheet.load_fact_sheet).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта (паттерн из test_coverage_monitor.py)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

import services.topic_selector as ts
from services.fact_sheet import FactSheetError


# ---------------------------------------------------------------------------
# Вспомогательные данные
# ---------------------------------------------------------------------------

def _make_fact_sheet(testimonial_enabled: bool = False) -> dict:
    """Минимальный fact_sheet для тестов приоритизации/фильтрации."""
    return {
        "products": {"proda": {}, "prodb": {}},
        "offer": ["Бесплатная консультация"],
        "promo_claims": ["Рассрочка банка-партнёра на 12 месяцев", "4 услуги в одном месте"],
        "social_proof_claims": [],
        "testimonials": [{"raw": "тест"}] if testimonial_enabled else [],
        "testimonial_mode_enabled": testimonial_enabled,
        "incompatibility_rules": [],
        "deadlines": [],
        "allowed_numbers": [],
        "cities": ["CityA", "CityB", "CityC", "CityD", "CityE"],
    }


_EMPTY_COVERAGE = {"empty": [], "thin": [], "ok_count": 10, "by_group": {}, "generated_at": "2026-07-02"}


def _coverage_with(empty=None, thin=None) -> dict:
    return {
        "empty": empty or [],
        "thin": thin or [],
        "ok_count": 5,
        "by_group": {},
        "generated_at": "2026-07-02",
    }


def _make_winner(name: str = "CityA | Спикер / Рассрочка | CR001", payments: int = 3, city: str = "CityA") -> dict:
    """Минимальный «победитель» в формате analyze_winners()["winners"]."""
    return {
        "name": name,
        "city": city,
        "angle": "Рассрочка банка-партнёра на 12 месяцев",
        "cpl": 3.5,
        "leads": 20,
        "payments": payments,
    }


# ---------------------------------------------------------------------------
# Приоритизация: empty > thin > teardown
# ---------------------------------------------------------------------------

def test_select_topics_empty_coverage_prioritized_over_teardown():
    """1 empty-пробел + 1 teardown-тема → empty идёт первой в результате."""
    fact_sheet = _make_fact_sheet()
    coverage = _coverage_with(empty=[{"city": "CityD", "adset_type": "L2", "count": 0}])

    teardown_topic = {
        "reference": {"name": "CityA | Спикер | CR001", "cpl": 3.5, "leads": 20, "payments": 3},
        "ad_format": "video_speaker",
        "variable_to_vary": "хук",
        "angle": "Рассрочка банка-партнёра на 12 месяцев",
        "city": "CityA",
        "cpl": 3.5,
        "leads": 20,
        "payments": 3,
    }

    with patch("services.fact_sheet.load_fact_sheet", return_value=fact_sheet), \
         patch("services.coverage_monitor.analyze_coverage", return_value=coverage), \
         patch("services.brief_generator._get_fresh_ads", return_value=[{"ad": "x"}]), \
         patch("services.creative_briefs.analyze_winners", return_value={"winners": [_make_winner()]}), \
         patch("services.creative_briefs.select_winner_teardowns", return_value=[teardown_topic]):
        topics = ts.select_topics(max_topics=5)

    assert len(topics) == 2
    assert topics[0]["source"] == "coverage", "empty coverage-тема должна идти первой"
    assert topics[0]["city"] == "CityD"
    assert topics[1]["source"] == "teardown", "teardown-тема должна идти после coverage"


def test_select_topics_thin_prioritized_over_teardown():
    """thin coverage-пробел приоритетнее teardown, но ниже empty."""
    fact_sheet = _make_fact_sheet()
    coverage = _coverage_with(
        empty=[{"city": "CityD", "adset_type": "L2", "count": 0}],
        thin=[{"city": "CityB", "adset_type": "L1", "count": 1}],
    )
    teardown_topic = {
        "reference": {"name": "CityA | Спикер | CR001", "cpl": 3.5, "leads": 20, "payments": 3},
        "ad_format": "video_speaker",
        "variable_to_vary": "хук",
        "angle": "Рассрочка банка-партнёра на 12 месяцев",
        "city": "CityA",
        "cpl": 3.5,
        "leads": 20,
        "payments": 3,
    }

    with patch("services.fact_sheet.load_fact_sheet", return_value=fact_sheet), \
         patch("services.coverage_monitor.analyze_coverage", return_value=coverage), \
         patch("services.brief_generator._get_fresh_ads", return_value=[{"ad": "x"}]), \
         patch("services.creative_briefs.analyze_winners", return_value={"winners": [_make_winner()]}), \
         patch("services.creative_briefs.select_winner_teardowns", return_value=[teardown_topic]):
        topics = ts.select_topics(max_topics=5)

    sources_order = [(t["source"], t.get("rationale", "")) for t in topics]
    assert topics[0]["city"] == "CityD", "empty первый"
    assert topics[1]["city"] == "CityB", "thin второй"
    assert topics[2]["source"] == "teardown", "teardown последний"


# ---------------------------------------------------------------------------
# Фильтр городов вне fact_sheet.cities
# ---------------------------------------------------------------------------

def test_select_topics_filters_unknown_coverage_city():
    """Coverage-город не из fact_sheet.cities («Мухосранск») отфильтрован."""
    fact_sheet = _make_fact_sheet()
    coverage = _coverage_with(empty=[
        {"city": "CityD", "adset_type": "L2", "count": 0},
        {"city": "Мухосранск", "adset_type": "L2", "count": 0},
    ])

    with patch("services.fact_sheet.load_fact_sheet", return_value=fact_sheet), \
         patch("services.coverage_monitor.analyze_coverage", return_value=coverage), \
         patch("services.brief_generator._get_fresh_ads", return_value=[]), \
         patch("services.creative_briefs.analyze_winners", return_value={"winners": []}), \
         patch("services.creative_briefs.select_winner_teardowns", return_value=[]):
        topics = ts.select_topics(max_topics=5)

    cities = [t["city"] for t in topics]
    assert "Мухосранск" not in cities
    assert "CityD" in cities


def test_select_topics_filters_unknown_teardown_city():
    """Teardown-город не из fact_sheet.cities отфильтрован."""
    fact_sheet = _make_fact_sheet()
    teardown_topic = {
        "reference": {"name": "Мухосранск | Спикер | CR001", "cpl": 3.5, "leads": 20, "payments": 3},
        "ad_format": "video_speaker",
        "variable_to_vary": "хук",
        "angle": "Рассрочка банка-партнёра на 12 месяцев",
        "city": "Мухосранск",
        "cpl": 3.5,
        "leads": 20,
        "payments": 3,
    }

    with patch("services.fact_sheet.load_fact_sheet", return_value=fact_sheet), \
         patch("services.coverage_monitor.analyze_coverage", return_value=_EMPTY_COVERAGE), \
         patch("services.brief_generator._get_fresh_ads", return_value=[{"ad": "x"}]), \
         patch("services.creative_briefs.analyze_winners", return_value={"winners": [_make_winner(city="Мухосранск")]}), \
         patch("services.creative_briefs.select_winner_teardowns", return_value=[teardown_topic]):
        topics = ts.select_topics(max_topics=5)

    assert topics == [], "Teardown-тема с неизвестным городом должна быть отфильтрована"


# ---------------------------------------------------------------------------
# Блок testimonial-тем при disabled режиме
# ---------------------------------------------------------------------------

def test_select_topics_blocks_testimonial_voice_when_disabled():
    """testimonial_mode_enabled=False → ни одна тема с voice=testimonial не возвращается."""
    fact_sheet = _make_fact_sheet(testimonial_enabled=False)
    coverage = _coverage_with(empty=[{"city": "CityD", "adset_type": "L2", "count": 0}])

    with patch("services.fact_sheet.load_fact_sheet", return_value=fact_sheet), \
         patch("services.coverage_monitor.analyze_coverage", return_value=coverage), \
         patch("services.brief_generator._get_fresh_ads", return_value=[]), \
         patch("services.creative_briefs.analyze_winners", return_value={"winners": []}), \
         patch("services.creative_briefs.select_winner_teardowns", return_value=[]):
        topics = ts.select_topics(max_topics=5)

    assert all(t.get("voice") != "testimonial" for t in topics), \
        "Ни одна тема не должна иметь voice=testimonial при disabled режиме"
    # Проверяем что тема реально существует и имеет voice=brand (не просто список пуст)
    assert len(topics) >= 1
    assert topics[0]["voice"] == "brand"


# ---------------------------------------------------------------------------
# max_topics — обрезает до нужного количества, топ по приоритету
# ---------------------------------------------------------------------------

def test_select_topics_respects_max_topics_limit():
    """Пробелов больше max_topics → берём топ по приоритету (empty first)."""
    fact_sheet = _make_fact_sheet()
    coverage = _coverage_with(empty=[
        {"city": "CityA", "adset_type": "L2", "count": 0},
        {"city": "CityB", "adset_type": "L2", "count": 0},
        {"city": "CityC", "adset_type": "L2", "count": 0},
        {"city": "CityD", "adset_type": "L2", "count": 0},
    ])

    with patch("services.fact_sheet.load_fact_sheet", return_value=fact_sheet), \
         patch("services.coverage_monitor.analyze_coverage", return_value=coverage), \
         patch("services.brief_generator._get_fresh_ads", return_value=[]), \
         patch("services.creative_briefs.analyze_winners", return_value={"winners": []}), \
         patch("services.creative_briefs.select_winner_teardowns", return_value=[]):
        topics = ts.select_topics(max_topics=2)

    assert len(topics) == 2, "Результат должен быть обрезан до max_topics"
    assert all(t["source"] == "coverage" for t in topics), "Все взятые темы должны быть empty coverage"


# ---------------------------------------------------------------------------
# Пустой случай: покрытие в норме И нет победителей с оплатами
# ---------------------------------------------------------------------------

def test_select_topics_returns_empty_when_no_gaps_and_no_winners():
    """Покрытие в норме (нет empty/thin) и нет свежих ads → []."""
    fact_sheet = _make_fact_sheet()

    with patch("services.fact_sheet.load_fact_sheet", return_value=fact_sheet), \
         patch("services.coverage_monitor.analyze_coverage", return_value=_EMPTY_COVERAGE), \
         patch("services.brief_generator._get_fresh_ads", return_value=[]), \
         patch("services.creative_briefs.analyze_winners", return_value={"winners": []}), \
         patch("services.creative_briefs.select_winner_teardowns", return_value=[]):
        topics = ts.select_topics(max_topics=3)

    assert topics == []


# ---------------------------------------------------------------------------
# Проброс FactSheetError (fail-closed)
# ---------------------------------------------------------------------------

def test_select_topics_propagates_fact_sheet_error():
    """Fact Sheet недоступен/повреждён → FactSheetError пробрасывается наружу (fail-closed)."""
    with patch("services.fact_sheet.load_fact_sheet", side_effect=FactSheetError("нет .md")):
        with pytest.raises(FactSheetError):
            ts.select_topics(max_topics=3)


# ---------------------------------------------------------------------------
# Мягкое влияние вердиктов гипотез (T7, ARCH-phase4-hypothesist.md)
# ---------------------------------------------------------------------------

def _base_setup(fact_sheet, coverage):
    """Общий набор патчей источников данных (fact_sheet/coverage/teardown-заглушки)."""
    return [
        patch("services.fact_sheet.load_fact_sheet", return_value=fact_sheet),
        patch("services.coverage_monitor.analyze_coverage", return_value=coverage),
        patch("services.brief_generator._get_fresh_ads", return_value=[]),
        patch("services.creative_briefs.analyze_winners", return_value={"winners": []}),
        patch("services.creative_briefs.select_winner_teardowns", return_value=[]),
    ]


def test_select_topics_influence_applied_confirmed_combo_rises():
    """enabled=True + подтверждённое комбо thin-темы → она поднимается выше empty-темы."""
    fact_sheet = _make_fact_sheet()
    coverage = _coverage_with(
        empty=[{"city": "CityD", "adset_type": "L2", "count": 0}],
        thin=[{"city": "CityB", "adset_type": "L1", "count": 1}],
    )

    # CityB (thin) получает подтверждённое комбо → должна обогнать CityD (empty)
    from services.hypothesis_influence import combo_key

    def fake_weights(now=None):
        # round-robin claim: CityD (empty, idx0) получает claim[0], CityB (thin, idx1) — claim[1]
        cityb_topic_angle = "4 услуги в одном месте"
        key = combo_key(cityb_topic_angle, "CityB", "video_speaker")
        return {key: {"confirmed": 3, "refuted": 0, "score": 3.0, "dead": False}}

    patches = _base_setup(fact_sheet, coverage)
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patch("services.topic_selector._hypothesist_enabled", return_value=True), \
         patch("services.hypothesis_influence.load_verdict_weights", side_effect=fake_weights):
        topics = ts.select_topics(max_topics=5)

    assert len(topics) == 2, "Влияние не должно укорачивать список"
    assert topics[0]["city"] == "CityB", "Подтверждённое комбо (CityB/thin) должно подняться выше empty"
    assert topics[0]["_influence_score"] == 3.0
    assert topics[1]["city"] == "CityD"


def test_select_topics_influence_disabled_keeps_original_order():
    """hypothesist.enabled=False → влияние не применяется, порядок как без него."""
    fact_sheet = _make_fact_sheet()
    coverage = _coverage_with(
        empty=[{"city": "CityD", "adset_type": "L2", "count": 0}],
        thin=[{"city": "CityB", "adset_type": "L1", "count": 1}],
    )

    patches = _base_setup(fact_sheet, coverage)
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patch("services.topic_selector._hypothesist_enabled", return_value=False), \
         patch("services.hypothesis_influence.load_verdict_weights") as mock_weights:
        topics = ts.select_topics(max_topics=5)

    mock_weights.assert_not_called()
    assert [t["city"] for t in topics] == ["CityD", "CityB"], "Порядок должен остаться исходным (empty > thin)"
    assert all("_influence_score" not in t for t in topics), "Поле влияния не должно добавляться при disabled"


def test_select_topics_influence_failure_returns_topics_without_influence():
    """Сбой load_verdict_weights (напр. БД недоступна) НЕ роняет select_topics — темы важнее весов."""
    fact_sheet = _make_fact_sheet()
    coverage = _coverage_with(empty=[{"city": "CityD", "adset_type": "L2", "count": 0}])

    patches = _base_setup(fact_sheet, coverage)
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         patch("services.topic_selector._hypothesist_enabled", return_value=True), \
         patch("services.hypothesis_influence.load_verdict_weights", side_effect=RuntimeError("KB не инициализирована")):
        topics = ts.select_topics(max_topics=5)

    assert len(topics) == 1, "Темы должны вернуться даже при сбое влияния"
    assert topics[0]["city"] == "CityD"


def test_hypothesist_enabled_reads_settings_file_directly(tmp_path):
    """_hypothesist_enabled читает settings.json напрямую (без agent.scheduler.load_settings) —
    иначе load_settings попадёт под мок test_manual_endpoint_not_gated_by_flag и сломает
    защищённый контракт «ручной путь brief_generator не читает settings» (см. комментарий
    у ts._SETTINGS_FILE)."""
    settings_file = tmp_path / "settings.json"

    with patch.object(ts, "_SETTINGS_FILE", settings_file):
        # Файла нет → дефолт True (безопасный контур включён по умолчанию)
        assert ts._hypothesist_enabled() is True

        settings_file.write_text('{"autopilot": {"hypothesist": {"enabled": false}}}', encoding="utf-8")
        assert ts._hypothesist_enabled() is False

        settings_file.write_text('{"autopilot": {"hypothesist": {"enabled": true}}}', encoding="utf-8")
        assert ts._hypothesist_enabled() is True

        settings_file.write_text('{}', encoding="utf-8")
        assert ts._hypothesist_enabled() is True, "Нет блока hypothesist в settings → дефолт True"
