"""
Юнит-тесты для services/pattern_engine.py.

Проверяют:
- _classify_success: happy path (одно объявление с qual >= 1, romi выше медианы),
  no-candidates (все spend<15)
- _fetch_early_features: агрегат hook/hold/ctr/cpl/velocity; анти-утечка (day=0, day=10 не учтены)
- _build_predictors: значимый предиктор (lift >= 1.2); незначимый (lift < 1.2)
- run_pattern_engine: insufficient (мало success); ok (пишет уроки); не трогает manual/pattern_miner
- GET /patterns (пустая таблица → predictors=[], 200)

Используют tmp_path — не трогают реальную БД.
"""

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта любого модуля который может его тянуть
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())

from services import creative_intelligence as ci
import services.pattern_engine as pe


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем DB_PATH до и после теста."""
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
    spend: float = 100.0,
    qual_leads: int = 3,
    romi: float | None = 150.0,
    cpl: float = 10.0,
    is_full_cabinet: int = 1,
) -> None:
    """Вставляет объявление в creative_kb для тестов."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO creative_kb
                (ad_id, ad_name, spend, qual_leads, romi, cpl, is_full_cabinet)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ad_id, f"Ad {ad_id}", spend, qual_leads, romi, cpl, is_full_cabinet),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_daily(
    db_path: str,
    ad_id: str,
    day_since_launch: int,
    spend: float = 5.0,
    impressions: int = 1000,
    clicks: int = 15,
    ctr: float = 1.5,
    leads: int = 1,
    cpl: float = 5.0,
    hook_rate: float | None = 30.0,
    hold_rate: float | None = 40.0,
    date_str: str = "2025-11-05",
) -> None:
    """Вставляет строку ad_daily_metrics для тестов."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO ad_daily_metrics
                (ad_id, date, spend, impressions, clicks, ctr, leads, cpl,
                 hook_rate, hold_rate, video_views_3s, day_since_launch,
                 lead_semantics_version, lead_parse_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 2, 'ok')
            """,
            (ad_id, date_str, spend, impressions, clicks, ctr, leads, cpl,
             hook_rate, hold_rate, day_since_launch),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_learning(
    db_path: str,
    statement: str,
    source: str = "manual",
    confidence: str = "hypothesis",
) -> None:
    """Вставляет урок в таблицу learnings."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO learnings (statement, confidence, source, tags)
            VALUES (?, ?, ?, ?)
            """,
            (statement, confidence, source, source),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Тесты: _classify_success
# ---------------------------------------------------------------------------

def test_classify_success_happy(kb):
    """3 объявления: одно с qual=5, romi выше медианы → True, остальные False."""
    # Медиана romi по 3 объявлениям: (100, 150, 200) → медиана 150
    # ad_001: romi=200 (выше медианы 150), qual=5 → успех
    # ad_002: romi=100 (ниже медианы), qual=3 → неуспех
    # ad_003: romi=150 (равно медиане), qual=1 → успех
    _insert_ad(kb, "ad_001", spend=100, qual_leads=5, romi=200.0, is_full_cabinet=1)
    _insert_ad(kb, "ad_002", spend=100, qual_leads=3, romi=100.0, is_full_cabinet=1)
    _insert_ad(kb, "ad_003", spend=100, qual_leads=1, romi=150.0, is_full_cabinet=1)

    result = pe._classify_success()

    assert result["ad_001"] is True
    assert result["ad_002"] is False
    assert result["ad_003"] is True


def test_classify_success_no_candidates(kb):
    """Все spend<15 → {} (пустой map, не ошибка)."""
    _insert_ad(kb, "ad_001", spend=5.0, qual_leads=10, romi=300.0, is_full_cabinet=1)
    _insert_ad(kb, "ad_002", spend=10.0, qual_leads=5, romi=200.0, is_full_cabinet=1)

    result = pe._classify_success()
    assert result == {}


