from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import config
import pytest
from services.approval_checker_models import (
    DailyInventorySnapshot,
    EvidenceRequest,
    PaginationCoverage,
    SourceSystem,
    TimeWindow,
)
from services.approval_source_daily_metrics import _validate_run, load_daily_metrics_evidence
from services.metrics_snapshot import capture_daily_snapshot_complete


@pytest.fixture
def tmp_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    from services.creative_intelligence import init_kb

    init_kb(db_path=db_path)
    return db_path


def test_daily_adapter_reads_complete_manifest_without_writer_calls(tmp_db, tmp_path, monkeypatch):
    manifest = tmp_path / "metrics_snapshot_state.json"
    monkeypatch.setattr(config, "REPORT_CHECKER_DECISIONS_DB_PATH", tmp_db)
    monkeypatch.setattr(config, "REPORT_CHECKER_METRICS_MANIFEST_PATH", manifest)
    now = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
    window = TimeWindow(
        datetime(2026, 7, 21, tzinfo=timezone.utc),
        datetime(2026, 7, 22, tzinfo=timezone.utc),
        "UTC",
        "YESTERDAY",
    )
    request = EvidenceRequest("r", "REPORT", None, now, (), (), (SourceSystem.AD_DAILY_METRICS,), (window,), ("account",), (), (), (), (), False, False, 300)
    coverage = PaginationCoverage("ACCOUNT_ADS", 1, ("ad-1",), True, None, "1" * 64)

    def inventory(_account_id, target_window, _now):
        return DailyInventorySnapshot(
            "account", 1, "USD", "UTC", target_window, now, 900, coverage,
            ("ad-1",), ("ad-1",), ("campaign-1",), (), "2" * 64, "3" * 64,
            "4" * 64, (), (), True, True,
        )

    row = {
        "ad_id": "ad-1", "date": "2026-07-21", "spend": 10.0, "impressions": 100,
        "clicks": 4, "ctr": 4.0, "leads": 2, "lead_semantics_version": 2,
        "lead_parse_status": "ok", "cpl": 5.0, "hook_rate": None, "hold_rate": None,
        "video_views_3s": 0, "day_since_launch": 1,
    }
    insights = PaginationCoverage("ACCOUNT_INSIGHTS", 1, ("ad-1",), True, None, "4" * 64)
    class FrozenDateTime(datetime):
        """Фиксирует wall clock, чтобы persisted_at не зависел от часа запуска теста."""

        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    with patch("services.metrics_snapshot.datetime", FrozenDateTime), \
         patch("services.creative_intelligence.DB_PATH", tmp_db), \
         patch("services.metrics_snapshot._fetch_account_metadata", return_value=(1, "USD", "UTC", now)), \
         patch("services.metrics_snapshot._fetch_live_daily_inventory", side_effect=inventory), \
         patch("services.metrics_snapshot._fetch_account_daily_rows", return_value=({"ad-1": row}, insights)):
        assert capture_daily_snapshot_complete("2026-07-21", now=now).complete is True
    with patch("services.metrics_snapshot.capture_daily_snapshot_complete") as writer:
        result = load_daily_metrics_evidence(request, now)
    assert result.complete is True
    assert len(result.records) == 3
    writer.assert_not_called()


def test_daily_adapter_never_calls_capture_writer(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORT_CHECKER_DECISIONS_DB_PATH", tmp_db)
    monkeypatch.setattr(config, "REPORT_CHECKER_METRICS_MANIFEST_PATH", tmp_path / "missing.json")
    now = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
    window = TimeWindow(now - timedelta(days=1), now, "UTC", "YESTERDAY")
    request = EvidenceRequest("r", "REPORT", None, now, (), (), (SourceSystem.AD_DAILY_METRICS,), (window,), ("account",), (), (), (), (), False, False, 300)
    with patch("services.metrics_snapshot.capture_daily_snapshot_complete", wraps=capture_daily_snapshot_complete) as writer:
        result = load_daily_metrics_evidence(request, now)
    assert result.complete is False
    writer.assert_not_called()


@pytest.mark.parametrize(
    "campaign_ids",
    [[], ["campaign-1", "campaign-extra"]],
    ids=("missing-eligible-campaign", "extra-campaign"),
)
def test_daily_adapter_rejects_non_exact_persisted_campaign_coverage(campaign_ids):
    now = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
    pagination = {
        "endpoint_kind": "CAMPAIGNS",
        "page_count": 1,
        "item_ids": campaign_ids,
        "pagination_complete": True,
        "failed_page_index": None,
        "response_state_sha256": "5" * 64,
    }
    run = {
        "persisted_at": now.isoformat(),
        "db_rows_state_sha256": "6" * 64,
        "result": {
            "inventory": {
                "account_id": "account",
                "fetched_at": now.isoformat(),
                "fresh": True,
                "complete": True,
                "eligible_campaign_ids": ["campaign-1"],
                "campaign_by_ad_sha256": "4" * 64,
            },
            "fetch_path": "CAMPAIGN_CHUNKS",
            "campaign_pagination": pagination,
            "campaign_chunks": [],
            "requested_ad_ids": ["ad-1"],
            "fetched_ad_ids": ["ad-1"],
            "upserted_ad_ids": ["ad-1"],
        },
    }

    with pytest.raises(
        RuntimeError,
        match="DAILY_CAMPAIGN_COVERAGE_INVALID",
    ):
        _validate_run(run)
