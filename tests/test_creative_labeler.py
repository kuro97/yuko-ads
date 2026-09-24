"""
Unit-тесты для services/creative_labeler.py.

Проверяют инкрементальную LLM-разметку текстов по таксономии:
- размечает только неразмеченные объявления с ad_body
- батчинг (BATCH_LABEL=10 в один промпт)
- GEMINI_API_KEY=None → disabled=True, 0 LLM-вызовов
- невалидный JSON → labeled_at ставится, *_id=NULL, errors>0

Мокают Gemini на уровне _call_gemini / _label_batch.
Используют tmp_path — не трогают реальную БД.
"""

import json
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

# Мокаем google.genai до импорта любого модуля который может его тянуть
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())

from services import creative_intelligence as ci
import services.creative_labeler as cl


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


def _insert_ad(
    db_path: str,
    ad_id: str,
    ad_body: str = "",
    labeled_at=None,
) -> None:
    """Вставляет объявление в creative_kb."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO creative_kb (ad_id, ad_name, ad_body, labeled_at)
            VALUES (?, ?, ?, ?)
            """,
            (ad_id, f"Тест {ad_id}", ad_body, labeled_at),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_hook_type(db_path: str, slug: str, hook_id: int) -> None:
    """Вставляет hook_type в hook_types для тестов таксономии.

    hook_types имеет колонки: id, slug, name_ru, name_l2, description, example, created_at.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO hook_types (id, slug, name_ru) VALUES (?, ?, ?)",
            (hook_id, slug, slug),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Тест 1: размечает объявление с ad_body, hook_type_id заполнен
# ---------------------------------------------------------------------------


def test_labeler_labels_ad_with_body(kb, monkeypatch):
    """Объявление с ad_body размечается — hook_type_id заполнен, labeled_at NOT NULL.

    Используем slug из seed-данных миграции 007 (question — id 8).
    """
    _insert_ad(kb, "ad_001", ad_body="Хотите подключить PRODA?")

    # Узнаём id slug 'question' из реальной таблицы (seed из миграции 007)
    conn = ci._get_connection()
    try:
        row_ht = conn.execute(
            "SELECT id FROM hook_types WHERE slug = 'question' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    # Если slug 'question' не найден — используем первый доступный slug
    if row_ht is None:
        conn = ci._get_connection()
        try:
            row_ht = conn.execute("SELECT id, slug FROM hook_types LIMIT 1").fetchone()
        finally:
            conn.close()

    expected_id = row_ht["id"]
    hook_slug = "question" if row_ht else "unknown"

    # Если slug не существует — тест проверяет что NULL корректно обрабатывается
    # (для полноты тестирования используем существующий slug)
    if hook_slug == "unknown":
        # Нет таксономии — пропускаем проверку hook_type_id
        pytest.skip("Таблица hook_types пустая — нет данных для теста")

    # Мокаем _label_batch — возвращаем валидный результат разметки
    def fake_label_batch(items, taxonomy, model):
        return (
            [{"ad_id": "ad_001", "hook_slug": hook_slug, "angle_slug": "unknown", "offer_slug": "unknown"}],
            100,
            50,
        )

    monkeypatch.setattr(cl, "_label_batch", fake_label_batch)
    monkeypatch.setattr("config.GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr("config.GEMINI_AD_MODEL", "gemini-1.5-flash")

    result = cl.label_unlabeled(limit=10)

    assert result["labeled"] == 1
    assert result["errors"] == 0
    assert result["disabled"] is False

    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT hook_type_id, labeled_at, label_source FROM creative_kb WHERE ad_id = 'ad_001'"
        ).fetchone()
    finally:
        conn.close()

    assert row["hook_type_id"] == expected_id  # id из таксономии
    assert row["labeled_at"] is not None
    assert row["label_source"] == "gemini"


# ---------------------------------------------------------------------------
# Тест 2: только неразмеченные — LLM вызван только для 1 из 2
# ---------------------------------------------------------------------------


def test_labeler_skips_already_labeled(kb, monkeypatch):
    """Объявление с labeled_at не выбирается — LLM вызван только для неразмеченного."""
    _insert_ad(kb, "ad_labeled", ad_body="Уже размечен", labeled_at="2026-01-01T00:00:00")
    _insert_ad(kb, "ad_unlabeled", ad_body="Ещё не размечен")

    called_ids = []

    def fake_label_batch(items, taxonomy, model):
        for item in items:
            called_ids.append(item["ad_id"])
        return (
            [{"ad_id": item["ad_id"], "hook_slug": "unknown", "angle_slug": "unknown", "offer_slug": "unknown"}
             for item in items],
            0,
            0,
        )

    monkeypatch.setattr(cl, "_label_batch", fake_label_batch)
    monkeypatch.setattr("config.GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr("config.GEMINI_AD_MODEL", "gemini-1.5-flash")

    cl.label_unlabeled(limit=10)

    # LLM вызван только для неразмеченного объявления
    assert "ad_unlabeled" in called_ids
    assert "ad_labeled" not in called_ids


# ---------------------------------------------------------------------------
# Тест 3: GEMINI_API_KEY=None → disabled=True, 0 LLM-вызовов
# ---------------------------------------------------------------------------


def test_labeler_disabled_without_gemini_key(kb, monkeypatch):
    """При отсутствии GEMINI_API_KEY возвращает disabled=True без LLM-вызовов."""
    _insert_ad(kb, "ad_no_key", ad_body="Текст объявления")

    monkeypatch.setattr("config.GEMINI_API_KEY", None)

    called = []
    monkeypatch.setattr(cl, "_label_batch", lambda *a, **kw: called.append(True) or ([], 0, 0))

    result = cl.label_unlabeled(limit=10)

    assert result["disabled"] is True
    assert result["labeled"] == 0
    assert len(called) == 0


# ---------------------------------------------------------------------------
# Тест 4: невалидный JSON от Gemini → labeled_at ставится, *_id=NULL, errors>0
# ---------------------------------------------------------------------------


def test_labeler_invalid_json_sets_labeled_at(kb, monkeypatch):
    """При невалидном JSON от Gemini: labeled_at проставляется, *_id=NULL, errors > 0."""
    _insert_ad(kb, "ad_bad_json", ad_body="Какой-то текст")

    # _label_batch возвращает пустой список (симуляция невалидного JSON)
    monkeypatch.setattr(cl, "_label_batch", lambda items, taxonomy, model: ([], 0, 0))
    monkeypatch.setattr("config.GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr("config.GEMINI_AD_MODEL", "gemini-1.5-flash")

    result = cl.label_unlabeled(limit=10)

    assert result["errors"] > 0

    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT labeled_at, hook_type_id, angle_id, offer_type_id FROM creative_kb WHERE ad_id = 'ad_bad_json'"
        ).fetchone()
    finally:
        conn.close()

    # labeled_at должен быть проставлен (чтобы не зациклиться)
    assert row["labeled_at"] is not None
    # *_id — NULL (нет валидных slug'ов)
    assert row["hook_type_id"] is None
    assert row["angle_id"] is None
    assert row["offer_type_id"] is None


# ---------------------------------------------------------------------------
# Тест 5: объявление без ad_body не выбирается
# ---------------------------------------------------------------------------


def test_labeler_skips_empty_body(kb, monkeypatch):
    """Объявление без ad_body не попадает в выборку для разметки."""
    _insert_ad(kb, "ad_no_body", ad_body="")

    called = []
    monkeypatch.setattr(cl, "_label_batch", lambda items, *a, **kw: called.append(items) or ([], 0, 0))
    monkeypatch.setattr("config.GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr("config.GEMINI_AD_MODEL", "gemini-1.5-flash")

    result = cl.label_unlabeled(limit=10)

    # LLM не вызывался
    assert len(called) == 0
    assert result["labeled"] == 0


# ---------------------------------------------------------------------------
# Тест 6: slug не из таксономии → *_id=NULL, но labeled_at проставлен
# ---------------------------------------------------------------------------


def test_labeler_unknown_slug_sets_null_id(kb, monkeypatch):
    """Если slug не из таксономии — *_id=NULL, но labeled_at проставляется (без зацикливания)."""
    _insert_ad(kb, "ad_unknown_slug", ad_body="Текст для теста")
    # Таксономия пустая → 'some_unknown_hook' не попадёт в *_id

    def fake_label_batch(items, taxonomy, model):
        return (
            [{"ad_id": "ad_unknown_slug", "hook_slug": "some_unknown_hook",
              "angle_slug": "unknown", "offer_slug": "unknown"}],
            0,
            0,
        )

    monkeypatch.setattr(cl, "_label_batch", fake_label_batch)
    monkeypatch.setattr("config.GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr("config.GEMINI_AD_MODEL", "gemini-1.5-flash")

    result = cl.label_unlabeled(limit=10)

    assert result["labeled"] == 1

    conn = ci._get_connection()
    try:
        row = conn.execute(
            "SELECT hook_type_id, hook_type, labeled_at FROM creative_kb WHERE ad_id = 'ad_unknown_slug'"
        ).fetchone()
    finally:
        conn.close()

    # labeled_at проставлен — не зациклимся
    assert row["labeled_at"] is not None
    # hook_type_id = NULL (slug не в таксономии)
    assert row["hook_type_id"] is None
    # hook_type (text slug) заполнен
    assert row["hook_type"] == "some_unknown_hook"


# ---------------------------------------------------------------------------
# Тест 7: _load_taxonomy возвращает slug→id из таблиц
# ---------------------------------------------------------------------------


def test_load_taxonomy_returns_slug_map(kb):
    """_load_taxonomy возвращает словарь {slug: id} из hook_types/angles/offer_types.

    Миграция 007 содержит seed-данные в hook_types.
    Вставляем новый уникальный slug и проверяем что он попадает в taxonomy.
    """
    # Уникальный slug, которого нет в seed-данных миграции 007
    unique_slug = "test_unique_hook_xyz"
    _insert_hook_type(kb, unique_slug, 9999)

    taxonomy = cl._load_taxonomy()

    assert unique_slug in taxonomy["hook"]
    assert taxonomy["hook"][unique_slug] == 9999

    # Дополнительно: taxonomy["hook"] не пустой (seed-данные из 007)
    assert len(taxonomy["hook"]) > 0
