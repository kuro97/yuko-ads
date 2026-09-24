"""
Юнит-тесты для services/metrics_backfill.py.

Проверяют:
- _month_key/_month_bounds/_all_months (детерминированные)
- backfill_range: happy path, rate-limit, from>to, reduce-data
- run_metrics_backfill_increment (курсорный):
  - курсор инициализируется в earliest при первом запуске
  - cursor_date двигается вперёд после успешного окна
  - повторный вызов продолжает с сохранённого cursor_date
  - ограничение max_windows: не больше N окон за вызов
  - done=True когда курсор прошёл latest
  - rate-limit: курсор не двигается, rate_limited_at ставится
- JSON-зеркало state: файл пишется после save

Используют tmp_path — не трогают реальную БД.
FB-функции мокируются через unittest.mock.patch.
"""

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта любого модуля который может его тянуть
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())

from services import creative_intelligence as ci
import services.metrics_backfill as mb
from agent.fb_common import FBApiError


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_kb_path(tmp_path):
    """Инициализирует изолированную временную KB для каждого теста."""
    ci.DB_PATH = None
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)
    yield db_path
    ci.DB_PATH = None


# ---------------------------------------------------------------------------
# Тесты: _month_key
# ---------------------------------------------------------------------------

def test_month_key_november():
    """date(2025,11,1) → '2025-11'."""
    assert mb._month_key(date(2025, 11, 1)) == "2025-11"


def test_month_key_june():
    """date(2026,6,16) → '2026-06'."""
    assert mb._month_key(date(2026, 6, 16)) == "2026-06"


def test_month_key_december():
    """date(2025,12,31) → '2025-12'."""
    assert mb._month_key(date(2025, 12, 31)) == "2025-12"


# ---------------------------------------------------------------------------
# Тесты: _month_bounds
# ---------------------------------------------------------------------------

def test_month_bounds_regular():
    """Обычный прошедший месяц: '2025-11' → ('2025-11-01', '2025-11-30')."""
    low, high = mb._month_bounds("2025-11")
    assert low == "2025-11-01"
    assert high == "2025-11-30"


def test_month_bounds_december():
    """Декабрь: '2025-12' → ('2025-12-01', '2025-12-31')."""
    low, high = mb._month_bounds("2025-12")
    assert low == "2025-12-01"
    assert high == "2025-12-31"


def test_month_bounds_current_month():
    """Текущий месяц: high обрезается до сегодня по локальному времени."""
    from services.metrics_snapshot import _TZ_LOCAL
    today_local = datetime.now(_TZ_LOCAL).date()
    month_key = today_local.strftime("%Y-%m")
    low, high = mb._month_bounds(month_key)
    assert low == today_local.replace(day=1).isoformat()
    assert high == today_local.isoformat()


# ---------------------------------------------------------------------------
# Тесты: _all_months
# ---------------------------------------------------------------------------

