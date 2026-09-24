"""
Тесты для services/creative_intelligence.py.

Используют tmp_path — не трогают реальную БД data/decisions.db.
"""

import json
import sqlite3

import pytest

from services import creative_intelligence as ci


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


@pytest.fixture
def fake_data_dir(tmp_path, monkeypatch):
    """Подменяет _DATA_DIR на tmp_path/fake_data с минимальными JSON-файлами."""
    data_dir = tmp_path / "fake_data"
    data_dir.mkdir()
    monkeypatch.setattr(ci, "_DATA_DIR", data_dir)
    return data_dir


@pytest.fixture
def learner_3_ads(fake_data_dir):
    """Записывает learner_results.json с 3 тестовыми объявлениями."""
    data = {
        "creative_table": [
            {
                "ad_id": "1001",
                "ad_name": "Winner Ad CityA",
                "city": "CityA",
                "adset_type": "L2",
                "status": "ACTIVE",
                "spend": 100.0,
                "leads": 10,
                "cpl": 10.0,
                "ctr": 2.0,
                "cpm": 5.0,
                "frequency": 1.5,
                "hook_rate": 35.0,
                "hold_rate": 50.0,
                "creative_class": "Winner",
                "business_class": "Прибыльный",
                "impressions": 5000,
                "clicks": 100,
                "video_views_3s": 1750,
                "thruplay": 875,
                "video_p25": 700,
                "video_p50": 500,
                "video_p75": 300,
                "video_p100": 100,
                "days_running": 14,
                "qual_pct": 25.0,
                "romi": 300.0,
                "payments": 3,
                "cpql": 33.0,
                "revenue": 450000.0,
                "qual_leads": 3,
            },
            {
                "ad_id": "1002",
                "ad_name": "Dead Ad CityB",
                "city": "CityB",
                "adset_type": "L1",
                "status": "PAUSED",
                "spend": 200.0,
                "leads": 2,
                "cpl": 100.0,
                "ctr": 0.3,
                "cpm": 8.0,
                "frequency": 4.5,
                "hook_rate": 12.0,
                "hold_rate": 30.0,
                "creative_class": "Dead",
                "business_class": "Убыточный",
                "impressions": 3000,
                "clicks": 9,
                "video_views_3s": 360,
                "thruplay": 108,
                "video_p25": 80,
                "video_p50": 50,
                "video_p75": 20,
                "video_p100": 5,
                "days_running": 30,
                "qual_pct": 5.0,
                "romi": 50.0,
                "payments": 0,
                "cpql": None,
                "revenue": 0.0,
                "qual_leads": 0,
            },
            {
                "ad_id": "1003",
                "ad_name": "Hidden Gem CityA",
                "city": "CityA",
                "adset_type": "L2",
                "status": "ACTIVE",
                "spend": 50.0,
                "leads": 8,
                "cpl": 6.25,
                "ctr": 1.8,
                "cpm": 4.0,
                "frequency": 2.0,
                "hook_rate": 40.0,
                "hold_rate": 55.0,
                "creative_class": "Hidden Gem",
                "business_class": "Перспективный",
                "impressions": 1500,
                "clicks": 27,
                "video_views_3s": 600,
                "thruplay": 330,
                "video_p25": 250,
                "video_p50": 180,
                "video_p75": 100,
                "video_p100": 40,
                "days_running": 7,
                "qual_pct": None,
                "romi": None,
                "payments": 0,
                "cpql": None,
                "revenue": 0.0,
                "qual_leads": 0,
            },
        ]
    }
    (fake_data_dir / "learner_results.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return data


@pytest.fixture
def amo_enrichment(fake_data_dir):
    """Записывает amo_data.json с обогащением для ad_id 1001."""
    amo = {
        "1001": {
            "qual_pct": 30.0,
            "romi": 350.0,
            "payments": 5,
            "cpql": 20.0,
            "revenue": 750000.0,
            "qual_leads": 5,
        }
    }
    (fake_data_dir / "amo_data.json").write_text(
        json.dumps(amo, ensure_ascii=False), encoding="utf-8"
    )
    return amo


@pytest.fixture
def vision_enrichment(fake_data_dir):
    """Записывает vision_analysis.json для ad_id 1001."""
    vision = {
        "1001": {
            "first_frame_type": "person_talking",
            "has_person": True,
            "has_subtitles": True,
            "emotion": "positive",
            "text_overlay": "PRODA 2025",
            "hook_description": "Эксперт смотрит в камеру",
            "summary": "Короткое объяснение, с чего начать",
        }
    }
    (fake_data_dir / "vision_analysis.json").write_text(
        json.dumps(vision, ensure_ascii=False), encoding="utf-8"
    )
    return vision


def _make_active_ad(**kwargs):
    """Базовое «здоровое» объявление — можно переопределять поля."""
    base = {
        "ad_id": "9999",
        "ad_name": "Test Ad",
        "city": "CityA",
        "adset_type": "L2",
        "status": "ACTIVE",
        "spend": 50.0,
        "leads": 10,
        "cpl": 5.0,
        "ctr": 2.0,
        "cpm": 5.0,
        "frequency": 1.5,
        "hook_rate": 35.0,
        "hold_rate": 55.0,
        "impressions": 1000,
        "qual_pct": 25.0,
        "creative_class": "Winner",
        "vision_tags": None,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Блок 1: Knowledge Base init + sync (5 тестов)
# ---------------------------------------------------------------------------


def test_init_kb_creates_table(tmp_path):
    """init_kb создаёт файл SQLite с таблицей creative_kb."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)

    conn = sqlite3.connect(db_path)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='creative_kb'"
        ).fetchall()
        assert len(tables) == 1, "Таблица creative_kb должна существовать"

        # Проверяем что можно вставить строку
        conn.execute(
            "INSERT INTO creative_kb (ad_id, ad_name) VALUES ('test', 'Test Ad')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT ad_name FROM creative_kb WHERE ad_id = 'test'"
        ).fetchone()
        assert row[0] == "Test Ad"
    finally:
        conn.close()


def test_sync_from_learner_only(kb, learner_3_ads, fake_data_dir):
    """sync_knowledge_base читает learner_results.json и вставляет 3 новых записи."""
    result = ci.sync_knowledge_base()

    assert result["synced"] == 3
    assert result["new"] == 3
    assert result["updated"] == 0
    assert result["sources"]["learner"] == 3


def test_sync_idempotent(kb, learner_3_ads, fake_data_dir):
    """Повторный sync не создаёт дубликатов — обновляет существующие (UPSERT)."""
    ci.sync_knowledge_base()
    result = ci.sync_knowledge_base()

    assert result["synced"] == 3
    assert result["new"] == 0
    assert result["updated"] == 3

    # Убеждаемся что в БД ровно 3 записи, а не 6
    conn = sqlite3.connect(kb)
    try:
        count = conn.execute("SELECT COUNT(*) FROM creative_kb").fetchone()[0]
        assert count == 3
    finally:
        conn.close()


def test_sync_with_amo_enrichment(kb, learner_3_ads, amo_enrichment, fake_data_dir):
    """sync обогащает записи AMO-данными (qual_pct, romi, payments)."""
    result = ci.sync_knowledge_base()

    assert result["sources"]["amo"] == 1

    conn = sqlite3.connect(kb)
    try:
        row = conn.execute(
            "SELECT qual_pct, romi, payments FROM creative_kb WHERE ad_id = '1001'"
        ).fetchone()
    finally:
        conn.close()

    # AMO перезаписывает данные из learner (30.0 > 25.0 из learner)
    assert row[0] == 30.0
    assert row[1] == 350.0
    assert row[2] == 5


def test_sync_no_learner_file(kb, fake_data_dir):
    """Если learner_results.json не существует — возвращает synced=0 без ошибки."""
    result = ci.sync_knowledge_base()

    assert result["synced"] == 0
    assert result["new"] == 0
    assert result["updated"] == 0


# ---------------------------------------------------------------------------
# Блок 2: get_kb_creatives — фильтры (3 теста)
# ---------------------------------------------------------------------------


def test_get_kb_filter_by_class(kb, learner_3_ads, fake_data_dir):
    """Фильтр creative_class='Winner' возвращает только Winners."""
    ci.sync_knowledge_base()

    winners = ci.get_kb_creatives(creative_class="Winner")

    assert len(winners) == 1
    assert winners[0]["ad_id"] == "1001"
    assert winners[0]["creative_class"] == "Winner"


def test_get_kb_filter_by_city(kb, learner_3_ads, fake_data_dir):
    """Фильтр по городу возвращает только записи из указанного города."""
    ci.sync_knowledge_base()

    citya_ads = ci.get_kb_creatives(city="CityA")

    assert len(citya_ads) == 2
    for ad in citya_ads:
        assert ad["city"] == "CityA"


def test_get_kb_limit_cap_500(kb, learner_3_ads, fake_data_dir):
    """limit=999 принудительно обрезается до 500 — не возвращает больше чем в БД."""
    ci.sync_knowledge_base()

    # У нас 3 записи — запрашиваем 999, должно вернуть <= 500 (в данном случае 3)
    result = ci.get_kb_creatives(limit=999)
    assert len(result) == 3  # в БД только 3 записи


# ---------------------------------------------------------------------------
# Блок 3: diagnose_creative — каскад (6 тестов)
# ---------------------------------------------------------------------------


def test_diagnose_insufficient_data():
    """spend < 5 и impressions < 500 → level='insufficient_data'."""
    ad = _make_active_ad(spend=2.0, impressions=100)
    result = ci.diagnose_creative(ad)

    assert result["level"] == "insufficient_data"
    assert "insufficient_data" in result["level"]


def test_diagnose_weak_hook():
    """hook_rate=15% (< 25%) → level='hook', первая проблема в каскаде."""
    ad = _make_active_ad(
        spend=50.0,
        impressions=2000,
        hook_rate=15.0,
        hold_rate=50.0,
        ctr=2.0,
        frequency=1.5,
        qual_pct=25.0,
    )
    result = ci.diagnose_creative(ad)

    assert result["level"] == "hook"
    assert "хук" in result["message"].lower() or "hook" in result["message"].lower()
    assert "15" in result["message"]


def test_diagnose_weak_hold():
    """hook_rate=35%, hold_rate=20% (< 40%) → level='hold'."""
    ad = _make_active_ad(
        spend=50.0,
        impressions=2000,
        hook_rate=35.0,
        hold_rate=20.0,
        ctr=2.0,
        frequency=1.5,
        qual_pct=25.0,
    )
    result = ci.diagnose_creative(ad)

    assert result["level"] == "hold"
    assert "20" in result["message"]


def test_diagnose_weak_ctr():
    """hook=35%, hold=55%, ctr=0.5% (< 1%) → level='ctr'."""
    ad = _make_active_ad(
        spend=50.0,
        impressions=2000,
        hook_rate=35.0,
        hold_rate=55.0,
        ctr=0.5,
        frequency=1.5,
        qual_pct=25.0,
    )
    result = ci.diagnose_creative(ad)

    assert result["level"] == "ctr"
    assert "0.5" in result["message"]


def test_diagnose_fatigue():
    """frequency=4.0 и ctr=1.0 (< 1.5 при frequency > 3) → level='fatigue'."""
    ad = _make_active_ad(
        spend=50.0,
        impressions=2000,
        hook_rate=35.0,
        hold_rate=55.0,
        ctr=1.0,
        frequency=4.0,
        qual_pct=25.0,
    )
    result = ci.diagnose_creative(ad)

    assert result["level"] == "fatigue"
    assert "4" in result["message"]


def test_diagnose_healthy():
    """Все метрики хорошие → level='healthy'."""
    ad = _make_active_ad(
        spend=50.0,
        impressions=2000,
        hook_rate=40.0,
        hold_rate=60.0,
        ctr=2.5,
        frequency=1.5,
        qual_pct=30.0,
    )
    result = ci.diagnose_creative(ad)

    assert result["level"] == "healthy"
    assert "recommendation" in result
    assert result["recommendation"]


# ---------------------------------------------------------------------------
# Блок 4: diagnose_all (2 теста)
# ---------------------------------------------------------------------------


def test_diagnose_all_returns_by_level(kb, learner_3_ads, fake_data_dir):
    """diagnose_all возвращает by_level dict и total == кол-во ACTIVE объявлений."""
    ci.sync_knowledge_base()

    # В данных: 1001 ACTIVE (Winner=healthy), 1002 PAUSED (skip), 1003 ACTIVE (Hidden Gem=healthy)
    result = ci.diagnose_all()

    assert result["total"] == 2  # только ACTIVE
    assert "by_level" in result
    assert isinstance(result["by_level"], dict)
    assert "diagnoses" in result
    assert len(result["diagnoses"]) == 2

    # Каждый диагноз содержит необходимые поля
    for d in result["diagnoses"]:
        assert "ad_id" in d
        assert "level" in d
        assert "message" in d
        assert "recommendation" in d


def test_diagnose_all_saves_to_db(kb, learner_3_ads, fake_data_dir):
    """diagnose_all сохраняет diagnosis_level в БД после диагностики."""
    ci.sync_knowledge_base()
    ci.diagnose_all()

    conn = sqlite3.connect(kb)
    try:
        # 1001 — ACTIVE, должен иметь diagnosis_level
        row = conn.execute(
            "SELECT diagnosis_level, diagnosed_at FROM creative_kb WHERE ad_id = '1001'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] is not None  # diagnosis_level заполнен
    assert row[1] is not None  # diagnosed_at заполнен


# ---------------------------------------------------------------------------
# Блок 5: find_similar_winners (3 теста)
# ---------------------------------------------------------------------------


def test_find_similar_winners_no_winners(kb, fake_data_dir):
    """Если в KB нет Winners → возвращает []."""
    # Вставляем только Dead объявление
    conn = sqlite3.connect(kb)
    try:
        conn.execute(
            """INSERT INTO creative_kb (ad_id, ad_name, creative_class, city, adset_type,
               spend, cpl, ctr, hook_rate, hold_rate)
               VALUES ('dead1', 'Dead Ad', 'Dead', 'CityA', 'L2', 100, 50, 0.5, 12, 25)"""
        )
        conn.commit()
    finally:
        conn.close()

    result = ci.find_similar_winners("dead1", top_n=3)

    assert result == []


def test_find_similar_winners_returns_winners(kb, learner_3_ads, fake_data_dir):
    """Если в KB есть Winner → find_similar_winners возвращает их."""
    ci.sync_knowledge_base()

    # 1003 — Hidden Gem, ищем похожих Winners
    result = ci.find_similar_winners("1003", top_n=3)

    assert isinstance(result, list)
    assert len(result) >= 1
    assert result[0]["creative_class"] == "Winner"
    assert "ad_id" in result[0]
    assert "ad_name" in result[0]


def test_find_similar_winners_with_vision_tags(kb, learner_3_ads, vision_enrichment, fake_data_dir):
    """find_similar_winners учитывает vision_tags при наличии и возвращает similarity."""
    ci.sync_knowledge_base()

    # ad 1001 — Winner с vision_tags, ищем похожие для 1003
    # У 1001 есть vision_tags, у 1003 нет → similarity будет None
    result = ci.find_similar_winners("1003", top_n=3)

    assert isinstance(result, list)
    # similarity может быть None если у target нет vision_tags
    for item in result:
        assert "similarity" in item
        assert "what_differs" in item


# ---------------------------------------------------------------------------
# Блок 6: дополнительные edge cases
# ---------------------------------------------------------------------------


def test_init_kb_returns_path(tmp_path):
    """init_kb возвращает строку — путь к созданному файлу."""
    db_path = str(tmp_path / "test.db")
    returned = ci.init_kb(db_path)

    assert returned == db_path
    assert isinstance(returned, str)


def test_sync_vision_enrichment(kb, learner_3_ads, vision_enrichment, fake_data_dir):
    """sync обогащает записи vision_tags из vision_analysis.json."""
    result = ci.sync_knowledge_base()

    assert result["sources"]["vision"] == 1

    conn = sqlite3.connect(kb)
    try:
        row = conn.execute(
            "SELECT vision_tags FROM creative_kb WHERE ad_id = '1001'"
        ).fetchone()
    finally:
        conn.close()

    assert row[0] is not None
    tags = json.loads(row[0])
    assert tags["first_frame_type"] == "person_talking"
    assert tags["has_person"] is True


def test_diagnose_creative_returns_metrics():
    """diagnose_creative всегда возвращает поле metrics с нужными ключами."""
    ad = _make_active_ad(spend=50.0, impressions=2000)
    result = ci.diagnose_creative(ad)

    assert "metrics" in result
    metrics = result["metrics"]
    assert "hook_rate" in metrics
    assert "hold_rate" in metrics
    assert "ctr" in metrics
    assert "frequency" in metrics
    assert "impressions" in metrics


def test_get_kb_returns_empty_when_db_empty(kb):
    """get_kb_creatives на пустой БД возвращает []."""
    result = ci.get_kb_creatives()

    assert result == []
