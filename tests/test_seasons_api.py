"""Тесты API эндпоинтов для сезонов и языковой аналитики."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient
from web.app import app
from agent.fb_common import FBApiError


@pytest.fixture
def client():
    """TestClient для FastAPI — сезонные роуты публичны."""
    return TestClient(app)


SAMPLE_ADS = [
    {"adset_type": "L2", "spend": 100.0, "leads": 10, "cpl": 10.0},
    {"adset_type": "L2", "spend": 50.0, "leads": 5, "cpl": 10.0},
    {"adset_type": "L1", "spend": 200.0, "leads": 20, "cpl": 10.0},
]


class TestSeasonsEndpoint:
    """Тесты GET /api/seasons."""

    def test_returns_current_season_and_all(self, client):
        """Ответ содержит текущий сезон и список всех сезонов."""
        resp = client.get("/api/seasons")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_season" in data
        assert "all_seasons" in data
        assert len(data["all_seasons"]) == 5

    def test_all_seasons_have_required_fields(self, client):
        """Каждый сезон имеет все обязательные поля."""
        resp = client.get("/api/seasons")
        required = {"id", "name", "start_month", "end_month", "description", "budget_modifier"}
        for season in resp.json()["all_seasons"]:
            assert required.issubset(season.keys()), f"Сезон {season.get('id')} без {required - season.keys()}"

    def test_current_season_can_be_null(self, client):
        """Текущий сезон может быть null (межсезонье)."""
        with patch("web.seasons_routes.get_current_season", return_value=None):
            resp = client.get("/api/seasons")
            assert resp.status_code == 200
            assert resp.json()["current_season"] is None


class TestAnalyticsByLanguageEndpoint:
    """Тесты GET /api/analytics/by-language."""

    def test_returns_language_analytics(self, client):
        """Возвращает метрики по L2 и L1."""
        with patch("web.seasons_routes.analyze_all", return_value=SAMPLE_ADS):
            resp = client.get("/api/analytics/by-language")
            assert resp.status_code == 200
            data = resp.json()
            assert "languages" in data
            assert data["languages"]["L2"]["count"] == 2
            assert data["languages"]["L2"]["spend"] == 150.0
            assert data["languages"]["L1"]["count"] == 1
            assert data["languages"]["L1"]["leads"] == 20

    def test_fb_api_error_returns_502(self, client):
        """Ошибка Facebook API → HTTP 502."""
        with patch("web.seasons_routes.analyze_all", side_effect=FBApiError("token expired")):
            resp = client.get("/api/analytics/by-language")
            assert resp.status_code == 502
            assert "FB API" in resp.json()["detail"]

    def test_includes_display_names(self, client):
        """Ответ содержит display_name каждого языка из реестра services.language.LANGUAGES."""
        from services.language import LANGUAGES

        with patch("web.seasons_routes.analyze_all", return_value=SAMPLE_ADS):
            resp = client.get("/api/analytics/by-language")
            data = resp.json()
            assert data["languages"]["L2"]["display_name"] == LANGUAGES["L2"]["display_name"]
            assert data["languages"]["L1"]["display_name"] == LANGUAGES["L1"]["display_name"]

    def test_includes_config(self, client):
        """Ответ содержит конфиг языков."""
        with patch("web.seasons_routes.analyze_all", return_value=SAMPLE_ADS):
            resp = client.get("/api/analytics/by-language")
            config = resp.json()["config"]
            assert "L2" in config
            assert "L1" in config
            assert config["L2"]["id"] == "L2"