def test_all_months_count():
    """От 2025-11 до 2026-06 включительно = 8 месяцев."""
    from services.metrics_snapshot import _TZ_LOCAL
    # Мокаем локальную дату на 2026-06-16
    fixed_now = datetime(2026, 6, 16, 12, 0, 0, tzinfo=_TZ_LOCAL)
    with patch("services.metrics_backfill.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        months = mb._all_months()
    assert len(months) == 8
    assert months[0] == "2025-11"
    assert months[-1] == "2026-06"


def test_all_months_order():
    """Месяцы идут от старого к свежему."""
    from services.metrics_snapshot import _TZ_LOCAL
    fixed_now = datetime(2026, 6, 16, 12, 0, 0, tzinfo=_TZ_LOCAL)
    with patch("services.metrics_backfill.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        months = mb._all_months()
    # Проверяем что список отсортирован по возрастанию
    assert months == sorted(months)


# ---------------------------------------------------------------------------
# Тесты: backfill_range
# ---------------------------------------------------------------------------

def test_backfill_range_happy(reset_kb_path):
    """3 дня, мок _fetch_daily_rows отдаёт 2 строки за подокно → days=3, upserted=2, rate_limited=False.
    Новая логика: 3 дня < window_size=5 → одно подокно, один вызов _fetch_daily_rows.
    """

    def _fake_fetch(date_from, date_to, **kwargs):
        # Возвращаем 2 строки за всё подокно (как реальный FB с time_increment=1)
        return {
            "ad_001": {
                "ad_id": "ad_001", "date": date_from,
                "spend": 1.0, "impressions": 100, "clicks": 5, "ctr": 5.0,
                "leads": 1, "cpl": 1.0, "hook_rate": None, "hold_rate": None,
                "video_views_3s": 0, "day_since_launch": 1,
                "lead_semantics_version": 2, "lead_parse_status": "ok",
            },
            "ad_002": {
                "ad_id": "ad_002", "date": date_from,
                "spend": 2.0, "impressions": 200, "clicks": 10, "ctr": 5.0,
                "leads": 2, "cpl": 1.0, "hook_rate": 30.0, "hold_rate": 20.0,
                "video_views_3s": 50, "day_since_launch": 2,
                "lead_semantics_version": 2, "lead_parse_status": "ok",
            },
        }

    with patch("services.metrics_backfill._fetch_daily_rows", side_effect=_fake_fetch), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}):
        result = mb.backfill_range("2025-11-01", "2025-11-03")

    assert result["days"] == 3
    assert result["fetched"] == 2   # 2 строки за одно подокно (3 дня < window_size=5)
    assert result["upserted"] == 2
    assert result["rate_limited"] is False


def test_backfill_refetch_overwrites_legacy_row_with_v2_provenance(reset_kb_path):
    """Резюмируемый Graph refetch заменяет legacy total обычным parser/upsert v2."""
    conn = sqlite3.connect(reset_kb_path)
    try:
        conn.execute(
            "INSERT INTO ad_daily_metrics (ad_id, date, leads) VALUES (?, ?, ?)",
            ("ad_legacy", "2026-07-01", 99),
        )
        conn.commit()
    finally:
        conn.close()

    from services.metrics_snapshot import _parse_daily_row

    parsed = _parse_daily_row(
        {
            "ad_id": "ad_legacy",
            "date_start": "2026-07-01",
            "spend": "8",
            "actions": [
                {"action_type": "lead", "value": "4"},
                {"action_type": "onsite_conversion.lead_grouped", "value": "4"},
            ],
        },
        created_times={},
        on_date="2026-07-01",
    )
    with patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", return_value={"ad_legacy": parsed}):
        result = mb.backfill_range("2026-07-01", "2026-07-01")

    conn = sqlite3.connect(reset_kb_path)
    try:
        row = conn.execute(
            "SELECT leads, lead_semantics_version, lead_parse_status "
            "FROM ad_daily_metrics WHERE ad_id = ? AND date = ?",
            ("ad_legacy", "2026-07-01"),
        ).fetchone()
    finally:
        conn.close()

    assert result["upserted"] == 1
    assert row == (4, 2, "ok")


def test_backfill_range_rate_limit_on_second_window(reset_kb_path):
    """Настоящий rate-limit на 2-м подокне → rate_limited=True, первое подокно уже в БД.
    Используем window_size=3 чтобы 6-дневный диапазон разбился на 2 подокна.
    """
    call_count = 0

    def _fake_fetch(date_from, date_to, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Первое подокно (дни 1-3) — успех, 2 строки
            return {
                "ad_001": {
                    "ad_id": "ad_001", "date": date_from,
                    "spend": 1.0, "impressions": 100, "clicks": 5, "ctr": 5.0,
                    "leads": 1, "cpl": 1.0, "hook_rate": None, "hold_rate": None,
                    "video_views_3s": 0, "day_since_launch": 1,
                    "lead_semantics_version": 2, "lead_parse_status": "ok",
                },
                "ad_002": {
                    "ad_id": "ad_002", "date": date_from,
                    "spend": 2.0, "impressions": 200, "clicks": 10, "ctr": 5.0,
                    "leads": 2, "cpl": 1.0, "hook_rate": None, "hold_rate": None,
                    "video_views_3s": 0, "day_since_launch": 2,
                    "lead_semantics_version": 2, "lead_parse_status": "ok",
                },
            }
        # Второе подокно — настоящий rate-limit (429)
        raise FBApiError("rate limit", 429)

    with patch("services.metrics_backfill._fetch_daily_rows", side_effect=_fake_fetch), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}):
        result = mb.backfill_range("2025-11-01", "2025-11-06", window_size=3)

    assert result["rate_limited"] is True
    assert result["upserted"] == 2     # первое подокно уже сохранено
    assert result["days"] == 3         # первые 3 дня обработаны


