"""
Unit-тесты для services/creative_backfill.py.

Проверяют резюмируемость курсора, rate-limit, завершение бэкфилла,
корректность UPSERT и сохранение архивных объявлений.

Используют tmp_path — не трогают реальную БД data/decisions.db.
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Мокаем google.genai до импорта любого модуля который может его тянуть
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())

from services import creative_intelligence as ci
import services.creative_backfill as cb
from agent.fb_common import FBApiError


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем глобальный DB_PATH перед и после каждого теста."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


@pytest.fixture
def kb(tmp_path):
    """Инициализирует пустую KB во временной директории и возвращает путь."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)
    return db_path


# Тестовые объявления — минимальный набор полей от FB API
_SAMPLE_ADS = [
    {
        "id": "ad_001",
        "name": "CityA | L2 | Тест 1",
        "status": "ACTIVE",
        "effective_status": "ACTIVE",
        "created_time": "2025-01-10T10:00:00+0000",
        "adset_id": "adset_1",
    },
    {
        "id": "ad_002",
        "name": "CityB | L1 | Тест 2",
        "status": "PAUSED",
        "effective_status": "PAUSED",
        "created_time": "2025-02-15T10:00:00+0000",
        "adset_id": "adset_2",
    },
    {
        "id": "ad_003",
        "name": "CityC | CR | Тест 3",
        "status": "DELETED",
        "effective_status": "ARCHIVED",
        "created_time": "2024-06-01T10:00:00+0000",
        "adset_id": "adset_unknown",
    },
]

# Мок метрик инсайтов — возвращаем нули (как для DELETED без данных)
_EMPTY_METRICS: dict = {}

# Мок creative полей
_EMPTY_CREATIVE: dict = {}


def _make_mock_insights(ad_ids: list[str]) -> dict:
    """Возвращает минимальные метрики для ad_ids."""
    return {
        ad_id: {
            "spend": 10.0, "leads": 1, "cpl": 10.0,
            "ctr": 1.0, "cpm": 5.0, "impressions": 1000,
            "clicks": 10, "frequency": 1.2,
            "video_views_3s": 300, "thruplay": 50,
            "video_p25": 200, "video_p50": 150, "video_p75": 100, "video_p100": 50,
            "hook_rate": 30.0, "hold_rate": 16.7,
        }
        for ad_id in ad_ids
    }


def _make_mock_creatives(ad_ids: list[str]) -> dict:
    """Возвращает минимальные creative поля для ad_ids."""
    return {
        ad_id: {
            "ad_body": f"Тест тело {ad_id}",
            "ad_headline": "",
            "image_url": None,
            "content_type": "video",
        }
        for ad_id in ad_ids
    }


# ---------------------------------------------------------------------------
# Тест 1: первая страница — upserted=N, cursor_after задан, done=False
# ---------------------------------------------------------------------------


def test_backfill_first_page(kb, monkeypatch):
    """Первый прогон: скачивает 3 объявления, сохраняет курсор, done=False."""
    page_ads = _SAMPLE_ADS[:3]

    monkeypatch.setattr(cb, "_fetch_ads_page", lambda cursor, page_size, statuses=None: (page_ads, "cursor_C2"))
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", _make_mock_insights)
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", _make_mock_creatives)
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    result = cb.sync_backfill_increment(max_ads=25)

    assert result["fetched"] == 3
    assert result["upserted"] == 3
    assert result["rate_limited"] is False
    assert result["cursor_after"] == "cursor_C2"
    assert result["done"] is False

    # Проверяем что в KB 3 строки с is_full_cabinet=1
    conn = ci._get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM creative_kb WHERE is_full_cabinet=1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 3


# ---------------------------------------------------------------------------
# Тест 2: резюм с курсора — _fetch_ads_page вызван с cursor="cursor_C2"
# ---------------------------------------------------------------------------


