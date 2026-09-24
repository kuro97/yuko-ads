"""Репозиторий архива победителей — JSON файл."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
WINNERS_FILE = DATA_DIR / "winner_archive.json"
LEARNER_FILE = DATA_DIR / "learner_results.json"

_next_id = 0


def _read() -> list[dict]:
    """Читает победителей из файла."""
    if not WINNERS_FILE.exists():
        return []
    try:
        return json.loads(WINNERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ошибка чтения winner_archive.json: %s", e)
        return []


def _write(data: list[dict]):
    """Записывает победителей в файл."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WINNERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_next_id(winners: list[dict]) -> int:
    """Генерирует следующий ID."""
    global _next_id
    if winners:
        max_id = max(w.get("id", 0) for w in winners)
        _next_id = max(max_id, _next_id)
    _next_id += 1
    return _next_id


def get_winners(tenant_id: str) -> list[dict]:
    """Получить всех победителей из архива."""
    winners = _read()
    return sorted(winners, key=lambda w: w.get("added_at", ""), reverse=True)


def add_winner(tenant_id: str, ad_name: str, source: str = "manual") -> dict:
    """Добавить победителя в архив."""
    winners = _read()
    entry = {
        "id": _get_next_id(winners),
        "ad_name": ad_name,
        "source": source,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    winners.append(entry)
    _write(winners)
    return entry


def delete_winner(tenant_id: str, winner_id: int) -> bool:
    """Удалить победителя из архива."""
    winners = _read()
    new_winners = [w for w in winners if w.get("id") != winner_id]
    if len(new_winners) == len(winners):
        return False
    _write(new_winners)
    return True


def _read_learner_winner_names() -> list[str]:
    """Читает имена победителей из learner_results.json."""
    if not LEARNER_FILE.exists():
        return []
    try:
        data = json.loads(LEARNER_FILE.read_text(encoding="utf-8"))
        table = data.get("creative_table", [])
        return [
            row["ad_name"]
            for row in table
            if row.get("creative_class") == "Winner"
        ]
    except (json.JSONDecodeError, KeyError, OSError) as e:
        logger.warning("Ошибка чтения learner_results.json: %s", e)
        return []


def sync_from_learner(tenant_id: str) -> dict:
    """Синхронизация из learner_results.json в winner_archive."""
    learner_names = _read_learner_winner_names()
    if not learner_names:
        return {"imported": 0, "skipped": 0}

    existing = get_winners(tenant_id)
    existing_names = {w["ad_name"] for w in existing}

    imported = 0
    skipped = 0

    for ad_name in learner_names:
        if ad_name in existing_names:
            skipped += 1
        else:
            add_winner(tenant_id, ad_name, source="learner_sync")
            existing_names.add(ad_name)
            imported += 1

    return {"imported": imported, "skipped": skipped}
