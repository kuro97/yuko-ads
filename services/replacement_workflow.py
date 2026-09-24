"""Durable workflow замены рекламы и журнал безопасной чистки adset.

Модуль хранит только состояние. Он не удаляет и не ставит объявления на паузу.
Любая неопределённость Facebook оставляет старую рекламу включённой.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_TZ_LOCAL = timezone(timedelta(hours=5))
_OPEN_PHASES = (
    "WAITING_SLOT",
    "WAITING_CARD",
    "LAUNCHING",
    "WAITING_ACTIVE",
    "READY_TO_PAUSE",
    "BLOCKED",
)
_CLAIMABLE_PHASES = ("WAITING_SLOT", "WAITING_CARD")
_AUDIT_ACTIONS = {
    "DRY_RUN_CANDIDATE",
    "SKIPPED",
    "DELETE_ATTEMPT",
    "DELETED",
    "DELETE_FAILED",
}
_REPLACEMENT_EVENT_TYPES = {
    "ENQUEUED",
    "SLOT_RESERVED",
    "LAUNCH_CLAIMED",
    "CREATE_RECORDED",
    "RECONCILED",
    "ACTIVE_CONFIRMED",
    "OLD_PAUSE_ATTEMPT",
    "OLD_PAUSED",
    "BLOCKED",
    "CANCELLED",
}
_SENSITIVE_JSON_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "header",
    "headers",
    "password",
    "proxy_authorization",
    "refresh_token",
    "secret",
    "set_cookie",
    "x_api_key",
}
_URL_WITH_QUERY_RE = re.compile(r"https?://[^\s?#]+\?[^\s#]*", re.IGNORECASE)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/=]+"),
    re.compile(
        r"(?i)\b(access[_-]?token|api[_-]?key|authorization|password|secret)"
        r"\s*[:=]\s*['\"]?[^\s&,'\"}]+"
    ),
    re.compile(r"\bEAA[A-Za-z0-9_-]{8,}\b"),
)


class WorkflowStateError(RuntimeError):
    """Запрошен недопустимый или небезопасный переход workflow."""


def _now_iso() -> str:
    return datetime.now(_TZ_LOCAL).isoformat(timespec="seconds")


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} не должен быть пустым")
    return text


def _optional_text(value: object) -> str:
    return str(value or "").strip()


def sanitize_text(value: object) -> str:
    """Удаляет токены и типовые секреты перед логом/БД/отчётом."""
    text = str(value or "")
    text = _URL_WITH_QUERY_RE.sub(
        lambda match: f"{match.group(0).split('?', 1)[0]}?<redacted>",
        text,
    )
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            safe_key = sanitize_text(key)
            normalized_key = safe_key.strip().lower().replace("-", "_")
            contains_sensitive_name = any(
                marker in normalized_key
                for marker in (
                    "api_key",
                    "authorization",
                    "password",
                    "secret",
                    "token",
                )
            )
            result[safe_key] = (
                "[REDACTED]"
                if normalized_key in _SENSITIVE_JSON_KEYS or contains_sensitive_name
                else _sanitize_json_value(nested)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def _json_dumps(value: Mapping[str, Any] | Sequence[Any]) -> str:
    """Сериализует канонический JSON после обязательной redaction."""
    return json.dumps(
        _sanitize_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def _get_connection() -> sqlite3.Connection:
    """Открывает тот же SQLite, который инициализирует creative_intelligence."""
    from services.creative_intelligence import DB_PATH

    if DB_PATH is None:
        raise RuntimeError("KB не инициализирована. Вызовите init_kb() при старте.")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _append_replacement_event(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    event_type: str,
    actor: str,
    evidence: Mapping[str, Any] | None = None,
    error: str | None = None,
    created_at: str | None = None,
) -> int:
    """Добавляет событие внутри уже открытой транзакции вызывающего кода."""
    event_type = _required_text(event_type, "event_type").upper()
    if event_type not in _REPLACEMENT_EVENT_TYPES:
        raise ValueError(f"неподдерживаемый replacement event: {event_type}")
    actor = sanitize_text(_required_text(actor, "actor"))
    if evidence is not None and not isinstance(evidence, Mapping):
        raise TypeError("evidence должен быть Mapping или None")
    cursor = conn.execute(
        """
        INSERT INTO ad_replacement_events (
            workflow_id, event_type, actor, evidence_json, error, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            workflow_id,
            event_type,
            actor,
            _json_dumps(evidence or {}),
            sanitize_text(error) if error else None,
            created_at or _now_iso(),
        ),
    )
    return int(cursor.lastrowid)


