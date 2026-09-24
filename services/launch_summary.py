"""Вечерняя сводка-воронка запусков — измерительный прибор конвейера.

В отличие от сводки автономных пауз («пусто → молчим»), эта сводка уходит
КАЖДЫЙ день, включая нулевой. Причина: месяц, в котором из сотен предложений
запуска до объявлений дошли 11, прошёл незамеченным именно потому, что сбой
выглядел как тишина. Нули здесь — не шум, а главный сигнал.

Модуль только читает decisions.db. Исполнение, предложения и Facebook
не трогает — сломанная сводка не может сломать конвейер.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "decisions.db"

# Запуск считается «висящим», если после старта исполнения прошло больше этого
# срока, а подтверждения (VERIFIED) или терминального исхода так и нет.
HANGING_AFTER_HOURS = 6

# Окно алерта застоя: предложения идут, созданных объявлений ноль.
STALL_WINDOW_DAYS = 3

# Не заваливать сводку кнопками: стоп-кнопки только на свежайшие запуски.
MAX_STOP_BUTTONS = 6

# Состояния «решение принято, исход не подтверждён» — кумулятивный хвост,
# который сводка показывает целиком, не ограничиваясь окном суток.
# APPROVED здесь не лишний: «одобрено, но диспетчер так и не взял» — реальный
# класс потерь (часть предложений истекала именно так), и без него
# сводка показала бы проблему только пост-мортем, строкой EXPIRED.
_HANGING_STATES = (
    "APPROVED",
    "EXECUTION_QUEUED",
    "LIVE_REVIEW",
    "PERMIT_ISSUED",
    "ATTEMPT_STARTED",
    "EXECUTION_RETRY_WAIT",
    "EXECUTED",
    "VERIFYING",
    "RECONCILE_REQUIRED",
)


def _connect() -> sqlite3.Connection:
    """Read-only соединение: сводка не имеет права писать в decisions.db."""
    conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _one(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row is not None else 0


def collect_launch_funnel(since_utc: datetime, until_utc: datetime) -> dict:
    """Счётчики воронки запусков за окно [since_utc, until_utc).

    Все created_at в decisions.db — ISO-строки в UTC, поэтому границы окна
    сравниваются строково. Возвращает словарь с нулями при пустой воронке —
    отсутствие данных и «ничего не произошло» здесь неразличимы намеренно:
    оба состояния владелец должен увидеть.
    """
    since = since_utc.astimezone(timezone.utc).isoformat()
    until = until_utc.astimezone(timezone.utc).isoformat()
    window = (since, until)
    hanging_before = (
        until_utc.astimezone(timezone.utc) - timedelta(hours=HANGING_AFTER_HOURS)
    ).isoformat()

    # closing, а не `with conn`: контекст sqlite3-соединения управляет
    # транзакцией и оставляет коннект открытым.
    from contextlib import closing

    with closing(_connect()) as conn:
        funnel: dict = {
            "proposed": _one(
                conn,
                """
                SELECT COUNT(*) FROM owner_action_proposals
                WHERE proposal_kind='LAUNCH' AND created_at >= ? AND created_at < ?
                """,
                window,
            ),
            "created_ads": _one(
                conn,
                """
                SELECT COUNT(*) FROM launch_check_audit
                WHERE event_type='CREATE_CONFIRMED' AND created_at >= ? AND created_at < ?
                """,
                window,
            ),
            "verified": _one(
                conn,
                """
                SELECT COUNT(DISTINCT e.proposal_id)
                FROM owner_action_events e
                JOIN owner_action_proposals p ON p.proposal_id = e.proposal_id
                WHERE p.proposal_kind='LAUNCH' AND e.event_type='LIFECYCLE_VERIFIED'
                  AND e.created_at >= ? AND e.created_at < ?
                """,
                window,
            ),
        }

        # Одобрения за окно, по источнику: SYSTEM = директива, OWNER = кнопка.
        funnel["approved_system"] = 0
        funnel["approved_owner"] = 0
        for row in conn.execute(
            """
            SELECT d.decision_source AS src, COUNT(*) AS n
            FROM owner_action_decisions d
            JOIN owner_action_proposals p ON p.proposal_id = d.proposal_id
            WHERE p.proposal_kind='LAUNCH' AND d.decision_kind='APPROVE'
              AND d.recorded_at >= ? AND d.recorded_at < ?
            GROUP BY d.decision_source
            """,
            window,
        ):
            key = "approved_system" if row["src"] == "SYSTEM" else "approved_owner"
            funnel[key] += int(row["n"])

        # Отказы исполнения за окно, по коду причины перехода.
        funnel["review_denied"] = 0
        funnel["review_failed"] = 0
        for row in conn.execute(
            """
            SELECT e.reason_code AS code, COUNT(DISTINCT e.proposal_id) AS n
            FROM owner_action_events e
            JOIN owner_action_proposals p ON p.proposal_id = e.proposal_id
            WHERE p.proposal_kind='LAUNCH'
              AND e.reason_code IN ('LIVE_REVIEW_DENIED', 'LIVE_REVIEW_FAILED')
              AND e.created_at >= ? AND e.created_at < ?
            GROUP BY e.reason_code
            """,
            window,
        ):
            key = (
                "review_denied"
                if row["code"] == "LIVE_REVIEW_DENIED"
                else "review_failed"
            )
            funnel[key] += int(row["n"])

        # Истёкшие за окно: одобренные («диспетчер не дошёл») и без решения —
        # это два разных диагноза, в одну цифру их не сливаем.
        funnel["expired_approved"] = _one(
            conn,
            """
            SELECT COUNT(DISTINCT e.proposal_id)
            FROM owner_action_events e
            JOIN owner_action_proposals p ON p.proposal_id = e.proposal_id
            WHERE p.proposal_kind='LAUNCH' AND e.reason_code='PROPOSAL_EXPIRED'
              AND e.created_at >= ? AND e.created_at < ?
              AND EXISTS (
                  SELECT 1 FROM owner_action_decisions d
                  WHERE d.proposal_id = e.proposal_id AND d.decision_kind='APPROVE'
              )
            """,
            window,
        )
        funnel["expired_unapproved"] = _one(
            conn,
            """
            SELECT COUNT(DISTINCT e.proposal_id)
            FROM owner_action_events e
            JOIN owner_action_proposals p ON p.proposal_id = e.proposal_id
            WHERE p.proposal_kind='LAUNCH' AND e.reason_code='PROPOSAL_EXPIRED'
              AND e.created_at >= ? AND e.created_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM owner_action_decisions d
                  WHERE d.proposal_id = e.proposal_id AND d.decision_kind='APPROVE'
              )
            """,
            window,
        )

        # Застой конвейера: за последние STALL_WINDOW_DAYS предложения были,
        # а созданных объявлений ноль. «Предложено > 0» — прокси непустого
        # бэклога: сводка не ходит в Trello, но раз продюсер предлагает,
        # значит готовые карточки есть.
        stall_since = (
            until_utc.astimezone(timezone.utc) - timedelta(days=STALL_WINDOW_DAYS)
        ).isoformat()
        funnel["stall_proposed"] = _one(
            conn,
            """
            SELECT COUNT(*) FROM owner_action_proposals
            WHERE proposal_kind='LAUNCH' AND created_at >= ? AND created_at < ?
            """,
            (stall_since, until),
        )
        funnel["stall_created"] = _one(
            conn,
            """
            SELECT COUNT(*) FROM launch_check_audit
            WHERE event_type='CREATE_CONFIRMED' AND created_at >= ? AND created_at < ?
            """,
            (stall_since, until),
        )

        # Кумулятивный хвост висяков — не по окну: висяк недельной давности
        # обязан оставаться в сводке, пока его не закрыли.
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n, MIN(l.updated_at) AS oldest
            FROM owner_action_lifecycle l
            JOIN owner_action_proposals p ON p.proposal_id = l.proposal_id
            WHERE p.proposal_kind='LAUNCH'
              AND l.state IN ({",".join("?" * len(_HANGING_STATES))})
              AND l.updated_at < ?
            """,
            (*_HANGING_STATES, hanging_before),
        ).fetchone()
        funnel["hanging"] = int(row["n"] or 0)
        funnel["hanging_oldest"] = str(row["oldest"] or "") or None

    return funnel