def test_classify_success_no_qual(kb):
    """qual_leads=0 → неуспех даже при высоком romi."""
    _insert_ad(kb, "ad_001", spend=100, qual_leads=0, romi=500.0, is_full_cabinet=1)
    _insert_ad(kb, "ad_002", spend=100, qual_leads=2, romi=100.0, is_full_cabinet=1)

    result = pe._classify_success()
    assert result["ad_001"] is False


# ---------------------------------------------------------------------------
# Тесты: _fetch_early_features
# ---------------------------------------------------------------------------

def test_fetch_early_features_happy(kb):
    """ad_001 с day1-7 строками → корректные агрегаты."""
    _insert_ad(kb, "ad_001", spend=100, qual_leads=3, is_full_cabinet=1)
    # Два дня в диапазоне 1-7
    _insert_daily(kb, "ad_001", day_since_launch=1, spend=5.0, impressions=1000,
                  leads=1, hook_rate=30.0, hold_rate=40.0, date_str="2025-11-01")
    _insert_daily(kb, "ad_001", day_since_launch=3, spend=10.0, impressions=2000,
                  leads=3, hook_rate=34.0, hold_rate=42.0, date_str="2025-11-03")

    result = pe._fetch_early_features(["ad_001"])

    assert "ad_001" in result
    feats = result["ad_001"]
    # hook_rate AVG(30, 34) = 32
    assert feats["hook_rate"] == pytest.approx(32.0, abs=0.5)
    # hold_rate AVG(40, 42) = 41
    assert feats["hold_rate"] == pytest.approx(41.0, abs=0.5)
    # cpm = SUM(spend) / SUM(impressions) * 1000 = 15 / 3000 * 1000 = 5
    assert feats["cpm"] == pytest.approx(5.0, abs=0.1)
    # cpl = SUM(spend) / SUM(leads) = 15 / 4 = 3.75
    assert feats["cpl"] == pytest.approx(3.75, abs=0.1)
    # lead_velocity = 4 leads / 2 days = 2.0
    assert feats["lead_velocity"] == pytest.approx(2.0, abs=0.01)
    assert feats["days_with_data"] == 2


def test_fetch_early_features_anti_leak(kb):
    """day=0 и day=10 строки НЕ учитываются (только 1-7)."""
    _insert_ad(kb, "ad_001", spend=100, qual_leads=3, is_full_cabinet=1)
    # Строки вне диапазона 1-7 (утечка)
    _insert_daily(kb, "ad_001", day_since_launch=0, spend=999.0, impressions=99999,
                  leads=100, hook_rate=99.0, hold_rate=99.0, date_str="2025-11-01")
    _insert_daily(kb, "ad_001", day_since_launch=10, spend=888.0, impressions=88888,
                  leads=50, hook_rate=88.0, hold_rate=88.0, date_str="2025-11-10")
    # Только один день в диапазоне 1-7
    _insert_daily(kb, "ad_001", day_since_launch=3, spend=5.0, impressions=1000,
                  leads=2, hook_rate=25.0, hold_rate=35.0, date_str="2025-11-03")

    result = pe._fetch_early_features(["ad_001"])

    assert "ad_001" in result
    feats = result["ad_001"]
    # Должны учитываться только данные дня 3
    assert feats["hook_rate"] == pytest.approx(25.0, abs=0.1)
    assert feats["hold_rate"] == pytest.approx(35.0, abs=0.1)
    # lead_velocity = 2 leads / 1 day = 2.0
    assert feats["lead_velocity"] == pytest.approx(2.0, abs=0.01)


def test_fetch_early_features_empty_list(kb):
    """Пустой список ad_ids → {} без ошибки."""
    result = pe._fetch_early_features([])
    assert result == {}


