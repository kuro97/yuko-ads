"""Репозиторий AMO данных — JSON файл."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
AMO_FILE = DATA_DIR / "amo_data.json"


def _read() -> dict:
    """Читает AMO данные из файла."""
    if not AMO_FILE.exists():
        return {}
    try:
        return json.loads(AMO_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ошибка чтения amo_data.json: %s", e)
        return {}


def _write(data: dict):
    """Записывает AMO данные в файл."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AMO_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_amo_data(tenant_id: str) -> dict:
    """Возвращает AMO данные как dict {ad_id: {qual_pct, romi, ...}}."""
    return _read()


def save_amo_entry(tenant_id: str, ad_id: str, data: dict):
    """Сохраняет одну запись AMO."""
    all_data = _read()
    all_data[ad_id] = {
        "qual_pct": data.get("qual_pct"),
        "romi": data.get("romi"),
        "payments": data.get("payments"),
        "cpql": data.get("cpql"),
        "revenue": data.get("revenue"),
        "qual_leads": data.get("qual_leads"),
    }
    _write(all_data)


def save_amo_bulk(tenant_id: str, metrics: dict):
    """Сохраняет все AMO метрики разом."""
    all_data = _read()
    for ad_id, data in metrics.items():
        all_data[ad_id] = {
            k: v for k, v in data.items()
            if k in ("qual_pct", "romi", "payments", "cpql", "revenue", "qual_leads")
        }
    _write(all_data)