def _fmt_day(iso_utc: str) -> str:
    """ISO UTC → 'ДД.ММ' по локальному времени; нечитаемое значение возвращаем как есть."""
    try:
        moment = datetime.fromisoformat(iso_utc)
        return moment.astimezone(timezone(timedelta(hours=5))).strftime("%d.%m")
    except ValueError:
        return iso_utc


def format_launch_summary(funnel: dict) -> str:
    """HTML-текст сводки. Нули не прячем — формулируем прямо."""
    lines = ["🚀 <b>Запуски за сутки</b>", ""]

    proposed = funnel.get("proposed", 0)
    approved = funnel.get("approved_system", 0) + funnel.get("approved_owner", 0)
    if proposed or approved:
        lines.append(f"Предложено запусков: {proposed}")
        approved_line = f"Одобрено: {approved}"
        if funnel.get("approved_owner"):
            approved_line += f" (из них кнопкой: {funnel['approved_owner']})"
        lines.append(approved_line)
    else:
        lines.append("Предложено запусков: 0 — конвейер не предложил ничего.")

    lines.append("")
    lines.append(f"Создано объявлений: {funnel.get('created_ads', 0)}")
    lines.append(f"Подтверждено запусков: {funnel.get('verified', 0)}")

    denied = funnel.get("review_denied", 0)
    failed = funnel.get("review_failed", 0)
    exp_approved = funnel.get("expired_approved", 0)
    exp_unapproved = funnel.get("expired_unapproved", 0)
    if denied or failed or exp_approved or exp_unapproved:
        lines.append("")
        lines.append("<b>Потери:</b>")
        if denied:
            lines.append(f" • отклонено проверкой перед исполнением: {denied}")
        if failed:
            lines.append(f" • проверка перед исполнением упала: {failed}")
        if exp_approved:
            lines.append(f" • одобрено, но не исполнено до истечения: {exp_approved}")
        if exp_unapproved:
            lines.append(f" • истекло без одобрения: {exp_unapproved}")

    hanging = funnel.get("hanging", 0)
    if hanging:
        oldest = funnel.get("hanging_oldest")
        tail = f" (старейший — {_fmt_day(oldest)})" if oldest else ""
        lines.append("")
        lines.append(
            f"⏳ Висит без подтверждения: {hanging} запусков"
            f" дольше {HANGING_AFTER_HOURS} ч{tail}"
        )

    if funnel.get("stall_proposed", 0) > 0 and funnel.get("stall_created", 0) == 0:
        lines.append("")
        lines.append(
            f"🚨 За {STALL_WINDOW_DAYS} дня: предложений "
            f"{funnel['stall_proposed']}, создано 0 — конвейер запусков стоит."
        )

    if not any(
        (
            proposed,
            approved,
            funnel.get("created_ads", 0),
            funnel.get("verified", 0),
            denied,
            failed,
            exp_approved,
            exp_unapproved,
            hanging,
        )
    ):
        lines.append("")
        lines.append("За сутки конвейер запусков не сделал ничего. Это сигнал, не норма.")

    return "\n".join(lines)


