"""Тесты сервисного слоя мониторинга конкурентов."""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from services.competitors import (
    fetch_and_store_ads,
    get_stored_ads,
    get_activity_summary,
    fetch_all_competitors,
)

TENANT_ID = "550e8400-e29b-41d4-a716-446655440000"
PAGE_ID = "page_111"


def _make_fb_ad(ad_id: str, **kwargs) -> dict:
    """Вспомогательная функция: объявление в формате FB Ad Library."""
    return {
        "ad_id": ad_id,
        "page_name": "Test Company",
        "title": f"Заголовок {ad_id}",
        "body": f"Текст {ad_id}",
        "started": "2026-01-15",
        "stopped": None,
        "snapshot_url": f"https://fb.com/snap/{ad_id}",
        "spend_lower": 100,
        "spend_upper": 499,
        "impressions_lower": 1000,
        "impressions_upper": 5000,
        **kwargs,
    }


def _make_stored_ad(ad_id: str, first_seen_at: str, **kwargs) -> dict:
    """Вспомогательная функция: объявление как хранится в Supabase."""
    return {
        "ad_id": ad_id,
        "tenant_id": TENANT_ID,
        "page_id": PAGE_ID,
        "page_name": "Test Company",
        "title": f"Заголовок {ad_id}",
        "body": f"Текст {ad_id}",
        "first_seen_at": first_seen_at,
        "last_seen_at": first_seen_at,
        "is_active": True,
        **kwargs,
    }


class TestFetchAndStoreAds:
    """Тесты fetch_and_store_ads."""

    def test_basic_flow(self, mock_supabase):
        """Базовый сценарий: fetch из FB → upsert → mark_inactive."""
        fb_ads = [_make_fb_ad("ad1"), _make_fb_ad("ad2")]

        with patch("services.competitors.get_competitor_ads", return_value=fb_ads) as mock_fb, \
             patch("services.competitors.competitor_ads_repo.upsert_ads", return_value={"new": 2, "total": 2}) as mock_upsert, \
             patch("services.competitors.competitor_ads_repo.mark_inactive", return_value=0) as mock_mark:

            result = fetch_and_store_ads(TENANT_ID, PAGE_ID)

            mock_fb.assert_called_once_with(PAGE_ID)
            mock_upsert.assert_called_once_with(TENANT_ID, PAGE_ID, fb_ads)
            mock_mark.assert_called_once_with(TENANT_ID, PAGE_ID, ["ad1", "ad2"])
            assert result == {"new": 2, "total": 2}

    def test_empty_ads_from_fb(self, mock_supabase):
        """FB вернул пустой список."""
        with patch("services.competitors.get_competitor_ads", return_value=[]), \
             patch("services.competitors.competitor_ads_repo.upsert_ads", return_value={"new": 0, "total": 0}) as mock_upsert, \
             patch("services.competitors.competitor_ads_repo.mark_inactive", return_value=0) as mock_mark:

            result = fetch_and_store_ads(TENANT_ID, PAGE_ID)

            mock_upsert.assert_called_once_with(TENANT_ID, PAGE_ID, [])
            mock_mark.assert_called_once_with(TENANT_ID, PAGE_ID, [])
            assert result == {"new": 0, "total": 0}

    def test_api_error_propagates(self, mock_supabase):
        """Ошибка FB API пробрасывается наружу."""
        with patch("services.competitors.get_competitor_ads", side_effect=ValueError("FB_TOKEN не задан")):
            with pytest.raises(ValueError, match="FB_TOKEN не задан"):
                fetch_and_store_ads(TENANT_ID, PAGE_ID)

    def test_returns_upsert_result(self, mock_supabase):
        """Возвращает результат upsert."""
        fb_ads = [_make_fb_ad("ad1"), _make_fb_ad("ad2"), _make_fb_ad("ad3")]

        with patch("services.competitors.get_competitor_ads", return_value=fb_ads), \
             patch("services.competitors.competitor_ads_repo.upsert_ads", return_value={"new": 1, "total": 3}), \
             patch("services.competitors.competitor_ads_repo.mark_inactive", return_value=0):

            result = fetch_and_store_ads(TENANT_ID, PAGE_ID)
            assert result["new"] == 1
            assert result["total"] == 3


