"""
Журнал сомнений Budget Scaler (пункт 1 фидбека владельца, недельный список мелочей).

Протокол сомнений (services/budget_scaler.py::_evaluate_doubt_triggers) раньше
только слал отдельное Telegram-сообщение в момент прогона и нигде не сохранял
свои срабатывания — вечерний отчёт («Итог дня») не мог показать секцию
«Сомнения» задним числом. Этот модуль добавляет постоянный журнал: append-запись
на каждый прогон с сработавшими триггерами, ротация по датам (~30 дней), чтение
записей за конкретный день.

Формат файла — список записей (не словарь по датам): один прогон Budget Scaler
может произойти несколько раз в день (несколько кампаний/кронов), каждая запись
хранит свою дату для фильтрации и ротации.

Запись: {"date": "YYYY-MM-DD", "triggers": [str, ...], "decision": str}
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Локальное время (UTC+5 по умолчанию, настраивается) — как во всех остальных модулях автопилота
_TZ_LOCAL = timezone(timedelta(hours=5))

# Файл журнала сомнений
_DOUBT_LOG_FILE = Path(__file__).resolve().parent.parent / "data" / "doubt_log.json"

# Сколько дней истории храним (ротация)
_RETENTION_DAYS = 30


def _load_doubt_log() -> list[dict]:
    """Загружает журнал сомнений из файла. При ошибке/отсутствии — пустой список."""
    if not _DOUBT_LOG_FILE.exists():
        return []
    try:
        data = json.loads(_DOUBT_LOG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("_load_doubt_log: не удалось прочитать — %s", exc)
        return []


def _save_doubt_log(entries: list[dict]) -> None:
    """Атомарно сохраняет журнал (tmp + rename) — тот же паттерн, что
    budget_scaler._save_scale_state / auto_launch._save_auto_launch_state."""
    _DOUBT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DOUBT_LOG_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_DOUBT_LOG_FILE)
    except Exception as exc:
        logger.error("_save_doubt_log: не удалось сохранить — %s", exc)
        raise


def append_doubt_entry(triggers: list[str], decision: str, now: datetime | None = None) -> None:
    """Добавляет запись сомнения в журнал и ротирует записи старше _RETENTION_DAYS.

    Информационная функция — ошибка записи НЕ должна ронять прогон Budget
    Scaler (вызывающий код budget_scaler.py оборачивает в try/except), но сама
    по себе тут исключение не глотаем молча — пусть падает и логируется
    выше по стеку, это же не критичный путь принятия решения.

    Args:
        triggers: список человекочитаемых причин сомнения (не пустой — вызывающий
            код проверяет `if doubts:` перед вызовом).
        decision: строка решения («поднимаю бюджет...» / «НЕ поднимаю: ...»).
        now: момент записи (по умолчанию — текущее локальное время).
    """
    now = now or datetime.now(_TZ_LOCAL)
    entries = _load_doubt_log()
    entries.append({
        "date": now.date().isoformat(),
        "triggers": list(triggers),
        "decision": decision,
    })

    # Ротация: оставляем только записи за последние _RETENTION_DAYS дней
    cutoff = (now.date() - timedelta(days=_RETENTION_DAYS)).isoformat()
    entries = [e for e in entries if e.get("date", "") >= cutoff]

    _save_doubt_log(entries)


def get_doubt_entries_for_date(date_str: str) -> list[dict]:
    """Возвращает все записи журнала за конкретную дату (YYYY-MM-DD).

    Пустой список, если записей нет или файл недоступен/битый — не бросает
    исключение (только для формирования секции отчёта, не критичный путь).
    """
    try:
        entries = _load_doubt_log()
    except Exception as exc:
        logger.warning("get_doubt_entries_for_date: журнал недоступен — %s", exc)
        return []
    return [e for e in entries if e.get("date") == date_str]
