"""
Тест C3: _upsert_ad (services/creative_backfill.py) не перезаписывает существующий
непустой business_class значением-заглушкой «Нет данных» (CASE в ON CONFLICT SET).

_upsert_ad всегда классифицирует без AMO-данных (romi/qual_pct/payments=None внутри
функции) → _classify обычно отдаёт «Нет данных». Чтобы проверить ветку «бэкфилл
приносит осмысленное значение → перезаписывает», мокаем _classify напрямую —
это единственный способ управлять business_class изнутри _upsert_ad без изменения
прод-кода.

Используют tmp_path — не трогают реальную БД data/decisions.db. Сеть не нужна.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from services import creative_intelligence as ci
import services.creative_backfill as cb


# ---------------------------------------------------------------------------
# Фикстуры (по образцу tests/test_creative_backfill.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем глобальный DB_PATH перед и после каждого теста."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


@pytest.fixture
def kb(tmp_path):
    """Инициализирует пустую KB (полная схема через миграции) во временной директории."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)
    return db_path


def _make_ad(ad_id="ad-1", name="Тест объявление", adset_id="adset_1"):
    return {
        "id": ad_id,
        "name": name,
        "status": "ACTIVE",
        "effective_status": "ACTIVE",
        "created_time": "2025-01-10T10:00:00+0000",
        "adset_id": adset_id,
    }


def _fetch_business_class(kb, ad_id="ad-1"):
    conn = sqlite3.connect(kb)
    try:
        row = conn.execute(
            "SELECT business_class FROM creative_kb WHERE ad_id = ?", (ad_id,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Существующий 'Прибыльный' + бэкфилл приносит заглушку 'Нет данных' → сохраняется
# ---------------------------------------------------------------------------


def test_backfill_keeps_existing_business_class_when_backfill_has_no_data(kb):
    """Строка уже размечена business_class='Прибыльный' (из AMO-синка). Бэкфилл
    не знает AMO (романтически всегда None внутри _upsert_ad) → classify_business
    отдаёт 'Нет данных'. UPSERT НЕ должен затирать 'Прибыльный' этой заглушкой."""
    conn = ci._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO creative_kb (ad_id, ad_name, business_class, is_full_cabinet)
            VALUES ('ad-1', 'Старое имя', 'Прибыльный', 1)
            """
        )
        conn.commit()
    finally:
        conn.close()

    conn = ci._get_connection()
    try:
        # _classify внутри _upsert_ad всегда вызывается с romi/qual_pct/payments=None
        # → реальный classify_business реально отдаст 'Нет данных' без мока, но мокаем
        # явно для устойчивости теста к будущим изменениям порогов классификации.
        with patch.object(cb, "_classify", return_value=("Dead", "Нет данных")):
            is_new = cb._upsert_ad(
                conn,
                _make_ad(),
                metrics={"spend": 10.0, "leads": 1, "hook_rate": 5.0, "hold_rate": 5.0},
                creative={"ad_body": "Текст", "image_url": None, "content_type": "video"},
                adset_map={"adset_1": ("CityA", "L2")},
            )
        conn.commit()
    finally:
        conn.close()

    assert is_new is False
    assert _fetch_business_class(kb) == "Прибыльный"


# ---------------------------------------------------------------------------
# Существующая строка + бэкфилл приносит осмысленное значение → перезаписывает
# ---------------------------------------------------------------------------


def test_backfill_overwrites_business_class_with_meaningful_value(kb):
    """Если бэкфилл (через _classify) приносит НЕ заглушку, а осмысленное значение
    (например появилось после ручного пересчёта) — CASE должен пропустить его в SET,
    т.е. новое значение перезаписывает старое."""
    conn = ci._get_connection()
    try:
        conn.execute(
            """
            INSERT INTO creative_kb (ad_id, ad_name, business_class, is_full_cabinet)
            VALUES ('ad-1', 'Старое имя', 'Убыточный', 1)
            """
        )
        conn.commit()
    finally:
        conn.close()

    conn = ci._get_connection()
    try:
        with patch.object(cb, "_classify", return_value=("Winner", "Окупается")):
            is_new = cb._upsert_ad(
                conn,
                _make_ad(),
                metrics={"spend": 10.0, "leads": 5, "hook_rate": 40.0, "hold_rate": 55.0},
                creative={"ad_body": "Текст", "image_url": None, "content_type": "video"},
                adset_map={"adset_1": ("CityA", "L2")},
            )
        conn.commit()
    finally:
        conn.close()

    assert is_new is False
    assert _fetch_business_class(kb) == "Окупается"


# ---------------------------------------------------------------------------
# Новая строка с 'Нет данных' → вставляется как есть
# ---------------------------------------------------------------------------


def test_backfill_inserts_new_row_with_no_data_as_is(kb):
    """Новой строки в БД нет — CASE в ON CONFLICT не участвует (это чистый INSERT),
    business_class 'Нет данных' пишется как есть."""
    conn = ci._get_connection()
    try:
        # Реальный _classify (без мока) — romi/qual_pct/payments всегда None
        # внутри _upsert_ad, поэтому classify_business закономерно отдаёт 'Нет данных'.
        is_new = cb._upsert_ad(
            conn,
            _make_ad(ad_id="ad-new"),
            metrics={"spend": 10.0, "leads": 1, "hook_rate": 5.0, "hold_rate": 5.0},
            creative={"ad_body": "Текст", "image_url": None, "content_type": "video"},
            adset_map={"adset_1": ("CityA", "L2")},
        )
        conn.commit()
    finally:
        conn.close()

    assert is_new is True
    assert _fetch_business_class(kb, "ad-new") == "Нет данных"


def test_backfill_inserts_new_row_with_meaningful_value_as_is(kb):
    """Новая строка + осмысленный business_class от _classify — тоже вставляется как есть
    (CASE не мешает INSERT-ветке, т.к. creative_kb.business_class ещё не существует)."""
    conn = ci._get_connection()
    try:
        with patch.object(cb, "_classify", return_value=("Winner", "Прибыльный")):
            is_new = cb._upsert_ad(
                conn,
                _make_ad(ad_id="ad-new2"),
                metrics={"spend": 500.0, "leads": 40, "hook_rate": 45.0, "hold_rate": 60.0},
                creative={"ad_body": "Текст", "image_url": None, "content_type": "video"},
                adset_map={"adset_1": ("CityA", "L2")},
            )
        conn.commit()
    finally:
        conn.close()

    assert is_new is True
    assert _fetch_business_class(kb, "ad-new2") == "Прибыльный"
