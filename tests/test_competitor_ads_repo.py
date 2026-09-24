"""Тесты репозитория снимков объявлений конкурентов (файловый)."""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from agent.repositories.competitor_ads_repo import (
    upsert_ads,
    get_ads_by_page,
    get_activity_summary,
    mark_inactive,
)

TENANT_ID = "default"
PAGE_ID_A = "page_111"
PAGE_ID_B = "page_222"


def _make_ad(ad_id: str, page_name: str = "Test Company", **kwargs) -> dict:
    return {
        "ad_id": ad_id,
        "page_name": page_name,
        "title": f"Заголовок {ad_id}",
        "body": f"Текст объявления {ad_id}",
        "started": "2026-01-15",
        "spend_lower": 100,
        "spend_upper": 499,
        "impressions_lower": 1000,
        "impressions_upper": 5000,
        "snapshot_url": f"https://fb.com/snap/{ad_id}",
        **kwargs,
    }


@pytest.fixture(autouse=True)
def clean_ads_file(tmp_path):
    """Изолируем файл через tmp_path."""
    test_file = tmp_path / "competitor_ads.json"
    with patch("agent.repositories.competitor_ads_repo.ADS_FILE", test_file):
        yield test_file


class TestUpsertAds:
    def test_upsert_ads_new(self):
        ads = [_make_ad("ad1"), _make_ad("ad2"), _make_ad("ad3")]
        result = upsert_ads(TENANT_ID, PAGE_ID_A, ads)
        assert result["new"] == 3
        assert result["total"] == 3

    def test_upsert_ads_empty_list(self):
        result = upsert_ads(TENANT_ID, PAGE_ID_A, [])
        assert result == {"new": 0, "total": 0}

    def test_upsert_ads_duplicate(self):
        ads = [_make_ad("ad1"), _make_ad("ad2")]
        result1 = upsert_ads(TENANT_ID, PAGE_ID_A, ads)
        assert result1["new"] == 2
        result2 = upsert_ads(TENANT_ID, PAGE_ID_A, ads)
        assert result2["new"] == 0

    def test_upsert_ads_adds_tenant_and_page(self):
        upsert_ads(TENANT_ID, PAGE_ID_A, [_make_ad("ad10")])
        stored = get_ads_by_page(TENANT_ID, PAGE_ID_A)
        assert len(stored) >= 1
        assert stored[0]["tenant_id"] == TENANT_ID
        assert stored[0]["page_id"] == PAGE_ID_A

    def test_upsert_ads_sets_last_seen_at(self):
        upsert_ads(TENANT_ID, PAGE_ID_A, [_make_ad("ad20")])
        stored = get_ads_by_page(TENANT_ID, PAGE_ID_A)
        assert stored[0]["last_seen_at"] is not None

    def test_upsert_ads_ad_id_fallback(self):
        ads = [{"id": "ad_via_id", "page_name": "Test", "body": "Text"}]
        result = upsert_ads(TENANT_ID, PAGE_ID_A, ads)
        assert result["new"] == 1


class TestGetAdsByPage:
    def test_get_ads_by_page(self):
        upsert_ads(TENANT_ID, PAGE_ID_A, [_make_ad("ad1"), _make_ad("ad2")])
        result = get_ads_by_page(TENANT_ID, PAGE_ID_A)
        assert len(result) == 2

    def test_get_ads_by_page_empty(self):
        result = get_ads_by_page(TENANT_ID, "nonexistent")
        assert result == []

    def test_get_ads_by_page_isolates_by_page(self):
        upsert_ads(TENANT_ID, PAGE_ID_A, [_make_ad("ad_a")])
        upsert_ads(TENANT_ID, PAGE_ID_B, [_make_ad("ad_b")])
        result = get_ads_by_page(TENANT_ID, PAGE_ID_A)
        ad_ids = {r["ad_id"] for r in result}
        assert "ad_a" in ad_ids
        assert "ad_b" not in ad_ids


class TestGetActivitySummary:
    def test_empty(self):
        assert get_activity_summary(TENANT_ID) == []

    def test_two_pages(self):
        upsert_ads(TENANT_ID, PAGE_ID_A, [
            _make_ad("a1", page_name="Company A", spend_lower=100, spend_upper=500),
            _make_ad("a2", page_name="Company A", spend_lower=200, spend_upper=1000),
        ])
        upsert_ads(TENANT_ID, PAGE_ID_B, [
            _make_ad("b1", page_name="Company B", spend_lower=300, spend_upper=999),
        ])
        result = get_activity_summary(TENANT_ID)
        assert len(result) == 2
        page_a = next(r for r in result if r["page_id"] == PAGE_ID_A)
        assert page_a["total_ads"] == 2
        assert page_a["name"] == "Company A"
        assert page_a["spend_lower_sum"] == 300
        assert page_a["spend_upper_sum"] == 1500

    def test_new_ads_counted(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=2)).isoformat()
        old = (now - timedelta(days=10)).isoformat()
        # Напрямую записываем данные с first_seen_at
        import json
        from agent.repositories.competitor_ads_repo import ADS_FILE, _write
        _write([
            {"tenant_id": TENANT_ID, "page_id": PAGE_ID_A, "ad_id": "new",
             "page_name": "T", "first_seen_at": recent, "is_active": True,
             "spend_lower": 0, "spend_upper": 0},
            {"tenant_id": TENANT_ID, "page_id": PAGE_ID_A, "ad_id": "old",
             "page_name": "T", "first_seen_at": old, "is_active": True,
             "spend_lower": 0, "spend_upper": 0},
        ])
        result = get_activity_summary(TENANT_ID)
        assert result[0]["new_ads_7d"] == 1
        assert result[0]["total_ads"] == 2


class TestMarkInactive:
    def test_marks_missing_ads(self):
        upsert_ads(TENANT_ID, PAGE_ID_A, [
            _make_ad("ad1"), _make_ad("ad2"), _make_ad("ad3"),
        ])
        count = mark_inactive(TENANT_ID, PAGE_ID_A, active_ad_ids=["ad1"])
        assert count == 2

    def test_all_active(self):
        upsert_ads(TENANT_ID, PAGE_ID_A, [_make_ad("ad1"), _make_ad("ad2")])
        count = mark_inactive(TENANT_ID, PAGE_ID_A, active_ad_ids=["ad1", "ad2"])
        assert count == 0

    def test_no_ads(self):
        count = mark_inactive(TENANT_ID, "none", active_ad_ids=[])
        assert count == 0
