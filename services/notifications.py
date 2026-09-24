"""
Лента уведомлений: in-app события + email (опционально).

Хранит историю действий автопилота в памяти.
Email через smtplib при отключении объявлений.
"""

import html
import logging
import os
import smtplib
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from services.approval_checker_models import RuntimeBufferSnapshot, TimeWindow

logger = logging.getLogger(__name__)

# Хранилище событий (последние 500, FIFO)
_events: deque = deque(maxlen=500)
_lock = threading.Lock()
_NOTIFICATION_RUNTIME_INSTANCE_ID = uuid.uuid4().hex

# Типы событий
EVENT_AD_PAUSED = "ad_paused"
EVENT_AD_DISMISSED = "ad_dismissed"
EVENT_LAUNCH_DONE = "launch_done"
EVENT_ALERT = "alert"
EVENT_SCHEDULER = "scheduler"


def add_event(
    event_type: str,
    title: str,
    detail: str = "",
    level: str = "info",
    meta: dict | None = None,
) -> dict:
    """Добавить событие в ленту.

    Args:
        event_type: тип события (ad_paused, alert, launch_done, ...)
        title: краткое описание
        detail: подробности (опционально)
        level: info / warning / critical
        meta: доп. данные (ad_id, city, ...)

    Returns:
        Созданное событие.
    """
    event = {
        "id": _next_id(),
        "type": event_type,
        "title": title,
        "detail": detail,
        "level": level,
        "meta": meta or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "read": False,
    }
    with _lock:
        _events.appendleft(event)

    # Email для critical событий (ad_paused)
    if event_type == EVENT_AD_PAUSED:
        _try_send_email(event)

    return event


def get_events(
    limit: int = 50,
    offset: int = 0,
    event_type: str | None = None,
    unread_only: bool = False,
) -> dict:
    """Получить список событий с фильтрацией.

    Returns:
        {"events": [...], "total": int, "unread": int}
    """
    with _lock:
        all_events = list(_events)

    # Фильтрация
    filtered = all_events
    if event_type:
        filtered = [e for e in filtered if e["type"] == event_type]
    if unread_only:
        filtered = [e for e in filtered if not e["read"]]

    total = len(filtered)
    unread = sum(1 for e in all_events if not e["read"])
    page = filtered[offset : offset + limit]

    return {"events": page, "total": total, "unread": unread}


def mark_read(event_id: int) -> bool:
    """Отметить событие как прочитанное."""
    with _lock:
        for event in _events:
            if event["id"] == event_id:
                event["read"] = True
                return True
    return False


def mark_all_read() -> int:
    """Отметить все события как прочитанные. Возвращает кол-во отмеченных."""
    count = 0
    with _lock:
        for event in _events:
            if not event["read"]:
                event["read"] = True
                count += 1
    return count


def clear_events() -> None:
    """Очистить все события (для тестов)."""
    with _lock:
        _events.clear()


def read_notification_runtime_snapshot(
    now: datetime | None = None,
) -> RuntimeBufferSnapshot:
    """Возвращает только aggregate timestamps/count; title/detail/meta не раскрывает."""

    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    window = TimeWindow(
        start=observed_at - timedelta(hours=1),
        end=observed_at,
        timezone_name="UTC",
        semantic="NOTIFICATION_LAST_HOUR",
    )
    with _lock:
        timestamp_values = tuple(event.get("timestamp") for event in _events)
        buffer_size = len(_events)
        capacity = int(_events.maxlen or 0)
    parsed: list[datetime] = []
    for raw in timestamp_values:
        if not isinstance(raw, str):
            continue
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is None or value.utcoffset() is None:
            continue
        parsed.append(value.astimezone(timezone.utc))
    relevant = tuple(sorted(value for value in parsed if window.start <= value < window.end))
    oldest_retained = min(parsed) if parsed else None
    truncated = bool(
        capacity > 0
        and buffer_size >= capacity
        and oldest_retained is not None
        and oldest_retained >= window.start
    )
    timestamps_complete = len(parsed) == len(timestamp_values)
    return RuntimeBufferSnapshot(
        source_instance_id=_NOTIFICATION_RUNTIME_INSTANCE_ID,
        observed_at=observed_at,
        window=window,
        event_timestamps=relevant,
        count_in_window=len(relevant),
        buffer_size=buffer_size,
        capacity=capacity,
        truncated_in_window=truncated,
        complete=timestamps_complete and not truncated,
    )