def test_backfill_resumes_from_cursor(kb, monkeypatch):
    """Второй прогон стартует с сохранённого курсора."""
    # Сначала сохраняем state с курсором
    cb.save_backfill_state({
        "cursor": "cursor_C2",
        "fetched_total": 3,
        "upserted_total": 3,
        "done": False,
        "last_run_at": None,
        "rate_limited_at": None,
    })

    captured_cursor = {}

    def fake_fetch(cursor, page_size, statuses=None):
        captured_cursor["cursor"] = cursor
        return ([], None)  # пустая страница — бэкфилл завершается

    monkeypatch.setattr(cb, "_fetch_ads_page", fake_fetch)
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    cb.sync_backfill_increment(max_ads=25)

    assert captured_cursor["cursor"] == "cursor_C2"


# ---------------------------------------------------------------------------
# Тест 3: rate-limit → rate_limited=True, курсор не двигается
# ---------------------------------------------------------------------------


def test_backfill_rate_limit_cursor_unchanged(kb, monkeypatch):
    """FBApiError → rate_limited=True, курсор остаётся прежним."""
    original_cursor = "cursor_BEFORE_RATELIMIT"
    cb.save_backfill_state({
        "cursor": original_cursor,
        "fetched_total": 0,
        "upserted_total": 0,
        "done": False,
        "last_run_at": None,
        "rate_limited_at": None,
    })

    def raise_rate_limit(cursor, page_size, statuses=None):
        raise FBApiError("Rate limit exceeded", status_code=429)

    monkeypatch.setattr(cb, "_fetch_ads_page", raise_rate_limit)

    result = cb.sync_backfill_increment(max_ads=25)

    assert result["rate_limited"] is True
    assert result["fetched"] == 0
    assert result["upserted"] == 0
    assert result["cursor_after"] == original_cursor
    assert result["done"] is False

    # Курсор в state НЕ изменился
    state = cb.get_backfill_state()
    assert state["cursor"] == original_cursor
    assert state["rate_limited_at"] is not None


# ---------------------------------------------------------------------------
# Тест 4: завершение — done=True при пустой странице без next_cursor
# ---------------------------------------------------------------------------


def test_backfill_done_on_empty_page(kb, monkeypatch):
    """Пустая страница + None курсор → done=True."""
    monkeypatch.setattr(cb, "_fetch_ads_page", lambda cursor, page_size, statuses=None: ([], None))
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    result = cb.sync_backfill_increment(max_ads=25)

    assert result["done"] is True
    state = cb.get_backfill_state()
    assert state["done"] is True


# ---------------------------------------------------------------------------
# Тест 5: повторный вызов когда done=True → no-op
# ---------------------------------------------------------------------------


def test_backfill_noop_when_done(kb, monkeypatch):
    """Если done=True — _fetch_ads_page не вызывается, возвращаем done."""
    cb.save_backfill_state({
        "cursor": None,
        "fetched_total": 100,
        "upserted_total": 100,
        "done": True,
        "last_run_at": "2026-06-10T02:00:00+00:00",
        "rate_limited_at": None,
    })

    fetch_called = []

    def should_not_call(cursor, page_size, statuses=None):
        fetch_called.append(True)
        return ([], None)

    monkeypatch.setattr(cb, "_fetch_ads_page", should_not_call)

    result = cb.sync_backfill_increment(max_ads=25)

    assert result["done"] is True
    assert result["fetched"] == 0
    assert fetch_called == []


# ---------------------------------------------------------------------------
# Тест 6: архивное объявление сохраняется с effective_status="ARCHIVED"
# ---------------------------------------------------------------------------