def test_backfill_range_from_greater_than_to():
    """date_from > date_to → days=0, upserted=0, не ошибка."""
    result = mb.backfill_range("2025-12-05", "2025-12-01")
    assert result["days"] == 0
    assert result["upserted"] == 0
    assert result["rate_limited"] is False


# ---------------------------------------------------------------------------
# Тесты: run_metrics_backfill_increment (курсорная логика)
# ---------------------------------------------------------------------------

def _make_fake_fetch_daily(rows_per_window: int = 2):
    """Фабрика фейкового _fetch_daily_rows: возвращает rows_per_window строк за окно."""
    def _fake(date_from, date_to, created_times=None):
        result = {}
        for i in range(rows_per_window):
            ad_id = f"ad_{date_from}_{i:02d}"
            result[ad_id] = {
                "ad_id": ad_id, "date": date_from,
                "spend": 1.0, "impressions": 100, "clicks": 5, "ctr": 5.0,
                "leads": 1, "cpl": 1.0, "hook_rate": None, "hold_rate": None,
                "video_views_3s": 0, "day_since_launch": 1,
                "lead_semantics_version": 2, "lead_parse_status": "ok",
            }
        return result
    return _fake


def test_cursor_initializes_to_earliest(reset_kb_path):
    """Пустой state → cursor_date инициализируется в BACKFILL_START_DATE."""
    from services.metrics_snapshot import _TZ_LOCAL
    # Мокируем latest = 2025-11-05 (5 дней вперёд от старта, одно окно)
    fake_latest = date(2025, 11, 5)

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", side_effect=_make_fake_fetch_daily(2)):
        result = mb.run_metrics_backfill_increment(max_windows=1, window_days=5)

    # Курсор должен сдвинуться вперёд на 1 окно (5 дней)
    assert result["cursor_date"] == "2025-11-06"
    assert result["windows_processed"] == 1
    assert result["fetched"] == 2


def test_cursor_advances_after_success(reset_kb_path):
    """После успешного окна cursor_date сохраняется в state (частичный прогресс)."""
    fake_latest = date(2025, 11, 10)  # 10 дней = 2 окна по 5 дней

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", side_effect=_make_fake_fetch_daily(1)):
        result = mb.run_metrics_backfill_increment(max_windows=1, window_days=5)

    # Курсор продвинулся на 1 окно
    assert result["cursor_date"] == "2025-11-06"
    # Сохранено в state
    state = mb.get_backfill_metrics_state()
    assert state["cursor_date"] == "2025-11-06"
    assert state["last_run_at"] is not None


def test_cursor_resumes_from_saved_state(reset_kb_path):
    """Повторный вызов продолжает с сохранённого cursor_date (не с earliest)."""
    # Сохраняем cursor_date вперёд на 10 дней
    state = mb.get_backfill_metrics_state()
    state["cursor_date"] = "2025-11-11"
    mb.save_backfill_metrics_state(state)

    fake_latest = date(2025, 11, 15)
    fetch_calls = []

    def _tracking_fetch(date_from, date_to, created_times=None):
        fetch_calls.append(date_from)
        return {f"ad_{date_from}": {
            "ad_id": f"ad_{date_from}", "date": date_from,
            "spend": 1.0, "impressions": 100, "clicks": 5, "ctr": 5.0,
            "leads": 1, "cpl": 1.0, "hook_rate": None, "hold_rate": None,
            "video_views_3s": 0, "day_since_launch": 1,
            "lead_semantics_version": 2, "lead_parse_status": "ok",
        }}

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", side_effect=_tracking_fetch):
        result = mb.run_metrics_backfill_increment(max_windows=1, window_days=5)

    # Должен начать с 2025-11-11, а не с BACKFILL_START_DATE (2025-11-01)
    assert fetch_calls[0] == "2025-11-11"
    assert result["cursor_date"] == "2025-11-16"