# --- Email ---

def _try_send_email(event: dict) -> None:
    """Попытка отправить email. Не блокирует, не падает."""
    email_to = os.environ.get("NOTIFY_EMAIL")
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")

    if not all([email_to, smtp_host, smtp_user, smtp_pass]):
        logger.debug("Email не настроен — пропускаем")
        return

    # Отправляем в фоне чтобы не блокировать
    threading.Thread(
        target=_send_email,
        args=(email_to, smtp_host, smtp_user, smtp_pass, event),
        daemon=True,
    ).start()


def _send_email(
    to: str, host: str, user: str, password: str, event: dict
) -> None:
    """Отправить email через SMTP."""
    try:
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        subject = f"ACME: {event['title']}"
        body = f"{event['title']}\n\n{event['detail']}\n\nВремя: {event['timestamp']}"

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to

        with smtplib.SMTP(host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)

        logger.info("Email отправлен: %s → %s", subject, to)
    except Exception as exc:
        logger.warning("Не удалось отправить email: %s", exc)


# --- Telegram ---

def send_telegram(text: str, channel: str = "ads") -> bool:
    """Отправляет сообщение в Telegram.

    channel="ads"    — основной бот (TELEGRAM_BOT_TOKEN, дефолт)
    channel="health" — бот здоровья системы (TELEGRAM_HEALTH_BOT_TOKEN);
                       если переменная не задана, падбэк на основной токен.

    Использует TELEGRAM_CHAT_ID из config (один для обоих каналов).
    Возвращает True при успехе, False при любой ошибке (не бросает исключений).
    """
    try:
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_HEALTH_BOT_TOKEN, TELEGRAM_CHAT_ID
        import requests as _requests

        # Выбираем токен по каналу
        if channel == "health" and TELEGRAM_HEALTH_BOT_TOKEN:
            token = TELEGRAM_HEALTH_BOT_TOKEN
        else:
            token = TELEGRAM_BOT_TOKEN

        if not token or not TELEGRAM_CHAT_ID:
            logger.debug("Telegram не настроен (нет токена или chat_id) — пропускаем")
            return False

        resp = _requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        result = resp.json()
        if result.get("ok"):
            return True
        logger.warning("Telegram ответил ошибкой: %s", result)
        return False
    except Exception as exc:
        # Не логируем сам exc — в нём URL с токеном бота
        logger.warning("Не удалось отправить в Telegram: %s", type(exc).__name__)
        return False


def send_critical_alert(title: str, detail: str = "", meta: dict | None = None, channel: str = "ads") -> dict:
    """Рассылает критический алерт по всем доступным каналам.

    channel передаётся в send_telegram для выбора нужного бота.
    Создаёт событие в ленте, отправляет в Telegram и email.
    Возвращает созданное событие. Не бросает исключений.
    """
    event = add_event(EVENT_ALERT, title, detail, level="critical", meta=meta)

    # Telegram (HTML-разметка) — экранируем title и detail перед вставкой в HTML
    safe_title = html.escape(title)
    safe_detail = html.escape(detail)
    tg_text = f"🚨 <b>{safe_title}</b>\n{safe_detail}" if detail else f"🚨 <b>{safe_title}</b>"
    send_telegram(tg_text, channel=channel)

    # Email (переиспользуем существующий механизм)
    _try_send_email(event)

    return event


# --- Внутреннее ---

_counter = 0
_counter_lock = threading.Lock()


def _next_id() -> int:
    """Потокобезопасный счётчик ID."""
    global _counter
    with _counter_lock:
        _counter += 1
        return _counter