def test_backfill_saves_archived_ad(kb, monkeypatch):
    """Объявление с effective_status=ARCHIVED → строка в KB с правильным статусом."""
    archived_ad = [
        {
            "id": "ad_arch_001",
            "name": "Архивное объявление",
            "status": "ARCHIVED",
            "effective_status": "ARCHIVED",
            "created_time": "2024-01-01T10:00:00+0000",
            "adset_id": "adset_x",
        }
    ]

    monkeypatch.setattr(cb, "_fetch_ads_page", lambda c, s, statuses=None: (archived_ad, None))
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    result = cb.sync_backfill_increment(max_ads=5)

    assert result["fetched"] == 1
    assert result["done"] is True  # None next_cursor

    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT effective_status, is_full_cabinet FROM creative_kb WHERE ad_id = ?",
            ("ad_arch_001",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["effective_status"] == "ARCHIVED"
    assert row["is_full_cabinet"] == 1


# ---------------------------------------------------------------------------
# Тест 7: повторный бэкфилл НЕ трёт labeled_at
# ---------------------------------------------------------------------------


def test_backfill_preserves_labeled_at(kb, monkeypatch):
    """Повторный UPSERT не затирает labeled_at из разметки."""
    # Сначала вставляем объявление с labeled_at
    conn = ci._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO creative_kb (ad_id, ad_name, is_full_cabinet, labeled_at)
            VALUES ('ad_001', 'Тест', 1, '2026-01-01T00:00:00')
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Запускаем бэкфилл для того же объявления
    monkeypatch.setattr(
        cb, "_fetch_ads_page",
        lambda c, s, statuses=None: ([{
            "id": "ad_001",
            "name": "Тест обновлённое",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "created_time": "2025-01-01T10:00:00+0000",
            "adset_id": "adset_any",
        }], None),
    )
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    cb.sync_backfill_increment(max_ads=5)

    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT labeled_at, ad_name FROM creative_kb WHERE ad_id = ?",
            ("ad_001",),
        ).fetchone()
    finally:
        conn.close()

    # labeled_at НЕ должен быть затёрт
    assert row["labeled_at"] == "2026-01-01T00:00:00"
    # ad_name обновился — UPSERT работает
    assert row["ad_name"] == "Тест обновлённое"


# ---------------------------------------------------------------------------
# Тест 8: get/save/reset_backfill_state
# ---------------------------------------------------------------------------


def test_backfill_state_get_default(kb):
    """get_backfill_state возвращает дефолт если строки нет."""
    state = cb.get_backfill_state()
    assert state["cursor"] is None
    assert state["fetched_total"] == 0
    assert state["upserted_total"] == 0
    assert state["done"] is False


def test_backfill_state_save_and_get(kb):
    """save → get возвращает то же самое."""
    data = {
        "cursor": "abc123",
        "fetched_total": 50,
        "upserted_total": 45,
        "done": False,
        "last_run_at": "2026-06-11T02:00:00+00:00",
        "rate_limited_at": None,
    }
    cb.save_backfill_state(data)
    loaded = cb.get_backfill_state()
    assert loaded["cursor"] == "abc123"
    assert loaded["fetched_total"] == 50
    assert loaded["done"] is False


def test_backfill_state_reset(kb):
    """reset_backfill_state сбрасывает cursor и done."""
    cb.save_backfill_state({
        "cursor": "xyz",
        "fetched_total": 999,
        "upserted_total": 999,
        "done": True,
        "last_run_at": "2026-01-01",
        "rate_limited_at": None,
    })
    cb.reset_backfill_state()
    state = cb.get_backfill_state()
    assert state["cursor"] is None
    assert state["done"] is False
    assert state["fetched_total"] == 0


# ---------------------------------------------------------------------------
# Тест 9: page_size ограничивается 25 независимо от max_ads
# ---------------------------------------------------------------------------


def test_backfill_page_size_capped_at_25(kb, monkeypatch):
    """page_size = min(max_ads, 25) — DEV-режим не любит большие батчи."""
    captured = {}

    def fake_fetch(cursor, page_size, statuses=None):
        captured["page_size"] = page_size
        return ([], None)

    monkeypatch.setattr(cb, "_fetch_ads_page", fake_fetch)
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    cb.sync_backfill_increment(max_ads=50)  # 50 > 25 → должно стать 25

    assert captured["page_size"] == 25


