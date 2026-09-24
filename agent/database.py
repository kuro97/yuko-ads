"""
SQLite хранилище для истории решений.

Миграция с JSON-файлов. Все решения сохраняются с датой, метриками и причиной.
"""

import json
import hashlib
import sqlite3
from pathlib import Path

from services.database_migrations import (
    apply_runtime_migrations,
    verify_runtime_schema,
)

DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH = None  # устанавливается в init_db()


def _get_connection() -> sqlite3.Connection:
    """Возвращает соединение к текущей БД."""
    if DB_PATH is None:
        raise RuntimeError("БД не инициализирована. Вызовите init_db() при старте.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = None, json_path: str = None) -> str:
    """Инициализирует БД: создаёт таблицу, индексы, мигрирует JSON.
    json_path — путь к decisions.json для миграции (None = data/decisions.json)."""
    global DB_PATH
    if db_path is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_path = str(DATA_DIR / "decisions.db")
    DB_PATH = db_path

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id TEXT NOT NULL,
                ad_name TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                confirmed_by TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                spend REAL,
                leads INTEGER,
                cpl REAL
            );
            CREATE INDEX IF NOT EXISTS idx_decisions_action ON decisions(action);
            CREATE INDEX IF NOT EXISTS idx_decisions_created_at ON decisions(created_at);
            CREATE INDEX IF NOT EXISTS idx_decisions_ad_id ON decisions(ad_id);
        """)

        # Миграция: добавляем новые колонки метрик (S15)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        for col, col_type in [("ctr", "REAL"), ("cpm", "REAL"), ("romi", "REAL"), ("qual_pct", "REAL"), ("effect_id", "TEXT")]:
            if col not in existing:
                conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} {col_type}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_effect_id "
            "ON decisions(effect_id) WHERE effect_id IS NOT NULL"
        )
        conn.commit()
    finally:
        conn.close()

    # Единый fail-closed путь миграций 019–022 до запуска фоновых workers.
    apply_runtime_migrations(DB_PATH)
    verify_runtime_schema(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    try:
        count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    finally:
        conn.close()
    if count == 0:
        migrate_from_json(json_path)

    return DB_PATH


def save_decision(ad_id: str, ad_name: str, action: str, reason: str,
                  confirmed_by: str = "user", spend=None, leads=None, cpl=None,
                  ctr=None, cpm=None, romi=None, qual_pct=None,
                  effect_id: str | None = None,
                  projection_kind: str | None = None,
                  projection_payload: dict | None = None,
                  outbox_channel: str | None = None,
                  outbox_payload: dict | None = None) -> bool:
    """Атомарно сохраняет gateway effect и решение в одной transaction."""
    conn = _get_connection()
    try:
        decision_payload = (
            ad_id, ad_name, action, reason, confirmed_by, spend, leads,
            cpl, ctr, cpm, romi, qual_pct,
        )
        payload_sha256 = hashlib.sha256(
            json.dumps(
                decision_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if (projection_kind is None) != (projection_payload is None):
            raise ValueError("projection_kind и projection_payload задаются вместе")
        if (outbox_channel is None) != (outbox_payload is None):
            raise ValueError("outbox_channel и outbox_payload задаются вместе")
        if outbox_channel is not None and effect_id is None:
            raise ValueError("Outbox требует effect_id")
        projection_json = (
            json.dumps(
                projection_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if projection_payload is not None
            else None
        )
        outbox_json = (
            json.dumps(
                outbox_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if outbox_payload is not None
            else None
        )
        conn.execute("BEGIN IMMEDIATE")
        if effect_id is not None:
            operation_id = effect_id.split(":decision:", 1)[0]
            if not operation_id or operation_id == effect_id:
                raise ValueError("effect_id должен быть связан с operation_id")
            effect_kind = f"decision:{action}"
            existing_effect = conn.execute(
                """SELECT operation_id, effect_kind, payload_sha256, state
                   FROM action_effects WHERE effect_id = ?""",
                (effect_id,),
            ).fetchone()
            if existing_effect is not None:
                expected_effect = (operation_id, effect_kind, payload_sha256, "APPLIED")
                if tuple(existing_effect) != expected_effect:
                    raise sqlite3.IntegrityError("action effect payload conflict")
                existing_decision = conn.execute(
                    """SELECT ad_id, ad_name, action, reason, confirmed_by, spend, leads,
                              cpl, ctr, cpm, romi, qual_pct
                       FROM decisions WHERE effect_id = ?""",
                    (effect_id,),
                ).fetchone()
                if existing_decision is None or tuple(existing_decision) != decision_payload:
                    raise sqlite3.IntegrityError("action effect projection conflict")
                if projection_kind is not None:
                    projection = conn.execute(
                        """SELECT projection_kind, subject_id, payload_json
                           FROM action_state_projections WHERE effect_id=?""",
                        (effect_id,),
                    ).fetchone()
                    if projection is None or tuple(projection) != (
                        projection_kind,
                        ad_id,
                        projection_json,
                    ):
                        raise sqlite3.IntegrityError("action state projection conflict")
                if outbox_channel is not None:
                    outbox = conn.execute(
                        """SELECT channel, typed_payload_json
                           FROM action_outbox WHERE effect_id=?""",
                        (effect_id,),
                    ).fetchone()
                    if outbox is None or tuple(outbox) != (outbox_channel, outbox_json):
                        raise sqlite3.IntegrityError("action outbox payload conflict")
                conn.commit()
                return False
            legacy_decision = conn.execute(
                """SELECT ad_id, ad_name, action, reason, confirmed_by, spend, leads,
                          cpl, ctr, cpm, romi, qual_pct
                   FROM decisions WHERE effect_id = ?""",
                (effect_id,),
            ).fetchone()
            if legacy_decision is not None:
                if tuple(legacy_decision) != decision_payload:
                    raise sqlite3.IntegrityError("legacy action effect payload conflict")
                conn.execute(
                    """INSERT INTO action_effects
                       (effect_id,operation_id,effect_kind,payload_sha256,state,applied_at)
                       VALUES (?,?,?,?,'APPLIED',datetime('now'))""",
                    (effect_id, operation_id, effect_kind, payload_sha256),
                )
                if projection_kind is not None and projection_json is not None:
                    conn.execute(
                        """INSERT INTO action_state_projections
                           (effect_id,projection_kind,subject_id,payload_json)
                           VALUES (?,?,?,?)""",
                        (effect_id, projection_kind, ad_id, projection_json),
                    )
                if outbox_channel is not None and outbox_json is not None:
                    conn.execute(
                        """INSERT INTO action_outbox
                           (effect_id,channel,typed_payload_json,state)
                           VALUES (?,?,?,'PENDING')""",
                        (effect_id, outbox_channel, outbox_json),
                    )
                conn.commit()
                return False
            conn.execute(
                """INSERT INTO action_effects
                   (effect_id, operation_id, effect_kind, payload_sha256, state)
                   VALUES (?, ?, ?, ?, 'PENDING')""",
                (effect_id, operation_id, effect_kind, payload_sha256),
            )
        try:
            cursor = conn.execute(
                """INSERT INTO decisions (ad_id, ad_name, action, reason, confirmed_by,
               spend, leads, cpl, ctr, cpm, romi, qual_pct, effect_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*decision_payload, effect_id),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            raise
        if effect_id is not None:
            if projection_kind is not None and projection_json is not None:
                conn.execute(
                    """INSERT INTO action_state_projections
                       (effect_id,projection_kind,subject_id,payload_json)
                       VALUES (?,?,?,?)""",
                    (effect_id, projection_kind, ad_id, projection_json),
                )
            if outbox_channel is not None and outbox_json is not None:
                conn.execute(
                    """INSERT INTO action_outbox
                       (effect_id,channel,typed_payload_json,state)
                       VALUES (?,?,?,'PENDING')""",
                    (effect_id, outbox_channel, outbox_json),
                )
            updated = conn.execute(
                """UPDATE action_effects SET state = 'APPLIED', applied_at = datetime('now')
                   WHERE effect_id = ? AND state = 'PENDING'""",
                (effect_id,),
            )
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("action effect commit conflict")
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_action_outbox(effect_id: str) -> dict | None:
    """Атомарно арендует pending notification; доставка at-least-once."""

    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM action_outbox WHERE effect_id=?",
            (effect_id,),
        ).fetchone()
        if row is None or row["state"] == "SENT":
            conn.commit()
            return None
        if row["state"] == "SENDING" and row["lease_until"] is not None:
            still_leased = conn.execute(
                "SELECT datetime(?) > datetime('now')",
                (row["lease_until"],),
            ).fetchone()[0]
            if still_leased:
                conn.commit()
                return None
        conn.execute(
            """UPDATE action_outbox
               SET state='SENDING',attempts=attempts+1,lease_until=datetime('now','+1 minute')
               WHERE effect_id=?""",
            (effect_id,),
        )
        conn.commit()
        return {
            "channel": row["channel"],
            "payload": json.loads(row["typed_payload_json"]),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_action_outbox_sent(effect_id: str) -> None:
    """Фиксирует успешную доставку; crash до commit допускает повтор."""

    conn = _get_connection()
    try:
        updated = conn.execute(
            """UPDATE action_outbox
               SET state='SENT',sent_at=datetime('now'),lease_until=NULL
               WHERE effect_id=? AND state='SENDING'""",
            (effect_id,),
        )
        if updated.rowcount != 1:
            raise RuntimeError("action outbox не находится в SENDING")
        conn.commit()
    finally:
        conn.close()


def get_action_state_projections(
    projection_kind: str,
    subject_id: str | None = None,
) -> list[dict]:
    """Читает immutable state events в transaction order."""

    conn = _get_connection()
    try:
        if subject_id is None:
            rows = conn.execute(
                """SELECT effect_id,subject_id,payload_json,created_at
                   FROM action_state_projections
                   WHERE projection_kind=? ORDER BY rowid""",
                (projection_kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT effect_id,subject_id,payload_json,created_at
                   FROM action_state_projections
                   WHERE projection_kind=? AND subject_id=? ORDER BY rowid""",
                (projection_kind, subject_id),
            ).fetchall()
        return [
            {
                "effect_id": row["effect_id"],
                "subject_id": row["subject_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def get_decisions(limit: int = 100) -> list[dict]:
    """Возвращает последние решения (backward compat с JSON форматом)."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "ad_id": r["ad_id"],
                "ad_name": r["ad_name"],
                "action": r["action"],
                "reason": r["reason"],
                "confirmed_by": r["confirmed_by"],
                "timestamp": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_decisions_history(action=None, ad_name=None, date_from=None, date_to=None,
                          limit: int = 50, offset: int = 0) -> dict:
    """Возвращает историю решений с фильтрами и пагинацией."""
    # Ограничение limit
    limit = min(limit, 200)

    conditions = []
    params = []

    if action:
        conditions.append("action = ?")
        params.append(action)
    if ad_name:
        conditions.append("ad_name LIKE ?")
        params.append(f"%{ad_name}%")
    if date_from:
        conditions.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("created_at < date(?, '+1 day')")
        params.append(date_to)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    conn = _get_connection()
    try:
        # Общее количество с учётом фильтров
        total = conn.execute(
            f"SELECT COUNT(*) FROM decisions {where}", params
        ).fetchone()[0]

        # Данные с пагинацией
        rows = conn.execute(
            f"SELECT * FROM decisions {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        decisions = [
            {
                "id": r["id"],
                "ad_id": r["ad_id"],
                "ad_name": r["ad_name"],
                "action": r["action"],
                "reason": r["reason"],
                "confirmed_by": r["confirmed_by"],
                "timestamp": r["created_at"],
                "spend": r["spend"],
                "leads": r["leads"],
                "cpl": r["cpl"],
                "ctr": r["ctr"],
                "cpm": r["cpm"],
                "romi": r["romi"],
                "qual_pct": r["qual_pct"],
            }
            for r in rows
        ]

        return {"decisions": decisions, "total": total, "limit": limit, "offset": offset}
    finally:
        conn.close()


def get_decisions_for_ad(ad_id: str) -> list[dict]:
    """Все решения для ad_id, sorted by created_at DESC."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE ad_id = ? ORDER BY id DESC",
            (ad_id,),
        ).fetchall()
        return [
            {
                "action": r["action"],
                "reason": r["reason"],
                "confirmed_by": r["confirmed_by"],
                "timestamp": r["created_at"],
                "metrics": {
                    "spend": r["spend"],
                    "leads": r["leads"],
                    "cpl": r["cpl"],
                    "ctr": r["ctr"],
                    "cpm": r["cpm"],
                    "romi": r["romi"],
                    "qual_pct": r["qual_pct"],
                },
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_decision_counts() -> dict[str, int]:
    """Возвращает {ad_id: количество_решений} для всех ad_id."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT ad_id, COUNT(*) as cnt FROM decisions GROUP BY ad_id"
        ).fetchall()
        return {r["ad_id"]: r["cnt"] for r in rows}
    finally:
        conn.close()


def has_repeat_problem(ad_id: str, current_recommendation: str) -> bool:
    """True если объявление уже отключали (PAUSED) и снова рекомендация ОТКЛЮЧИТЬ."""
    if current_recommendation != "ОТКЛЮЧИТЬ":
        return False
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM decisions WHERE ad_id = ? AND action = 'PAUSED'",
            (ad_id,),
        ).fetchone()
        return row["cnt"] > 0
    finally:
        conn.close()


def migrate_from_json(json_path: str = None):
    """Мигрирует данные из decisions.json в SQLite."""
    if json_path is None:
        json_path = str(DATA_DIR / "decisions.json")

    path = Path(json_path)
    if not path.exists():
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return

    if not data:
        return

    conn = _get_connection()
    try:
        for item in data:
            conn.execute(
                """INSERT INTO decisions (ad_id, ad_name, action, reason, confirmed_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    item.get("ad_id", ""),
                    item.get("ad_name", ""),
                    item.get("action", ""),
                    item.get("reason", ""),
                    item.get("confirmed_by", "user"),
                    item.get("timestamp", ""),
                ),
            )
        conn.commit()
    finally:
        conn.close()
