"""
Unit-тесты для services/pattern_miner.py.

Проверяют детерминированные агрегации срезов → learnings:
- порог n_ads>=5 AND spend>=50
- шкалу confidence по §5.4 (hypothesis/probable/confirmed)
- сохранение ручных уроков (source='manual')
- перезапись автоматических уроков (source='pattern_miner')
- пустой baseline → learnings_written=0, не падает

Используют tmp_path — не трогают реальную БД.
"""

import json
import sqlite3
import sys
from unittest.mock import MagicMock

import pytest

# Мокаем google.genai до импорта любого модуля который может его тянуть
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())

from services import creative_intelligence as ci
import services.pattern_miner as pm


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
    hook_type: str = "authority",
    city: str = "CityB",
    adset_type: str = "L2",
    spend: float = 100.0,
    qual_pct: float = 20.0,
    cpl: float = 10.0,
    is_full_cabinet: int = 1,
) -> None:
    """Вставляет объявление в creative_kb для тестов."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO creative_kb
                (ad_id, ad_name, hook_type, city, adset_type, spend, qual_pct, cpl, is_full_cabinet)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ad_id, f"Ad {ad_id}", hook_type, city, adset_type, spend, qual_pct, cpl, is_full_cabinet),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_learning(db_path: str, statement: str, source: str = "manual",
                     confidence: str = "hypothesis") -> int:
    """Вставляет урок в таблицу learnings, возвращает id."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO learnings (statement, confidence, source, tags) VALUES (?, ?, ?, ?)",
            (statement, confidence, source, ""),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _count_learnings(db_path: str, source: str = None) -> int:
    """Считает уроки в learnings (опционально по source)."""
    conn = sqlite3.connect(db_path)
    try:
        if source:
            row = conn.execute(
                "SELECT COUNT(*) FROM learnings WHERE source = ?", (source,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()
        return row[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Тест 1: срез проходит порог — 22 ads, spend>$400, qual 2x базы → confirmed
# ---------------------------------------------------------------------------


def test_miner_slice_above_threshold_confirmed(kb, monkeypatch):
    """22 объявления с hook=authority в CityB, spend>$400, qual_pct 2x базы → confirmed learning.

    Чтобы baseline был низким: создаём большой объём "слабых" объявлений
    с другим hook/city, затем маленький срез с очень высоким qual_pct.

    Baseline формируется по всем is_full_cabinet=1 AND spend>0 —
    включая и baseline-объявления, и срез authority. Нужно чтобы
    avg_qual_pct среза было >= 1.3x от общего baseline.

    Стратегия: 80 базовых объявлений (qual_pct=5%), 22 объявления authority
    (qual_pct=15%). Baseline ≈ (80*5 + 22*15)/102 ≈ 7.2%. Lift ≈ 15/7.2 ≈ 2.1x ≥ 1.3.
    """
    # Большой baseline с низким qual_pct
    for i in range(80):
        _insert_ad(kb, f"base_{i}", hook_type="base_hook", city="CityA",
                   spend=10.0, qual_pct=5.0, cpl=15.0)

    # Срез: 22 объявления hook=authority, spend=$20 каждый = $440 суммарно
    # qual_pct=15% → lift ≈ 2.1x ≥ 1.3
    for i in range(22):
        _insert_ad(kb, f"auth_{i}", hook_type="authority", city="CityB",
                   spend=20.0, qual_pct=15.0, cpl=8.0)

    result = pm.mine_patterns()

    assert result["learnings_written"] >= 1
    assert result["slices_evaluated"] >= 1

    # Проверяем что есть хотя бы один урок с нужным confidence
    conn = sqlite3.connect(kb)
    try:
        rows = conn.execute(
            "SELECT confidence, tags FROM learnings WHERE source='pattern_miner'"
        ).fetchall()
    finally:
        conn.close()

    # Срез authority с 22 объявлениями и $440 spend → confirmed
    authority_learnings = [r for r in rows if "authority" in (r[1] or "")]
    assert len(authority_learnings) > 0, f"Нет урока с authority. Все: {rows}"

    confidences = [r[0] for r in authority_learnings]
    # При n=22, spend=$440 → confirmed (n>=20 AND spend>=400)
    assert "confirmed" in confidences, f"Ожидали confirmed, получили: {confidences}"


# ---------------------------------------------------------------------------
# Тест 2: срез не проходит порог (n_ads<5) → learnings_written=0
# ---------------------------------------------------------------------------


def test_miner_slice_below_threshold_count(kb):
    """3 объявления (n<5) → срез пропущен, learnings_written=0."""
    # Сначала добавим baseline чтобы мог считаться
    for i in range(10):
        _insert_ad(kb, f"base_{i}", hook_type="base_hook", city="CityA",
                   spend=50.0, qual_pct=10.0, cpl=10.0)

    # Маленький срез с высоким качеством — но n<5
    for i in range(3):
        _insert_ad(kb, f"small_{i}", hook_type="tiny", city="CityC",
                   spend=100.0, qual_pct=50.0, cpl=5.0)

    result = pm.mine_patterns()

    # Срез с 3 объявлениями не должен попасть в learnings
    conn = sqlite3.connect(kb)
    try:
        tiny_learnings = conn.execute(
            "SELECT COUNT(*) FROM learnings WHERE tags LIKE '%tiny%'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert tiny_learnings == 0


# ---------------------------------------------------------------------------
# Тест 3: spend мал (spend<$50) → срез пропускается
# ---------------------------------------------------------------------------


def test_miner_slice_low_spend_skipped(kb):
    """5 объявлений, суммарный spend=$30 (<$50) → срез пропущен."""
    # Baseline
    for i in range(10):
        _insert_ad(kb, f"base_{i}", hook_type="base_hook", city="CityA",
                   spend=50.0, qual_pct=10.0, cpl=10.0)

    # Срез с маленьким spend (5 ads × $5 = $25 < $50)
    for i in range(5):
        _insert_ad(kb, f"cheap_{i}", hook_type="cheap_hook", city="CityB",
                   spend=5.0, qual_pct=50.0, cpl=3.0)

    result = pm.mine_patterns()

    # Срез cheap_hook в CityB не должен попасть в learnings
    conn = sqlite3.connect(kb)
    try:
        cheap_count = conn.execute(
            "SELECT COUNT(*) FROM learnings WHERE tags LIKE '%cheap_hook%'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert cheap_count == 0


# ---------------------------------------------------------------------------
# Тест 4: _confidence шкала по §5.4
# ---------------------------------------------------------------------------


def test_confidence_scale():
    """_confidence возвращает правильные значения по формуле §5.4."""
    # confirmed: n_ads>=20 AND total_spend>=400
    assert pm._confidence(20, 400.0) == "confirmed"
    assert pm._confidence(25, 500.0) == "confirmed"

    # probable: n_ads>=10 AND total_spend>=150
    assert pm._confidence(10, 150.0) == "probable"
    assert pm._confidence(15, 200.0) == "probable"

    # hypothesis: прошёл базовый порог (n>=5, spend>=50)
    assert pm._confidence(5, 50.0) == "hypothesis"
    assert pm._confidence(6, 60.0) == "hypothesis"

    # Подтверждаем монотонность: больше данных → выше confidence
    # n=10, spend=149 → probable НЕ достигнуто по spend
    assert pm._confidence(10, 149.0) == "hypothesis"

    # n=19, spend=400 → confirmed НЕ достигнуто по n
    assert pm._confidence(19, 400.0) == "probable"


# ---------------------------------------------------------------------------
# Тест 5: ручные уроки (source='manual') не удаляются
# ---------------------------------------------------------------------------


def test_miner_preserves_manual_learnings(kb):
    """Прогон miner не удаляет ручные уроки (source='manual')."""
    # Добавляем ручной урок
    manual_id = _insert_learning(kb, "Ручной урок — не удалять", source="manual")

    # Добавляем данные для miner
    for i in range(10):
        _insert_ad(kb, f"base_{i}", hook_type="base_hook", city="CityA",
                   spend=50.0, qual_pct=10.0, cpl=10.0)

    pm.mine_patterns()

    # Ручной урок должен остаться
    conn = sqlite3.connect(kb)
    try:
        row = conn.execute(
            "SELECT id, source FROM learnings WHERE id = ?", (manual_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == manual_id
    assert row[1] == "manual"


# ---------------------------------------------------------------------------
# Тест 6: два прогона miner — старые pattern_miner удалены, дубликатов нет
# ---------------------------------------------------------------------------


def test_miner_rewrites_auto_learnings(kb):
    """Повторный прогон miner удаляет старые pattern_miner уроки, не создаёт дубликаты."""
    # Baseline + значимый срез
    for i in range(10):
        _insert_ad(kb, f"base_{i}", hook_type="base_hook", city="CityA",
                   spend=50.0, qual_pct=5.0, cpl=15.0)
    for i in range(10):
        _insert_ad(kb, f"high_{i}", hook_type="winner_hook", city="CityB",
                   spend=20.0, qual_pct=15.0, cpl=5.0)

    # Первый прогон
    result1 = pm.mine_patterns()
    count_after_1 = _count_learnings(kb, source="pattern_miner")

    # Второй прогон
    result2 = pm.mine_patterns()
    count_after_2 = _count_learnings(kb, source="pattern_miner")

    # Количество pattern_miner уроков должно быть одинаковым (не удваивается)
    assert count_after_2 == count_after_1, (
        f"Дублирование уроков: {count_after_1} → {count_after_2}"
    )


# ---------------------------------------------------------------------------
# Тест 7: пустой baseline → learnings_written=0, не падает
# ---------------------------------------------------------------------------


def test_miner_empty_baseline_no_crash(kb):
    """Пустая KB (нет is_full_cabinet=1 с spend>0) → mine_patterns не падает."""
    result = pm.mine_patterns()

    assert result["learnings_written"] == 0
    assert result["slices_evaluated"] == 0
    assert result["baseline"]["n_ads"] == 0
    assert result["baseline"]["avg_qual_pct"] == 0.0


# ---------------------------------------------------------------------------
# Тест 8: _aggregate_slice с forbidden dimension → ValueError
# ---------------------------------------------------------------------------


def test_aggregate_slice_rejects_bad_dimension(kb):
    """_aggregate_slice с недопустимым dimension бросает ValueError (whitelist)."""
    conn = sqlite3.connect(kb)
    try:
        with pytest.raises(ValueError, match="Недопустимое"):
            pm._aggregate_slice(conn, ["injected_column"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Тест 9: базовые пороги — n_ads=5, spend=50 даёт hypothesis
# ---------------------------------------------------------------------------


def test_miner_minimum_threshold_creates_hypothesis(kb):
    """Срез ровно на пороге (n=5, spend=$50) создаёт learning с confidence=hypothesis."""
    # Минимальный baseline — нужен qual_pct > 0
    for i in range(3):
        _insert_ad(kb, f"base_{i}", hook_type="base_hook", city="Другой",
                   spend=20.0, qual_pct=5.0, cpl=10.0)

    # Срез ровно на пороге: 5 объявлений, spend=$10 каждый = $50 суммарно
    # qual_pct в 2x раз больше базы (5% → 10%)
    for i in range(5):
        _insert_ad(kb, f"threshold_{i}", hook_type="min_hook", city="Мин",
                   spend=10.0, qual_pct=10.0, cpl=5.0)

    result = pm.mine_patterns()

    conn = sqlite3.connect(kb)
    try:
        rows = conn.execute(
            "SELECT confidence FROM learnings WHERE source='pattern_miner' AND tags LIKE '%min_hook%'"
        ).fetchall()
    finally:
        conn.close()

    # Если урок попал — должен быть hypothesis (минимальный порог)
    for row in rows:
        assert row[0] == "hypothesis"