def test_max_windows_limit(reset_kb_path):
    """За один вызов обрабатывается не более max_windows окон."""
    fake_latest = date(2025, 11, 30)  # 30 дней = 6 окон по 5 дней
    fetch_calls = []

    def _counting_fetch(date_from, date_to, created_times=None):
        fetch_calls.append(date_from)
        return {}

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", side_effect=_counting_fetch):
        result = mb.run_metrics_backfill_increment(max_windows=3, window_days=5)

    # Не более 3 вызовов fetch
    assert len(fetch_calls) <= 3
    assert result["windows_processed"] <= 3
    assert result["done"] is False
    assert result["stopped_reason"] == "max_windows"


def test_max_seconds_limit(reset_kb_path):
    """Лимит max_seconds=0 → выход после 0 окон (время уже истекло до первого окна)."""
    fake_latest = date(2025, 11, 30)

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", side_effect=_make_fake_fetch_daily(1)):
        # max_seconds=0 — время уже истекло до первой итерации
        result = mb.run_metrics_backfill_increment(max_windows=10, max_seconds=0, window_days=5)

    assert result["windows_processed"] == 0
    assert result["stopped_reason"] == "max_seconds"


def test_done_when_cursor_passes_latest(reset_kb_path):
    """done=True когда cursor_date > latest."""
    # Уже залит весь диапазон — cursor за latest
    fake_latest = date(2025, 11, 10)
    state = mb.get_backfill_metrics_state()
    state["cursor_date"] = "2025-11-11"  # уже за latest
    mb.save_backfill_metrics_state(state)

    fetch_calls = []
    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", side_effect=lambda *a, **kw: fetch_calls.append(1) or {}):
        result = mb.run_metrics_backfill_increment(max_windows=5, window_days=5)

    assert result["done"] is True
    assert result["stopped_reason"] == "done"
    assert len(fetch_calls) == 0  # FB не дёргался — уже done


def test_rate_limit_does_not_advance_cursor(reset_kb_path):
    """rate-limit → cursor_date не меняется, rate_limited_at проставляется."""
    from agent.fb_common import FBApiError as FBErr
    fake_latest = date(2025, 11, 10)

    def _rate_limit_fetch(date_from, date_to, created_times=None):
        raise FBErr("rate limited", 429)

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", side_effect=_rate_limit_fetch):
        result = mb.run_metrics_backfill_increment(max_windows=3, window_days=5)

    assert result["rate_limited"] is True
    assert result["done"] is False
    # Курсор остался на начале (earliest = 2025-11-01)
    state = mb.get_backfill_metrics_state()
    assert state.get("cursor_date") in (None, "2025-11-01")
    assert state["rate_limited_at"] is not None


def test_rows_upserted_after_successful_window(reset_kb_path):
    """После успешного окна строки реально в ad_daily_metrics."""
    import sqlite3
    fake_latest = date(2025, 11, 5)

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", side_effect=_make_fake_fetch_daily(3)):
        result = mb.run_metrics_backfill_increment(max_windows=1, window_days=5)

    assert result["upserted"] == 3
    # Проверяем в БД
    from services.creative_intelligence import _get_connection
    conn = _get_connection()
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM ad_daily_metrics").fetchone()[0]
    finally:
        conn.close()
    assert cnt == 3


# ---------------------------------------------------------------------------
# Тест: run_metrics_backfill — обратная совместимость
# ---------------------------------------------------------------------------

def test_run_backfill_returns_expected_keys(reset_kb_path):
    """run_metrics_backfill возвращает ключи совместимые с эндпоинтом."""
    fake_latest = date(2025, 11, 5)

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", side_effect=_make_fake_fetch_daily(1)):
        result = mb.run_metrics_backfill(months_back=2)

    # Ключи, которые ожидает эндпоинт BackfillMetricsResponse
    assert "increments_run" in result
    assert "fetched_total" in result
    assert "upserted_total" in result
    assert "rate_limited" in result
    assert "done" in result


