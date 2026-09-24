"""
Общий атомарный JSON state-стор.

Единая реализация для мест, где состояние крона/сервиса хранится в JSON-файле:
load с дефолтом при отсутствующем/битом файле, save через tmp+rename (атомарно,
без риска битого файла при падении процесса посередине записи).

Не мигрирует существующие места (T-STATE-1) — только новый переиспользуемый модуль.
Места с кастомной логикой (дефолтные поля, спец-обработка ключей) НЕ используют этот
стор напрямую — только "простые" 1-в-1 read/write (см. ARCH-phase6-engineering §8.4).
"""

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Глобальный lock на процесс — защищает от гонки при параллельной записи в РАЗНЫЕ
# файлы из разных потоков одного процесса (простая, консервативная защита;
# межпроцессной блокировки не даёт — как и не давали существующие tmp+rename места).
_lock = threading.Lock()


def load_json_state(path: Path) -> dict:
    """Загружает JSON-состояние из файла.

    Возвращает {} если файл не существует или содержит невалидный JSON —
    вызывающий код никогда не падает на чтении state-файла.

    Args:
        path: путь к JSON-файлу состояния.

    Returns:
        dict с содержимым файла, либо {} при отсутствии/битом JSON.
    """
    if not path.exists():
        return {}
    try:
        with _lock:
            raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning("state_store: %s содержит не dict (%s) — возвращаю {}", path, type(data))
            return {}
        return data
    except Exception as exc:
        logger.warning("state_store: не удалось прочитать %s: %s", path, exc)
        return {}


def save_json_state(path: Path, state: dict) -> None:
    """Сохраняет JSON-состояние атомарно (tmp-файл + rename).

    rename на одной ФС атомарен — читающий процесс никогда не увидит
    частично записанный файл.

    Args:
        path: путь к JSON-файлу состояния.
        state: dict для сохранения.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with _lock:
        try:
            tmp_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except Exception as exc:
            logger.error("state_store: не удалось сохранить %s: %s", path, exc)
            raise