# ---------------------------------------------------------------------------
# Тест 10: _fetch_lifetime_insights падает — объявление всё равно сохраняется
# ---------------------------------------------------------------------------


def test_backfill_saves_ad_when_insights_fail(kb, monkeypatch):
    """Если _fetch_lifetime_insights вернул {}, объявление всё равно сохраняется с метриками=0."""
    ad = [{
        "id": "ad_no_insights",
        "name": "Без инсайтов",
        "status": "DELETED",
        "effective_status": "DELETED",
        "created_time": "2024-03-01T10:00:00+0000",
        "adset_id": "adset_z",
    }]

    monkeypatch.setattr(cb, "_fetch_ads_page", lambda c, s, statuses=None: (ad, None))
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})  # пустой результат
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    result = cb.sync_backfill_increment(max_ads=5)

    assert result["fetched"] == 1
    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT spend, is_full_cabinet FROM creative_kb WHERE ad_id = 'ad_no_insights'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["spend"] == 0
    assert row["is_full_cabinet"] == 1


# ---------------------------------------------------------------------------
# Тест 11: reduce-data (code=1) → уменьшает page_size, повторяет, rate_limited=False
# ---------------------------------------------------------------------------


def test_backfill_reduce_data_retries_with_smaller_page_size(kb, monkeypatch):
    """FBApiError(code=1) при page_size=25 → повторяет с меньшим page_size, не помечает rate_limited."""
    call_log = []

    def fetch_with_reduce_data(cursor, page_size, statuses=None):
        call_log.append(page_size)
        if page_size >= 25:
            # Имитируем reduce-data на большом размере
            raise FBApiError("FB /ads ошибка: 400 reduce data", status_code=1)
        # Меньший page_size — успех
        return ([{
            "id": "ad_reduce_ok",
            "name": "Тест reduce",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "created_time": "2025-01-01T10:00:00+0000",
            "adset_id": "adset_q",
        }], None)

    monkeypatch.setattr(cb, "_fetch_ads_page", fetch_with_reduce_data)
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    result = cb.sync_backfill_increment(max_ads=25)

    # НЕ должен помечать rate_limited=True
    assert result["rate_limited"] is False
    # Должен успешно обработать хотя бы одно объявление
    assert result["fetched"] == 1
    # Первый вызов был с page_size=25, потом с меньшим
    assert call_log[0] == 25
    assert any(ps < 25 for ps in call_log[1:])
    # Нет поля error
    assert "error" not in result


# ---------------------------------------------------------------------------
# Тест 12: настоящий rate-limit (code=17) → rate_limited=True, курсор не двигается
# ---------------------------------------------------------------------------


def test_backfill_true_rate_limit_sets_flag(kb, monkeypatch):
    """FBApiError(code=17) → rate_limited=True, error не set, курсор не двигается."""
    original_cursor = "cursor_BEFORE_RL"
    cb.save_backfill_state({
        "cursor": original_cursor,
        "fetched_total": 0,
        "upserted_total": 0,
        "done": False,
        "last_run_at": None,
        "rate_limited_at": None,
    })

    def raise_true_rl(cursor, page_size, statuses=None):
        raise FBApiError("User request limit reached", status_code=17)

    monkeypatch.setattr(cb, "_fetch_ads_page", raise_true_rl)

    result = cb.sync_backfill_increment(max_ads=25)

    assert result["rate_limited"] is True
    assert result["fetched"] == 0
    assert result["cursor_after"] == original_cursor
    assert result["done"] is False
    # Нет поля error — это rate-limit, не ошибка логики
    assert "error" not in result
    # rate_limited_at должен быть проставлен в state
    state = cb.get_backfill_state()
    assert state["rate_limited_at"] is not None
    assert state["cursor"] == original_cursor


# ---------------------------------------------------------------------------
# Тест 13: прочая ошибка (code=2 transient) → rate_limited=False, поле error
# ---------------------------------------------------------------------------


