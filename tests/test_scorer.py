"""Тесты скоринга креативов: похожесть на победителей."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.scorer as scorer_module
from agent.scorer import score_creative, get_winner_names, reset_cache


@pytest.fixture(autouse=True)
def _isolate_scorer(tmp_path):
    """Изолирует scorer — tmp_path для LEARNER_FILE, сброс кеша."""
    reset_cache()
    fake_learner = tmp_path / "learner_results.json"
    original = scorer_module.LEARNER_FILE
    scorer_module.LEARNER_FILE = fake_learner
    yield fake_learner
    scorer_module.LEARNER_FILE = original
    reset_cache()


def _mock_model():
    """Мок SentenceTransformer — нормализованные векторы на основе хеша текста."""
    model = MagicMock()

    def encode_side_effect(texts, convert_to_numpy=True):
        result = []
        for t in texts:
            h = hash(t) % 1000 / 1000.0
            vec = np.array([h, 1.0 - h, h * 0.5] + [0.0] * 381)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            result.append(vec)
        return np.array(result)

    model.encode = MagicMock(side_effect=encode_side_effect)
    return model


def _write_learner_results(ads):
    """Записывает learner_results.json с указанными объявлениями."""
    data = {"creative_table": ads}
    learner_file = scorer_module.LEARNER_FILE
    learner_file.parent.mkdir(parents=True, exist_ok=True)
    learner_file.write_text(json.dumps(data), encoding="utf-8")


# --- score_creative ---

def test_score_high_similarity():
    """Идентичное название → HIGH (cosine similarity = 1.0)."""
    winner_name = "Петров / Тема А / Подтема 1"
    _write_learner_results([
        {"ad_name": winner_name, "creative_class": "Winner"},
    ])
    model = _mock_model()
    with patch("agent.scorer._load_model", return_value=model):
        result = score_creative(winner_name)
    assert result["level"] == "HIGH"
    assert result["value"] >= 0.75


def test_score_med_similarity():
    """Частично похожее название → MED."""
    _write_learner_results([
        {"ad_name": "Петров / Тема А / Подтема 1", "creative_class": "Winner"},
    ])
    model = _mock_model()

    # Подменяем encode: winner → [1,0,...], new → [0.6,0.8,...] → similarity ~0.6
    def encode_med(texts, convert_to_numpy=True):
        result = []
        for t in texts:
            if "Тема А" in t and "Тема Б" not in t:
                vec = np.zeros(384)
                vec[0] = 1.0
            else:
                vec = np.zeros(384)
                vec[0] = 0.6
                vec[1] = 0.8
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            result.append(vec)
        return np.array(result)

    model.encode = MagicMock(side_effect=encode_med)
    with patch("agent.scorer._load_model", return_value=model):
        result = score_creative("Петров / Тема Б / Подтема 2")
    assert result["level"] == "MED"
    assert 0.55 <= result["value"] < 0.75


def test_score_low_similarity():
    """Совсем не похожее название → LOW."""
    _write_learner_results([
        {"ad_name": "Обзор PRODA / Тема А", "creative_class": "Winner"},
    ])
    model = _mock_model()

    # Ортогональные вектора → similarity ≈ 0
    def encode_low(texts, convert_to_numpy=True):
        result = []
        for i, t in enumerate(texts):
            vec = np.zeros(384)
            if "Тема А" in t:
                vec[0] = 1.0
            else:
                vec[1] = 1.0  # ортогональный
            result.append(vec)
        return np.array(result)

    model.encode = MagicMock(side_effect=encode_low)
    with patch("agent.scorer._load_model", return_value=model):
        result = score_creative("Кулинарные рецепты для бабушки")
    assert result["level"] == "LOW"
    assert result["value"] < 0.55


def test_score_no_winners():
    """Нет данных о победителях → LOW с причиной."""
    _write_learner_results([
        {"ad_name": "Dead Ad", "creative_class": "Dead"},
    ])
    model = _mock_model()
    with patch("agent.scorer._load_model", return_value=model):
        result = score_creative("Любое название")
    assert result["level"] == "LOW"
    assert "Нет данных" in result["reason"]


def test_score_no_library():
    """sentence-transformers не установлен → LOW."""
    _write_learner_results([
        {"ad_name": "Winner Ad", "creative_class": "Winner"},
    ])
    with patch("agent.scorer._load_model", return_value=None):
        result = score_creative("Любое название")
    assert result["level"] == "LOW"
    assert "недоступен" in result["reason"]


def test_score_empty_card_name():
    """Пустое название → LOW."""
    result = score_creative("")
    assert result["level"] == "LOW"
    assert "Пустое" in result["reason"]

    result2 = score_creative("   ")
    assert result2["level"] == "LOW"
    assert "Пустое" in result2["reason"]


def test_score_exception_handling():
    """model.encode бросает RuntimeError → LOW."""
    _write_learner_results([
        {"ad_name": "Winner Ad", "creative_class": "Winner"},
    ])
    model = MagicMock()
    model.encode = MagicMock(side_effect=RuntimeError("GPU error"))
    with patch("agent.scorer._load_model", return_value=model):
        result = score_creative("Любое название")
    assert result["level"] == "LOW"
    assert "Ошибка" in result["reason"]


# --- get_winner_names ---

def test_get_winner_names():
    """Фильтрует только Winner из creative_table."""
    _write_learner_results([
        {"ad_name": "Winner 1", "creative_class": "Winner"},
        {"ad_name": "Dead 1", "creative_class": "Dead"},
        {"ad_name": "Winner 2", "creative_class": "Winner"},
        {"ad_name": "Clickbait 1", "creative_class": "Clickbait"},
    ])
    names = get_winner_names()
    assert names == ["Winner 1", "Winner 2"]


def test_get_winner_names_no_file():
    """Файл не существует → пустой список."""
    if scorer_module.LEARNER_FILE.exists():
        scorer_module.LEARNER_FILE.unlink()
    names = get_winner_names()
    assert names == []


def test_get_winner_names_no_winners():
    """creative_table без Winner → пустой список."""
    _write_learner_results([
        {"ad_name": "Dead 1", "creative_class": "Dead"},
        {"ad_name": "Clickbait 1", "creative_class": "Clickbait"},
    ])
    names = get_winner_names()
    assert names == []