def append_replacement_event(
    workflow_id: str,
    event_type: str,
    actor: str,
    evidence: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> int:
    """Append-only сохраняет sanitized событие существующего workflow."""
    workflow_id = _required_text(workflow_id, "workflow_id")
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM ad_replacement_workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if exists is None:
            raise WorkflowStateError(f"workflow {workflow_id} не найден")
        event_id = _append_replacement_event(
            conn,
            workflow_id=workflow_id,
            event_type=event_type,
            actor=actor,
            evidence=evidence,
            error=error,
        )
        conn.commit()
        return event_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def get_workflow(workflow_id: str) -> dict[str, Any] | None:
    """Возвращает workflow без изменения состояния."""
    workflow_id = _required_text(workflow_id, "workflow_id")
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM ad_replacement_workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        return _row_dict(row)
    finally:
        conn.close()


def enqueue_replacement(
    old_ad_id: str,
    old_ad_name: str,
    adset_id: str,
    city: str,
    adset_type: str,
) -> str:
    """Создаёт одну открытую заявку по ``(old_ad_id, adset_id)``.

    Повторный вызов возвращает существующий workflow, включая ``BLOCKED``:
    блокировка требует разбора, а не создания параллельного дубля.
    """
    old_ad_id = _required_text(old_ad_id, "old_ad_id")
    adset_id = _required_text(adset_id, "adset_id")
    old_ad_name = _optional_text(old_ad_name)
    city = _optional_text(city)
    adset_type = _optional_text(adset_type)
    now = _now_iso()

    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in _OPEN_PHASES)
        row = conn.execute(
            f"SELECT workflow_id FROM ad_replacement_workflows "
            f"WHERE old_ad_id = ? AND adset_id = ? AND phase IN ({placeholders}) "
            "ORDER BY created_at LIMIT 1",
            (old_ad_id, adset_id, *_OPEN_PHASES),
        ).fetchone()
        if row is not None:
            conn.commit()
            return str(row["workflow_id"])

        workflow_id = f"replacement-{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO ad_replacement_workflows (
                workflow_id, old_ad_id, old_ad_name, adset_id, city,
                adset_type, phase, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'WAITING_SLOT', ?, ?)
            """,
            (
                workflow_id,
                old_ad_id,
                old_ad_name,
                adset_id,
                city,
                adset_type,
                now,
                now,
            ),
        )
        _append_replacement_event(
            conn,
            workflow_id=workflow_id,
            event_type="ENQUEUED",
            actor="replacement-workflow",
            evidence={"old_ad_id": old_ad_id, "adset_id": adset_id},
            created_at=now,
        )
        conn.commit()
        return workflow_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_unique_texts(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{field_name} должен быть Sequence[str]")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{field_name} должен содержать только строки")
        normalized.append(_required_text(value, field_name))
    if not normalized:
        raise ValueError(f"{field_name} не должен быть пустым")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} должен содержать уникальные значения")
    return tuple(normalized)


def _load_unique_texts(value: object, field_name: str) -> tuple[str, ...]:
    try:
        loaded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkflowStateError(f"durable {field_name} содержит invalid JSON") from exc
    try:
        return _normalize_unique_texts(loaded, field_name)
    except (TypeError, ValueError) as exc:
        raise WorkflowStateError(f"durable {field_name} некорректен") from exc


def _load_optional_unique_texts(value: object, field_name: str) -> tuple[str, ...]:
    try:
        loaded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkflowStateError(f"durable {field_name} содержит invalid JSON") from exc
    if loaded == []:
        return ()
    try:
        return _normalize_unique_texts(loaded, field_name)
    except (TypeError, ValueError) as exc:
        raise WorkflowStateError(f"durable {field_name} некорректен") from exc


def _validate_durable_launch_link(
    link: sqlite3.Row,
    *,
    workflow_id: str,
    city: str,
    adset_id: str,
    require_uncreated: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Fail-closed валидирует immutable link перед каждым CAS к CREATE."""
    required_fields = (
        "workflow_id",
        "launch_attempt_key",
        "card_id",
        "city",
        "account_kind",
        "account_id",
        "adset_id",
        "media_manifest_sha256",
    )
    if any(not _optional_text(link[field]) for field in required_fields):
        raise WorkflowStateError("durable launch link неполный")
    if link["workflow_id"] != workflow_id:
        raise WorkflowStateError("durable launch link workflow_id drift")
    if link["city"] != city or link["adset_id"] != adset_id:
        raise WorkflowStateError("durable launch link scope drift")
    if link["account_kind"] not in {"offline", "online"}:
        raise WorkflowStateError("durable launch link account_kind некорректен")
    expected_names = _load_unique_texts(
        link["expected_ad_names_json"],
        "expected_ad_names_json",
    )
    if type(link["expected_ad_count"]) is not int or int(
        link["expected_ad_count"]
    ) != len(expected_names):
        raise WorkflowStateError("durable expected_ad_count не совпадает с names")
    created_ad_ids = _load_optional_unique_texts(
        link["created_ad_ids_json"],
        "created_ad_ids_json",
    )
    if require_uncreated and created_ad_ids:
        raise WorkflowStateError("launch link уже содержит created IDs до CREATE")
    return expected_names, created_ad_ids


def get_replacement_launch(workflow_id: str) -> dict[str, Any] | None:
    """Возвращает immutable launch link без изменения состояния."""
    workflow_id = _required_text(workflow_id, "workflow_id")
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM ad_replacement_launch_links WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        return _row_dict(row)
    finally:
        conn.close()