def test_backfill_other_fb_error_not_rate_limited(kb, monkeypatch):
    """FBApiError(code=2) → rate_limited=False, в результате поле error, курсор не двигается."""
    original_cursor = "cursor_BEFORE_ERR"
    cb.save_backfill_state({
        "cursor": original_cursor,
        "fetched_total": 0,
        "upserted_total": 0,
        "done": False,
        "last_run_at": None,
        "rate_limited_at": None,
    })

    def raise_transient(cursor, page_size, statuses=None):
        raise FBApiError("FB /ads ошибка: 400 transient error code 2", status_code=2)

    monkeypatch.setattr(cb, "_fetch_ads_page", raise_transient)

    result = cb.sync_backfill_increment(max_ads=25)

    # НЕ rate-limit — внешний цикл не должен уходить в бесконечный бэкофф
    assert result["rate_limited"] is False
    # Поле error заполнено
    assert "error" in result
    assert result["fetched"] == 0
    assert result["cursor_after"] == original_cursor
    assert result["done"] is False


# ---------------------------------------------------------------------------
# Тест 14: DELETED not supported (code=100 subcode=1815001) →
#          автодеградация без DELETED, НЕ rate_limited
# ---------------------------------------------------------------------------


def test_backfill_deleted_not_supported_fallback(kb, monkeypatch):
    """code=100 subcode=1815001 → повтор без DELETED в statuses, rate_limited=False."""
    call_statuses = []

    def fetch_with_deleted_error(cursor, page_size, statuses=None):
        call_statuses.append(statuses)
        if statuses is None:
            # Первый вызов с дефолтными статусами (включает DELETED) → ошибка
            raise FBApiError(
                "FB /ads ошибка: 400 {\"error\":{\"code\":100,\"error_subcode\":1815001,"
                "\"message\":\"Invalid parameter\"}}",
                status_code=400,
            )
        # Повтор без DELETED — успех
        return ([{
            "id": "ad_no_deleted",
            "name": "Без удалённых",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "created_time": "2025-01-01T10:00:00+0000",
            "adset_id": "adset_r",
        }], None)

    monkeypatch.setattr(cb, "_fetch_ads_page", fetch_with_deleted_error)
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    result = cb.sync_backfill_increment(max_ads=25)

    # Должен успешно обработать — НЕ rate_limited
    assert result["rate_limited"] is False
    assert result["fetched"] == 1
    # Нет поля error
    assert "error" not in result
    # Второй вызов должен был быть без DELETED
    second_call_statuses = call_statuses[1]
    assert second_call_statuses is not None
    assert "DELETED" not in second_call_statuses


# ---------------------------------------------------------------------------
# sync_recent_ads — ежедневный досинк недавних объявлений (created_at в creative_kb)
# ---------------------------------------------------------------------------

_RECENT_AD = {
    "id": "ad_recent_1",
    "name": "Свежее объявление",
    "status": "ACTIVE",
    "effective_status": "ACTIVE",
    "created_time": "2026-07-12T10:00:00+0000",
    "adset_id": "adset_1",
}


def test_sync_recent_ads_writes_created_at(kb, monkeypatch):
    """sync_recent_ads пишет created_at из created_time в creative_kb."""
    # Один кабинет: изолируем тест от боевой карты роутинга (data/settings.json) —
    # мультикабинетный обход зовёт fetch по числу кабинетов карты.
    monkeypatch.setattr(
        "services.launch_routing.accounts_to_scan", lambda: ("152882611033373",)
    )
    monkeypatch.setattr(cb, "_fetch_recent_ads", lambda days=7: [dict(_RECENT_AD)])
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    result = cb.sync_recent_ads(days=7)

    assert result["fetched"] == 1
    assert result["upserted"] == 1
    assert result["rate_limited"] is False
    assert result["error"] is None

    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT created_at FROM creative_kb WHERE ad_id = 'ad_recent_1'"
        ).fetchone()
    finally:
        conn.close()
    assert row["created_at"] == "2026-07-12T10:00:00+0000"


