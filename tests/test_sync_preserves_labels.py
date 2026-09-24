"""
Тесты C1: sync_knowledge_base (services/creative_intelligence.py) больше не стирает
разметку/скоринг/AMO-исходы существующей строки при повторном синке (INSERT OR REPLACE
заменён на ON CONFLICT DO UPDATE только FB-полей).

Используют tmp_path — не трогают реальную БД data/decisions.db. Сеть не нужна:
enrich_creatives=False (дозагрузка ad_body/image_url через FB API — отдельный шаг,
не относится к задаче C1 и заблокирована глобальным pytest-socket).
"""

import json
import sqlite3

import pytest

from services import creative_intelligence as ci


# ---------------------------------------------------------------------------
# Фикстуры (по образцу tests/test_creative_intelligence.py)
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


@pytest.fixture
def fake_data_dir(tmp_path, monkeypatch):
    """Подменяет _DATA_DIR на tmp_path/fake_data — sync читает JSON-источники оттуда."""
    data_dir = tmp_path / "fake_data"
    data_dir.mkdir()
    monkeypatch.setattr(ci, "_DATA_DIR", data_dir)
    return data_dir


def _write_learner_results(fake_data_dir, rows):
    (fake_data_dir / "learner_results.json").write_text(
        json.dumps({"creative_table": rows}, ensure_ascii=False), encoding="utf-8"
    )