# ---------------------------------------------------------------------------
# Тест: increment — старые тесты адаптированы к новому интерфейсу
# ---------------------------------------------------------------------------

def test_increment_rate_limit_does_not_advance(reset_kb_path):
    """rate-limit → cursor не растёт, rate_limited=True."""
    from agent.fb_common import FBApiError as FBErr
    fake_latest = date(2025, 11, 10)

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows",
               side_effect=FBErr("rate limit", 429)):
        result = mb.run_metrics_backfill_increment(max_windows=3, window_days=5)

    assert result["rate_limited"] is True
    # cursor_date остался в начале (None или earliest)
    state = mb.get_backfill_metrics_state()
    assert state.get("cursor_date") in (None, mb.BACKFILL_START_DATE.isoformat())
    assert state["rate_limited_at"] is not None


# ---------------------------------------------------------------------------
# Тест: JSON-зеркало
# ---------------------------------------------------------------------------

def test_state_json_mirror(reset_kb_path, tmp_path):
    """После save_backfill_metrics_state файл metrics_backfill_state.json содержит months_done."""
    state = {"months_done": ["2025-11", "2025-12"], "last_run_at": None, "rate_limited_at": None}

    # Перенаправляем путь к зеркалу во временную директорию
    json_path = tmp_path / "metrics_backfill_state.json"
    with patch.object(mb, "_STATE_JSON_PATH", json_path):
        mb.save_backfill_metrics_state(state)

    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["months_done"] == ["2025-11", "2025-12"]


def test_state_json_mirror_io_error(reset_kb_path, tmp_path, caplog):
    """Ошибка записи JSON → только warning, БД-состояние всё равно сохранено."""
    import logging
    state = {"months_done": ["2025-11"], "last_run_at": None, "rate_limited_at": None}

    # Используем несуществующий и неписаемый путь
    bad_path = Path("/nonexistent_dir/metrics_backfill_state.json")
    with patch.object(mb, "_STATE_JSON_PATH", bad_path), \
         caplog.at_level(logging.WARNING):
        mb.save_backfill_metrics_state(state)

    # Должно быть предупреждение, но не исключение
    assert any("JSON" in r.message or "зеркало" in r.message for r in caplog.records)

    # БД-состояние должно быть сохранено
    loaded = mb.get_backfill_metrics_state()
    assert "2025-11" in loaded["months_done"]


# ---------------------------------------------------------------------------
# Новые тесты: reduce-data → уменьшение окна, без rate_limited
# ---------------------------------------------------------------------------

def _make_row(ad_id: str, day: str) -> dict:
    """Хелпер: создаёт минимальную корректную строку для UPSERT."""
    return {
        "ad_id": ad_id,
        "date": day,
        "spend": 1.0,
        "impressions": 100,
        "clicks": 5,
        "ctr": 5.0,
        "leads": 1,
        "cpl": 1.0,
        "hook_rate": None,
        "hold_rate": None,
        "video_views_3s": 0,
        "day_since_launch": 1,
        "lead_semantics_version": 2,
        "lead_parse_status": "ok",
    }


def test_backfill_range_reduce_data_shrinks_window(reset_kb_path):
    """Первый вызов 5 дней → reduce-data; вызов 2 дня → данные.
    Проверяем: rate_limited=False, upsert вызван, данные есть."""
    call_log = []

    def _fake_fetch(date_from, date_to, **kwargs):
        span = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
        call_log.append((date_from, date_to, span))
        if span >= 5:
            # Слишком большое окно — reduce-data
            raise FBApiError("Please reduce the amount of data", status_code=1)
        # Маленькое окно — отдаём строки
        rows = {}
        cur = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
        while cur <= end:
            aid = f"ad_{cur.isoformat()}"
            rows[aid] = _make_row(aid, cur.isoformat())
            cur += timedelta(days=1)
        return rows

    with patch("services.metrics_backfill._fetch_daily_rows", side_effect=_fake_fetch), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}):
        result = mb.backfill_range("2025-11-01", "2025-11-05", window_size=5)

    # rate_limited НЕ ставится при reduce-data
    assert result["rate_limited"] is False
    # Данные получены — upsert произошёл
    assert result["fetched"] > 0
    assert result["upserted"] > 0
    # Был хотя бы один вызов с меньшим окном (< 5 дней)
    assert any(span < 5 for _, _, span in call_log)