def test_sync_recent_ads_idempotent(kb, monkeypatch):
    """Повторный вызов не создаёт дубль строки (ON CONFLICT), новых=0 на 2-м прогоне."""
    monkeypatch.setattr(cb, "_fetch_recent_ads", lambda days=7: [dict(_RECENT_AD)])
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    first = cb.sync_recent_ads(days=7)
    second = cb.sync_recent_ads(days=7)

    assert first["upserted"] == 1  # новая строка
    assert second["upserted"] == 0  # обновление, не новая

    conn = ci._get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM creative_kb WHERE ad_id = 'ad_recent_1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_sync_recent_ads_rate_limit(kb, monkeypatch):
    """FBApiError(code=17) при выкачке → rate_limited=True, error не set."""
    def raise_rl(days=7):
        raise FBApiError("User request limit reached", status_code=17)

    monkeypatch.setattr(cb, "_fetch_recent_ads", raise_rl)

    result = cb.sync_recent_ads(days=7)

    assert result["rate_limited"] is True
    assert result["error"] is None
    assert result["fetched"] == 0


def test_sync_recent_ads_other_error(kb, monkeypatch):
    """Прочая FBApiError (code=2) → rate_limited=False, error заполнен."""
    def raise_err(days=7):
        raise FBApiError("transient", status_code=2)

    monkeypatch.setattr(cb, "_fetch_recent_ads", raise_err)

    result = cb.sync_recent_ads(days=7)

    assert result["rate_limited"] is False
    assert result["error"] is not None
    assert result["fetched"] == 0


def test_sync_recent_ads_empty_is_noop(kb, monkeypatch):
    """Нет свежих объявлений → fetched=0, upserted=0, без ошибок."""
    monkeypatch.setattr(cb, "_fetch_recent_ads", lambda days=7: [])

    result = cb.sync_recent_ads(days=7)

    assert result["fetched"] == 0
    assert result["upserted"] == 0
    assert result["error"] is None
    assert result["rate_limited"] is False


def test_sync_recent_ads_does_not_touch_backfill_done(kb, monkeypatch):
    """Независимый проход: done-флаг разового бэкфилла кабинета НЕ трогается."""
    cb.save_backfill_state({
        "cursor": None, "fetched_total": 100, "upserted_total": 100,
        "done": True, "last_run_at": "2026-06-19T02:00:00+00:00", "rate_limited_at": None,
    })
    monkeypatch.setattr(cb, "_fetch_recent_ads", lambda days=7: [dict(_RECENT_AD)])
    monkeypatch.setattr(cb, "_fetch_lifetime_insights", lambda ids: {})
    monkeypatch.setattr(cb, "_fetch_creative_fields_batch", lambda ids: {})
    monkeypatch.setattr(cb, "build_adset_map", lambda: {})

    cb.sync_recent_ads(days=7)

    state = cb.get_backfill_state()
    assert state["done"] is True  # не сброшен


def test_fetch_recent_ads_local_recheck_filters_old(kb, monkeypatch):
    """_fetch_recent_ads: локальная перепроверка created_time отсеивает старьё,
    даже если FB вернул его в странице (проигнорил серверный фильтр)."""
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    old = (now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    page = [
        {"id": "ad_new", "created_time": recent, "name": "n"},
        {"id": "ad_old", "created_time": old, "name": "o"},
    ]
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"data": page, "paging": {}}

    monkeypatch.setattr(cb, "_throttled_get", lambda url, **kw: resp)
    monkeypatch.setattr(cb, "get_fb_token", lambda: "tok")
    monkeypatch.setattr(cb, "get_fb_account_id", lambda: "123")

    ads = cb._fetch_recent_ads(days=7)

    ids = [a["id"] for a in ads]
    assert "ad_new" in ids
    assert "ad_old" not in ids
