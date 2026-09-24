"""
Юнит-тесты для services/metrics_snapshot.py.
Используют временную SQLite-БД (tmp_path) и моки FB API.
"""

import hashlib
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта web-модулей (известная проблема WIP)
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())

from services.metrics_snapshot import (
    _fetch_account_daily_rows,
    _fetch_campaign_daily_rows,
    _compute_day_since_launch,
    _fetch_daily_rows,
    _fetch_created_times,
    _fetch_live_daily_inventory,
    _load_created_times_cache,
    _save_created_times_cache,
    _parse_daily_row,
    _upsert_daily,
    capture_daily_snapshot,
    capture_daily_snapshot_complete,
    DailyMetricsCompletenessError,
)
from services.approval_checker_models import (  # noqa: E402
    DailyInventorySnapshot,
    PaginationCoverage,
    TimeWindow,
    canonical_json,
)
from agent.fb_common import FBApiError


def _graph_response(payload: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


# ---------------------------------------------------------------------------
# Фикстура: временная БД с таблицей ad_daily_metrics
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """Создаёт временную SQLite БД с таблицей ad_daily_metrics."""
    db_path = str(tmp_path / "test.db")

    from services.creative_intelligence import init_kb
    init_kb(db_path=db_path)

    # Возвращаем путь к БД — тесты подключаются напрямую
    return db_path


@pytest.fixture
def tmp_conn(tmp_db):
    """Соединение к временной БД."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Тест: _compute_day_since_launch
# ---------------------------------------------------------------------------

def test_day_since_launch():
    """created_time 2024-01-01, on_date 2024-01-08 → 7."""
    result = _compute_day_since_launch("2024-01-01T00:00:00+0000", "2024-01-08")
    assert result == 7


def test_day_since_launch_negative_clamped():
    """created позже on_date → 0 (не отрицательный)."""
    result = _compute_day_since_launch("2024-01-08T00:00:00+0000", "2024-01-01")
    assert result == 0


def test_day_since_launch_bad_input():
    """created_time='' → 0 (без исключений)."""
    result = _compute_day_since_launch("", "2024-01-08")
    assert result == 0


def test_day_since_launch_bad_date():
    """Некорректный формат created_time → 0."""
    result = _compute_day_since_launch("not-a-date", "2024-01-08")
    assert result == 0


# ---------------------------------------------------------------------------
# Тест: _upsert_daily — идемпотентность
# ---------------------------------------------------------------------------

def test_upsert_idempotent(tmp_conn):
    """UPSERT одной и той же (ad_id, date) дважды → 1 строка, значения второго вызова."""
    row_first = {
        "ad_id": "ad_001",
        "date": "2024-06-01",
        "spend": 10.0,
        "impressions": 1000,
        "clicks": 50,
        "ctr": 5.0,
        "leads": 3,
        "lead_semantics_version": 2,
        "lead_parse_status": "ok",
        "cpl": 3.33,
        "hook_rate": None,
        "hold_rate": None,
        "video_views_3s": 0,
        "day_since_launch": 7,
    }
    row_second = {**row_first, "spend": 20.0, "leads": 5, "cpl": 4.0}

    _upsert_daily(tmp_conn, [row_first])
    _upsert_daily(tmp_conn, [row_second])

    rows = tmp_conn.execute(
        "SELECT * FROM ad_daily_metrics WHERE ad_id=? AND date=?",
        ("ad_001", "2024-06-01"),
    ).fetchall()

    assert len(rows) == 1
    r = rows[0]
    assert float(r["spend"]) == 20.0
    assert int(r["leads"]) == 5
    assert float(r["cpl"]) == 4.0
    assert int(r["lead_semantics_version"]) == 2
    assert r["lead_parse_status"] == "ok"


def test_upsert_returns_count(tmp_conn):
    """_upsert_daily возвращает число строк."""
    rows = [
        {"ad_id": f"ad_{i}", "date": "2024-06-01", "spend": 1.0,
         "impressions": 100, "clicks": 5, "ctr": 5.0, "leads": 1, "cpl": 1.0,
         "lead_semantics_version": 2, "lead_parse_status": "ok",
         "hook_rate": None, "hold_rate": None, "video_views_3s": 0, "day_since_launch": 0}
        for i in range(3)
    ]
    count = _upsert_daily(tmp_conn, rows)
    assert count == 3


# ---------------------------------------------------------------------------
# Тест: capture_daily_snapshot при FBApiError → rate_limited
# ---------------------------------------------------------------------------

def test_capture_rate_limited(tmp_db):
    """_fetch_daily_rows бросает FBApiError → {"rate_limited": True}, не бросает."""
    with patch("services.creative_intelligence.DB_PATH", tmp_db), \
         patch("services.metrics_snapshot._fetch_daily_rows",
               side_effect=FBApiError("rate limit", 429)):
        result = capture_daily_snapshot(target_date="2024-06-01")

    assert result["rate_limited"] is True
    assert result.get("upserted", 0) == 0
    # НЕ должно было бросить исключение — дошли до сюда


# ---------------------------------------------------------------------------
# Тест: target_date=None → вчера по локальному времени
# ---------------------------------------------------------------------------

def test_capture_default_yesterday(tmp_db):
    """target_date=None → date == вчера по локальному времени (UTC+5 по умолчанию)."""
    _TZ_LOCAL = timezone(timedelta(hours=5))
    expected_yesterday = (datetime.now(_TZ_LOCAL) - timedelta(days=1)).date().isoformat()

    captured_date = None

    def _mock_fetch(date_from, date_to):
        nonlocal captured_date
        captured_date = date_from
        return {}  # пустой ответ

    with patch("services.creative_intelligence.DB_PATH", tmp_db), \
         patch("services.metrics_snapshot._fetch_daily_rows", side_effect=_mock_fetch):
        result = capture_daily_snapshot(target_date=None)

    assert captured_date == expected_yesterday
    assert result["date"] == expected_yesterday


# ---------------------------------------------------------------------------
# Тест: capture_daily_snapshot записывает строки в БД
# ---------------------------------------------------------------------------

def test_capture_upserts_rows(tmp_db):
    """Мок FB вернул 2 объявления → upserted==2, строки в БД."""
    fake_rows = {
        "ad_001": {
            "ad_id": "ad_001", "date": "2024-06-01",
            "spend": 15.0, "impressions": 500, "clicks": 25, "ctr": 5.0,
            "leads": 3, "cpl": 5.0, "hook_rate": None, "hold_rate": None,
            "video_views_3s": 0, "day_since_launch": 10,
            "lead_semantics_version": 2, "lead_parse_status": "ok",
        },
        "ad_002": {
            "ad_id": "ad_002", "date": "2024-06-01",
            "spend": 20.0, "impressions": 800, "clicks": 40, "ctr": 5.0,
            "leads": 5, "cpl": 4.0, "hook_rate": 30.0, "hold_rate": 10.0,
            "video_views_3s": 150, "day_since_launch": 5,
            "lead_semantics_version": 2, "lead_parse_status": "ok",
        },
    }

    with patch("services.creative_intelligence.DB_PATH", tmp_db), \
         patch("services.metrics_snapshot._fetch_daily_rows", return_value=fake_rows):
        result = capture_daily_snapshot(target_date="2024-06-01")

    assert result["upserted"] == 2
    assert result["fetched"] == 2
    assert result["rate_limited"] is False

    # Проверяем что строки реально в БД
    conn = sqlite3.connect(tmp_db)
    try:
        rows = conn.execute(
            "SELECT ad_id FROM ad_daily_metrics WHERE date=?", ("2024-06-01",)
        ).fetchall()
        ad_ids_in_db = {r[0] for r in rows}
    finally:
        conn.close()

    assert "ad_001" in ad_ids_in_db
    assert "ad_002" in ad_ids_in_db


# ---------------------------------------------------------------------------
# Тест: FB вернул 0 объявлений — не ошибка
# ---------------------------------------------------------------------------

def test_capture_empty_no_error(tmp_db):
    """Мок FB вернул 0 → {"fetched":0,"upserted":0,"rate_limited":False}."""
    with patch("services.creative_intelligence.DB_PATH", tmp_db), \
         patch("services.metrics_snapshot._fetch_daily_rows", return_value={}):
        result = capture_daily_snapshot(target_date="2024-06-01")

    assert result["fetched"] == 0
    assert result["upserted"] == 0
    assert result["rate_limited"] is False
    assert result["skipped_reason"] is None


def test_fetch_daily_rows_preserves_two_ads_across_three_days():
    """Составной ключ сохраняет все 2 ads × 3 dates без перезаписи по ad_id."""
    rows = [
        {
            "ad_id": ad_id,
            "date_start": row_date,
            "spend": "1",
            "actions": [{"action_type": "lead", "value": "1"}],
        }
        for ad_id in ("ad-1", "ad-2")
        for row_date in ("2026-07-01", "2026-07-02", "2026-07-03")
    ]
    response = _graph_response({"data": rows, "paging": {}})

    with patch("services.metrics_snapshot._throttled_get", return_value=response), \
         patch("services.metrics_snapshot.get_fb_account_id", return_value="account"), \
         patch("services.metrics_snapshot.get_fb_token", return_value="token"):
        result = _fetch_daily_rows("2026-07-01", "2026-07-03", created_times={})

    assert set(result) == {
        (ad_id, row_date)
        for ad_id in ("ad-1", "ad-2")
        for row_date in ("2026-07-01", "2026-07-02", "2026-07-03")
    }
    assert len(result) == 6
    assert {row["date"] for row in result.values()} == {
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
    }


def test_fetch_daily_rows_rejects_partial_pagination():
    first_page = _graph_response({
        "data": [{"ad_id": "ad-1", "date_start": "2026-07-01", "actions": []}],
        "paging": {"next": "https://next"},
    })
    failed_page = _graph_response({}, status_code=500)

    with patch(
        "services.metrics_snapshot._throttled_get",
        side_effect=[first_page, failed_page],
    ), patch("services.metrics_snapshot.get_fb_account_id", return_value="account"), \
         patch("services.metrics_snapshot.get_fb_token", return_value="token"):
        with pytest.raises(FBApiError, match="page=2"):
            _fetch_daily_rows("2026-07-01", "2026-07-03", created_times={})


def test_fetch_daily_rows_rejects_partial_campaign_chunk():
    reduce_response = _graph_response(
        {"error": {"code": 1, "message": "Please reduce the amount of data"}},
        status_code=500,
    )
    campaigns_response = _graph_response({
        "data": [{"id": f"campaign-{index}"} for index in range(51)],
        "paging": {},
    })
    first_chunk = _graph_response({
        "data": [{"ad_id": "ad-1", "date_start": "2026-07-01", "actions": []}],
        "paging": {},
    })
    failed_chunk = _graph_response({}, status_code=500)

    with patch(
        "services.metrics_snapshot._throttled_get",
        side_effect=[reduce_response, campaigns_response, first_chunk, failed_chunk],
    ), patch("services.metrics_snapshot.get_fb_account_id", return_value="account"), \
         patch("services.metrics_snapshot.get_fb_token", return_value="token"):
        with pytest.raises(FBApiError, match="campaign chunk 2"):
            _fetch_daily_rows("2026-07-01", "2026-07-03", created_times={})


def test_fetch_daily_rows_rejects_parser_failure():
    response = _graph_response({
        "data": [{
            "ad_id": "ad-1",
            "date_start": "2026-07-01",
            "actions": [{"action_type": "lead", "value": "0.5"}],
        }],
        "paging": {},
    })

    with patch("services.metrics_snapshot._throttled_get", return_value=response), \
         patch("services.metrics_snapshot.get_fb_account_id", return_value="account"), \
         patch("services.metrics_snapshot.get_fb_token", return_value="token"):
        with pytest.raises(FBApiError, match="parser error"):
            _fetch_daily_rows("2026-07-01", "2026-07-01", created_times={})


# ---------------------------------------------------------------------------
# Тест: _parse_daily_row — валидные видео-поля
# ---------------------------------------------------------------------------

def test_parse_daily_row_video_metrics():
    """video_thruplay_watched_actions + video_view из actions →
    hook_rate = 3s-просмотры / impressions * 100
    hold_rate = thruplay / 3s-просмотры * 100
    (формула совпадает с creative_backfill.py)
    """
    row = {
        "ad_id": "ad_video",
        "date_start": "2024-06-01",
        "spend": "10.0",
        "impressions": "1000",
        "clicks": "50",
        "ctr": "5.0",
        "actions": [
            {"action_type": "lead", "value": "3"},
            {"action_type": "video_view", "value": "200"},  # 3-секундные просмотры
        ],
        "video_thruplay_watched_actions": [
            {"action_type": "video_view", "value": "100"},  # thruplay
        ],
    }
    result = _parse_daily_row(row, created_times={}, on_date="2024-06-01")

    # Лиды
    assert result["leads"] == 3
    # video_views_3s берётся из actions[action_type=video_view]
    assert result["video_views_3s"] == 200
    # hook_rate = 200 / 1000 * 100 = 20.0
    assert abs(result["hook_rate"] - 20.0) < 0.01
    # hold_rate = 100 / 200 * 100 = 50.0
    assert abs(result["hold_rate"] - 50.0) < 0.01


def test_parse_daily_row_no_video():
    """Объявление без видео-метрик → hook_rate=None, hold_rate=None."""
    row = {
        "ad_id": "ad_image",
        "date_start": "2024-06-01",
        "spend": "5.0",
        "impressions": "500",
        "clicks": "20",
        "ctr": "4.0",
        "actions": [
            {"action_type": "lead", "value": "2"},
        ],
        "video_thruplay_watched_actions": [],
    }
    result = _parse_daily_row(row, created_times={}, on_date="2024-06-01")

    assert result["hook_rate"] is None
    assert result["hold_rate"] is None
    assert result["video_views_3s"] == 0


def test_parse_daily_row_zero_impressions_video():
    """Видео-метрики есть, impressions=0 → hook_rate=0.0, hold_rate по thruplay/3s."""
    row = {
        "ad_id": "ad_zero",
        "date_start": "2024-06-01",
        "spend": "0.0",
        "impressions": "0",
        "clicks": "0",
        "ctr": "0.0",
        "actions": [
            {"action_type": "video_view", "value": "0"},
        ],
        "video_thruplay_watched_actions": [
            {"action_type": "video_view", "value": "50"},
        ],
    }
    result = _parse_daily_row(row, created_times={}, on_date="2024-06-01")

    # thruplay > 0 → видео-ветка активна
    assert result["hook_rate"] == 0.0
    # video_views_3s == 0 → hold_rate = 0.0 (деление на ноль защищено)
    assert result["hold_rate"] == 0.0


# ---------------------------------------------------------------------------
# Тесты: кеш created_times
# ---------------------------------------------------------------------------

def test_fetch_created_times_uses_cache(tmp_path):
    """При наличии свежего кеш-файла FB не дёргается."""
    cache_path = tmp_path / "ad_created_times.json"
    # Пишем «свежий» кеш
    fake_data = {"ad_111": "2025-01-01T00:00:00+0000", "ad_222": "2025-03-15T00:00:00+0000"}
    _save_created_times_cache(fake_data, cache_path=cache_path)

    fb_call_count = []

    def _mock_throttled(*args, **kwargs):
        fb_call_count.append(1)
        raise AssertionError("FB не должен вызываться при свежем кеше")

    with patch("services.metrics_snapshot._throttled_get", side_effect=_mock_throttled):
        result = _fetch_created_times(cache_path=cache_path)

    assert result == fake_data
    assert len(fb_call_count) == 0


def test_fetch_created_times_refreshes_stale_cache(tmp_path):
    """Устаревший кеш (старше TTL) → перетягиваем из FB и пишем новый кеш."""
    import os, time as t
    cache_path = tmp_path / "ad_created_times.json"
    old_data = {"ad_old": "2024-01-01T00:00:00+0000"}
    _save_created_times_cache(old_data, cache_path=cache_path)

    # Искусственно делаем файл старым (mtime = 8 дней назад)
    old_mtime = t.time() - 8 * 86400
    os.utime(str(cache_path), (old_mtime, old_mtime))

    fresh_data = {"ad_new": "2025-06-01T00:00:00+0000"}

    # Мок ответа FB
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [{"id": "ad_new", "created_time": "2025-06-01T00:00:00+0000"}],
        "paging": {},
    }

    with patch("services.metrics_snapshot._throttled_get", return_value=mock_resp), \
         patch("services.metrics_snapshot.get_fb_account_id", return_value="12345"), \
         patch("services.metrics_snapshot.get_fb_token", return_value="fake_token"):
        result = _fetch_created_times(cache_path=cache_path)

    assert "ad_new" in result
    # Кеш обновлён
    loaded = _load_created_times_cache(cache_path=cache_path)
    assert loaded is not None and "ad_new" in loaded


def test_fetch_created_times_no_cache_calls_fb(tmp_path):
    """Кеша нет → тянем из FB, сохраняем в кеш."""
    cache_path = tmp_path / "ad_created_times.json"
    assert not cache_path.exists()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [{"id": "ad_abc", "created_time": "2025-05-10T10:00:00+0000"}],
        "paging": {},
    }

    with patch("services.metrics_snapshot._throttled_get", return_value=mock_resp), \
         patch("services.metrics_snapshot.get_fb_account_id", return_value="12345"), \
         patch("services.metrics_snapshot.get_fb_token", return_value="fake_token"):
        result = _fetch_created_times(cache_path=cache_path)

    assert "ad_abc" in result
    # Кеш теперь создан
    assert cache_path.exists()


def test_save_load_cache_roundtrip(tmp_path):
    """Атомарная запись → чтение возвращает те же данные."""
    cache_path = tmp_path / "ad_created_times.json"
    data = {"ad_1": "2025-01-01T00:00:00+0000", "ad_2": "2025-06-15T12:00:00+0000"}
    _save_created_times_cache(data, cache_path=cache_path)
    loaded = _load_created_times_cache(cache_path=cache_path)
    assert loaded == data


def test_load_cache_missing_file(tmp_path):
    """Отсутствующий файл кеша → None (без ошибки)."""
    cache_path = tmp_path / "nonexistent.json"
    result = _load_created_times_cache(cache_path=cache_path)
    assert result is None


# ---------------------------------------------------------------------------
# Тест: миграция 009 идемпотентна
# ---------------------------------------------------------------------------

def test_migration_idempotent(tmp_path):
    """Применить миграцию 009 дважды — без ошибок, таблица есть."""
    db_path = str(tmp_path / "test_idempotent.db")

    from services.creative_intelligence import init_kb
    # Первый раз
    init_kb(db_path=db_path)
    # Второй раз — не должно падать
    init_kb(db_path=db_path)

    # Таблица существует
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ad_daily_metrics'"
        ).fetchall()
        assert len(rows) == 1, "Таблица ad_daily_metrics должна существовать после двойного init_kb"
    finally:
        conn.close()


def test_complete_snapshot_isolated_from_legacy_and_materializes_verified_zero(tmp_db, tmp_path):
    now = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
    manifest_path = tmp_path / "metrics_snapshot_state.json"

    def inventory(_account_id, window, _now):
        coverage = PaginationCoverage("ACCOUNT_ADS", 1, ("ad-1", "ad-2"), True, None, "1" * 64)
        return DailyInventorySnapshot(
            account_id="account", account_status=1, currency="USD", timezone_name="UTC",
            target_window=window, fetched_at=now, max_age_seconds=900,
            ads_pagination=coverage, accessible_ad_ids=("ad-1", "ad-2"),
            eligible_ad_ids=("ad-1", "ad-2"), eligible_campaign_ids=("campaign-1",),
            exact_lookup_ad_ids=(),
            created_time_by_ad_sha256="2" * 64, status_by_ad_sha256="3" * 64,
            campaign_by_ad_sha256="4" * 64,
            cache_missing_ad_ids=(), cache_mismatched_ad_ids=(), fresh=True, complete=True,
        )

    row = {
        "ad_id": "ad-1", "date": "2026-07-21", "spend": 10.0, "impressions": 100,
        "clicks": 4, "ctr": 4.0, "leads": 2, "lead_semantics_version": 2,
        "lead_parse_status": "ok", "cpl": 5.0, "hook_rate": None, "hold_rate": None,
        "video_views_3s": 0, "day_since_launch": 1,
    }
    insights_coverage = PaginationCoverage("ACCOUNT_INSIGHTS", 1, ("ad-1",), True, None, "4" * 64)
    with patch("services.creative_intelligence.DB_PATH", tmp_db), \
         patch("config.REPORT_CHECKER_METRICS_MANIFEST_PATH", manifest_path), \
         patch("services.metrics_snapshot._fetch_account_metadata", return_value=(1, "USD", "UTC", now)), \
         patch("services.metrics_snapshot._fetch_live_daily_inventory", side_effect=inventory), \
         patch("services.metrics_snapshot._fetch_account_daily_rows", return_value=({"ad-1": row}, insights_coverage)), \
         patch("services.metrics_snapshot._fetch_daily_rows") as legacy_fetch:
        result = capture_daily_snapshot_complete("2026-07-21", now=now)

    assert result.complete is True
    assert result.verified_zero_ad_ids == ("ad-2",)
    assert result.requested_ad_ids == result.fetched_ad_ids == result.upserted_ad_ids
    assert manifest_path.exists()
    legacy_fetch.assert_not_called()


def test_live_inventory_binds_exact_ad_campaign_mapping(tmp_path):
    fetched_at = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
    window = TimeWindow(
        datetime(2026, 7, 21, tzinfo=timezone.utc),
        datetime(2026, 7, 22, tzinfo=timezone.utc),
        "UTC",
        "CLOSED_ACCOUNT_DAY",
    )
    campaign_by_ad = {"ad-1": "campaign-1", "ad-2": "campaign-future"}
    response = _graph_response(
        {
            "data": [
                {
                    "id": "ad-1",
                    "account_id": "account",
                    "campaign_id": "campaign-1",
                    "created_time": "2026-07-21T08:00:00+00:00",
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                },
                {
                    "id": "ad-2",
                    "account_id": "account",
                    "campaign_id": "campaign-future",
                    "created_time": "2026-07-22T08:00:00+00:00",
                    "status": "PAUSED",
                    "effective_status": "PAUSED",
                },
            ],
            "paging": {},
        }
    )
    with patch(
        "services.metrics_snapshot._fetch_account_metadata",
        return_value=(1, "USD", "UTC", fetched_at),
    ), patch("services.metrics_snapshot._throttled_get", return_value=response), patch(
        "services.metrics_snapshot.get_fb_token", return_value="token"
    ):
        inventory = _fetch_live_daily_inventory(
            "account",
            window,
            fetched_at,
            created_times_cache_path=tmp_path / "created_times.json",
        )

    assert inventory.eligible_ad_ids == ("ad-1",)
    assert inventory.eligible_campaign_ids == ("campaign-1",)
    assert inventory.campaign_by_ad_sha256 == hashlib.sha256(
        canonical_json(campaign_by_ad)
    ).hexdigest()


def test_strict_account_page_failure_does_not_return_partial_inventory():
    first = _graph_response({"data": [{"ad_id": "ad-1", "date_start": "2026-07-21", "actions": []}], "paging": {"next": "next"}})
    failed = _graph_response({}, status_code=500)
    window = TimeWindow(
        datetime(2026, 7, 21, tzinfo=timezone.utc),
        datetime(2026, 7, 22, tzinfo=timezone.utc),
        "UTC",
        "CLOSED_ACCOUNT_DAY",
    )
    inventory = DailyInventorySnapshot(
        account_id="account",
        account_status=1,
        currency="USD",
        timezone_name="UTC",
        target_window=window,
        fetched_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        max_age_seconds=900,
        ads_pagination=PaginationCoverage(
            "ACCOUNT_ADS", 1, ("ad-1",), True, None, "1" * 64
        ),
        accessible_ad_ids=("ad-1",),
        eligible_ad_ids=("ad-1",),
        eligible_campaign_ids=("campaign-1",),
        exact_lookup_ad_ids=(),
        created_time_by_ad_sha256="2" * 64,
        status_by_ad_sha256="3" * 64,
        campaign_by_ad_sha256="4" * 64,
        cache_missing_ad_ids=(),
        cache_mismatched_ad_ids=(),
        fresh=True,
        complete=True,
    )
    with patch("services.metrics_snapshot._throttled_get", side_effect=[first, failed]), \
         patch("services.metrics_snapshot.get_fb_token", return_value="token"):
        with pytest.raises(DailyMetricsCompletenessError, match="ACCOUNT_INSIGHTS_HTTP_500"):
            _fetch_account_daily_rows("account", window, inventory)


@pytest.mark.parametrize(
    "campaign_ids",
    [
        ("campaign-1",),
        ("campaign-1", "campaign-2", "campaign-extra"),
    ],
    ids=("missing-eligible-campaign", "extra-campaign"),
)
def test_campaign_fallback_rejects_non_exact_campaign_coverage(campaign_ids):
    window = TimeWindow(
        datetime(2026, 7, 21, tzinfo=timezone.utc),
        datetime(2026, 7, 22, tzinfo=timezone.utc),
        "UTC",
        "CLOSED_ACCOUNT_DAY",
    )
    inventory = DailyInventorySnapshot(
        account_id="account",
        account_status=1,
        currency="USD",
        timezone_name="UTC",
        target_window=window,
        fetched_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        max_age_seconds=900,
        ads_pagination=PaginationCoverage(
            "ACCOUNT_ADS", 2, ("ad-1", "ad-2"), True, None, "1" * 64
        ),
        accessible_ad_ids=("ad-1", "ad-2"),
        eligible_ad_ids=("ad-1", "ad-2"),
        eligible_campaign_ids=("campaign-1", "campaign-2"),
        exact_lookup_ad_ids=(),
        created_time_by_ad_sha256="2" * 64,
        status_by_ad_sha256="3" * 64,
        campaign_by_ad_sha256="4" * 64,
        cache_missing_ad_ids=(),
        cache_mismatched_ad_ids=(),
        fresh=True,
        complete=True,
    )
    campaigns = _graph_response(
        {"data": [{"id": campaign_id} for campaign_id in campaign_ids], "paging": {}}
    )
    with patch("services.metrics_snapshot._throttled_get", return_value=campaigns), patch(
        "services.metrics_snapshot.get_fb_token", return_value="token"
    ):
        with pytest.raises(
            DailyMetricsCompletenessError,
            match="CAMPAIGN_ID_COVERAGE_MISMATCH",
        ):
            _fetch_campaign_daily_rows("account", window, inventory)


def test_complete_snapshot_account_failure_persists_incomplete(tmp_path):
    manifest_path = tmp_path / "metrics_snapshot_state.json"
    now = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
    with patch("config.REPORT_CHECKER_METRICS_MANIFEST_PATH", manifest_path), \
         patch("services.metrics_snapshot.get_fb_account_id", return_value="account"), \
         patch("services.metrics_snapshot._fetch_account_metadata", side_effect=RuntimeError("offline")):
        result = capture_daily_snapshot_complete("2026-07-21", now=now)
    assert result.complete is False
    assert result.incomplete_reason_codes == ("offline",)
    assert manifest_path.exists()


def test_created_times_cache_path_per_account():
    """Дефолтный кабинет читает legacy-файл кеша created_times,
    маршрутизированный — свой файл с суффиксом account_id."""
    from services import metrics_snapshot as ms
    from services.fb_token_provider import fb_account, offline_account_context

    with patch("config.FB_ACCOUNT_ID", "152882611033373"), \
         patch("services.fb_credentials.get_active_account_id", return_value=None), \
         patch("services.launch_routing.get_route_table",
               return_value={("CityF", "L2"): "29716040622546856"}):
        assert ms._created_times_cache_path() == ms._CREATED_TIMES_CACHE_PATH
        with fb_account(offline_account_context("29716040622546856")):
            routed = ms._created_times_cache_path()

    assert routed.name == "ad_created_times_29716040622546856.json"
    assert routed.parent == ms._CREATED_TIMES_CACHE_PATH.parent
