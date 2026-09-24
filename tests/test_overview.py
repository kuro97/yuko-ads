"""
Тесты обзорного дашборда: агрегация по городам, дельты, алерты, API.
"""

from unittest.mock import patch

from services.overview import (
    get_overview,
    _aggregate_by_city,
    _generate_alerts,
)


# --- Моковые данные ---

def _make_ad(city="CityA", adset_type="L2", spend=100, leads=5, cpl=20,
             ctr=1.5, romi=None, qual_pct=None, revenue=None, **extra):
    """Создать моковое объявление."""
    ad = {
        "id": f"ad_{city}_{adset_type}",
        "name": f"Ad {city} {adset_type}",
        "status": "ACTIVE",
        "city": city,
        "adset_type": adset_type,
        "spend": spend,
        "leads": leads,
        "cpl": cpl,
        "ctr": ctr,
        "cpm": 5.0,
        "impressions": 10000,
        "clicks": 150,
        "qual_pct": qual_pct,
        "romi": romi,
        "payments": None,
        "revenue": revenue,
    }
    ad.update(extra)
    return ad


# --- Тесты агрегации ---

class TestAggregateByCity:
    """Агрегация метрик по городам."""

    def test_basic_aggregation(self):
        """Базовая агрегация: spend и leads суммируются."""
        current = [
            _make_ad("CityA", "L2", spend=100, leads=5, cpl=20, ctr=1.5),
            _make_ad("CityA", "L2", spend=200, leads=10, cpl=20, ctr=2.0),
        ]
        previous = []
        result = _aggregate_by_city(current, previous)

        # Найти CityA L2
        citya_l2 = next(c for c in result if c["name"] == "CityA L2")
        assert citya_l2["spend"] == 300
        assert citya_l2["leads"] == 15
        # CPL = 300/15 = 20
        assert citya_l2["cpl"] == 20.0
        # CTR = среднее (1.5 + 2.0) / 2 = 1.75
        assert citya_l2["ctr"] == 1.75

    def test_delta_calculation(self):
        """Расчёт дельт: текущий vs предыдущий период."""
        current = [_make_ad("CityA", "L2", spend=200, leads=10, cpl=20)]
        previous = [_make_ad("CityA", "L2", spend=100, leads=5, cpl=20)]
        result = _aggregate_by_city(current, previous)

        citya_l2 = next(c for c in result if c["name"] == "CityA L2")
        # leads: 10 vs 5 = +100%
        assert citya_l2["delta_leads"] == 100.0
        # CPL: 20 vs 20 = 0%
        assert citya_l2["delta_cpl"] == 0

    def test_delta_zero_previous(self):
        """Дельта = 0 когда нет данных за предыдущий период."""
        current = [_make_ad("CityB", "L1", spend=50, leads=3, cpl=16.7)]
        previous = []
        result = _aggregate_by_city(current, previous)

        cityb_l1 = next(c for c in result if c["name"] == "CityB L1")
        assert cityb_l1["delta_cpl"] == 0
        assert cityb_l1["delta_leads"] == 0

    def test_amo_metrics_averaged(self):
        """AMO метрики (ROMI, qual_pct) считаются агрегатно, а не как среднее.

        Гнилой тест (A4): раньше romi/qual_pct брались напрямую из полей ad и
        усреднялись. Сейчас (осознанное изменение, см. комментарий в коде
        _aggregate_by_city — "AMO метрики — агрегаты, не средние!") romi
        считается из сумм revenue/spend, а qual_pct — из сумм qual_leads/leads.
        Поля "romi"/"qual_pct" в самом ad больше не читаются функцией — их
        нужно задавать через revenue и qual_leads, чтобы агрегат сработал."""
        current = [
            _make_ad("CityA", "L2", spend=100, leads=10, revenue=20000, qual_leads=3),
            _make_ad("CityA", "L2", spend=100, leads=10, revenue=10000, qual_leads=1),
        ]
        result = _aggregate_by_city(current, [])

        citya_l2 = next(c for c in result if c["name"] == "CityA L2")
        # revenue=30000 LCY, spend=200 USD → romi = 30000 / (200*курс) * 100
        from services.exchange_rate import get_usd_to_lcy
        expected_romi = round(30000 / (200 * get_usd_to_lcy()) * 100, 1)
        assert citya_l2["romi"] == expected_romi
        # qual_leads_total=4, leads=20 → qual_pct = 4/20*100 = 20.0
        assert citya_l2["qual_pct"] == 20.0

    def test_empty_city_in_config(self):
        """Города из конфига без объявлений всё равно появляются."""
        result = _aggregate_by_city([], [])
        city_names = [c["name"] for c in result]
        assert "CityA L2" in city_names
        assert "CityA L1" in city_names
        assert "CityB L2" in city_names

    def test_revenue_summed(self):
        """Revenue суммируется (не усредняется)."""
        current = [
            _make_ad("CityA", "L2", revenue=100000),
            _make_ad("CityA", "L2", revenue=200000),
        ]
        result = _aggregate_by_city(current, [])

        citya_l2 = next(c for c in result if c["name"] == "CityA L2")
        assert citya_l2["revenue"] == 300000


