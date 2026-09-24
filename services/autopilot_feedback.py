"""
Хранилище фидбэка владельца на решения автопилота.

Таблица autopilot_feedback в data/decisions.db.
Фидбэк связан с решением по (ad_id, action).
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path  # noqa: F401 — используется в owner_decision_counts

logger = logging.getLogger(__name__)

# Используем тот же файл БД что и agent/database.py
_DATA_DIR = Path(__file__).parent.parent / "data"
_TZ_LOCAL = timezone(timedelta(hours=5))


def _get_connection() -> sqlite3.Connection:
    """Возвращает соединение к БД decisions.db."""
    from agent.database import DB_PATH
    if DB_PATH is None:
        raise RuntimeError("БД не инициализирована. Вызовите init_db() при старте.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table() -> None:
    """Создаёт таблицу autopilot_feedback если не существует. Идемпотентно."""
    conn = _get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS autopilot_feedback (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id     TEXT NOT NULL,
                action    TEXT NOT NULL,
                verdict   TEXT NOT NULL CHECK(verdict IN ('up', 'down')),
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_apfb_ad_action "
            "ON autopilot_feedback(ad_id, action)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_apfb_created "
            "ON autopilot_feedback(created_at)"
        )
        conn.commit()
    finally:
        conn.close()


def save_feedback(ad_id: str, action: str, verdict: str) -> None:
    """Сохраняет оценку владельца на решение автопилота.

    Args:
        ad_id:   идентификатор объявления FB
        action:  'p' (pause) или 's' (scale) — короткий код из callback
        verdict: 'up' (правильно) или 'down' (неправильно)
    """
    ensure_table()
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO autopilot_feedback (ad_id, action, verdict) VALUES (?, ?, ?)",
            (ad_id, action, verdict),
        )
        conn.commit()
    finally:
        conn.close()


def owner_decision_counts(
    db_path,
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[int, int, tuple[str, ...], tuple[str, ...]]:
    """Решённые решения владельца за окно: (approve, reject, approve_ids, reject_ids).

    Источник — ``owner_action_decisions`` (migration 022). Терминальными считаются
    только APPROVE/REJECT: POSTPONE это «ещё не решено» и в знаменатель согласия
    не входит. Читаем строго на чтение; отсутствие таблицы — не ошибка, а нули
    (у старой БД контура одобрения ещё нет).
    """
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("Границы окна должны содержать timezone")
    start = window_start.astimezone(timezone.utc)
    end = window_end.astimezone(timezone.utc)
    try:
        conn = sqlite3.connect(
            f"file:{Path(db_path)}?mode=ro",
            uri=True,
            timeout=30,
        )
    except sqlite3.Error:
        return 0, 0, (), ()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT decision_id, decision_kind, recorded_at
            FROM owner_action_decisions
            WHERE decision_kind IN ('APPROVE','REJECT')
            ORDER BY recorded_at, decision_id
            """
        ).fetchall()
    except sqlite3.Error:
        # Таблицы нет (или БД повреждена) — метрика просто не имеет источника.
        return 0, 0, (), ()
    finally:
        conn.close()

    approve_ids: list[str] = []
    reject_ids: list[str] = []
    for row in rows:
        try:
            recorded_at = datetime.fromisoformat(str(row["recorded_at"]))
        except ValueError:
            continue
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=timezone.utc)
        if not start <= recorded_at.astimezone(timezone.utc) < end:
            continue
        if str(row["decision_kind"]) == "APPROVE":
            approve_ids.append(str(row["decision_id"]))
        else:
            reject_ids.append(str(row["decision_id"]))
    return (
        len(approve_ids),
        len(reject_ids),
        tuple(approve_ids),
        tuple(reject_ids),
    )


def get_feedback_stats(since_days: int = 7) -> dict:
    """Возвращает агрегированную статистику согласия владельца за N дней.

    Основной источник — РЕАЛЬНЫЕ решения владельца ``owner_action_decisions``
    (доля APPROVE от решённых): кнопок 👍/👎 больше нет. Пока в окне нет ни одного
    терминального решения, отдаём наследие ``autopilot_feedback``, чтобы историю
    старых недель не обнулять.

    Returns:
        {
            "up": int,           # APPROVE (наследие: 👍)
            "down": int,         # REJECT  (наследие: 👎)
            "total": int,        # сумма
            "agreement_pct": float | None,  # up/total * 100, None если total == 0
        }
    """
    now_local = datetime.now(_TZ_LOCAL)
    # Наследственная таблица создаётся всегда: кросс-проверка отчёта
    # (approval_source_decisions) читает её схему и без таблицы отдаёт
    # incomplete, даже если согласие считается по решениям владельца.
    ensure_table()
    from agent.database import DB_PATH

    if DB_PATH is not None:
        approve, reject, _, _ = owner_decision_counts(
            DB_PATH,
            window_start=now_local - timedelta(days=since_days),
            window_end=now_local,
        )
        if approve + reject > 0:
            return {
                "up": approve,
                "down": reject,
                "total": approve + reject,
                "agreement_pct": round(approve / (approve + reject) * 100, 1),
            }

    conn = _get_connection()
    try:
        # Период: N дней назад от текущего момента (локальное время)
        since = (
            datetime.now(_TZ_LOCAL) - timedelta(days=since_days)
        ).strftime("%Y-%m-%d %H:%M:%S")

        rows = conn.execute(
            """
            SELECT verdict, COUNT(*) as cnt
            FROM autopilot_feedback
            WHERE created_at >= ?
            GROUP BY verdict
            """,
            (since,),
        ).fetchall()

        counts: dict[str, int] = {}
        for row in rows:
            counts[row["verdict"]] = row["cnt"]

        up = counts.get("up", 0)
        down = counts.get("down", 0)
        total = up + down

        agreement_pct: float | None = None
        if total > 0:
            agreement_pct = round(up / total * 100, 1)

        return {
            "up": up,
            "down": down,
            "total": total,
            "agreement_pct": agreement_pct,
        }
    finally:
        conn.close()