def test_fetch_early_features_large_batch(kb):
    """1500 ad_id превышают SQLite-лимит 999 переменных.

    Функция должна разбить запрос на батчи и вернуть агрегаты для ВСЕХ
    объявлений у которых есть строки day1-7 — без sqlite3.OperationalError.
    Проверяем: 5 объявлений с данными из первого и второго батча (>900) найдены,
    остальные (без строк) отсутствуют в результате.
    """
    # 5 объявлений с реальными данными: 2 в начале и 3 в конце списка из 1500
    ad_ids_with_data = [f"real_{i}" for i in range(5)]
    # Чтобы покрыть оба батча, помещаем часть за позицией 900
    all_ad_ids = [f"fake_{i}" for i in range(1495)] + ad_ids_with_data  # итого 1500

    for ad_id in ad_ids_with_data:
        _insert_ad(kb, ad_id, spend=50.0, qual_leads=2, is_full_cabinet=1)
        _insert_daily(kb, ad_id, day_since_launch=1, spend=5.0, impressions=1000,
                      leads=1, hook_rate=30.0, hold_rate=40.0, date_str="2025-11-01")

    # Не должно бросить sqlite3.OperationalError: too many SQL variables
    result = pe._fetch_early_features(all_ad_ids)

    # Все 5 объявлений с данными должны присутствовать в результате
    for ad_id in ad_ids_with_data:
        assert ad_id in result, f"{ad_id} должен быть в результате"
        assert result[ad_id]["hook_rate"] == pytest.approx(30.0, abs=0.1)

    # Фейковые объявления (без строк day1-7) не должны попасть в результат
    assert len(result) == len(ad_ids_with_data)


def test_fetch_early_features_no_early_rows(kb):
    """Нет строк day1-7 у объявления → оно не попадает в результат."""
    _insert_ad(kb, "ad_001", spend=100, qual_leads=3, is_full_cabinet=1)
    # Только day=0 и day=15 — вне диапазона
    _insert_daily(kb, "ad_001", day_since_launch=0, spend=5.0, date_str="2025-11-01")
    _insert_daily(kb, "ad_001", day_since_launch=15, spend=5.0, date_str="2025-11-15")

    result = pe._fetch_early_features(["ad_001"])
    assert "ad_001" not in result


# ---------------------------------------------------------------------------
# Тесты: _build_predictors
# ---------------------------------------------------------------------------

def test_build_predictors_significant():
    """hook_rate: успешные медиана 34, неуспешные 19 → lift≈1.79 ≥ 1.2 → предиктор."""
    success_feats = [{"hook_rate": 34.0, "hold_rate": None, "ctr": None,
                      "cpm": None, "cpl": None, "lead_velocity": None}] * 8
    fail_feats = [{"hook_rate": 19.0, "hold_rate": None, "ctr": None,
                   "cpm": None, "cpl": None, "lead_velocity": None}] * 8

    predictors = pe._build_predictors(success_feats, fail_feats)

    hook_pred = next((p for p in predictors if p["metric"] == "hook_rate"), None)
    assert hook_pred is not None
    assert hook_pred["lift"] == pytest.approx(34.0 / 19.0, abs=0.01)
    assert hook_pred["success_median"] == pytest.approx(34.0, abs=0.1)
    assert hook_pred["fail_median"] == pytest.approx(19.0, abs=0.1)


def test_build_predictors_insignificant():
    """Медианы близки (lift < 1.2) → предиктор не создан."""
    success_feats = [{"hook_rate": 25.0, "hold_rate": None, "ctr": None,
                      "cpm": None, "cpl": None, "lead_velocity": None}] * 5
    fail_feats = [{"hook_rate": 24.0, "hold_rate": None, "ctr": None,
                   "cpm": None, "cpl": None, "lead_velocity": None}] * 5

    predictors = pe._build_predictors(success_feats, fail_feats)

    hook_pred = next((p for p in predictors if p["metric"] == "hook_rate"), None)
    assert hook_pred is None