# --- Тесты алертов ---

class TestGenerateAlerts:
    """Генерация алертов по правилам."""

    def test_cpl_above_threshold(self):
        """CPL выше порога → warning."""
        cities = [{"name": "CityA L2", "cpl": 45, "leads": 5, "spend": 225,
                   "romi": None, "qual_pct": None}]
        alerts = _generate_alerts(cities)

        assert len(alerts) == 1
        assert alerts[0]["level"] == "warning"
        assert "CPL" in alerts[0]["message"]
        assert alerts[0]["city"] == "CityA L2"

    def test_zero_leads_critical(self):
        """0 лидов при расходе → critical."""
        cities = [{"name": "CityB L1", "cpl": 0, "leads": 0, "spend": 50,
                   "romi": None, "qual_pct": None}]
        alerts = _generate_alerts(cities)

        assert len(alerts) == 1
        assert alerts[0]["level"] == "critical"
        assert "0 лидов" in alerts[0]["message"]

    def test_zero_leads_no_spend_no_alert(self):
        """0 лидов БЕЗ расхода — не алерт (нет объявлений)."""
        cities = [{"name": "CityE L2", "cpl": 0, "leads": 0, "spend": 0,
                   "romi": None, "qual_pct": None}]
        alerts = _generate_alerts(cities)
        assert len(alerts) == 0

    def test_romi_below_threshold(self):
        """ROMI ниже 100% → warning."""
        cities = [{"name": "CityC L2", "cpl": 20, "leads": 10, "spend": 200,
                   "romi": 85, "qual_pct": None}]
        alerts = _generate_alerts(cities)

        romi_alert = [a for a in alerts if "ROMI" in a["message"]]
        assert len(romi_alert) == 1
        assert romi_alert[0]["level"] == "warning"

    def test_no_alerts_when_ok(self):
        """Нет алертов когда всё в норме."""
        cities = [{"name": "CityA L2", "cpl": 15, "leads": 20, "spend": 300,
                   "romi": 250, "qual_pct": 30}]
        alerts = _generate_alerts(cities)
        assert len(alerts) == 0

    def test_alerts_sorted_by_priority(self):
        """Алерты сортируются: critical → warning."""
        cities = [
            {"name": "CityA L2", "cpl": 45, "leads": 5, "spend": 225,
             "romi": None, "qual_pct": None},  # warning (CPL)
            {"name": "CityB L1", "cpl": 0, "leads": 0, "spend": 50,
             "romi": None, "qual_pct": None},  # critical (0 leads)
        ]
        alerts = _generate_alerts(cities)

        assert alerts[0]["level"] == "critical"
        assert alerts[1]["level"] == "warning"

    def test_multiple_alerts_same_city(self):
        """Несколько алертов для одного города."""
        cities = [{"name": "CityC L1", "cpl": 40, "leads": 3, "spend": 120,
                   "romi": 50, "qual_pct": None}]
        alerts = _generate_alerts(cities)

        # CPL > 30 → warning + ROMI < 100 → warning
        assert len(alerts) == 2
        messages = [a["message"] for a in alerts]
        assert any("CPL" in m for m in messages)
        assert any("ROMI" in m for m in messages)