def test_backfill_range_true_rate_limit_stops(reset_kb_path):
    """Настоящий rate-limit (code 17/429) → rate_limited=True, месяц не done."""
    call_count = []

    def _fake_fetch(date_from, date_to, **kwargs):
        call_count.append(1)
        # Имитируем настоящий rate-limit (HTTP 429)
        raise FBApiError("User request limit reached", status_code=429)

    with patch("services.metrics_backfill._fetch_daily_rows", side_effect=_fake_fetch), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}):
        result = mb.backfill_range("2025-11-01", "2025-11-05", window_size=5)

    assert result["rate_limited"] is True
    # Был хотя бы один вызов
    assert len(call_count) >= 1


# test_backfill_range_true_rate_limit_via_increment — удалён:
# тестировал помесячную логику через мок backfill_range, которая заменена
# курсорной логикой. Аналогичный кейс покрыт test_rate_limit_does_not_advance_cursor.


def test_backfill_range_unexpected_error_stops_fast(reset_kb_path, caplog):
    """FBApiError с code=100 (не reduce-data, не rate-limit) →
    backfill_range останавливается после 1 вызова (не зацикливается),
    rate_limited=False, месяц НЕ помечен done."""
    import logging
    call_count = []

    def _fake_fetch(date_from, date_to, **kwargs):
        call_count.append(1)
        # code=100 — невалидное поле (напр. video_3_sec_watched_actions)
        raise FBApiError("Invalid parameter (code 100)", status_code=100)

    with patch("services.metrics_backfill._fetch_daily_rows", side_effect=_fake_fetch), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         caplog.at_level(logging.ERROR):
        result = mb.backfill_range("2025-11-01", "2025-11-05", window_size=5)

    # Выборка вызвана РОВНО ОДИН РАЗ — не зациклилась
    assert len(call_count) == 1
    # Не rate-limit — это другая ошибка
    assert result["rate_limited"] is False
    # Данных нет — ошибка произошла до сохранения
    assert result["fetched"] == 0
    assert result["upserted"] == 0
    # Ошибка залогирована на уровне ERROR
    assert any("code=100" in r.message or "неожиданная" in r.message for r in caplog.records)


def test_backfill_range_reduce_data_1day_is_incomplete(reset_kb_path):
    """Reduce-data даже за 1 день → incomplete без ложного пропуска дня."""
    call_log = []

    def _fake_fetch(date_from, date_to, **kwargs):
        span = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
        call_log.append((date_from, date_to, span))
        if date_from == "2025-11-01":
            # Первый день — всегда reduce-data (включая 1-дневное окно)
            raise FBApiError("Please reduce the amount of data", status_code=1)
        return {"unexpected": _make_row("unexpected", date_from)}

    # 3-дневный диапазон с window_size=1 чтобы сразу идти по 1 дню
    with patch("services.metrics_backfill._fetch_daily_rows", side_effect=_fake_fetch), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}):
        result = mb.backfill_range("2025-11-01", "2025-11-03", window_size=1)

    # Это не rate-limit, но и не complete: курсор не имеет права пропускать день.
    assert result["rate_limited"] is False
    assert result["complete"] is False
    assert result["days"] == 0
    assert len(call_log) == 1


# ---------------------------------------------------------------------------
# Новые тесты: поведение run_metrics_backfill_increment через backfill_range
# ---------------------------------------------------------------------------

