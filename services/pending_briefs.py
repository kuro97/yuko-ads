"""
Файловая очередь ТЗ на одобрение владельцем (data/pending_briefs.json).

Генератор (services/brief_generator.py) кладёт сюда сгенерированное и прошедшее
оба гейта ТЗ (status=pending) вместо прямого создания карточки Trello. Владелец
одобряет/отклоняет через Telegram-кнопки (services/telegram_bot.py), которые
двигают статус (approved/rejected). Просроченные pending уходят в expired при
очередном прогоне генератора (ретеншн RETENTION_DAYS).

Это ЧИСТЫЙ репозиторий: только чтение/запись JSON + дедуп-сигнатуры + ретеншн.
Никакого Trello/Telegram/LLM — оркестрацию (создать карточку, ответить в чат)
делает вызывающий код (telegram_bot._execute_approve_brief). Так модуль
тестируется без сети и не создаёт циклов импорта.

Потокобезопасность (ВАЖНО): файл мутируется из ДВУХ независимых потоков одного
процесса — обработчик approve/reject идёт в потоке крона поллера
(_cron_telegram_poll, каждые 60с), а generate_and_push_briefs (expire_old +
несколько add_pending) — в фоновом threading.Thread команды /brief ИЛИ в
параллельной джобе _cron_brief_generator. APScheduler max_instances=1 защищает
только от повторного запуска ОДНОГО джоба, а не от параллельного исполнения
РАЗНЫХ джобов/потоков. Без синхронизации возможен lost-update: mark_approved
пишет approved, а параллельный add_pending, читавший файл ДО, перезаписывает его
старым снимком (status снова pending) — карточка в Trello уже создана, повторный
клик создаст ДУБЛЬ (нарушение AC4). Поэтому весь цикл _load->мутация->_save в
КАЖДОЙ мутирующей функции обёрнут в один module-level threading.Lock. Атомарная
запись (tempfile+os.replace) защищает только от БИТОГО файла при падении в момент
записи (crash-safety), но НЕ от гонки чтение-изменение-запись — для неё нужен
именно lock.

Атомарная запись — тот же паттерн, что services/telegram_bot._save_offset
(tempfile + os.replace). Ни одна функция не бросает исключений наружу при
проблемах файла — деградирует на пустую очередь и логирует warning.
"""

import json
import logging
import os
import secrets
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent
PENDING_FILE = _PROJECT_ROOT / "data" / "pending_briefs.json"

# Единый module-level lock от гонки чтение-изменение-запись между потоками
# (поллер approve/reject vs фоновая генерация /brief или _cron_brief_generator).
# Обычный Lock (не RLock): мутирующие функции НЕ вызывают друг друга и НЕ вызывают
# публичные read-функции под локом — каждая делает свой _load/_save напрямую,
# вложенного захвата нет.
_LOCK = threading.Lock()

# Часовой пояс CityA (UTC+5) — единый со всеми модулями проекта
_TZ_LOCAL = timezone(timedelta(hours=5))

# Статусы записи очереди
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

# Ретеншн: pending старше стольких дней -> expired при прогоне генератора
RETENTION_DAYS = 14
# Максимум записей в файле (после чистки) — чтобы очередь не пухла
MAX_RECORDS = 200
# Длина короткого id в hex-символах. callback "approve_brief:<id>" <= 64 байта:
# "approve_brief:" = 14 байт + 6 = 20 байт, с огромным запасом.
_ID_LEN = 6


def _load() -> dict:
    """Читает PENDING_FILE. При отсутствии/битом JSON/любой ошибке — {"briefs": []}.

    Гарантирует, что data["briefs"] — список (иначе чинит на []).
    Сам lock НЕ берёт — примитив, вызывается уже под захваченным _LOCK.
    """
    try:
        if not PENDING_FILE.exists():
            return {"briefs": []}
        data = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("briefs"), list):
            logger.warning("pending_briefs: битый формат файла — сброс на пустую очередь")
            return {"briefs": []}
        return data
    except Exception as exc:
        logger.warning("pending_briefs: не удалось прочитать очередь — %s", type(exc).__name__)
        return {"briefs": []}


def _save(data: dict) -> None:
    """Атомарная запись через tempfile+os.replace (паттерн telegram_bot._save_offset).

    Ошибку ловит, логирует warning, не бросает наружу.
    Сам lock НЕ берёт (примитив, вызывается уже под захваченным _LOCK).
    """
    try:
        PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=PENDING_FILE.parent, prefix=".pending_briefs_", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, PENDING_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("pending_briefs: не удалось сохранить очередь — %s", type(exc).__name__)