class TestGetStoredAds:
    """Тесты get_stored_ads — добавление поля is_new."""

    def test_recent_ad_is_new(self, mock_supabase):
        """Объявление появившееся 3 дня назад — is_new=True."""
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        stored = [_make_stored_ad("ad1", first_seen_at=three_days_ago)]

        with patch("services.competitors.competitor_ads_repo.get_ads_by_page", return_value=stored):
            result = get_stored_ads(TENANT_ID, PAGE_ID)

        assert len(result) == 1
        assert result[0]["is_new"] is True

    def test_old_ad_not_new(self, mock_supabase):
        """Объявление появившееся 10 дней назад — is_new=False."""
        ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        stored = [_make_stored_ad("ad1", first_seen_at=ten_days_ago)]

        with patch("services.competitors.competitor_ads_repo.get_ads_by_page", return_value=stored):
            result = get_stored_ads(TENANT_ID, PAGE_ID)

        assert len(result) == 1
        assert result[0]["is_new"] is False

    def test_empty_result(self, mock_supabase):
        """Нет сохранённых объявлений — пустой список."""
        with patch("services.competitors.competitor_ads_repo.get_ads_by_page", return_value=[]):
            result = get_stored_ads(TENANT_ID, PAGE_ID)

        assert result == []

    def test_missing_first_seen_at(self, mock_supabase):
        """Объявление без first_seen_at — is_new=False."""
        stored = [{"ad_id": "ad1", "tenant_id": TENANT_ID, "page_id": PAGE_ID}]

        with patch("services.competitors.competitor_ads_repo.get_ads_by_page", return_value=stored):
            result = get_stored_ads(TENANT_ID, PAGE_ID)

        assert result[0]["is_new"] is False

    def test_mixed_new_and_old(self, mock_supabase):
        """Часть объявлений новые, часть старые."""
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        twenty_days_ago = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        stored = [
            _make_stored_ad("new_ad", first_seen_at=two_days_ago),
            _make_stored_ad("old_ad", first_seen_at=twenty_days_ago),
        ]

        with patch("services.competitors.competitor_ads_repo.get_ads_by_page", return_value=stored):
            result = get_stored_ads(TENANT_ID, PAGE_ID)

        new_flags = {ad["ad_id"]: ad["is_new"] for ad in result}
        assert new_flags["new_ad"] is True
        assert new_flags["old_ad"] is False

    def test_original_ad_not_mutated(self, mock_supabase):
        """Оригинальные объявления не мутируются."""
        one_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        original = _make_stored_ad("ad1", first_seen_at=one_day_ago)
        stored = [original]

        with patch("services.competitors.competitor_ads_repo.get_ads_by_page", return_value=stored):
            result = get_stored_ads(TENANT_ID, PAGE_ID)

        assert "is_new" not in original
        assert "is_new" in result[0]


class TestGetActivitySummary:
    """Тесты get_activity_summary."""

    def test_delegates_to_repo(self, mock_supabase):
        """Делегирует вызов в competitor_ads_repo."""
        expected = [{"page_id": PAGE_ID, "name": "Test Company", "total_ads": 5}]

        with patch("services.competitors.competitor_ads_repo.get_activity_summary", return_value=expected) as mock_summary:
            result = get_activity_summary(TENANT_ID)

        mock_summary.assert_called_once_with(TENANT_ID)
        assert result == expected

    def test_empty_summary(self, mock_supabase):
        """Нет данных — пустой список."""
        with patch("services.competitors.competitor_ads_repo.get_activity_summary", return_value=[]):
            result = get_activity_summary(TENANT_ID)

        assert result == []


