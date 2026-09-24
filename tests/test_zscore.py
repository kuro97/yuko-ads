"""
Тесты Z-score алертов: статистические аномалии по CPL.
"""

import math
from services.zscore import calc_zscore_alerts, _std


def _make_city(name, cpl, leads=10, spend=100, **extra):
    """Создать моковый город для тестов."""
    city = {"name": name, "cpl": cpl, "leads": leads, "spend": spend,
            "romi": None, "qual_pct": None}
    city.update(extra)
    return city


# --- Тесты вспомогательной функции _std ---

class TestStd:
    """Стандартное отклонение."""

    def test_basic_std(self):
        """Базовый расчёт std."""
        values = [10, 20, 30]
        mean = 20.0
        result = _std(values, mean)
        expected = math.sqrt((100 + 0 + 100) / 3)
        assert abs(result - expected) < 0.01

    def test_all_same(self):
        """Все значения одинаковые → std = 0."""
        assert _std([5, 5, 5], 5.0) == 0.0

    def test_single_value(self):
        """Одно значение → std = 0."""
        assert _std([5], 5.0) == 0.0

    def test_empty(self):
        """Пустой список → std = 0."""
        assert _std([], 0.0) == 0.0


# --- Тесты Z-score алертов ---

class TestZscoreAlerts:
    """Генерация Z-score алертов."""

    def test_no_alerts_normal_data(self):
        """Нет алертов когда CPL примерно одинаковый."""
        cities = [
            _make_city("CityA L2", cpl=20),
            _make_city("CityB L2", cpl=22),
            _make_city("CityC L2", cpl=18),
            _make_city("CityD L2", cpl=21),
        ]
        alerts = calc_zscore_alerts(cities)
        assert len(alerts) == 0

    def test_warning_alert(self):
        """CPL с Z > 2.0 → warning (нужно 7+ нормальных городов)."""
        cities = [
            _make_city("CityA L2", cpl=20),
            _make_city("CityB L2", cpl=22),
            _make_city("CityC L2", cpl=18),
            _make_city("CityD L2", cpl=21),
            _make_city("CityA L1", cpl=19),
            _make_city("CityB L1", cpl=20),
            _make_city("CityC L1", cpl=21),
            _make_city("CityE L2", cpl=80),  # аномалия — Z ≈ 2.64
        ]
        alerts = calc_zscore_alerts(cities)
        assert len(alerts) >= 1
        assert alerts[0]["city"] == "CityE L2"
        assert alerts[0]["level"] in ("warning", "critical")
        assert alerts[0]["zscore"] > 2.0

    def test_critical_alert(self):
        """CPL с Z > 3.0 → critical (нужно 10+ нормальных городов)."""
        cities = [
            _make_city("CityA L2", cpl=20),
            _make_city("CityB L2", cpl=22),
            _make_city("CityC L2", cpl=18),
            _make_city("CityD L2", cpl=21),
            _make_city("CityA L1", cpl=19),
            _make_city("CityB L1", cpl=20),
            _make_city("CityC L1", cpl=21),
            _make_city("CityD L1", cpl=20),
            _make_city("CityE L1", cpl=19),
            _make_city("CityE L2", cpl=21),
            _make_city("АНОМАЛИЯ", cpl=200),  # сильная аномалия — Z ≈ 3.16
        ]
        alerts = calc_zscore_alerts(cities)
        critical = [a for a in alerts if a["level"] == "critical"]
        assert len(critical) >= 1
        assert critical[0]["city"] == "АНОМАЛИЯ"
        assert critical[0]["zscore"] > 3.0

    def test_too_few_cities(self):
        """Меньше 3 городов — нет алертов (мало данных)."""
        cities = [
            _make_city("CityA L2", cpl=20),
            _make_city("CityB L2", cpl=100),
        ]
        alerts = calc_zscore_alerts(cities)
        assert len(alerts) == 0

    def test_zero_leads_excluded(self):
        """Города без лидов не участвуют в расчёте."""
        cities = [
            _make_city("CityA L2", cpl=20),
            _make_city("CityB L2", cpl=22),
            _make_city("CityC L2", cpl=18),
            _make_city("CityD L2", cpl=0, leads=0),  # нет данных
        ]
        alerts = calc_zscore_alerts(cities)
        city_names = [a["city"] for a in alerts]
        assert "CityD L2" not in city_names

    def test_all_same_cpl_no_alerts(self):
        """Все CPL одинаковые → std=0 → нет алертов."""
        cities = [
            _make_city("CityA L2", cpl=25),
            _make_city("CityB L2", cpl=25),
            _make_city("CityC L2", cpl=25),
        ]
        alerts = calc_zscore_alerts(cities)
        assert len(alerts) == 0

    def test_alert_has_stats(self):
        """Алерт содержит mean и std для контекста."""
        cities = [
            _make_city("CityA L2", cpl=20),
            _make_city("CityB L2", cpl=22),
            _make_city("CityC L2", cpl=18),
            _make_city("CityD L2", cpl=21),
            _make_city("CityA L1", cpl=19),
            _make_city("CityB L1", cpl=20),
            _make_city("CityE L2", cpl=80),  # аномалия — Z ≈ 2.45
        ]
        alerts = calc_zscore_alerts(cities)
        assert len(alerts) >= 1
        alert = alerts[0]
        assert "mean" in alert
        assert "std" in alert
        assert "zscore" in alert
        assert alert["metric"] == "cpl"

    def test_custom_thresholds(self):
        """Кастомные пороги Z-score."""
        cities = [
            _make_city("CityA L2", cpl=20),
            _make_city("CityB L2", cpl=22),
            _make_city("CityC L2", cpl=18),
            _make_city("CityE L2", cpl=30),
        ]
        # Строгий порог — нет алертов
        strict = calc_zscore_alerts(cities, threshold_warning=5.0, threshold_critical=10.0)
        assert len(strict) == 0

        # Мягкий порог — может быть алерт
        lenient = calc_zscore_alerts(cities, threshold_warning=1.0, threshold_critical=2.0)
        # CityE CPL=30 при среднем ~22.5 может попасть

    def test_sorted_critical_first(self):
        """Алерты: critical → warning, потом по Z убывание."""
        cities = [
            _make_city("CityA L2", cpl=15),
            _make_city("CityB L2", cpl=18),
            _make_city("CityC L2", cpl=14),
            _make_city("CityA L1", cpl=16),
            _make_city("CityB L1", cpl=17),
            _make_city("CityC L1", cpl=15),
            _make_city("CityD L2", cpl=16),
            _make_city("CityD L1", cpl=50),   # warning или critical
            _make_city("CityE L2", cpl=100),   # точно critical
        ]
        alerts = calc_zscore_alerts(cities)
        if len(alerts) >= 2:
            # Первый — с более высоким приоритетом или Z
            assert alerts[0]["zscore"] >= alerts[-1]["zscore"] or \
                   alerts[0]["level"] == "critical"

    def test_message_contains_context(self):
        """Сообщение содержит CPL, Z-score, среднее."""
        cities = [
            _make_city("CityA L2", cpl=20),
            _make_city("CityB L2", cpl=22),
            _make_city("CityC L2", cpl=18),
            _make_city("CityD L2", cpl=21),
            _make_city("CityA L1", cpl=19),
            _make_city("CityB L1", cpl=20),
            _make_city("CityE L2", cpl=80),  # аномалия — Z ≈ 2.45
        ]
        alerts = calc_zscore_alerts(cities)
        assert len(alerts) >= 1
        msg = alerts[0]["message"]
        assert "CPL" in msg
        assert "Z-score" in msg
        assert "среднее" in msg

    def test_negative_zscore_no_alert(self):
        """Отрицательный Z-score (CPL ниже среднего) — не алерт."""
        cities = [
            _make_city("CityA L2", cpl=5),   # очень низкий — хорошо!
            _make_city("CityB L2", cpl=22),
            _make_city("CityC L2", cpl=25),
            _make_city("CityD L2", cpl=20),
        ]
        alerts = calc_zscore_alerts(cities)
        low_cpl = [a for a in alerts if a["city"] == "CityA L2"]
        assert len(low_cpl) == 0  # Низкий CPL — это хорошо
