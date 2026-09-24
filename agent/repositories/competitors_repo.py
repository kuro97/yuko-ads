"""Репозиторий конкурентов — JSON файл."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
COMPETITORS_FILE = DATA_DIR / "competitors.json"


def _read() -> list[dict]:
    if not COMPETITORS_FILE.exists():
        return []
    try:
        return json.loads(COMPETITORS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ошибка чтения competitors.json: %s", e)
        return []


def _write(data: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COMPETITORS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_competitors(tenant_id: str) -> list[dict]:
    """Возвращает список конкурентов."""
    competitors = _read()
    return sorted(competitors, key=lambda c: c.get("added_at", ""), reverse=True)


def add_competitor(tenant_id: str, page_id: str, name: str) -> dict:
    """Добавляет конкурента."""
    competitors = _read()
    entry = {
        "page_id": page_id,
        "name": name,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    competitors.append(entry)
    _write(competitors)
    return {"page_id": page_id, "name": name}


def delete_competitor(tenant_id: str, page_id: str) -> bool:
    """Удаляет конкурента."""
    competitors = _read()
    new_list = [c for c in competitors if c.get("page_id") != page_id]
    if len(new_list) == len(competitors):
        return False
    _write(new_list)
    return True


def competitor_exists(tenant_id: str, page_id: str) -> bool:
    """Проверяет существование конкурента."""
    competitors = _read()
    return any(c.get("page_id") == page_id for c in competitors)