class TestFetchAllCompetitors:
    """Тесты fetch_all_competitors."""

    def test_multiple_competitors_success(self, mock_supabase):
        """Успешный fetch для нескольких конкурентов."""
        competitors = [
            {"page_id": "page_1", "name": "Компания А"},
            {"page_id": "page_2", "name": "Компания Б"},
        ]

        with patch("services.competitors.competitors_repo.get_competitors", return_value=competitors), \
             patch("services.competitors.fetch_and_store_ads") as mock_fetch:

            mock_fetch.side_effect = [
                {"new": 3, "total": 10},
                {"new": 1, "total": 5},
            ]

            result = fetch_all_competitors(TENANT_ID)

        assert result["fetched"] == 2
        assert result["errors"] == 0
        assert len(result["details"]) == 2

    def test_error_per_competitor_continues(self, mock_supabase):
        """Ошибка для одного конкурента не прерывает остальных."""
        competitors = [
            {"page_id": "page_1", "name": "Компания А"},
            {"page_id": "page_2", "name": "Компания Б"},
            {"page_id": "page_3", "name": "Компания В"},
        ]

        with patch("services.competitors.competitors_repo.get_competitors", return_value=competitors), \
             patch("services.competitors.fetch_and_store_ads") as mock_fetch:

            mock_fetch.side_effect = [
                {"new": 2, "total": 7},
                RuntimeError("FB API недоступен"),
                {"new": 0, "total": 3},
            ]

            result = fetch_all_competitors(TENANT_ID)

        assert result["fetched"] == 2
        assert result["errors"] == 1

        statuses = {d["page_id"]: d["status"] for d in result["details"]}
        assert statuses["page_1"] == "ok"
        assert statuses["page_2"] == "error"
        assert statuses["page_3"] == "ok"

    def test_empty_competitors_list(self, mock_supabase):
        """Нет конкурентов — все счётчики нули."""
        with patch("services.competitors.competitors_repo.get_competitors", return_value=[]):
            result = fetch_all_competitors(TENANT_ID)

        assert result == {"fetched": 0, "errors": 0, "details": []}

    def test_competitor_without_page_id(self, mock_supabase):
        """Конкурент без page_id считается ошибкой."""
        competitors = [
            {"page_id": None, "name": "Без ID"},
            {"page_id": "page_1", "name": "Нормальная компания"},
        ]

        with patch("services.competitors.competitors_repo.get_competitors", return_value=competitors), \
             patch("services.competitors.fetch_and_store_ads", return_value={"new": 1, "total": 3}):

            result = fetch_all_competitors(TENANT_ID)

        assert result["fetched"] == 1
        assert result["errors"] == 1


class TestAdLibraryCountry:
    """Страна поиска в FB Ad Library — настройка, без привязки к конкретной стране."""

    @staticmethod
    def _fake_response():
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": []}
        return resp

    def test_default_country_from_env(self, monkeypatch):
        """country не передан → берётся AD_LIBRARY_COUNTRY из окружения."""
        import integrations.fb_ad_library as lib

        monkeypatch.setattr(lib.config, "FB_TOKEN", "test-token")
        monkeypatch.delattr(lib.config, "AD_LIBRARY_COUNTRY", raising=False)
        monkeypatch.setenv("AD_LIBRARY_COUNTRY", "de")

        with patch.object(lib.session, "get", return_value=self._fake_response()) as mock_get:
            lib.get_competitor_ads("page_1")

        params = mock_get.call_args.kwargs["params"]
        assert params["ad_reached_countries"] == '["DE"]'

    def test_neutral_default_without_setting(self, monkeypatch):
        """Нет ни аргумента, ни настройки → нейтральный дефолт US."""
        import integrations.fb_ad_library as lib

        monkeypatch.setattr(lib.config, "FB_TOKEN", "test-token")
        monkeypatch.delattr(lib.config, "AD_LIBRARY_COUNTRY", raising=False)
        monkeypatch.delenv("AD_LIBRARY_COUNTRY", raising=False)

        with patch.object(lib.session, "get", return_value=self._fake_response()) as mock_get:
            lib.get_competitor_ads("page_1")

        params = mock_get.call_args.kwargs["params"]
        assert params["ad_reached_countries"] == '["US"]'

    def test_explicit_country_wins(self, monkeypatch):
        """Явный аргумент country важнее настройки."""
        import integrations.fb_ad_library as lib

        monkeypatch.setattr(lib.config, "FB_TOKEN", "test-token")
        monkeypatch.setenv("AD_LIBRARY_COUNTRY", "DE")

        with patch.object(lib.session, "get", return_value=self._fake_response()) as mock_get:
            lib.get_competitor_ads("page_1", country="FR")

        params = mock_get.call_args.kwargs["params"]
        assert params["ad_reached_countries"] == '["FR"]'