# --- Тесты полного пайплайна ---

class TestGetOverview:
    """Полный пайплайн get_overview()."""

    @patch("services.overview.sync_amo_data")
    @patch("services.overview.get_ads_with_metrics")
    def test_basic_flow(self, mock_get_ads, mock_amo):
        """Базовый сценарий: получение данных + алерты."""
        mock_get_ads.return_value = [
            _make_ad("CityA", "L2", spend=100, leads=5, cpl=20),
        ]
        mock_amo.return_value = {}

        result = get_overview(days=14)

        assert "cities" in result
        assert "alerts" in result
        assert "zscore_alerts" in result
        assert "period" in result
        assert result["period"]["from"] is not None
        assert result["period"]["to"] is not None

        # get_ads_with_metrics вызывается 2 раза (текущий + предыдущий период)
        assert mock_get_ads.call_count == 2

    @patch("services.overview.sync_amo_data")
    @patch("services.overview.get_ads_with_metrics")
    def test_amo_failure_doesnt_block(self, mock_get_ads, mock_amo):
        """Ошибка AMO не блокирует обзор."""
        mock_get_ads.return_value = [
            _make_ad("CityA", "L2", spend=50, leads=2),
        ]
        mock_amo.side_effect = Exception("AMO offline")

        result = get_overview(days=7)
        # Не падает, возвращает данные
        assert "cities" in result

    @patch("services.overview.sync_amo_data")
    @patch("services.overview.get_ads_with_metrics")
    def test_empty_ads(self, mock_get_ads, mock_amo):
        """Пустые данные: нет объявлений."""
        mock_get_ads.return_value = []
        mock_amo.return_value = {}

        result = get_overview(days=30)

        # Города из конфига всё равно есть (с нулями)
        assert len(result["cities"]) > 0
        # Нет алертов (нет расхода)
        assert len(result["alerts"]) == 0


# --- Тесты API эндпоинта ---

class TestOverviewAPI:
    """Тесты FastAPI эндпоинта /api/overview."""

    @patch("services.overview.sync_amo_data")
    @patch("services.overview.get_ads_with_metrics")
    def test_api_overview_success(self, mock_get_ads, mock_amo):
        """GET /api/overview — успех."""
        from fastapi.testclient import TestClient
        from web.app import app

        mock_get_ads.return_value = [
            _make_ad("CityA", "L2", spend=100, leads=5),
        ]
        mock_amo.return_value = {}

        client = TestClient(app)
        resp = client.get("/api/overview?days=14")

        assert resp.status_code == 200
        data = resp.json()
        assert "cities" in data
        assert "alerts" in data
        assert "zscore_alerts" in data
        assert "period" in data

    @patch("services.overview.sync_amo_data")
    @patch("services.overview.get_ads_with_metrics")
    def test_api_overview_default_days(self, mock_get_ads, mock_amo):
        """GET /api/overview без параметров — 14 дней по умолчанию."""
        from fastapi.testclient import TestClient
        from web.app import app

        mock_get_ads.return_value = []
        mock_amo.return_value = {}

        client = TestClient(app)
        resp = client.get("/api/overview")
        assert resp.status_code == 200

    def test_api_overview_invalid_days(self):
        """GET /api/overview?days=5 — ошибка валидации."""
        from fastapi.testclient import TestClient
        from web.app import app

        client = TestClient(app)
        resp = client.get("/api/overview?days=5")
        assert resp.status_code == 400

    @patch("services.overview.get_ads_with_metrics")
    def test_api_overview_fb_error(self, mock_get_ads):
        """GET /api/overview — ошибка FB API → 502."""
        from fastapi.testclient import TestClient
        from web.app import app
        from web.overview_routes import _cache
        from agent.fb_common import FBApiError

        _cache.clear()  # Сбросить кэш от предыдущих тестов
        mock_get_ads.side_effect = FBApiError("FB down", 500)

        client = TestClient(app)
        resp = client.get("/api/overview?days=14")
        assert resp.status_code == 502