def collect_stop_buttons(
    since_utc: datetime, until_utc: datetime
) -> list[list[tuple[str, str]]]:
    """Кнопки «⏸ Остановить» на запуски окна сводки.

    Источник — stop_map auto_launch (карточка → ad_ids), callback
    ``stop_launch:<card_id>`` обрабатывает существующий поллер Telegram:
    сводка ничего не изобретает, только показывает уже готовый рычаг.
    Ошибка чтения state не роняет сводку — кнопок просто не будет.
    """
    try:
        from services.auto_launch import _load_auto_launch_state
        from services.formatting import truncate_at_word_boundary

        stop_map = _load_auto_launch_state().get("stop_map")
        if not isinstance(stop_map, dict):
            return []
        fresh: list[tuple[datetime, str, str]] = []
        for card_id, entry in stop_map.items():
            if not isinstance(entry, dict) or entry.get("stopped"):
                continue
            if not entry.get("ad_ids"):
                continue
            try:
                at = datetime.fromisoformat(str(entry.get("at") or ""))
            except ValueError:
                continue
            if at.tzinfo is None or not (since_utc <= at.astimezone(timezone.utc) < until_utc):
                continue
            callback_data = f"stop_launch:{card_id}"
            if len(callback_data.encode("utf-8")) > 64:
                continue
            name = str(entry.get("name") or card_id)
            fresh.append((at, name, callback_data))
        fresh.sort(key=lambda item: item[0], reverse=True)
        return [
            [(f"⏸ Остановить «{truncate_at_word_boundary(name, 22)}»", callback_data)]
            for _at, name, callback_data in fresh[:MAX_STOP_BUTTONS]
        ]
    except Exception as exc:  # noqa: BLE001 — кнопки вторичны, сводка важнее
        logger.warning("launch_summary: стоп-кнопки не собрались — %s", exc)
        return []


def send_launch_summary(
    now: datetime | None = None, *, since: str | None = None
) -> bool:
    """Собирает воронку и шлёт сводку. Отправляется всегда, включая нулевую.

    ``since`` (ISO UTC) — окно «с прошлой сводки»; без него — последние сутки.
    """
    until = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            since_dt = until - timedelta(days=1)
    else:
        since_dt = until - timedelta(days=1)

    from services.notifications import send_telegram

    try:
        funnel = collect_launch_funnel(since_dt, until)
    except Exception as exc:
        # Прибор обязан падать громко: молчащая сводка — ровно тот сбой,
        # который она построена ловить.
        logger.error("launch_summary: не удалось собрать воронку — %s", exc)
        send_telegram(
            "⚠️ Сводка запусков не собралась: "
            f"{type(exc).__name__}. Воронку за сутки посмотреть некому."
        )
        return False

    text = format_launch_summary(funnel)
    buttons = collect_stop_buttons(since_dt, until)
    if buttons:
        from services.telegram_bot import send_with_buttons

        return bool(send_with_buttons(text, buttons))
    return bool(send_telegram(text))