def test_increment_empty_window_advances_cursor(reset_kb_path):
    """Окно где backfill_range возвращает fetched=0 (пустое, праздники) →
    курсор ВСЁ РАВНО продвигается, не застревает."""
    fake_latest = date(2025, 11, 10)  # 10 дней = 2 окна по 5 дней

    # backfill_range возвращает пустой результат (нет данных в FB за эти дни)
    empty_result = {"fetched": 0, "upserted": 0, "rate_limited": False, "days": 5,
                    "date_from": "", "date_to": ""}

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill.backfill_range", return_value=empty_result) as mock_br:
        result = mb.run_metrics_backfill_increment(max_windows=2, window_days=5)

    # Два окна обработано — курсор сдвинулся несмотря на fetched=0
    assert mock_br.call_count == 2
    assert result["windows_processed"] == 2
    # Курсор за latest → done
    assert result["done"] is True
    assert result["stopped_reason"] == "done"
    # State в БД обновился
    state = mb.get_backfill_metrics_state()
    assert state["cursor_date"] == "2025-11-11"


def test_increment_fb_error_keeps_cursor(reset_kb_path, caplog):
    """Неожиданная ошибка окна останавливает прогон без продвижения курсора."""
    import logging
    fake_latest = date(2025, 11, 15)  # 15 дней = 3 окна по 5 дней

    call_count = [0]

    def _failing_range(date_from, date_to, **kwargs):
        call_count[0] += 1
        # Всегда бросаем неожиданное исключение
        raise RuntimeError(f"Неожиданная ошибка сети на {date_from}")

    cursor_before = mb.BACKFILL_START_DATE.isoformat()  # 2025-11-01

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill.backfill_range", side_effect=_failing_range), \
         caplog.at_level(logging.ERROR):
        result = mb.run_metrics_backfill_increment(max_windows=10, window_days=5)

    assert call_count[0] == 1
    assert result["stopped_reason"] == "fb_error"
    assert result["cursor_date"] == cursor_before
    assert mb.get_backfill_metrics_state().get("cursor_date") in (None, cursor_before)
    assert any("Курсор не изменён" in r.message for r in caplog.records)


@pytest.mark.parametrize("failure_source", ["pagination", "campaign_chunk", "parser"])
def test_increment_partial_fetch_result_keeps_cursor(reset_kb_path, failure_source):
    """Любой partial fetch result оставляет внешний cursor на начале окна."""
    fake_latest = date(2025, 11, 5)
    incomplete = {
        "fetched": 2,
        "upserted": 2,
        "rate_limited": False,
        "complete": False,
        "error": f"partial_{failure_source}",
        "days": 0,
        "date_from": "2025-11-01",
        "date_to": "2025-11-05",
    }

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill.backfill_range", return_value=incomplete):
        result = mb.run_metrics_backfill_increment(max_windows=1, window_days=5)

    assert result["stopped_reason"] == "fb_error"
    assert result["cursor_date"] == mb.BACKFILL_START_DATE.isoformat()
    assert result["windows_processed"] == 0
    state = mb.get_backfill_metrics_state()
    assert state.get("cursor_date") in (None, mb.BACKFILL_START_DATE.isoformat())


def test_increment_rate_limit_from_backfill_range(reset_kb_path):
    """backfill_range возвращает rate_limited=True → курсор не двигается,
    rate_limited_at проставляется."""
    fake_latest = date(2025, 11, 10)

    rate_limited_result = {"fetched": 0, "upserted": 0, "rate_limited": True, "days": 0,
                           "date_from": "2025-11-01", "date_to": "2025-11-05"}

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill.backfill_range", return_value=rate_limited_result):
        result = mb.run_metrics_backfill_increment(max_windows=3, window_days=5)

    assert result["rate_limited"] is True
    assert result["done"] is False
    assert result["stopped_reason"] == "rate_limited"
    # Курсор не сдвинулся (остался на earliest = 2025-11-01)
    assert result["cursor_date"] == mb.BACKFILL_START_DATE.isoformat()
    # rate_limited_at проставлен в state
    state = mb.get_backfill_metrics_state()
    assert state["rate_limited_at"] is not None


# ---------------------------------------------------------------------------
# Тесты: account-aware курсор (кабинеты карты роутинга)
# ---------------------------------------------------------------------------