def link_replacement_launch(
    workflow_id: str,
    launch_attempt_key: str,
    card_id: str,
    card_name: str,
    city: str,
    account_kind: str,
    account_id: str,
    adset_id: str,
    expected_ad_names: Sequence[str],
    expected_ad_count: int,
    media_manifest_sha256: str,
) -> None:
    """До CREATE связывает WAITING_SLOT workflow с immutable launch manifest."""
    workflow_id = _required_text(workflow_id, "workflow_id")
    launch_attempt_key = _required_text(launch_attempt_key, "launch_attempt_key")
    card_id = _required_text(card_id, "card_id")
    card_name = _optional_text(card_name)
    city = _required_text(city, "city")
    account_kind = _required_text(account_kind, "account_kind").lower()
    if account_kind not in {"offline", "online"}:
        raise ValueError("account_kind должен быть offline или online")
    account_id = _required_text(account_id, "account_id")
    adset_id = _required_text(adset_id, "adset_id")
    expected_names = _normalize_unique_texts(
        expected_ad_names,
        "expected_ad_names",
    )
    if type(expected_ad_count) is not int or expected_ad_count != len(expected_names):
        raise ValueError("expected_ad_count должен совпадать с expected_ad_names")
    media_manifest_sha256 = _required_text(
        media_manifest_sha256,
        "media_manifest_sha256",
    )
    expected_names_json = _json_dumps(expected_names)
    now = _now_iso()

    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute(
            """
            SELECT phase, city, adset_id, replacement_ad_id, released_ad_id
            FROM ad_replacement_workflows WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        if state is None:
            raise WorkflowStateError(f"workflow {workflow_id} не найден")
        existing = conn.execute(
            "SELECT * FROM ad_replacement_launch_links WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if existing is not None:
            durable_names = _load_unique_texts(
                existing["expected_ad_names_json"],
                "expected_ad_names_json",
            )
            exact_match = (
                existing["launch_attempt_key"] == launch_attempt_key
                and existing["card_id"] == card_id
                and existing["card_name"] == card_name
                and existing["city"] == city
                and existing["account_kind"] == account_kind
                and existing["account_id"] == account_id
                and existing["adset_id"] == adset_id
                and int(existing["expected_ad_count"]) == expected_ad_count
                and durable_names == expected_names
                and existing["media_manifest_sha256"] == media_manifest_sha256
            )
            if exact_match:
                conn.commit()
                return
            raise WorkflowStateError("immutable replacement launch link уже отличается")
        if state["phase"] != "WAITING_SLOT":
            raise WorkflowStateError(
                f"launch link нельзя создать из фазы {state['phase']}"
            )
        if state["city"] != city or state["adset_id"] != adset_id:
            raise WorkflowStateError("launch link scope не совпадает с workflow")
        if _optional_text(state["replacement_ad_id"]) or _optional_text(
            state["released_ad_id"]
        ):
            raise WorkflowStateError("launch link требует workflow без ad references")
        attempt_owner = conn.execute(
            """
            SELECT workflow_id FROM ad_replacement_launch_links
            WHERE launch_attempt_key = ?
            """,
            (launch_attempt_key,),
        ).fetchone()
        if attempt_owner is not None:
            raise WorkflowStateError("launch_attempt_key уже связан с другим workflow")
        conn.execute(
            """
            INSERT INTO ad_replacement_launch_links (
                workflow_id, launch_attempt_key, card_id, card_name, city,
                account_kind, account_id, adset_id, expected_ad_count,
                expected_ad_names_json, media_manifest_sha256,
                created_ad_ids_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
            """,
            (
                workflow_id,
                launch_attempt_key,
                card_id,
                card_name,
                city,
                account_kind,
                account_id,
                adset_id,
                expected_ad_count,
                expected_names_json,
                media_manifest_sha256,
                now,
                now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_slot_available(workflow_id: str, released_ad_id: str | None = None) -> None:
    """Фиксирует подтверждённый свободный слот: ``WAITING_SLOT -> WAITING_CARD``.

    ``released_ad_id`` остаётся NULL, если слот уже был свободен и DELETE не было.
    Повтор с тем же значением безопасен.
    """
    workflow_id = _required_text(workflow_id, "workflow_id")
    released = _optional_text(released_ad_id) or None
    now = _now_iso()
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT phase, old_ad_id, city, adset_id,
                   replacement_ad_id, released_ad_id
            FROM ad_replacement_workflows WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        if row is None:
            raise WorkflowStateError(f"workflow {workflow_id} не найден")
        if row["phase"] == "WAITING_CARD" and row["released_ad_id"] == released:
            conn.commit()
            return
        if row["phase"] != "WAITING_SLOT":
            raise WorkflowStateError(f"слот нельзя подтвердить из фазы {row['phase']}")
        if released is not None and released in {
            _optional_text(row["old_ad_id"]),
            _optional_text(row["replacement_ad_id"]),
        }:
            raise WorkflowStateError("released_ad_id конфликтует с workflow references")
        link = conn.execute(
            "SELECT * FROM ad_replacement_launch_links WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if link is None:
            raise WorkflowStateError("слот нельзя подтвердить без launch link")
        _validate_durable_launch_link(
            link,
            workflow_id=workflow_id,
            city=str(row["city"]),
            adset_id=str(row["adset_id"]),
            require_uncreated=True,
        )
        cursor = conn.execute(
            """
            UPDATE ad_replacement_workflows
            SET phase = 'WAITING_CARD', released_ad_id = ?, updated_at = ?, last_error = NULL
            WHERE workflow_id = ? AND phase = 'WAITING_SLOT'
            """,
            (released, now, workflow_id),
        )
        if cursor.rowcount != 1:
            raise WorkflowStateError("WAITING_SLOT -> WAITING_CARD CAS не выполнен")
        _append_replacement_event(
            conn,
            workflow_id=workflow_id,
            event_type="SLOT_RESERVED",
            actor="replacement-workflow",
            evidence={
                "launch_attempt_key": link["launch_attempt_key"],
                "released_ad_id": released,
            },
            created_at=now,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_waiting_workflow(adset_id: str) -> dict[str, Any] | None:
    """Атомарно забирает старейшую ожидающую заявку для запуска.

    Вызывающий код обязан перед вызовом живым запросом подтвердить ёмкость adset.
    CREATE разрешён только после immutable link и ``WAITING_CARD``; CAS переводит
    ровно одну строку в ``LAUNCHING`` и атомарно пишет событие.
    """
    adset_id = _required_text(adset_id, "adset_id")
    now = _now_iso()
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT workflow.workflow_id, workflow.city, workflow.adset_id,
                   launch.launch_attempt_key
            FROM ad_replacement_workflows AS workflow
            JOIN ad_replacement_launch_links AS launch
                ON launch.workflow_id = workflow.workflow_id
            WHERE workflow.adset_id = ? AND workflow.phase = 'WAITING_CARD'
              AND launch.adset_id = workflow.adset_id
            ORDER BY workflow.created_at, workflow.workflow_id
            LIMIT 1
            """,
            (adset_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None

        workflow_id = str(row["workflow_id"])
        launch_link = conn.execute(
            "SELECT * FROM ad_replacement_launch_links WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if launch_link is None:
            raise WorkflowStateError("WAITING_CARD потерял durable launch link")
        _validate_durable_launch_link(
            launch_link,
            workflow_id=workflow_id,
            city=str(row["city"]),
            adset_id=str(row["adset_id"]),
            require_uncreated=True,
        )
        cursor = conn.execute(
            """
            UPDATE ad_replacement_workflows
            SET phase = 'LAUNCHING', attempt_count = attempt_count + 1,
                last_error = NULL, updated_at = ?
            WHERE workflow_id = ? AND phase = 'WAITING_CARD'
              AND EXISTS (
                  SELECT 1 FROM ad_replacement_launch_links
                  WHERE workflow_id = ad_replacement_workflows.workflow_id
                    AND launch_attempt_key = ?
                    AND adset_id = ad_replacement_workflows.adset_id
              )
            """,
            (now, workflow_id, row["launch_attempt_key"]),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        _append_replacement_event(
            conn,
            workflow_id=workflow_id,
            event_type="LAUNCH_CLAIMED",
            actor="replacement-workflow",
            evidence={"launch_attempt_key": row["launch_attempt_key"]},
            created_at=now,
        )
        claimed = conn.execute(
            "SELECT * FROM ad_replacement_workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        conn.commit()
        return _row_dict(claimed)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_workflow_for_launch(workflow_id: str) -> dict[str, Any] | None:
    """Атомарно забирает один exact ``WAITING_CARD`` workflow.

    ``launch_attempt_key`` служит durable ownership-token запуска. Конкурентный
    вызов для той же заявки получает ``None`` и никогда не забирает соседнюю
    заявку того же adset.
    """
    workflow_id = _required_text(workflow_id, "workflow_id")
    now = _now_iso()
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT workflow.*, launch.launch_attempt_key,
                   launch.account_kind, launch.account_id,
                   launch.expected_ad_count, launch.expected_ad_names_json,
                   launch.media_manifest_sha256, launch.created_ad_ids_json
            FROM ad_replacement_workflows AS workflow
            JOIN ad_replacement_launch_links AS launch
                ON launch.workflow_id = workflow.workflow_id
            WHERE workflow.workflow_id = ?
              AND workflow.phase = 'WAITING_CARD'
              AND launch.adset_id = workflow.adset_id
            """,
            (workflow_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None

        launch_link = conn.execute(
            "SELECT * FROM ad_replacement_launch_links WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if launch_link is None:  # pragma: no cover - защищено JOIN выше
            raise WorkflowStateError("WAITING_CARD потерял durable launch link")
        expected_names, _ = _validate_durable_launch_link(
            launch_link,
            workflow_id=workflow_id,
            city=str(row["city"]),
            adset_id=str(row["adset_id"]),
            require_uncreated=True,
        )
        cursor = conn.execute(
            """
            UPDATE ad_replacement_workflows
            SET phase = 'LAUNCHING', attempt_count = attempt_count + 1,
                last_error = NULL, updated_at = ?
            WHERE workflow_id = ? AND phase = 'WAITING_CARD'
              AND EXISTS (
                  SELECT 1 FROM ad_replacement_launch_links
                  WHERE workflow_id = ad_replacement_workflows.workflow_id
                    AND launch_attempt_key = ?
                    AND adset_id = ad_replacement_workflows.adset_id
                    AND created_ad_ids_json = '[]'
              )
            """,
            (now, workflow_id, row["launch_attempt_key"]),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        _append_replacement_event(
            conn,
            workflow_id=workflow_id,
            event_type="LAUNCH_CLAIMED",
            actor="replacement-workflow",
            evidence={"launch_attempt_key": row["launch_attempt_key"]},
            created_at=now,
        )
        claimed = conn.execute(
            "SELECT * FROM ad_replacement_workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        conn.commit()
        result = _row_dict(claimed)
        if result is None:  # pragma: no cover - строка защищена UPDATE выше
            raise WorkflowStateError("claimed workflow исчез после CAS")
        result.update(
            {
                "launch_attempt_key": str(row["launch_attempt_key"]),
                "account_kind": str(row["account_kind"]),
                "account_id": str(row["account_id"]),
                "expected_ad_count": int(row["expected_ad_count"]),
                "expected_ad_names": list(expected_names),
                "media_manifest_sha256": str(row["media_manifest_sha256"]),
            }
        )
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_replacement_created(
    workflow_id: str,
    replacement_ad_ids: Sequence[str],
    launch_attempt_key: str,
) -> None:
    """Атомарно сохраняет все created IDs: ``LAUNCHING -> WAITING_ACTIVE``."""
    workflow_id = _required_text(workflow_id, "workflow_id")
    created_ad_ids = _normalize_unique_texts(
        replacement_ad_ids,
        "replacement_ad_ids",
    )
    launch_attempt_key = _required_text(launch_attempt_key, "launch_attempt_key")
    created_ad_ids_json = _json_dumps(created_ad_ids)
    primary_replacement_ad_id = created_ad_ids[0]
    now = _now_iso()

    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT phase, old_ad_id, city, adset_id, released_ad_id,
                   card_id, card_name, replacement_ad_id
            FROM ad_replacement_workflows WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        if row is None:
            raise WorkflowStateError(f"workflow {workflow_id} не найден")
        link = conn.execute(
            "SELECT * FROM ad_replacement_launch_links WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if link is None:
            raise WorkflowStateError("created IDs нельзя записать без launch link")
        if link["launch_attempt_key"] != launch_attempt_key:
            raise WorkflowStateError("launch_attempt_key не совпадает с durable link")
        if link["adset_id"] != row["adset_id"]:
            raise WorkflowStateError(
                "durable launch link adset не совпадает с workflow"
            )
        expected_names, durable_created_ad_ids = _validate_durable_launch_link(
            link,
            workflow_id=workflow_id,
            city=str(row["city"]),
            adset_id=str(row["adset_id"]),
            require_uncreated=False,
        )
        if len(expected_names) != len(created_ad_ids):
            raise WorkflowStateError("created IDs count не совпадает с expected count")
        forbidden_own_references = {
            _optional_text(row["old_ad_id"]),
            _optional_text(row["released_ad_id"]),
        }
        if any(ad_id in forbidden_own_references for ad_id in created_ad_ids):
            raise WorkflowStateError("created ad совпадает с old/released reference")

        if row["phase"] in {
            "WAITING_ACTIVE",
            "READY_TO_PAUSE",
            "BLOCKED",
            "COMPLETED",
            "CANCELLED",
        }:
            if (
                durable_created_ad_ids == created_ad_ids
                and row["replacement_ad_id"] == primary_replacement_ad_id
                and row["card_id"] == link["card_id"]
                and row["card_name"] == link["card_name"]
            ):
                conn.commit()
                return
            raise WorkflowStateError("для workflow уже записан другой launch result")

        other_workflows = conn.execute(
            """
            SELECT workflow.workflow_id, workflow.old_ad_id,
                   workflow.replacement_ad_id, workflow.released_ad_id,
                   launch.created_ad_ids_json
            FROM ad_replacement_workflows AS workflow
            LEFT JOIN ad_replacement_launch_links AS launch
                ON launch.workflow_id = workflow.workflow_id
            WHERE workflow.workflow_id <> ?
              AND workflow.phase NOT IN ('COMPLETED','CANCELLED')
            ORDER BY workflow.workflow_id
            """,
            (workflow_id,),
        ).fetchall()
        for other in other_workflows:
            other_references = {
                _optional_text(other["old_ad_id"]),
                _optional_text(other["replacement_ad_id"]),
                _optional_text(other["released_ad_id"]),
            }
            if other["created_ad_ids_json"] is not None:
                other_references.update(
                    _load_optional_unique_texts(
                        other["created_ad_ids_json"],
                        "created_ad_ids_json",
                    )
                )
            if any(ad_id in other_references for ad_id in created_ad_ids):
                raise WorkflowStateError(
                    "created ad уже участвует в другом open workflow"
                )
        if row["phase"] != "LAUNCHING":
            raise WorkflowStateError(
                f"созданную замену нельзя записать из фазы {row['phase']}"
            )
        if durable_created_ad_ids:
            raise WorkflowStateError("LAUNCHING содержит несогласованные created IDs")
        if _optional_text(row["replacement_ad_id"]):
            raise WorkflowStateError("LAUNCHING уже содержит replacement_ad_id")

        link_cursor = conn.execute(
            """
            UPDATE ad_replacement_launch_links
            SET created_ad_ids_json = ?, updated_at = ?
            WHERE workflow_id = ? AND launch_attempt_key = ?
              AND created_ad_ids_json = ?
            """,
            (
                created_ad_ids_json,
                now,
                workflow_id,
                launch_attempt_key,
                link["created_ad_ids_json"],
            ),
        )
        if link_cursor.rowcount != 1:
            raise WorkflowStateError("created IDs launch-link CAS не выполнен")
        workflow_cursor = conn.execute(
            """
            UPDATE ad_replacement_workflows
            SET card_id = ?, card_name = ?, replacement_ad_id = ?,
                phase = 'WAITING_ACTIVE', replacement_created_at = ?,
                updated_at = ?, last_error = NULL
            WHERE workflow_id = ? AND phase = 'LAUNCHING'
            """,
            (
                link["card_id"],
                link["card_name"],
                primary_replacement_ad_id,
                now,
                now,
                workflow_id,
            ),
        )
        if workflow_cursor.rowcount != 1:
            raise WorkflowStateError("LAUNCHING -> WAITING_ACTIVE CAS не выполнен")
        _append_replacement_event(
            conn,
            workflow_id=workflow_id,
            event_type="CREATE_RECORDED",
            actor="replacement-workflow",
            evidence={
                "launch_attempt_key": launch_attempt_key,
                "created_ad_ids": created_ad_ids,
            },
            created_at=now,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _validated_active_evidence(
    evidence: Mapping[str, Any],
    *,
    created_ad_ids: tuple[str, ...],
    expected_ad_names: tuple[str, ...],
    adset_id: str,
) -> list[dict[str, str]]:
    """Проверяет exact proof всех созданных реклам перед storage CAS."""
    if not isinstance(evidence, Mapping):
        raise TypeError("evidence должен быть Mapping")
    ads = evidence.get("ads")
    if isinstance(ads, (str, bytes)) or not isinstance(ads, Sequence):
        raise WorkflowStateError("evidence.ads должен содержать exact список реклам")
    normalized: list[dict[str, str]] = []
    for item in ads:
        if not isinstance(item, Mapping):
            raise WorkflowStateError("каждый элемент evidence.ads должен быть Mapping")
        normalized.append(
            {
                "id": _required_text(item.get("id"), "evidence.ads.id"),
                "name": _required_text(item.get("name"), "evidence.ads.name"),
                "adset_id": _required_text(
                    item.get("adset_id"),
                    "evidence.ads.adset_id",
                ),
                "effective_status": _required_text(
                    item.get("effective_status"),
                    "evidence.ads.effective_status",
                ).upper(),
            }
        )
    if tuple(item["id"] for item in normalized) != created_ad_ids:
        raise WorkflowStateError("ACTIVE evidence IDs не совпадают с durable created IDs")
    if tuple(item["name"] for item in normalized) != expected_ad_names:
        raise WorkflowStateError("ACTIVE evidence names не совпадают с durable manifest")
    if any(item["adset_id"] != adset_id for item in normalized):
        raise WorkflowStateError("ACTIVE evidence содержит рекламу другого adset")
    if any(item["effective_status"] != "ACTIVE" for item in normalized):
        raise WorkflowStateError("ACTIVE evidence содержит неактивную рекламу")
    return normalized


def confirm_replacement_active(
    workflow_id: str,
    replacement_ad_ids: Sequence[str],
    *,
    evidence: Mapping[str, Any],
) -> bool:
    """CAS подтверждает все exact ACTIVE IDs конкретного workflow.

    Возвращает ``True`` только победителю перехода
    ``WAITING_ACTIVE -> READY_TO_PAUSE``. Exact retry после уже выполненного
    перехода возвращает ``False`` без второго события.
    """
    workflow_id = _required_text(workflow_id, "workflow_id")
    replacement_ids = _normalize_unique_texts(
        replacement_ad_ids,
        "replacement_ad_ids",
    )
    now = _now_iso()
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT phase, old_ad_id, city, adset_id, replacement_ad_id
            FROM ad_replacement_workflows WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        if row is None:
            raise WorkflowStateError(f"workflow {workflow_id} не найден")
        link = conn.execute(
            "SELECT * FROM ad_replacement_launch_links WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if link is None:
            raise WorkflowStateError("ACTIVE confirmation требует durable launch link")
        expected_names, durable_created_ids = _validate_durable_launch_link(
            link,
            workflow_id=workflow_id,
            city=str(row["city"]),
            adset_id=str(row["adset_id"]),
            require_uncreated=False,
        )
        if replacement_ids != durable_created_ids:
            raise WorkflowStateError(
                "replacement_ad_ids не совпадают с durable created IDs"
            )
        if row["replacement_ad_id"] != replacement_ids[0]:
            raise WorkflowStateError("primary replacement_ad_id drift")
        if str(row["old_ad_id"]) in replacement_ids:
            raise WorkflowStateError("old_ad_id присутствует среди replacement IDs")
        verified_ads = _validated_active_evidence(
            evidence,
            created_ad_ids=durable_created_ids,
            expected_ad_names=expected_names,
            adset_id=str(row["adset_id"]),
        )

        if row["phase"] in {"READY_TO_PAUSE", "COMPLETED"}:
            conn.commit()
            return False
        if row["phase"] != "WAITING_ACTIVE":
            raise WorkflowStateError(
                f"ACTIVE confirmation запрещён из фазы {row['phase']}"
            )
        cursor = conn.execute(
            """
            UPDATE ad_replacement_workflows
            SET phase = 'READY_TO_PAUSE', replacement_active_at = ?,
                updated_at = ?, last_error = NULL
            WHERE workflow_id = ? AND phase = 'WAITING_ACTIVE'
              AND replacement_ad_id = ? AND adset_id = ? AND old_ad_id <> ?
            """,
            (
                now,
                now,
                workflow_id,
                replacement_ids[0],
                row["adset_id"],
                replacement_ids[0],
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        _append_replacement_event(
            conn,
            workflow_id=workflow_id,
            event_type="ACTIVE_CONFIRMED",
            actor="replacement-workflow",
            evidence={"ads": verified_ads},
            created_at=now,
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _fetch_live_ad(ad_id: str) -> dict[str, Any]:
    """Читает exact ad из Facebook. Ошибки пробрасываются для fail-closed."""
    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import get_fb_token

    response = _throttled_get(
        f"{API}/{ad_id}",
        params={
            "access_token": get_fb_token(),
            "fields": "id,name,adset_id,status,effective_status",
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"FB вернул HTTP {response.status_code} для ad {ad_id}")
    payload = response.json()
    if not isinstance(payload, dict) or str(payload.get("id") or "") != ad_id:
        raise RuntimeError(f"FB не вернул exact ad {ad_id}")
    return payload


def _save_waiting_error(workflow_id: str, message: str) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            """
            UPDATE ad_replacement_workflows
            SET last_error = ?, updated_at = ?
            WHERE workflow_id = ? AND phase = 'WAITING_ACTIVE'
            """,
            (message, _now_iso(), workflow_id),
        )
        conn.commit()
    finally:
        conn.close()


def refresh_replacement_statuses(limit: int = 20) -> dict[str, Any]:
    """Живым GET продвигает только exact ACTIVE-замены в ``READY_TO_PAUSE``.

    FB-ошибка, пустой ответ и любой не-ACTIVE статус оставляют старую рекламу
    включённой. Замена из другого adset или с ID старой рекламы блокирует workflow.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("limit должен быть положительным целым числом")

    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT workflow_id
            FROM ad_replacement_workflows
            WHERE phase = 'WAITING_ACTIVE'
            ORDER BY updated_at, workflow_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    result: dict[str, Any] = {
        "checked": 0,
        "ready": 0,
        "waiting": 0,
        "blocked": 0,
        "errors": [],
    }
    for row in rows:
        workflow_id = str(row["workflow_id"])
        result["checked"] += 1
        launch = get_replacement_launch(workflow_id)
        if launch is None:
            message = "durable launch link отсутствует"
            _save_waiting_error(workflow_id, message)
            result["waiting"] += 1
            result["errors"].append({"workflow_id": workflow_id, "error": message})
            continue
        try:
            replacement_ad_ids = _load_unique_texts(
                launch["created_ad_ids_json"],
                "created_ad_ids_json",
            )
            expected_ad_names = _load_unique_texts(
                launch["expected_ad_names_json"],
                "expected_ad_names_json",
            )
        except WorkflowStateError as exc:
            message = sanitize_text(exc)
            _save_waiting_error(workflow_id, message)
            result["waiting"] += 1
            result["errors"].append({"workflow_id": workflow_id, "error": message})
            continue

        live_ads: list[dict[str, Any]] = []
        try:
            for replacement_ad_id in replacement_ad_ids:
                live_ads.append(_fetch_live_ad(replacement_ad_id))
        except Exception as exc:
            message = sanitize_text(f"FB-проверка замены не удалась: {exc}")
            _save_waiting_error(workflow_id, message)
            result["waiting"] += 1
            result["errors"].append({"workflow_id": workflow_id, "error": message})
            logger.warning("replacement workflow %s: %s", workflow_id, message)
            continue

        live_ids = tuple(str(live.get("id") or "") for live in live_ads)
        if live_ids != replacement_ad_ids:
            mark_workflow_blocked(workflow_id, "replacement_ids_mismatch")
            result["blocked"] += 1
            continue
        if any(
            str(live.get("adset_id") or "") != str(launch["adset_id"])
            for live in live_ads
        ):
            mark_workflow_blocked(workflow_id, "replacement_wrong_adset")
            result["blocked"] += 1
            continue
        live_names = tuple(str(live.get("name") or "") for live in live_ads)
        if live_names != expected_ad_names:
            mark_workflow_blocked(workflow_id, "replacement_names_mismatch")
            result["blocked"] += 1
            continue

        if any(
            str(live.get("effective_status") or "").upper() != "ACTIVE"
            for live in live_ads
        ):
            # PENDING_REVIEW/IN_PROCESS/etc. — нормальное ожидание, не ошибка.
            conn = _get_connection()
            try:
                conn.execute(
                    """
                    UPDATE ad_replacement_workflows
                    SET last_error = NULL, updated_at = ?
                    WHERE workflow_id = ? AND phase = 'WAITING_ACTIVE'
                    """,
                    (_now_iso(), workflow_id),
                )
                conn.commit()
            finally:
                conn.close()
            result["waiting"] += 1
            continue

        evidence_ads = [
            {
                "id": str(live.get("id") or ""),
                "name": str(live.get("name") or ""),
                "adset_id": str(live.get("adset_id") or ""),
                "effective_status": str(live.get("effective_status") or ""),
            }
            for live in live_ads
        ]
        if confirm_replacement_active(
            workflow_id,
            replacement_ad_ids,
            evidence={"ads": evidence_ads},
        ):
            result["ready"] += 1
        else:
            result["waiting"] += 1

    return result


def mark_old_paused(workflow_id: str) -> None:
    """Завершает workflow только из ``READY_TO_PAUSE``."""
    workflow_id = _required_text(workflow_id, "workflow_id")
    now = _now_iso()
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT phase FROM ad_replacement_workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            raise WorkflowStateError(f"workflow {workflow_id} не найден")
        if row["phase"] == "COMPLETED":
            conn.commit()
            return
        if row["phase"] != "READY_TO_PAUSE":
            raise WorkflowStateError(
                f"старую рекламу нельзя завершить из фазы {row['phase']}"
            )
        cursor = conn.execute(
            """
            UPDATE ad_replacement_workflows
            SET phase = 'COMPLETED', old_paused_at = ?, updated_at = ?, last_error = NULL
            WHERE workflow_id = ? AND phase = 'READY_TO_PAUSE'
            """,
            (now, now, workflow_id),
        )
        if cursor.rowcount != 1:
            raise WorkflowStateError("READY_TO_PAUSE -> COMPLETED CAS не выполнен")
        _append_replacement_event(
            conn,
            workflow_id=workflow_id,
            event_type="OLD_PAUSED",
            actor="replacement-workflow",
            created_at=now,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_workflow_blocked(workflow_id: str, reason: str) -> None:
    """Блокирует незавершённый workflow с обязательной причиной."""
    workflow_id = _required_text(workflow_id, "workflow_id")
    reason = sanitize_text(_required_text(reason, "reason"))
    now = _now_iso()
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT phase, last_error FROM ad_replacement_workflows
            WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        if row is None:
            raise WorkflowStateError(f"workflow {workflow_id} не найден")
        if row["phase"] in {"COMPLETED", "CANCELLED"}:
            raise WorkflowStateError(
                f"workflow в терминальной фазе {row['phase']} нельзя блокировать"
            )
        if row["phase"] == "BLOCKED":
            if row["last_error"] == reason:
                conn.commit()
                return
            raise WorkflowStateError(
                "BLOCKED workflow нельзя переписать другой причиной"
            )
        cursor = conn.execute(
            """
            UPDATE ad_replacement_workflows
            SET phase = 'BLOCKED', last_error = ?, updated_at = ?
            WHERE workflow_id = ?
              AND phase NOT IN ('COMPLETED','CANCELLED','BLOCKED')
            """,
            (reason, now, workflow_id),
        )
        if cursor.rowcount != 1:
            raise WorkflowStateError("open workflow -> BLOCKED CAS не выполнен")
        _append_replacement_event(
            conn,
            workflow_id=workflow_id,
            event_type="BLOCKED",
            actor="replacement-workflow",
            error=reason,
            created_at=now,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_workflow_cancelled(workflow_id: str, reason: str) -> None:
    """Терминально закрывает workflow без паузы старой рекламы.

    Единственный законный повод — владелец ОТКЛОНИЛ предложение погасить старое
    объявление: замена уже живёт, старое остаётся включённым по решению
    владельца, и просить об одном и том же на каждом тике нельзя. ``CANCELLED``
    предусмотрен схемой (migrations/016) как терминальная фаза наравне с
    ``COMPLETED``: уникальный индекс открытых workflow её не держит, поэтому
    новая замена того же объявления возможна.
    """
    workflow_id = _required_text(workflow_id, "workflow_id")
    reason = sanitize_text(_required_text(reason, "reason"))
    now = _now_iso()
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT phase, last_error FROM ad_replacement_workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            raise WorkflowStateError(f"workflow {workflow_id} не найден")
        if row["phase"] == "CANCELLED":
            # Идемпотентный повтор того же отказа.
            if row["last_error"] == reason:
                conn.commit()
                return
            raise WorkflowStateError(
                "CANCELLED workflow нельзя переписать другой причиной"
            )
        if row["phase"] == "COMPLETED":
            raise WorkflowStateError(
                "завершённый workflow нельзя отменить: старое уже погашено"
            )
        cursor = conn.execute(
            """
            UPDATE ad_replacement_workflows
            SET phase = 'CANCELLED', last_error = ?, updated_at = ?
            WHERE workflow_id = ? AND phase NOT IN ('COMPLETED','CANCELLED')
            """,
            (reason, now, workflow_id),
        )
        if cursor.rowcount != 1:
            raise WorkflowStateError("open workflow -> CANCELLED CAS не выполнен")
        _append_replacement_event(
            conn,
            workflow_id=workflow_id,
            event_type="CANCELLED",
            actor="replacement-workflow",
            error=reason,
            created_at=now,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def append_cleanup_audit(
    *,
    run_id: str,
    workflow_id: str | None,
    ad_id: str,
    ad_name: str,
    adset_id: str,
    action: str,
    reason: str,
    configured_status: str | None = None,
    effective_status: str | None = None,
    age_days: int | None = None,
    lifetime_spend_usd: float | None = None,
    local_spend_usd: float | None = None,
    capacity_before: int | None = None,
    capacity_after: int | None = None,
    evidence: dict[str, Any] | None = None,
    error: str | None = None,
    actor: str,
) -> int:
    """Append-only пишет одну строку cleanup audit и отдельно коммитит её.

    ``None`` сохраняется как SQL NULL; неизвестные spend/capacity не становятся 0.
    Возврат из функции означает, что COMMIT уже успешно завершён.
    """
    run_id = _required_text(run_id, "run_id")
    ad_id = _required_text(ad_id, "ad_id")
    adset_id = _required_text(adset_id, "adset_id")
    action = _required_text(action, "action").upper()
    reason = sanitize_text(_required_text(reason, "reason"))
    actor = _required_text(actor, "actor")
    if action not in _AUDIT_ACTIONS:
        raise ValueError(f"неподдерживаемое audit action: {action}")
    if evidence is not None and not isinstance(evidence, dict):
        raise TypeError("evidence должен быть dict или None")
    evidence_json = json.dumps(
        _sanitize_json_value(evidence or {}),
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )

    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO ad_cleanup_audit (
                run_id, workflow_id, ad_id, ad_name, adset_id, action, reason,
                configured_status, effective_status, age_days,
                lifetime_spend_usd, local_spend_usd, capacity_before,
                capacity_after, evidence_json, error, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                _optional_text(workflow_id) or None,
                ad_id,
                sanitize_text(_optional_text(ad_name)),
                adset_id,
                action,
                reason,
                _optional_text(configured_status) or None,
                _optional_text(effective_status) or None,
                age_days,
                lifetime_spend_usd,
                local_spend_usd,
                capacity_before,
                capacity_after,
                evidence_json,
                sanitize_text(_optional_text(error)) or None,
                actor,
                _now_iso(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