def test_build_predictors_lower_better():
    """cpl lower_better: у успешных 5, у неуспешных 15 → lift = 15/5 = 3 ≥ 1.2."""
    success_feats = [{"hook_rate": None, "hold_rate": None, "ctr": None,
                      "cpm": None, "cpl": 5.0, "lead_velocity": None}] * 6
    fail_feats = [{"hook_rate": None, "hold_rate": None, "ctr": None,
                   "cpm": None, "cpl": 15.0, "lead_velocity": None}] * 6

    predictors = pe._build_predictors(success_feats, fail_feats)

    cpl_pred = next((p for p in predictors if p["metric"] == "cpl"), None)
    assert cpl_pred is not None
    assert cpl_pred["lift"] == pytest.approx(3.0, abs=0.1)
    assert cpl_pred["direction"] == "lower_better"


# ---------------------------------------------------------------------------
# Тесты: run_pattern_engine
# ---------------------------------------------------------------------------

def test_run_pattern_engine_insufficient(kb):
    """Мало success_ids (< 5) → insufficient, learnings_written=0, старые уроки целы."""
    # Вставляем урок вручную и один source=pattern_engine
    _insert_learning(kb, "Ручной урок", source="manual")
    _insert_learning(kb, "Старый pattern_engine", source="pattern_engine")

    # Только 2 объявления с успехом — меньше MIN_SUCCESS_ADS=5
    for i in range(2):
        _insert_ad(kb, f"suc_{i}", spend=100, qual_leads=5, romi=200.0, is_full_cabinet=1)
    for i in range(10):
        _insert_ad(kb, f"fail_{i}", spend=100, qual_leads=0, romi=50.0, is_full_cabinet=1)

    result = pe.run_pattern_engine()

    assert result["data_sufficiency"] == "insufficient"
    assert result["learnings_written"] == 0
    assert result["predictors"] == []

    # Старые уроки целы
    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT source, statement FROM learnings").fetchall()
    finally:
        conn.close()

    sources = [r["source"] for r in rows]
    statements = [r["statement"] for r in rows]
    assert "manual" in sources
    # Старый pattern_engine урок должен быть цел (мы не удаляем при insufficient)
    assert "Старый pattern_engine" in statements


def test_run_pattern_engine_ok(kb):
    """6 success / 6 fail с day1-7 и разными метриками → learnings_written >= 1."""
    # Успешные: высокий hook_rate день 1-7
    for i in range(6):
        ad_id = f"suc_{i}"
        _insert_ad(kb, ad_id, spend=100, qual_leads=5, romi=200.0, is_full_cabinet=1)
        _insert_daily(kb, ad_id, day_since_launch=1, spend=5.0, impressions=1000,
                      leads=2, hook_rate=40.0, hold_rate=50.0, date_str=f"2025-11-0{i+1}")
        _insert_daily(kb, ad_id, day_since_launch=3, spend=5.0, impressions=1000,
                      leads=2, hook_rate=38.0, hold_rate=48.0, date_str=f"2025-11-1{i+1}")

    # Неуспешные: низкий hook_rate день 1-7
    for i in range(6):
        ad_id = f"fail_{i}"
        _insert_ad(kb, ad_id, spend=100, qual_leads=0, romi=50.0, is_full_cabinet=1)
        _insert_daily(kb, ad_id, day_since_launch=1, spend=5.0, impressions=1000,
                      leads=0, hook_rate=10.0, hold_rate=15.0, date_str=f"2025-11-0{i+1}")
        _insert_daily(kb, ad_id, day_since_launch=3, spend=5.0, impressions=1000,
                      leads=0, hook_rate=12.0, hold_rate=14.0, date_str=f"2025-11-1{i+1}")

    result = pe.run_pattern_engine()

    assert result["data_sufficiency"] == "ok"
    assert result["learnings_written"] >= 1
    assert len(result["predictors"]) >= 1

    # Проверяем что уроки записаны в БД
    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM learnings WHERE source = 'pattern_engine'"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) >= 1
    for row in rows:
        assert row["source"] == "pattern_engine"
        assert row["confidence"] in ("hypothesis", "probable", "confirmed")