def test_state_key_and_mirror_per_account(reset_kb_path, tmp_path):
    """Маршрутизированный кабинет пишет свой ключ backfill_state и свой
    JSON-файл зеркала; legacy-состояние дефолтного кабинета не трогается."""
    from services.creative_backfill import _get_state_by_key

    state = {"months_done": [], "cursor_date": "2026-08-18",
             "last_run_at": None, "rate_limited_at": None}
    json_path = tmp_path / "metrics_backfill_state.json"
    with patch.object(mb, "_STATE_JSON_PATH", json_path), \
         patch("config.FB_ACCOUNT_ID", "152882611033373"):
        mb.save_backfill_metrics_state(state, account_id="act_29716040622546856")

        raw = _get_state_by_key("metrics_backfill:29716040622546856")
        assert raw.get("cursor_date") == "2026-08-18"
        # Legacy-ключ дефолтного кабинета остался пуст
        assert mb.get_backfill_metrics_state()["cursor_date"] is None
        # Зеркало пишется в файл с суффиксом кабинета
        assert (tmp_path / "metrics_backfill_state_29716040622546856.json").exists()
        assert not json_path.exists()


def test_increment_routed_account_own_cursor_and_context(reset_kb_path, tmp_path):
    """Инкремент маршрутизированного кабинета: курсор стартует с даты рождения
    кабинета (_ACCOUNT_START_DATES), FB-вызовы идут под контекстом кабинета,
    состояние — под ключом с суффиксом."""
    from services.fb_token_provider import get_fb_account_id

    fake_latest = date(2026, 1, 5)
    seen_accounts = []
    fetch_calls = []

    def _tracking_fetch(date_from, date_to, created_times=None):
        fetch_calls.append((date_from, date_to))
        seen_accounts.append(get_fb_account_id())
        return {}

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", side_effect=_tracking_fetch), \
         patch("services.launch_routing.get_route_table",
               return_value={("CityF", "L2"): "29716040622546856"}), \
         patch("config.FB_ACCOUNT_ID", "152882611033373"), \
         patch.object(mb, "_STATE_JSON_PATH", tmp_path / "metrics_backfill_state.json"):
        result = mb.run_metrics_backfill_increment(
            max_windows=1, window_days=5, account_id="29716040622546856"
        )

        # Стартовали с даты кабинета из _ACCOUNT_START_DATES, а не с BACKFILL_START_DATE
        assert fetch_calls[0][0] == "2026-01-01"
        assert seen_accounts == ["29716040622546856"]
        assert result["cursor_date"] == "2026-01-06"
        # Свой ключ состояния; legacy-курсор не тронут
        assert mb.get_backfill_metrics_state("29716040622546856")["cursor_date"] == "2026-01-06"
        assert mb.get_backfill_metrics_state()["cursor_date"] is None


def test_increment_default_account_id_uses_legacy_key(reset_kb_path, tmp_path):
    """account_id дефолтного кабинета (как отдаёт accounts_to_scan) — это
    legacy-путь: ключ «metrics_backfill», earliest = BACKFILL_START_DATE."""
    fake_latest = date(2025, 11, 5)

    with patch("services.metrics_backfill._get_latest_date", return_value=fake_latest), \
         patch("services.metrics_backfill._fetch_created_times", return_value={}), \
         patch("services.metrics_backfill._fetch_daily_rows", return_value={}), \
         patch("config.FB_ACCOUNT_ID", "152882611033373"), \
         patch.object(mb, "_STATE_JSON_PATH", tmp_path / "metrics_backfill_state.json"):
        result = mb.run_metrics_backfill_increment(
            max_windows=1, window_days=5, account_id="152882611033373"
        )

        assert result["cursor_date"] == "2025-11-06"
        assert mb.get_backfill_metrics_state()["cursor_date"] == "2025-11-06"


def test_increment_unregistered_account_fails_closed(reset_kb_path):
    """Незарегистрированный кабинет — RuntimeError до любых FB-вызовов."""
    with patch("services.launch_routing.get_route_table",
               return_value={("CityF", "L2"): "29716040622546856"}), \
         patch("config.FB_ACCOUNT_ID", "152882611033373"):
        with pytest.raises(RuntimeError):
            mb.run_metrics_backfill_increment(account_id="912221544354116")