def _seed_labeled_row(kb):
    """Засеивает строку 'ad-1' со ВСЕЙ разметкой/скорингом/AMO-полями/исходами —
    имитирует объявление, которое уже прошло Gemini-лейблинг и AMO-сверку."""
    conn = sqlite3.connect(kb)
    try:
        conn.execute(
            """
            INSERT INTO creative_kb (
                ad_id, ad_name, city, adset_type, status,
                spend, leads, cpl, ctr, cpm,
                labeled_at, hook_type_id, angle_id, offer_type_id, label_source,
                created_at, outcomes_matched_at,
                qual_leads, payments, revenue, romi, cpql, qual_pct,
                is_full_cabinet, effective_status,
                vision_tags, image_url, ad_body,
                business_class
            ) VALUES (
                'ad-1', 'Старое имя', 'CityA', 'L2', 'ACTIVE',
                50.0, 5, 10.0, 1.5, 4.0,
                '2026-01-01T00:00:00', 3, 2, 1, 'gemini',
                '2025-12-01T10:00:00+00:00', '2026-01-05T00:00:00',
                4, 3, 450000.0, 300.0, 20.0, 25.0,
                1, 'ACTIVE',
                '{"has_person": true}', 'https://example.com/img.jpg', 'Старый текст объявления',
                'Прибыльный'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_row(kb, ad_id="ad-1"):
    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM creative_kb WHERE ad_id = ?", (ad_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# C1: happy path — разметка/AMO/created_at сохраняются, FB-метрики обновляются
# ---------------------------------------------------------------------------


def test_sync_preserves_labeling_and_amo_and_updates_fb_metrics(kb, fake_data_dir):
    """sync с новыми FB-метриками для уже размеченной строки: разметка/скоринг/
    AMO-поля/created_at/outcomes_matched_at сохраняются как есть, а FB-метрики
    (spend/leads/cpl/ctr/cpm/...) и creative_class обновляются пришедшими значениями."""
    _seed_labeled_row(kb)

    # Синк приносит новые FB-метрики для того же ad_id, но БЕЗ разметки/AMO-полей
    # (learner_results.json их не содержит вообще — синк не должен их трогать).
    _write_learner_results(fake_data_dir, [
        {
            "ad_id": "ad-1",
            "ad_name": "Новое имя из FB",
            "city": "CityA",
            "adset_type": "L2",
            "status": "ACTIVE",
            "spend": 200.0,
            "leads": 15,
            "cpl": 13.3,
            "ctr": 2.2,
            "cpm": 6.0,
            "impressions": 8000,
            "clicks": 176,
            "frequency": 1.8,
            "hook_rate": 40.0,
            "hold_rate": 55.0,
            "creative_class": "Winner",
            # vision/business_class/AMO-поля намеренно отсутствуют — синк их не приносит
        }
    ])

    result = ci.sync_knowledge_base(enrich_creatives=False)

    # Счётчики: 1 запись, это обновление (updated), не новая
    assert result["synced"] == 1
    assert result["new"] == 0
    assert result["updated"] == 1

    row = _fetch_row(kb)
    assert row is not None

    # --- Разметка/скоринг/AMO-исходы/даты — НЕ затёрты ---
    assert row["labeled_at"] == "2026-01-01T00:00:00"
    assert row["hook_type_id"] == 3
    assert row["angle_id"] == 2
    assert row["offer_type_id"] == 1
    assert row["label_source"] == "gemini"
    assert row["created_at"] == "2025-12-01T10:00:00+00:00"
    assert row["outcomes_matched_at"] == "2026-01-05T00:00:00"
    assert row["qual_leads"] == 4
    assert row["payments"] == 3
    assert row["revenue"] == 450000.0
    assert row["romi"] == 300.0
    assert row["cpql"] == 20.0
    assert row["is_full_cabinet"] == 1
    assert row["effective_status"] == "ACTIVE"
    assert row["image_url"] == "https://example.com/img.jpg"
    assert row["ad_body"] == "Старый текст объявления"
    assert row["business_class"] == "Прибыльный"
    # vision_tags: синк не принёс новый vision (нет в vision_analysis.json) — сохраняем старый
    assert row["vision_tags"] == '{"has_person": true}'

    # --- FB-метрики обновились пришедшими значениями ---
    assert row["ad_name"] == "Новое имя из FB"
    assert row["spend"] == 200.0
    assert row["leads"] == 15
    assert row["cpl"] == 13.3
    assert row["ctr"] == 2.2
    assert row["cpm"] == 6.0
    assert row["impressions"] == 8000
    assert row["clicks"] == 176
    assert row["frequency"] == 1.8
    assert row["hook_rate"] == 40.0
    assert row["hold_rate"] == 55.0
    assert row["creative_class"] == "Winner"


# ---------------------------------------------------------------------------
# C1: vision_tags COALESCE в обе стороны
# ---------------------------------------------------------------------------


def test_sync_preserves_vision_tags_when_not_in_source(kb, fake_data_dir):
    """Если vision_analysis.json не содержит запись для ad_id — существующий
    vision_tags НЕ затирается (COALESCE(excluded.vision_tags, creative_kb.vision_tags))."""
    _seed_labeled_row(kb)
    _write_learner_results(fake_data_dir, [
        {"ad_id": "ad-1", "ad_name": "X", "spend": 10.0, "leads": 1},
    ])
    # vision_analysis.json намеренно не создаём

    ci.sync_knowledge_base(enrich_creatives=False)

    row = _fetch_row(kb)
    assert row["vision_tags"] == '{"has_person": true}'


def test_sync_updates_vision_tags_when_source_has_new_data(kb, fake_data_dir):
    """Если vision_analysis.json содержит новый vision для ad_id — он перезаписывает
    существующий (COALESCE берёт excluded, если тот не NULL)."""
    _seed_labeled_row(kb)
    _write_learner_results(fake_data_dir, [
        {"ad_id": "ad-1", "ad_name": "X", "spend": 10.0, "leads": 1},
    ])
    new_vision = {"has_person": False, "emotion": "neutral"}
    (fake_data_dir / "vision_analysis.json").write_text(
        json.dumps({"ad-1": new_vision}, ensure_ascii=False), encoding="utf-8"
    )

    ci.sync_knowledge_base(enrich_creatives=False)

    row = _fetch_row(kb)
    assert json.loads(row["vision_tags"]) == new_vision


# ---------------------------------------------------------------------------
# C1: новая строка → AMO-поля NULL (не 0)
# ---------------------------------------------------------------------------


def test_sync_new_row_has_null_amo_fields_not_zero(kb, fake_data_dir):
    """Новая запись (нет в БД, нет обогащения из amo_data.json) → AMO-поля
    (payments/revenue/qual_leads) пишутся как NULL, а не 0 (честная семантика B1)."""
    _write_learner_results(fake_data_dir, [
        {
            "ad_id": "ad-new",
            "ad_name": "Совсем новое объявление",
            "city": "CityB",
            "adset_type": "L1",
            "status": "ACTIVE",
            "spend": 30.0,
            "leads": 3,
        }
    ])
    # amo_data.json намеренно отсутствует — обогащения нет

    result = ci.sync_knowledge_base(enrich_creatives=False)

    assert result["synced"] == 1
    assert result["new"] == 1
    assert result["updated"] == 0

    row = _fetch_row(kb, "ad-new")
    assert row is not None
    assert row["payments"] is None
    assert row["revenue"] is None
    assert row["qual_leads"] is None
    # outcomes_matched_at на новой строке синк не ставит вообще (не входит в INSERT-колонки)
    assert row["outcomes_matched_at"] is None


# ---------------------------------------------------------------------------
# C1: синк по-прежнему обновляет FB-метрики; счётчики synced/new/updated корректны
# ---------------------------------------------------------------------------


def test_sync_still_updates_fb_metrics_and_counters(kb, fake_data_dir):
    """Смешанный синк: одна новая запись + одна обновляемая — счётчики считают верно,
    FB-метрики обеих записей на месте после синка."""
    _seed_labeled_row(kb)
    _write_learner_results(fake_data_dir, [
        {"ad_id": "ad-1", "ad_name": "Обновлено", "spend": 99.0, "leads": 9},
        {"ad_id": "ad-2", "ad_name": "Новая запись", "spend": 5.0, "leads": 1},
    ])

    result = ci.sync_knowledge_base(enrich_creatives=False)

    assert result["synced"] == 2
    assert result["new"] == 1
    assert result["updated"] == 1

    row1 = _fetch_row(kb, "ad-1")
    row2 = _fetch_row(kb, "ad-2")
    assert row1["spend"] == 99.0
    assert row1["leads"] == 9
    assert row2["spend"] == 5.0
    assert row2["leads"] == 1
    # Разметка первой записи не пострадала
    assert row1["labeled_at"] == "2026-01-01T00:00:00"
