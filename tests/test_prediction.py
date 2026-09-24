"""
Тесты сервиса предсказания победителей.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from services.prediction import (
    FEATURE_KEYS,
    MIN_SAMPLES,
    predict_winner,
    reset_model,
    train_model,
    get_model_status,
    _extract_features,
    _load_training_data,
)


@pytest.fixture(autouse=True)
def _reset():
    """Сброс модели перед каждым тестом."""
    reset_model()
    yield
    reset_model()


def _make_creative_table(n: int = 50, winner_ratio: float = 0.3) -> list[dict]:
    """Генерирует creative_table с заданным числом записей."""
    rng = np.random.RandomState(42)
    table = []
    for i in range(n):
        is_winner = i < int(n * winner_ratio)
        table.append({
            "ad_name": f"Ad_{i}",
            "ctr": round(rng.uniform(1.5, 4.0) if is_winner else rng.uniform(0.3, 1.5), 2),
            "hook_rate": round(rng.uniform(25, 50) if is_winner else rng.uniform(5, 25), 1),
            "hold_rate": round(rng.uniform(30, 60) if is_winner else rng.uniform(5, 30), 1),
            "cpl": round(rng.uniform(5, 15) if is_winner else rng.uniform(15, 50), 1),
            "creative_class": "Winner" if is_winner else "Dead",
        })
    return table


@pytest.fixture
def learner_file(tmp_path):
    """Создаёт временный learner_results.json."""
    table = _make_creative_table(50)
    data = {"creative_table": table}
    fpath = tmp_path / "learner_results.json"
    fpath.write_text(json.dumps(data), encoding="utf-8")
    return fpath


@pytest.fixture
def small_learner_file(tmp_path):
    """Файл с малым числом записей (< MIN_SAMPLES)."""
    table = _make_creative_table(10)
    data = {"creative_table": table}
    fpath = tmp_path / "learner_results.json"
    fpath.write_text(json.dumps(data), encoding="utf-8")
    return fpath


# --- Загрузка данных ---

class TestLoadData:
    """Загрузка обучающих данных."""

    def test_load_from_file(self, learner_file):
        """Успешная загрузка из файла."""
        with patch("services.prediction.LEARNER_FILE", learner_file):
            features, labels = _load_training_data()
        assert len(features) == 50
        assert len(labels) == 50
        assert sum(labels) == 15  # 30% winners of 50

    def test_load_missing_file(self, tmp_path):
        """Файл не существует — пустой результат."""
        with patch("services.prediction.LEARNER_FILE", tmp_path / "nope.json"):
            features, labels = _load_training_data()
        assert features == []
        assert labels == []

    def test_load_invalid_json(self, tmp_path):
        """Невалидный JSON — пустой результат."""
        fpath = tmp_path / "bad.json"
        fpath.write_text("not json", encoding="utf-8")
        with patch("services.prediction.LEARNER_FILE", fpath):
            features, labels = _load_training_data()
        assert features == []

    def test_skip_incomplete_rows(self, tmp_path):
        """Пропускает записи без нужных фичей."""
        data = {"creative_table": [
            {"ad_name": "ok", "ctr": 2.0, "hook_rate": 30, "hold_rate": 40, "cpl": 10, "creative_class": "Winner"},
            {"ad_name": "no_ctr", "hook_rate": 30, "hold_rate": 40, "cpl": 10, "creative_class": "Dead"},
        ]}
        fpath = tmp_path / "learner_results.json"
        fpath.write_text(json.dumps(data), encoding="utf-8")
        with patch("services.prediction.LEARNER_FILE", fpath):
            features, labels = _load_training_data()
        assert len(features) == 1


class TestExtractFeatures:
    """Извлечение фичей в numpy."""

    def test_shape(self):
        """Правильная форма матрицы."""
        rows = [{"ctr": 1.0, "hook_rate": 20.0, "hold_rate": 30.0, "cpl": 15.0}]
        X = _extract_features(rows)
        assert X.shape == (1, len(FEATURE_KEYS))

    def test_values(self):
        """Правильные значения."""
        rows = [{"ctr": 2.5, "hook_rate": 35.0, "hold_rate": 45.0, "cpl": 12.0}]
        X = _extract_features(rows)
        assert X[0][0] == 2.5  # ctr
        assert X[0][3] == 12.0  # cpl


# --- Обучение модели ---

class TestTrainModel:
    """Обучение модели."""

    def test_train_success(self, learner_file):
        """Успешное обучение."""
        with patch("services.prediction.LEARNER_FILE", learner_file):
            result = train_model()
        assert result["trained"] is True
        assert result["samples"] == 50
        assert result["winners"] == 15
        assert result["accuracy"] > 50  # Хотя бы лучше рандома

    def test_train_insufficient_data(self, small_learner_file):
        """Мало данных — не обучаем."""
        with patch("services.prediction.LEARNER_FILE", small_learner_file):
            result = train_model()
        assert result["trained"] is False
        assert "Недостаточно" in result["reason"]

    def test_train_no_file(self, tmp_path):
        """Нет файла — не обучаем."""
        with patch("services.prediction.LEARNER_FILE", tmp_path / "nope.json"):
            result = train_model()
        assert result["trained"] is False

    def test_model_status_after_train(self, learner_file):
        """Статус модели после обучения."""
        with patch("services.prediction.LEARNER_FILE", learner_file):
            train_model()
        assert get_model_status()["trained"] is True

    def test_model_status_before_train(self):
        """Статус модели до обучения."""
        assert get_model_status()["trained"] is False


# --- Предсказание ---

class TestPredictWinner:
    """Предсказание победителей."""

    def test_predict_without_model(self):
        """Без обученной модели — unknown."""
        result = predict_winner({"ctr": 2.0, "hook_rate": 30, "hold_rate": 40, "cpl": 10})
        assert result["label"] == "unknown"
        assert result["probability"] is None

    def test_predict_missing_features(self, learner_file):
        """Не все фичи — unknown."""
        with patch("services.prediction.LEARNER_FILE", learner_file):
            train_model()
        result = predict_winner({"ctr": 2.0})  # нет hook_rate, hold_rate, cpl
        assert result["label"] == "unknown"
        assert "Нет данных" in result["reason"]

    def test_predict_winner_candidate(self, learner_file):
        """Хорошие метрики → высокая вероятность победителя."""
        with patch("services.prediction.LEARNER_FILE", learner_file):
            train_model()
        # Метрики типичного победителя
        result = predict_winner({"ctr": 3.5, "hook_rate": 45, "hold_rate": 50, "cpl": 8})
        assert result["probability"] is not None
        assert result["probability"] > 0.3  # Должен быть выше среднего
        assert result["label"] in ("winner", "potential")

    def test_predict_loser_candidate(self, learner_file):
        """Плохие метрики → низкая вероятность победителя."""
        with patch("services.prediction.LEARNER_FILE", learner_file):
            train_model()
        # Метрики типичного проигравшего
        result = predict_winner({"ctr": 0.5, "hook_rate": 8, "hold_rate": 10, "cpl": 45})
        assert result["probability"] is not None
        assert result["probability"] < 0.5  # Не победитель

    def test_predict_returns_confidence(self, learner_file):
        """Предсказание содержит уровень уверенности."""
        with patch("services.prediction.LEARNER_FILE", learner_file):
            train_model()
        result = predict_winner({"ctr": 2.0, "hook_rate": 30, "hold_rate": 35, "cpl": 15})
        assert result["confidence"] in ("high", "medium", "low")

    def test_predict_probability_range(self, learner_file):
        """Вероятность в диапазоне [0, 1]."""
        with patch("services.prediction.LEARNER_FILE", learner_file):
            train_model()
        result = predict_winner({"ctr": 2.0, "hook_rate": 30, "hold_rate": 35, "cpl": 15})
        assert 0 <= result["probability"] <= 1


# --- API ---

class TestPredictionAPI:
    """FastAPI эндпоинты."""

    def test_get_prediction_status(self):
        """GET /api/prediction/status."""
        from fastapi.testclient import TestClient
        from web.app import app
        client = TestClient(app)
        resp = client.get("/api/prediction/status")
        assert resp.status_code == 200
        assert "trained" in resp.json()

    def test_post_train(self, learner_file):
        """POST /api/prediction/train."""
        from fastapi.testclient import TestClient
        from web.app import app
        with patch("services.prediction.LEARNER_FILE", learner_file):
            client = TestClient(app)
            resp = client.post("/api/prediction/train")
            assert resp.status_code == 200
            data = resp.json()
            assert data["trained"] is True

    def test_post_predict(self, learner_file):
        """POST /api/prediction/predict."""
        from fastapi.testclient import TestClient
        from web.app import app
        with patch("services.prediction.LEARNER_FILE", learner_file):
            client = TestClient(app)
            # Сначала обучим
            client.post("/api/prediction/train")
            # Потом предскажем
            resp = client.post("/api/prediction/predict", json={
                "ctr": 3.0, "hook_rate": 40, "hold_rate": 45, "cpl": 10
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "probability" in data
            assert "label" in data