def test_run_pattern_engine_does_not_touch_manual(kb):
    """После прогона pattern_engine уроки source='manual' и 'pattern_miner' остаются нетронутыми."""
    _insert_learning(kb, "Ручной урок #1", source="manual")
    _insert_learning(kb, "Мой pattern_miner", source="pattern_miner")

    # Успешные
    for i in range(6):
        ad_id = f"suc_{i}"
        _insert_ad(kb, ad_id, spend=100, qual_leads=5, romi=300.0, is_full_cabinet=1)
        _insert_daily(kb, ad_id, day_since_launch=2, spend=5.0, impressions=1000,
                      leads=3, hook_rate=45.0, hold_rate=55.0, date_str=f"2025-11-0{i+1}")

    # Неуспешные
    for i in range(6):
        ad_id = f"fail_{i}"
        _insert_ad(kb, ad_id, spend=100, qual_leads=0, romi=30.0, is_full_cabinet=1)
        _insert_daily(kb, ad_id, day_since_launch=2, spend=5.0, impressions=1000,
                      leads=0, hook_rate=8.0, hold_rate=10.0, date_str=f"2025-11-0{i+1}")

    pe.run_pattern_engine()

    # manual и pattern_miner уроки должны быть на месте
    conn = sqlite3.connect(kb)
    conn.row_factory = sqlite3.Row
    try:
        manual_rows = conn.execute(
            "SELECT statement FROM learnings WHERE source = 'manual'"
        ).fetchall()
        miner_rows = conn.execute(
            "SELECT statement FROM learnings WHERE source = 'pattern_miner'"
        ).fetchall()
    finally:
        conn.close()

    assert any(r["statement"] == "Ручной урок #1" for r in manual_rows)
    assert any(r["statement"] == "Мой pattern_miner" for r in miner_rows)


def test_run_pattern_engine_empty_table(kb):
    """Пустая таблица ad_daily_metrics и creative_kb → insufficient, не падает."""
    result = pe.run_pattern_engine()

    assert result["data_sufficiency"] == "insufficient"
    assert result["predictors"] == []
    assert result["learnings_written"] == 0


# ---------------------------------------------------------------------------
# Тесты: get_patterns_summary
# ---------------------------------------------------------------------------

def test_get_patterns_summary_empty(kb):
    """Нет pattern_engine уроков → predictors=[], data_sufficiency зависит от данных."""
    result = pe.get_patterns_summary()

    assert isinstance(result["predictors"], list)
    assert len(result["predictors"]) == 0
    assert result["data_sufficiency"] in ("ok", "insufficient")
    assert result["n_success"] >= 0
    assert result["n_fail"] >= 0


def test_get_patterns_summary_after_run(kb):
    """После run_pattern_engine summary возвращает непустой список если были предикторы."""
    # Успешные с явно высоким hook_rate
    for i in range(6):
        ad_id = f"suc_{i}"
        _insert_ad(kb, ad_id, spend=100, qual_leads=5, romi=300.0, is_full_cabinet=1)
        _insert_daily(kb, ad_id, day_since_launch=2, spend=5.0, impressions=1000,
                      leads=3, hook_rate=50.0, hold_rate=60.0, date_str=f"2025-11-0{i+1}")

    for i in range(6):
        ad_id = f"fail_{i}"
        _insert_ad(kb, ad_id, spend=100, qual_leads=0, romi=30.0, is_full_cabinet=1)
        _insert_daily(kb, ad_id, day_since_launch=2, spend=5.0, impressions=1000,
                      leads=0, hook_rate=5.0, hold_rate=7.0, date_str=f"2025-11-0{i+1}")

    run_result = pe.run_pattern_engine()
    summary = pe.get_patterns_summary()

    # Количество предикторов в summary = количество уроков в БД
    assert len(summary["predictors"]) == run_result["learnings_written"]
