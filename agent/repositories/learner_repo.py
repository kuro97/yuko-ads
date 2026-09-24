"""Репозиторий кеша Learner — JSON файл."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "learner_cache.json"


def get_learner_cache(tenant_id: str) -> dict | None:
    """Возвращает кешированные результаты Learner."""
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ошибка чтения learner_cache.json: %s", e)
        return None


def save_learner_cache(tenant_id: str, data: dict):
    """Сохраняет результаты Learner в кеш."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
