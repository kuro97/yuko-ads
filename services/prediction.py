"""
Предсказание победителей — scikit-learn классификатор.
Обучается на исторических данных (creative_table из learner).
Фичи: CTR, hook_rate, hold_rate, CPL нормализованный.
Цель: бинарная (Winner / не Winner по creative_class).
"""

import json
import logging
import threading
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
LEARNER_FILE = DATA_DIR / "learner_results.json"

# Минимум объявлений для обучения
MIN_SAMPLES = 30

# Фичи для модели
FEATURE_KEYS = ["ctr", "hook_rate", "hold_rate", "cpl"]

_lock = threading.Lock()
_model: LogisticRegression | None = None
_scaler: StandardScaler | None = None
_trained = False


def _load_training_data() -> tuple[list[dict], list[int]]:
    """Загружает creative_table из learner_results.json.
    Возвращает (features_dicts, labels) где label=1 для Winner."""
    if not LEARNER_FILE.exists():
        return [], []

    try:
        data = json.loads(LEARNER_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], []

    table = data.get("creative_table", [])
    features = []
    labels = []

    for row in table:
        # Пропускаем записи без нужных метрик
        vals = {k: row.get(k) for k in FEATURE_KEYS}
        if any(v is None for v in vals.values()):
            continue
        # Пропускаем нулевые CTR (нет данных)
        if vals["ctr"] == 0 and vals["hook_rate"] == 0:
            continue

        features.append(vals)
        labels.append(1 if row.get("creative_class") == "Winner" else 0)

    return features, labels


def _extract_features(rows: list[dict]) -> np.ndarray:
    """Конвертирует список словарей в numpy матрицу."""
    return np.array([[r[k] for k in FEATURE_KEYS] for r in rows], dtype=np.float64)


def train_model() -> dict:
    """Обучает модель на исторических данных.
    Возвращает статус: {trained, samples, winners, accuracy}."""
    global _model, _scaler, _trained

    features_dicts, labels = _load_training_data()

    if len(features_dicts) < MIN_SAMPLES:
        return {
            "trained": False,
            "reason": f"Недостаточно данных: {len(features_dicts)}/{MIN_SAMPLES}",
            "samples": len(features_dicts),
        }

    X = _extract_features(features_dicts)
    y = np.array(labels)

    # Нормализация фичей
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Обучение (class_weight='balanced' для несбалансированных классов)
    model = LogisticRegression(class_weight="balanced", max_iter=1000, random_state=42)
    model.fit(X_scaled, y)

    # Точность на обучающей выборке (для информации)
    accuracy = round(model.score(X_scaled, y) * 100, 1)

    with _lock:
        _model = model
        _scaler = scaler
        _trained = True

    winner_count = int(y.sum())
    logger.info(
        "Модель обучена: %d образцов, %d победителей, accuracy=%.1f%%",
        len(y), winner_count, accuracy,
    )

    return {
        "trained": True,
        "samples": len(y),
        "winners": winner_count,
        "accuracy": accuracy,
    }


def predict_winner(ad_metrics: dict) -> dict:
    """Предсказание вероятности победителя для одного объявления.
    ad_metrics: {ctr, hook_rate, hold_rate, cpl}.
    Возвращает: {probability, label, confidence}."""
    with _lock:
        model = _model
        scaler = _scaler
        trained = _trained

    if not trained or model is None or scaler is None:
        return {"probability": None, "label": "unknown", "confidence": "none", "reason": "Модель не обучена"}

    # Проверяем наличие фичей
    vals = {k: ad_metrics.get(k) for k in FEATURE_KEYS}
    if any(v is None for v in vals.values()):
        missing = [k for k in FEATURE_KEYS if vals[k] is None]
        return {"probability": None, "label": "unknown", "confidence": "none", "reason": f"Нет данных: {missing}"}

    X = np.array([[vals[k] for k in FEATURE_KEYS]], dtype=np.float64)
    X_scaled = scaler.transform(X)

    proba = model.predict_proba(X_scaled)[0]
    # Индекс класса 1 (Winner)
    winner_idx = list(model.classes_).index(1) if 1 in model.classes_ else -1
    if winner_idx < 0:
        return {"probability": 0.0, "label": "not_winner", "confidence": "low"}

    prob = round(float(proba[winner_idx]), 3)

    # Уровень уверенности
    if prob >= 0.7:
        confidence = "high"
        label = "winner"
    elif prob >= 0.4:
        confidence = "medium"
        label = "potential"
    else:
        confidence = "low"
        label = "not_winner"

    return {"probability": prob, "label": label, "confidence": confidence}


def get_model_status() -> dict:
    """Возвращает статус модели."""
    with _lock:
        return {"trained": _trained}


def reset_model() -> None:
    """Сбрасывает модель (для тестов)."""
    global _model, _scaler, _trained
    with _lock:
        _model = None
        _scaler = None
        _trained = False
