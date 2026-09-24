"""
Юнит-тесты для services/hypothesis_influence.py.

Проверяют:
- combo_key: нормализация (lower/strip, '*' для пустых частей)
- load_verdict_weights: агрегация confirmed/refuted по комбо, TTL-отсечение
- is_dead_combo: dead = refuted >= dead_min_refuted AND confirmed == 0
- apply_soft_influence: подтверждённые вверх, мёртвые вниз БЕЗ удаления,
  пустые weights → исходный порядок, enabled=false → без изменений (проверяем
  на уровне вызывающего кода: apply_soft_influence сам не знает про флаг,
  но пустые weights от него эквивалентны выключенному влиянию)

Используют tmp_path (реальная hypotheses-таблица через init_kb). Без сети.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта любого модуля, который может его тянуть
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())

import sqlite3

from services import creative_intelligence as ci
from services.hypothesis_influence import (
    apply_soft_influence,
    combo_key,
    is_dead_combo,
    load_verdict_weights,
)


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


def _insert_hypothesis(
    db_path: str,
    angle: str,
    city: str,
    ad_format: str,
    status: str,
    verdict_at: datetime | None,
) -> None:
    """Вставляет закрытую (или открытую) гипотезу напрямую в БД для теста.

    hypothesis_verdict.py (T4) ещё не реализован, поэтому строки создаём
    напрямую — это позволяет тестировать influence независимо от verdict.
    """
    conn = sqlite3.connect(db_path)
    try:
        verdict_at_str = verdict_at.strftime("%Y-%m-%d %H:%M:%S") if verdict_at else None
        conn.execute(
            """
            INSERT INTO hypotheses
                (angle, city, ad_format, segment, source, card_name,
                 ad_ids, expectation_json, status, verdict_at)
            VALUES (?, ?, ?, 'общий', 'coverage', 'Карточка теста', '[]', '{}', ?, ?)
            """,
            (angle, city, ad_format, status, verdict_at_str),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# combo_key
# ---------------------------------------------------------------------------

def test_combo_key_normalizes_case_and_spaces():
    """Нормализация: lower(), strip(), разделитель '::'."""
    key = combo_key("  Карусель В CityD  ", " CityD ", "Carousel")
    assert key == "карусель в cityd::cityd::carousel"


def test_combo_key_empty_parts_become_star():
    """Пустые части (город/формат не заданы) → '*'."""
    key = combo_key("угол", "", "")
    assert key == "угол::*::*"


def test_combo_key_consistent():
    """Одинаковые вход при разном регистре дают одинаковый ключ."""
    assert combo_key("Angle", "City", "Format") == combo_key("angle", "city", "format")


# ---------------------------------------------------------------------------
# load_verdict_weights
# ---------------------------------------------------------------------------

def test_load_verdict_weights_confirmed_and_refuted(kb):
    """Агрегация: 2 confirmed + 1 refuted для одного комбо → score=1, dead=False."""
    now = datetime(2026, 7, 2, 10, 0, 0)
    _insert_hypothesis(kb, "Угол А", "CityA", "video_speaker", "confirmed", now - timedelta(days=1))
    _insert_hypothesis(kb, "Угол А", "CityA", "video_speaker", "confirmed", now - timedelta(days=2))
    _insert_hypothesis(kb, "Угол А", "CityA", "video_speaker", "refuted", now - timedelta(days=3))

    weights = load_verdict_weights(now=now)
    key = combo_key("Угол А", "CityA", "video_speaker")

    assert weights[key]["confirmed"] == 2
    assert weights[key]["refuted"] == 1
    assert weights[key]["score"] == 1
    assert weights[key]["dead"] is False


def test_load_verdict_weights_dead_combo(kb):
    """2 refuted, 0 confirmed → dead=True (dead_min_refuted=2 по дефолту)."""
    now = datetime(2026, 7, 2, 10, 0, 0)
    _insert_hypothesis(kb, "Карусель", "CityD", "carousel", "refuted", now - timedelta(days=1))
    _insert_hypothesis(kb, "Карусель", "CityD", "carousel", "refuted", now - timedelta(days=2))

    weights = load_verdict_weights(now=now)
    key = combo_key("Карусель", "CityD", "carousel")

    assert weights[key]["refuted"] == 2
    assert weights[key]["confirmed"] == 0
    assert weights[key]["dead"] is True
    assert weights[key]["score"] == -2


def test_load_verdict_weights_ttl_expired(kb):
    """verdict_at старше ttl_days (60) → комбо не попадает в weights (реабилитация)."""
    now = datetime(2026, 7, 2, 10, 0, 0)
    _insert_hypothesis(kb, "Старый угол", "CityC", "static", "refuted", now - timedelta(days=70))

    weights = load_verdict_weights(now=now)
    key = combo_key("Старый угол", "CityC", "static")

    assert key not in weights


def test_load_verdict_weights_open_hypotheses_ignored(kb):
    """Открытые гипотезы (status='open', verdict_at=None) не участвуют в агрегации."""
    now = datetime(2026, 7, 2, 10, 0, 0)
    _insert_hypothesis(kb, "Угол Б", "CityB", "video_speaker", "open", None)

    weights = load_verdict_weights(now=now)
    key = combo_key("Угол Б", "CityB", "video_speaker")

    assert key not in weights


def test_load_verdict_weights_empty_db(kb):
    """Пустая БД → пустой dict, без ошибок."""
    assert load_verdict_weights() == {}


# ---------------------------------------------------------------------------
# is_dead_combo
# ---------------------------------------------------------------------------

def test_is_dead_combo_true():
    weights = {combo_key("а", "б", "в"): {"confirmed": 0, "refuted": 3, "score": -3, "dead": True}}
    assert is_dead_combo(weights, "а", "б", "в") is True


def test_is_dead_combo_false_when_confirmed():
    weights = {combo_key("а", "б", "в"): {"confirmed": 1, "refuted": 2, "score": -1, "dead": False}}
    assert is_dead_combo(weights, "а", "б", "в") is False


def test_is_dead_combo_missing_key():
    """Комбо отсутствует в weights → не мёртвое (нет данных != мёртвое)."""
    assert is_dead_combo({}, "неизвестный", "город", "формат") is False


# ---------------------------------------------------------------------------
# apply_soft_influence
# ---------------------------------------------------------------------------

def test_apply_soft_influence_confirmed_moves_up():
    """Подтверждённое комбо (score>0) поднимается выше неподтверждённого."""
    topics = [
        {"angle": "Обычная тема", "city": "CityA", "ad_format": "static", "priority": "LOW"},
        {"angle": "Подтверждённая", "city": "CityB", "ad_format": "video_speaker", "priority": "LOW"},
    ]
    weights = {
        combo_key("Подтверждённая", "CityB", "video_speaker"): {
            "confirmed": 3, "refuted": 0, "score": 3, "dead": False,
        },
    }

    result = apply_soft_influence(topics, weights)

    assert len(result) == 2
    assert result[0]["angle"] == "Подтверждённая"
    assert result[0]["_influence_score"] == 3
    assert result[1]["angle"] == "Обычная тема"
    assert result[1]["_influence_score"] == 0


def test_apply_soft_influence_dead_goes_last_not_removed():
    """Мёртвое комбо получает штраф -100 и уходит в конец, но НЕ удаляется."""
    topics = [
        {"angle": "Мёртвая", "city": "CityD", "ad_format": "carousel", "priority": "HIGH"},
        {"angle": "Живая", "city": "CityA", "ad_format": "video_speaker", "priority": "LOW"},
    ]
    weights = {
        combo_key("Мёртвая", "CityD", "carousel"): {
            "confirmed": 0, "refuted": 2, "score": -2, "dead": True,
        },
    }

    result = apply_soft_influence(topics, weights)

    assert len(result) == 2  # список НЕ укорочен
    angles = [t["angle"] for t in result]
    assert angles == ["Живая", "Мёртвая"]  # мёртвая ушла в конец
    assert result[1]["_influence_score"] == -102  # -2 (score) + -100 (штраф)


def test_apply_soft_influence_dead_only_candidate_still_returned():
    """Единственная тема — мёртвое комбо → список НЕ пустой (мягкость, §AC7)."""
    topics = [
        {"angle": "Единственная мёртвая", "city": "CityD", "ad_format": "carousel", "priority": "HIGH"},
    ]
    weights = {
        combo_key("Единственная мёртвая", "CityD", "carousel"): {
            "confirmed": 0, "refuted": 5, "score": -5, "dead": True,
        },
    }

    result = apply_soft_influence(topics, weights)

    assert len(result) == 1
    assert result[0]["angle"] == "Единственная мёртвая"


def test_apply_soft_influence_empty_weights_keeps_original_order():
    """Пустые weights → порядок не меняется (все _influence_score=0, стабильная сортировка)."""
    topics = [
        {"angle": "Первая", "city": "CityA", "ad_format": "static"},
        {"angle": "Вторая", "city": "CityB", "ad_format": "carousel"},
        {"angle": "Третья", "city": "CityC", "ad_format": "video_speaker"},
    ]

    result = apply_soft_influence(topics, {})

    angles = [t["angle"] for t in result]
    assert angles == ["Первая", "Вторая", "Третья"]
    assert all(t["_influence_score"] == 0 for t in result)


def test_apply_soft_influence_empty_topics_list():
    """Пустой список тем → пустой результат, не падает."""
    assert apply_soft_influence([], {}) == []


def test_apply_soft_influence_does_not_mutate_input():
    """Исходные dict тем не мутируются (копия для _influence_score)."""
    topics = [{"angle": "Тема", "city": "CityA", "ad_format": "static"}]
    apply_soft_influence(topics, {})
    assert "_influence_score" not in topics[0]


# ---------------------------------------------------------------------------
# enabled=false — гейт применяется вызывающим кодом (topic_selector, T7),
# сам apply_soft_influence не знает про autopilot-конфиг. Проверяем контракт:
# если вызывающий код передаёт пустые weights (как при enabled=false, когда
# load_verdict_weights не вызывается вовсе) — список тем не меняется.
# ---------------------------------------------------------------------------

def test_apply_soft_influence_disabled_equivalent_no_change():
    """При enabled=false вызывающий код не должен применять влияние вовсе —
    здесь фиксируем, что «нет weights» = «нет изменений порядка», что и
    является ожидаемым поведением при выключенном флаге."""
    topics = [
        {"angle": "А", "city": "CityA", "ad_format": "static"},
        {"angle": "Б", "city": "CityB", "ad_format": "carousel"},
    ]
    result = apply_soft_influence(topics, {})
    assert [t["angle"] for t in result] == ["А", "Б"]
    assert len(result) == len(topics)
