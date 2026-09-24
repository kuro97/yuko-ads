"""
Скоринг новых креативов — похожесть на победителей.
sentence-transformers + all-MiniLM-L6-v2 + cosine similarity.
"""
import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
LEARNER_FILE = DATA_DIR / "learner_results.json"

# Пороги похожести
THRESHOLD_HIGH = 0.75
THRESHOLD_MED = 0.55

_lock = threading.Lock()
_model = None
_model_loaded = False
_winner_embeddings = None
_winner_names: list[str] = []

# Репозиторий победителей (может быть недоступен в тестах без Supabase)
try:
    from agent.repositories import winners_repo as _winners_repo
except Exception:  # noqa: BLE001
    _winners_repo = None  # type: ignore[assignment]


def _get_winners_from_supabase(tenant_id: str) -> list[str]:
    """Получает имена победителей из Supabase winner_archive."""
    if _winners_repo is None:
        return []
    try:
        rows = _winners_repo.get_winners(tenant_id)
        return [row["ad_name"] for row in rows if row.get("ad_name")]
    except Exception as e:
        logger.warning("Не удалось получить победителей из Supabase: %s", e)
        return []


def _get_winners_from_file() -> list[str]:
    """Получает имена победителей из learner_results.json (файловый fallback)."""
    if not LEARNER_FILE.exists():
        return []
    try:
        data = json.loads(LEARNER_FILE.read_text(encoding="utf-8"))
        table = data.get("creative_table", [])
        return [row["ad_name"] for row in table if row.get("creative_class") == "Winner"]
    except (json.JSONDecodeError, KeyError):
        return []


def get_winner_names(tenant_id: str | None = None) -> list[str]:
    """Извлекает имена победителей.

    Порядок: Supabase winner_archive (если tenant_id) → файл learner_results.json.
    """
    if tenant_id is not None:
        supabase_names = _get_winners_from_supabase(tenant_id)
        if supabase_names:
            return supabase_names
    return _get_winners_from_file()


def _load_model():
    """Лениво загружает модель. Возвращает SentenceTransformer или None."""
    global _model, _model_loaded
    if _model_loaded:
        return _model
    with _lock:
        if _model_loaded:
            return _model
        _model_loaded = True
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer("all-MiniLM-L6-v2")
            return _model
        except ImportError:
            logger.warning("sentence-transformers не установлен")
            return None
        except Exception as e:
            logger.warning("Ошибка загрузки модели: %s", e)
            return None


def _ensure_winner_embeddings(tenant_id: str | None = None):
    """Пересчитывает эмбеддинги победителей если список изменился."""
    global _winner_embeddings, _winner_names
    model = _load_model()
    if model is None:
        _winner_names = []
        _winner_embeddings = None
        return
    names = get_winner_names(tenant_id)
    with _lock:
        if names == _winner_names and _winner_embeddings is not None:
            return
        _winner_names = names
        if not names:
            _winner_embeddings = None
            return
        _winner_embeddings = model.encode(names, convert_to_numpy=True)


def _compute_similarity(text, model, winner_embeddings, winner_names):
    """Cosine similarity текста со всеми победителями."""
    import numpy as np
    text_emb = model.encode([text], convert_to_numpy=True)[0]
    norms = np.linalg.norm(winner_embeddings, axis=1) * np.linalg.norm(text_emb)
    similarities = np.dot(winner_embeddings, text_emb) / np.where(norms == 0, 1, norms)
    best_idx = int(np.argmax(similarities))
    return float(similarities[best_idx]), winner_names[best_idx]


def score_creative(card_name: str, tenant_id: str | None = None) -> dict:
    """Скорит один креатив по похожести на победителей.

    Args:
        card_name: Название креатива.
        tenant_id: ID тенанта. Если задан — победители из Supabase + fallback на файл.
    """
    if not card_name or not card_name.strip():
        return {"level": "LOW", "value": 0, "reason": "Пустое название"}
    card_name = card_name[:500]
    model = _load_model()
    if model is None:
        return {"level": "LOW", "value": 0, "reason": "Скоринг недоступен"}
    try:
        _ensure_winner_embeddings(tenant_id)
    except Exception as e:
        logger.warning("Ошибка при загрузке победителей: %s", e)
        return {"level": "LOW", "value": 0, "reason": "Ошибка скоринга"}
    if _winner_embeddings is None or len(_winner_names) == 0:
        return {"level": "LOW", "value": 0, "reason": "Нет данных о победителях"}
    try:
        similarity, closest_name = _compute_similarity(
            card_name, model, _winner_embeddings, _winner_names
        )
        similarity = round(similarity, 2)
        if similarity >= THRESHOLD_HIGH:
            level = "HIGH"
        elif similarity >= THRESHOLD_MED:
            level = "MED"
        else:
            level = "LOW"
        short_name = closest_name[:60] + ("..." if len(closest_name) > 60 else "")
        return {"level": level, "value": similarity, "reason": f"Похож на: {short_name}"}
    except Exception as e:
        logger.warning("Ошибка скоринга: %s", e)
        return {"level": "LOW", "value": 0, "reason": "Ошибка скоринга"}


def reset_cache():
    """Сбрасывает кеш (для тестов)."""
    global _model, _model_loaded, _winner_embeddings, _winner_names
    _model = None
    _model_loaded = False
    _winner_embeddings = None
    _winner_names = []