def _new_id(existing: set[str]) -> str:
    """Генерирует короткий уникальный id (6 hex-символов). Lock НЕ берёт."""
    for _ in range(10):
        candidate = secrets.token_hex(3)
        if candidate not in existing:
            return candidate
    # Коллизия практически невозможна — берём длинный id без проверки
    return secrets.token_hex(_ID_LEN)


def add_pending(name: str, desc: str, product: str | None, signature: str,
                 now: datetime | None = None) -> dict:
    """Добавляет новую pending-запись в очередь. Возвращает созданную запись."""
    now = now or datetime.now(_TZ_LOCAL)
    with _LOCK:  # весь цикл load->mutate->save атомарен относительно других потоков
        data = _load()
        briefs = data["briefs"]
        brief_id = _new_id({b.get("id") for b in briefs})
        record = {
            "id": brief_id,
            "name": name,
            "desc": desc,
            "product": product,
            "signature": signature,
            "status": STATUS_PENDING,
            "created_at": now.isoformat(),
            "decided_at": None,
            "card_id": None,
            "card_url": None,
        }
        briefs.append(record)
        _save(data)
        return record


def get_brief(brief_id: str) -> dict | None:
    """Возвращает запись по id или None, если не найдена."""
    with _LOCK:
        data = _load()
        for brief in data["briefs"]:
            if brief.get("id") == brief_id:
                return brief
        return None


def active_signatures() -> set[str]:
    """Множество сигнатур записей со статусом pending ИЛИ approved.

    rejected/expired НЕ включаются — эти темы можно генерировать заново.
    Пустые/None сигнатуры пропускаются.
    """
    with _LOCK:
        data = _load()
        sigs = set()
        for brief in data["briefs"]:
            if brief.get("status") in (STATUS_PENDING, STATUS_APPROVED):
                sig = brief.get("signature")
                if sig:
                    sigs.add(sig)
        return sigs


def mark_approved(brief_id: str, card_id: str | None, card_url: str) -> bool:
    """Помечает запись approved с card_id/card_url. False если запись не найдена."""
    now = datetime.now(_TZ_LOCAL)
    with _LOCK:
        data = _load()
        for brief in data["briefs"]:
            if brief.get("id") == brief_id:
                brief["status"] = STATUS_APPROVED
                brief["card_id"] = card_id
                brief["card_url"] = card_url
                brief["decided_at"] = now.isoformat()
                _save(data)
                return True
        return False


def mark_rejected(brief_id: str) -> bool:
    """Помечает запись rejected. False если запись не найдена."""
    now = datetime.now(_TZ_LOCAL)
    with _LOCK:
        data = _load()
        for brief in data["briefs"]:
            if brief.get("id") == brief_id:
                brief["status"] = STATUS_REJECTED
                brief["decided_at"] = now.isoformat()
                _save(data)
                return True
        return False


def expire_old(now: datetime | None = None) -> int:
    """Помечает pending-записи старше RETENTION_DAYS как expired, обрезает
    очередь до MAX_RECORDS (последние по порядку добавления). Возвращает
    число просроченных записей.
    """
    now = now or datetime.now(_TZ_LOCAL)
    with _LOCK:
        data = _load()
        briefs = data["briefs"]
        expired_count = 0

        for brief in briefs:
            if brief.get("status") != STATUS_PENDING:
                continue
            created_at = brief.get("created_at")
            try:
                created_dt = datetime.fromisoformat(created_at)
            except (ValueError, TypeError):
                # Битая дата — пропускаем запись, не роняем прогон
                continue

            # Сравнение tz-aware с tz-aware; naive created_at приводим к naive now
            compare_now = now
            if created_dt.tzinfo is None and now.tzinfo is not None:
                compare_now = now.replace(tzinfo=None)
            elif created_dt.tzinfo is not None and now.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=None)

            try:
                delta_days = (compare_now - created_dt).days
            except TypeError:
                continue

            if delta_days >= RETENTION_DAYS:
                brief["status"] = STATUS_EXPIRED
                brief["decided_at"] = now.isoformat()
                expired_count += 1

        # Обрезка до MAX_RECORDS (оставляем последние по порядку добавления)
        if len(briefs) > MAX_RECORDS:
            data["briefs"] = briefs[-MAX_RECORDS:]

        _save(data)
        return expired_count
