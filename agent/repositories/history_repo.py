"""Репозиторий истории запусков — JSON файл."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
HISTORY_FILE = DATA_DIR / "launch_history.json"


def _read() -> list[dict]:
    """Читает историю из файла."""
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ошибка чтения launch_history.json: %s", e)
        return []


def _write(data: list[dict]):
    """Записывает историю в файл."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_history_entry(tenant_id: str, data: dict):
    """Сохраняет запись в историю запусков."""
    history = _read()
    history.insert(0, {
        "data": data,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    _write(history)


def get_history(tenant_id: str) -> list[dict]:
    """Возвращает историю запусков."""
    history = _read()
    return [entry["data"] for entry in history]
