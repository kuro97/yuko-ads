"""SQLite-граница proactive cleaner, slot runs и single-ad DELETE claims.

Модуль не вызывает Facebook/Trello и не выполняет внешние мутации. Его задача —
зафиксировать durable dry-run manifest и выдать claim только concrete replacement
workflow. Любая неоднозначность оставляет run/claim в блокирующем состоянии.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Sequence, TypedDict


CleanupRunKind = Literal["PROACTIVE_DAILY", "REPLACEMENT_SLOT", "MANUAL_DRY_RUN"]
CleanupRunPhase = Literal[
    "PLANNED",
    "RUNNING",
    "COMPLETED",
    "COMPLETED_WITH_WARNINGS",
    "BLOCKED",
    "FAILED",
]
DeleteOutcome = Literal["DELETED", "DELETE_FAILED", "RECONCILE_REQUIRED"]

_RUN_KINDS = frozenset({"PROACTIVE_DAILY", "REPLACEMENT_SLOT", "MANUAL_DRY_RUN"})
_FINISH_PHASES = frozenset(
    {"COMPLETED", "COMPLETED_WITH_WARNINGS", "BLOCKED", "FAILED"}
)
_CANDIDATE_STATES = frozenset(
    {
        "DISCOVERED",
        "ELIGIBLE",
        "CLAIMED",
        "DELETED",
        "DELETE_FAILED",
        "SKIPPED",
        "BLOCKED",
        "RECONCILE_REQUIRED",
    }
)
_UPSERT_CANDIDATE_STATES = frozenset({"DISCOVERED", "ELIGIBLE", "SKIPPED", "BLOCKED"})
_PROTECTED_CANDIDATE_STATES = frozenset(
    {"CLAIMED", "DELETED", "DELETE_FAILED", "RECONCILE_REQUIRED"}
)
_UNRESOLVED_CLAIM_STATES = ("CLAIMED", "DELETE_FAILED", "RECONCILE_REQUIRED")
_DELETE_OUTCOMES = frozenset({"DELETED", "DELETE_FAILED", "RECONCILE_REQUIRED"})
_COUNTER_COLUMNS = {
    "discovered": "discovered_count",
    "eligible": "eligible_count",
    "would_delete": "would_delete_count",
    "deleted": "deleted_count",
    "skipped": "skipped_count",
    "warnings": "warning_count",
    "errors": "error_count",
}
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(access[_-]?token|api[_-]?key|authorization|password|secret)"
        r"\s*[:=]\s*['\"]?[^\s&,'\"}]+"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/=]+"),
    re.compile(r"\bEAA[A-Za-z0-9_-]{8,}\b"),
)
_URL_WITH_QUERY_RE = re.compile(r"(?i)\bhttps?://[^\s?]+\?[^\s\"'<>]+")
_SENSITIVE_JSON_KEYS = frozenset(
    {
        "access_token",
        "accesstoken",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "cookie",
        "set_cookie",
    }
)
_SAFE_CANDIDATE_EFFECTIVE_STATUSES = frozenset(
    {"PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED"}
)


class CleanupRepositoryError(RuntimeError):
    """Базовая ошибка durable cleaner repository."""


class CleanupLeaseError(CleanupRepositoryError):
    """Lease отсутствует, истёк или принадлежит другому worker."""


class CleanupClaimError(CleanupRepositoryError):
    """Single-ad claim не прошёл fail-closed проверки."""


@dataclass(frozen=True, slots=True)
class CleanupRunLease:
    """Результат CAS-захвата одного durable cleanup run."""

    run_id: str
    run_kind: CleanupRunKind
    workflow_id: str | None
    acquired: bool
    reason: str | None
    phase: CleanupRunPhase
    lease_owner: str
    lease_expires_at: datetime | None


class CleanupCandidateRecord(TypedDict):
    """Нормализованная запись кандидата durable manifest."""

    account_kind: Literal["offline", "online"]
    adset_id: str
    adset_name: str
    ad_id: str
    ad_name: str
    ordinal: int
    state: Literal[
        "DISCOVERED",
        "ELIGIBLE",
        "CLAIMED",
        "DELETED",
        "DELETE_FAILED",
        "SKIPPED",
        "BLOCKED",
        "RECONCILE_REQUIRED",
    ]
    reason: str
    configured_status: str | None
    effective_status: str | None
    age_days: int | None
    lifetime_spend_usd: float | None
    lifetime_impressions: int | None
    lifetime_clicks: int | None
    local_spend_usd: float | None
    local_impressions: int | None
    local_clicks: int | None
    local_leads: int | None
    local_payments: int | None
    evidence: dict[str, Any]
    capacity_before: int


@dataclass(frozen=True, slots=True)
class CleanupDeleteClaim:
    """Committed single-ad claim, пригодный для одноразовой authorization."""

    claim_id: str
    run_id: str
    workflow_id: str
    ad_id: str
    ad_name: str
    adset_id: str
    purpose: Literal["REPLACEMENT_SLOT"]
    state: Literal["CLAIMED", "DELETED", "DELETE_FAILED", "RECONCILE_REQUIRED"]
    capacity_before: int
    claimed_at: datetime


def _now() -> datetime:
    """Возвращает timezone-aware UTC для сравнимых lease timestamp."""
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """Сериализует только timezone-aware timestamp."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("datetime должен содержать timezone")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} не должен быть пустым")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def sanitize_text(value: object) -> str:
    """Удаляет secrets и query-string перед durable записью."""
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
            result[safe_key] = (
                "[REDACTED]"
                if normalized_key in _SENSITIVE_JSON_KEYS
                else _sanitize_json_value(nested)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def _json_dumps(value: Mapping[str, Any] | Sequence[Any]) -> str:
    """Пишет канонический JSON без NaN/Infinity и секретов."""
    return json.dumps(
        _sanitize_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def _json_object(raw: object, field_name: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CleanupRepositoryError(
            f"{field_name} содержит некорректный JSON"
        ) from exc
    if not isinstance(value, dict):
        raise CleanupRepositoryError(f"{field_name} должен быть JSON-объектом")
    return value


def _get_connection() -> sqlite3.Connection:
    """Открывает штатную SQLite с FK, Row и timeout=30."""
    from services.creative_intelligence import DB_PATH

    if DB_PATH is None:
        raise RuntimeError("KB не инициализирована. Вызовите init_kb() при старте.")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _lease_ttl_seconds(lease_ttl: timedelta) -> float:
    if not isinstance(lease_ttl, timedelta):
        raise TypeError("lease_ttl должен быть timedelta")
    seconds = lease_ttl.total_seconds()
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("lease_ttl должен быть положительным")
    return seconds


def _mode_for_run(run_kind: str, config: Mapping[str, Any]) -> tuple[str, str]:
    if run_kind in {"PROACTIVE_DAILY", "MANUAL_DRY_RUN"}:
        requested = config.get("requested_mode", "dry_run")
        effective = config.get("effective_mode", "dry_run")
        if requested != "dry_run" or effective != "dry_run":
            raise ValueError(f"{run_kind} допускает только dry_run")
        return "dry_run", "dry_run"
    requested = config.get("requested_mode", "active")
    effective = config.get("effective_mode", requested)
    if requested not in {"dry_run", "active"} or effective not in {"dry_run", "active"}:
        raise ValueError("requested_mode/effective_mode должны быть dry_run или active")
    if requested != effective:
        raise ValueError("requested_mode и effective_mode должны совпадать")
    return str(requested), str(effective)


def _lease_from_row(
    row: sqlite3.Row,
    *,
    acquired: bool,
    reason: str | None,
) -> CleanupRunLease:
    return CleanupRunLease(
        run_id=str(row["run_id"]),
        run_kind=str(row["run_kind"]),  # type: ignore[arg-type]
        workflow_id=_optional_text(row["workflow_id"]),
        acquired=acquired,
        reason=reason,
        phase=str(row["phase"]),  # type: ignore[arg-type]
        lease_owner=_optional_text(row["lease_owner"]) or "",
        lease_expires_at=_parse_iso(row["lease_expires_at"]),
    )


def _find_existing_run(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    run_kind: str,
    scheduled_date: date,
    workflow_id: str | None,
) -> sqlite3.Row | None:
    if run_kind == "PROACTIVE_DAILY":
        return conn.execute(
            """
            SELECT * FROM ad_cleanup_runs
            WHERE tenant_id = ? AND run_kind = 'PROACTIVE_DAILY' AND scheduled_date = ?
            """,
            (tenant_id, scheduled_date.isoformat()),
        ).fetchone()
    if run_kind == "REPLACEMENT_SLOT":
        return conn.execute(
            """
            SELECT * FROM ad_cleanup_runs
            WHERE run_kind = 'REPLACEMENT_SLOT' AND workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
    return None


def _unresolved_claim_count(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM ad_cleanup_delete_claims
        WHERE run_id = ? AND state IN ('CLAIMED','DELETE_FAILED','RECONCILE_REQUIRED')
        """,
        (run_id,),
    ).fetchone()
    return int(row["count"])


def acquire_cleanup_run(
    tenant_id: str,
    run_kind: CleanupRunKind,
    scheduled_date: date,
    workflow_id: str | None,
    lease_owner: str,
    lease_ttl: timedelta,
    config: Mapping[str, Any],
) -> CleanupRunLease:
    """CAS-захватывает daily/slot run или безопасно возобновляет expired lease."""
    tenant_id = _required_text(tenant_id, "tenant_id")
    run_kind = _required_text(run_kind, "run_kind")  # type: ignore[assignment]
    if run_kind not in _RUN_KINDS:
        raise ValueError(f"неподдерживаемый run_kind: {run_kind}")
    if isinstance(scheduled_date, datetime) or not isinstance(scheduled_date, date):
        raise TypeError("scheduled_date должен быть date")
    lease_owner = _required_text(lease_owner, "lease_owner")
    ttl_seconds = _lease_ttl_seconds(lease_ttl)
    if not isinstance(config, Mapping):
        raise TypeError("config должен быть Mapping")
    normalized_workflow_id = _optional_text(workflow_id)
    if run_kind == "REPLACEMENT_SLOT" and normalized_workflow_id is None:
        raise ValueError("REPLACEMENT_SLOT требует workflow_id")
    if run_kind != "REPLACEMENT_SLOT" and normalized_workflow_id is not None:
        raise ValueError(f"{run_kind} не допускает workflow_id")
    requested_mode, effective_mode = _mode_for_run(run_kind, config)
    config_json = _json_dumps(config)
    now = _now()
    now_iso = _iso(now)
    expires_iso = _iso(now + timedelta(seconds=ttl_seconds))

    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = _find_existing_run(
            conn,
            tenant_id=tenant_id,
            run_kind=run_kind,
            scheduled_date=scheduled_date,
            workflow_id=normalized_workflow_id,
        )
        if existing is None:
            run_id = f"cleanup-{run_kind.lower()}-{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO ad_cleanup_runs (
                    run_id, tenant_id, run_kind, workflow_id, scheduled_date,
                    requested_mode, effective_mode, phase, config_json,
                    evidence_json, lease_owner, lease_expires_at,
                    created_at, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, '{}', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    tenant_id,
                    run_kind,
                    normalized_workflow_id,
                    scheduled_date.isoformat(),
                    requested_mode,
                    effective_mode,
                    config_json,
                    lease_owner,
                    expires_iso,
                    now_iso,
                    now_iso,
                    now_iso,
                ),
            )
            row = conn.execute(
                "SELECT * FROM ad_cleanup_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            conn.commit()
            return _lease_from_row(
                row,
                acquired=True,
                reason=None,
            )

        phase = str(existing["phase"])
        if phase in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}:
            conn.commit()
            return _lease_from_row(
                existing,
                acquired=False,
                reason="already_completed",
            )
        if phase in {"BLOCKED", "FAILED"}:
            conn.commit()
            return _lease_from_row(
                existing,
                acquired=False,
                reason="terminal_run",
            )

        expires_at = _parse_iso(existing["lease_expires_at"])
        if phase == "RUNNING" and expires_at is None:
            conn.execute(
                """
                UPDATE ad_cleanup_runs
                SET phase = 'BLOCKED', lease_owner = NULL, lease_expires_at = NULL,
                    error = 'invalid_lease', updated_at = ?
                WHERE run_id = ? AND phase = 'RUNNING'
                """,
                (now_iso, existing["run_id"]),
            )
            blocked = conn.execute(
                "SELECT * FROM ad_cleanup_runs WHERE run_id = ?", (existing["run_id"],)
            ).fetchone()
            conn.commit()
            return _lease_from_row(
                blocked,
                acquired=False,
                reason="invalid_lease",
            )
        if phase == "RUNNING" and expires_at is not None and expires_at > now:
            conn.commit()
            return _lease_from_row(
                existing,
                acquired=False,
                reason="already_running",
            )

        if _unresolved_claim_count(conn, str(existing["run_id"])):
            conn.execute(
                """
                UPDATE ad_cleanup_runs
                SET phase = 'BLOCKED', lease_owner = NULL, lease_expires_at = NULL,
                    error = 'cleanup_reconcile_required', updated_at = ?
                WHERE run_id = ?
                """,
                (now_iso, existing["run_id"]),
            )
            blocked = conn.execute(
                "SELECT * FROM ad_cleanup_runs WHERE run_id = ?", (existing["run_id"],)
            ).fetchone()
            conn.commit()
            return _lease_from_row(
                blocked,
                acquired=False,
                reason="reconcile_required",
            )

        evidence = _json_object(existing["evidence_json"], "evidence_json")
        resume_count = evidence.get("resume_count", 0)
        if type(resume_count) is not int or resume_count < 0:
            resume_count = 0
        evidence["resume_count"] = resume_count + 1
        evidence["resumed_at"] = now_iso
        cursor = conn.execute(
            """
            UPDATE ad_cleanup_runs
            SET phase = 'RUNNING', lease_owner = ?, lease_expires_at = ?,
                evidence_json = ?, error = NULL,
                started_at = COALESCE(started_at, ?), updated_at = ?
            WHERE run_id = ? AND phase IN ('PLANNED','RUNNING')
            """,
            (
                lease_owner,
                expires_iso,
                _json_dumps(evidence),
                now_iso,
                now_iso,
                existing["run_id"],
            ),
        )
        if cursor.rowcount != 1:
            raise CleanupLeaseError("cleanup run нельзя возобновить из текущей фазы")
        resumed = conn.execute(
            "SELECT * FROM ad_cleanup_runs WHERE run_id = ?", (existing["run_id"],)
        ).fetchone()
        conn.commit()
        return _lease_from_row(
            resumed,
            acquired=True,
            reason="lease_resumed",
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def renew_cleanup_run_lease(
    run_id: str,
    lease_owner: str,
    lease_ttl: timedelta,
) -> bool:
    """Продлевает только ещё живой lease того же owner через CAS."""
    run_id = _required_text(run_id, "run_id")
    lease_owner = _required_text(lease_owner, "lease_owner")
    ttl_seconds = _lease_ttl_seconds(lease_ttl)
    now = _now()
    now_iso = _iso(now)
    expires_iso = _iso(now + timedelta(seconds=ttl_seconds))

    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT phase, lease_owner, lease_expires_at
            FROM ad_cleanup_runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        current_expiry = (
            _parse_iso(row["lease_expires_at"]) if row is not None else None
        )
        if (
            row is None
            or row["phase"] != "RUNNING"
            or row["lease_owner"] != lease_owner
            or current_expiry is None
            or current_expiry <= now
        ):
            conn.commit()
            return False
        cursor = conn.execute(
            """
            UPDATE ad_cleanup_runs
            SET lease_expires_at = ?, updated_at = ?
            WHERE run_id = ? AND phase = 'RUNNING'
              AND lease_owner = ? AND lease_expires_at = ?
            """,
            (expires_iso, now_iso, run_id, lease_owner, row["lease_expires_at"]),
        )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_cleanup_run_evidence(
    run_id: str,
    evidence: Mapping[str, Any],
    *,
    lease_owner: str,
) -> None:
    """CAS-обновляет durable run evidence живым lease owner.

    Отдельная функция нужна для pressure/adset snapshot даже когда в adset нет
    ни одного candidate row. Значения верхнего уровня сливаются с существующим
    evidence; secrets и URL query-string редактируются до записи.
    """
    run_id = _required_text(run_id, "run_id")
    lease_owner = _required_text(lease_owner, "lease_owner")
    if not isinstance(evidence, Mapping):
        raise TypeError("evidence должен быть Mapping")
    now = _now()
    now_iso = _iso(now)
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            "SELECT * FROM ad_cleanup_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        expiry = _parse_iso(run["lease_expires_at"]) if run is not None else None
        if (
            run is None
            or run["phase"] != "RUNNING"
            or run["lease_owner"] != lease_owner
            or expiry is None
            or expiry <= now
        ):
            raise CleanupLeaseError("run evidence потерял live lease_owner")
        merged = _json_object(run["evidence_json"], "evidence_json")
        merged.update(_sanitize_json_value(evidence))
        cursor = conn.execute(
            """
            UPDATE ad_cleanup_runs
            SET evidence_json = ?, updated_at = ?
            WHERE run_id = ? AND phase = 'RUNNING' AND lease_owner = ?
              AND lease_expires_at = ?
            """,
            (
                _json_dumps(merged),
                now_iso,
                run_id,
                lease_owner,
                run["lease_expires_at"],
            ),
        )
        if cursor.rowcount != 1:
            raise CleanupLeaseError("run evidence CAS не выполнен")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _nonnegative_int(
    value: object, field_name: str, *, maximum: int | None = None
) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} должен быть неотрицательным int")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} должен быть <= {maximum}")
    return value


def _optional_nonnegative_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, field_name)


def _optional_nonnegative_float(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} должен быть числом или None")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} должен быть finite и неотрицательным")
    return number


def _normalize_candidate(candidate: CleanupCandidateRecord) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise TypeError("candidate должен быть dict")
    account_kind = _required_text(candidate.get("account_kind"), "account_kind")
    if account_kind not in {"offline", "online"}:
        raise ValueError("account_kind должен быть offline или online")
    state = _required_text(candidate.get("state"), "state")
    if state not in _CANDIDATE_STATES:
        raise ValueError(f"неподдерживаемый candidate state: {state}")
    evidence = candidate.get("evidence")
    if not isinstance(evidence, Mapping):
        raise TypeError("candidate.evidence должен быть Mapping")
    normalized = {
        "account_kind": account_kind,
        "adset_id": _required_text(candidate.get("adset_id"), "adset_id"),
        "adset_name": sanitize_text(candidate.get("adset_name")),
        "ad_id": _required_text(candidate.get("ad_id"), "ad_id"),
        "ad_name": sanitize_text(candidate.get("ad_name")),
        "ordinal": _nonnegative_int(candidate.get("ordinal"), "ordinal"),
        "state": state,
        "reason": sanitize_text(candidate.get("reason")),
        "configured_status": _optional_text(candidate.get("configured_status")),
        "effective_status": _optional_text(candidate.get("effective_status")),
        "age_days": _optional_nonnegative_int(candidate.get("age_days"), "age_days"),
        "lifetime_spend_usd": _optional_nonnegative_float(
            candidate.get("lifetime_spend_usd"), "lifetime_spend_usd"
        ),
        "lifetime_impressions": _optional_nonnegative_int(
            candidate.get("lifetime_impressions"), "lifetime_impressions"
        ),
        "lifetime_clicks": _optional_nonnegative_int(
            candidate.get("lifetime_clicks"), "lifetime_clicks"
        ),
        "local_spend_usd": _optional_nonnegative_float(
            candidate.get("local_spend_usd"), "local_spend_usd"
        ),
        "local_impressions": _optional_nonnegative_int(
            candidate.get("local_impressions"), "local_impressions"
        ),
        "local_clicks": _optional_nonnegative_int(
            candidate.get("local_clicks"), "local_clicks"
        ),
        "local_leads": _optional_nonnegative_int(
            candidate.get("local_leads"), "local_leads"
        ),
        "local_payments": _optional_nonnegative_int(
            candidate.get("local_payments"), "local_payments"
        ),
        "capacity_before": _nonnegative_int(
            candidate.get("capacity_before"), "capacity_before", maximum=50
        ),
        "evidence_json": _json_dumps(evidence),
    }
    if state == "ELIGIBLE":
        _validate_zero_candidate(normalized, minimum_age_days=15)
    return normalized


def _validate_zero_candidate(
    candidate: Mapping[str, Any],
    *,
    minimum_age_days: int,
) -> None:
    """Fail-closed проверяет durable zero-evidence перед claim."""
    if candidate["configured_status"] != "PAUSED":
        raise CleanupClaimError("ELIGIBLE candidate требует configured PAUSED")
    if candidate["effective_status"] not in _SAFE_CANDIDATE_EFFECTIVE_STATUSES:
        raise CleanupClaimError(
            "ELIGIBLE candidate имеет небезопасный effective_status"
        )
    age_days = candidate["age_days"]
    if type(age_days) is not int or age_days < minimum_age_days:
        raise CleanupClaimError(
            f"ELIGIBLE candidate должен быть старше {minimum_age_days} дней"
        )
    zero_fields = (
        "lifetime_spend_usd",
        "lifetime_impressions",
        "lifetime_clicks",
        "local_spend_usd",
        "local_impressions",
        "local_clicks",
        "local_leads",
        "local_payments",
    )
    if any(candidate[field] is None or candidate[field] != 0 for field in zero_fields):
        raise CleanupClaimError(
            "ELIGIBLE candidate требует exact zero live/local evidence"
        )
    if isinstance(candidate, sqlite3.Row):
        raw_evidence = candidate["evidence_json"]
    else:
        raw_evidence = candidate.get("evidence_json", candidate.get("evidence"))
    if isinstance(raw_evidence, str):
        evidence = _json_object(raw_evidence, "candidate.evidence_json")
    elif isinstance(raw_evidence, Mapping):
        evidence = dict(raw_evidence)
    else:
        raise CleanupClaimError("candidate evidence отсутствует")
    required_completeness = {
        "inventory_complete": True,
        "lifetime_row_count": 1,
        "local_evidence_complete": True,
        "kb_present": True,
    }
    if any(evidence.get(key) != value for key, value in required_completeness.items()):
        raise CleanupClaimError("candidate evidence completeness не доказана")


def _candidate_changed(existing: sqlite3.Row, candidate: Mapping[str, Any]) -> bool:
    fields = (
        "account_kind",
        "adset_id",
        "adset_name",
        "ad_name",
        "ordinal",
        "state",
        "reason",
        "configured_status",
        "effective_status",
        "age_days",
        "lifetime_spend_usd",
        "lifetime_impressions",
        "lifetime_clicks",
        "local_spend_usd",
        "local_impressions",
        "local_clicks",
        "local_leads",
        "local_payments",
        "capacity_before",
        "evidence_json",
    )
    return any(existing[field] != candidate[field] for field in fields)


def _insert_candidate_audit(
    conn: sqlite3.Connection,
    *,
    run: sqlite3.Row,
    candidate: Mapping[str, Any],
    created_at: str,
) -> None:
    action = "DRY_RUN_CANDIDATE" if candidate["state"] == "ELIGIBLE" else "SKIPPED"
    actor = (
        "proactive-cleaner"
        if run["run_kind"] == "PROACTIVE_DAILY"
        else "replacement-cleaner"
    )
    conn.execute(
        """
        INSERT INTO ad_cleanup_audit (
            run_id, workflow_id, ad_id, ad_name, adset_id, action, reason,
            configured_status, effective_status, age_days,
            lifetime_spend_usd, local_spend_usd, capacity_before,
            capacity_after, evidence_json, error, actor, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            run["run_id"],
            run["workflow_id"],
            candidate["ad_id"],
            candidate["ad_name"],
            candidate["adset_id"],
            action,
            candidate["reason"] or str(candidate["state"]).lower(),
            candidate["configured_status"],
            candidate["effective_status"],
            candidate["age_days"],
            candidate["lifetime_spend_usd"],
            candidate["local_spend_usd"],
            candidate["capacity_before"],
            candidate["evidence_json"],
            actor,
            created_at,
        ),
    )


def upsert_cleanup_candidate(
    run_id: str,
    candidate: CleanupCandidateRecord,
    *,
    lease_owner: str,
) -> None:
    """Под live lease сохраняет manifest row и соответствующий dry-run audit."""
    run_id = _required_text(run_id, "run_id")
    lease_owner = _required_text(lease_owner, "lease_owner")
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = _now()
        now_iso = _iso(now)
        run = conn.execute(
            "SELECT * FROM ad_cleanup_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise CleanupRepositoryError(f"cleanup run {run_id} не найден")
        lease_expires_at = _parse_iso(run["lease_expires_at"])
        if (
            run["phase"] != "RUNNING"
            or run["lease_owner"] != lease_owner
            or lease_expires_at is None
            or lease_expires_at <= now
        ):
            raise CleanupLeaseError("manifest upsert требует live lease_owner")
        # Caller payload не трогаем до lease CAS: потерявший lease worker не
        # должен влиять на порядок ошибок или тратить время на normalization.
        normalized = _normalize_candidate(candidate)
        if normalized["state"] not in _UPSERT_CANDIDATE_STATES:
            raise CleanupClaimError(
                "claim/outcome state меняется только claim/finish функциями"
            )
        existing = conn.execute(
            """
            SELECT * FROM ad_cleanup_candidates WHERE run_id = ? AND ad_id = ?
            """,
            (run_id, normalized["ad_id"]),
        ).fetchone()
        if existing is not None and (
            existing["claim_id"] is not None
            or str(existing["state"]) in _PROTECTED_CANDIDATE_STATES
        ):
            raise CleanupClaimError("claimed/finished candidate нельзя перезаписать")
        changed = existing is None or _candidate_changed(existing, normalized)
        if not changed:
            conn.commit()
            return
        conn.execute(
            """
            INSERT INTO ad_cleanup_candidates (
                run_id, ad_id, account_kind, adset_id, adset_name, ad_name,
                ordinal, state, reason, configured_status, effective_status,
                age_days, lifetime_spend_usd, lifetime_impressions,
                lifetime_clicks, local_spend_usd, local_impressions,
                local_clicks, local_leads, local_payments, capacity_before,
                evidence_json, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(run_id, ad_id) DO UPDATE SET
                account_kind = excluded.account_kind,
                adset_id = excluded.adset_id,
                adset_name = excluded.adset_name,
                ad_name = excluded.ad_name,
                ordinal = excluded.ordinal,
                state = excluded.state,
                reason = excluded.reason,
                configured_status = excluded.configured_status,
                effective_status = excluded.effective_status,
                age_days = excluded.age_days,
                lifetime_spend_usd = excluded.lifetime_spend_usd,
                lifetime_impressions = excluded.lifetime_impressions,
                lifetime_clicks = excluded.lifetime_clicks,
                local_spend_usd = excluded.local_spend_usd,
                local_impressions = excluded.local_impressions,
                local_clicks = excluded.local_clicks,
                local_leads = excluded.local_leads,
                local_payments = excluded.local_payments,
                capacity_before = excluded.capacity_before,
                evidence_json = excluded.evidence_json,
                updated_at = excluded.updated_at
            """,
            (
                run_id,
                normalized["ad_id"],
                normalized["account_kind"],
                normalized["adset_id"],
                normalized["adset_name"],
                normalized["ad_name"],
                normalized["ordinal"],
                normalized["state"],
                normalized["reason"],
                normalized["configured_status"],
                normalized["effective_status"],
                normalized["age_days"],
                normalized["lifetime_spend_usd"],
                normalized["lifetime_impressions"],
                normalized["lifetime_clicks"],
                normalized["local_spend_usd"],
                normalized["local_impressions"],
                normalized["local_clicks"],
                normalized["local_leads"],
                normalized["local_payments"],
                normalized["capacity_before"],
                normalized["evidence_json"],
                now_iso,
                now_iso,
            ),
        )
        _insert_candidate_audit(conn, run=run, candidate=normalized, created_at=now_iso)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _config_int(
    config: Mapping[str, Any], key: str, *, minimum: int, maximum: int
) -> int:
    value = config.get(key)
    if value is None and isinstance(config.get("cleaner"), Mapping):
        value = config["cleaner"].get(key)  # type: ignore[index]
    if type(value) is not int or not minimum <= value <= maximum:
        raise CleanupClaimError(f"config.{key} должен быть int {minimum}..{maximum}")
    return value


def _validate_launch_link(link: sqlite3.Row, candidate: Mapping[str, Any]) -> int:
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
        raise CleanupClaimError("replacement launch link неполный")
    if link["account_kind"] != candidate["account_kind"]:
        raise CleanupClaimError("candidate account_kind не совпадает с launch link")
    if link["adset_id"] != candidate["adset_id"]:
        raise CleanupClaimError("candidate adset не совпадает с launch link")
    try:
        names = json.loads(str(link["expected_ad_names_json"]))
    except json.JSONDecodeError as exc:
        raise CleanupClaimError("expected_ad_names_json некорректен") from exc
    if not isinstance(names, list):
        raise CleanupClaimError("expected_ad_names_json должен быть списком")
    normalized_names = [str(name).strip() for name in names]
    if (
        not normalized_names
        or any(not name for name in normalized_names)
        or len(set(normalized_names)) != len(normalized_names)
    ):
        raise CleanupClaimError(
            "expected ad names должны быть ordered unique и непустыми"
        )
    expected_count = int(link["expected_ad_count"])
    if expected_count != len(normalized_names):
        raise CleanupClaimError("expected_ad_count не совпадает с expected names")
    return expected_count


def _assert_not_referenced_by_open_workflow(
    conn: sqlite3.Connection,
    ad_id: str,
) -> None:
    """Запрещает DELETE для любого ad, упомянутого открытой заменой."""
    workflows = conn.execute(
        """
        SELECT
            workflow.workflow_id,
            workflow.old_ad_id,
            workflow.replacement_ad_id,
            workflow.released_ad_id,
            launch.created_ad_ids_json
        FROM ad_replacement_workflows AS workflow
        LEFT JOIN ad_replacement_launch_links AS launch
            ON launch.workflow_id = workflow.workflow_id
        WHERE workflow.phase NOT IN ('COMPLETED','CANCELLED')
        ORDER BY workflow.workflow_id
        """
    ).fetchall()
    for workflow in workflows:
        references = {
            value
            for field in ("old_ad_id", "replacement_ad_id", "released_ad_id")
            if (value := _optional_text(workflow[field])) is not None
        }
        created_json = workflow["created_ad_ids_json"]
        if created_json is not None:
            try:
                created_ids = json.loads(str(created_json))
            except json.JSONDecodeError as exc:
                raise CleanupClaimError(
                    "open workflow created_ad_ids_json некорректен"
                ) from exc
            if not isinstance(created_ids, list):
                raise CleanupClaimError(
                    "open workflow created_ad_ids_json должен быть списком"
                )
            normalized_created_ids: list[str] = []
            for created_id in created_ids:
                if not isinstance(created_id, str) or not created_id.strip():
                    raise CleanupClaimError(
                        "open workflow created ad references некорректны"
                    )
                normalized_created_ids.append(created_id.strip())
            if len(normalized_created_ids) != len(set(normalized_created_ids)):
                raise CleanupClaimError(
                    "open workflow created ad references содержат дубликаты"
                )
            references.update(normalized_created_ids)
        if ad_id in references:
            raise CleanupClaimError("candidate участвует в open replacement workflow")


def _claim_from_row(row: sqlite3.Row) -> CleanupDeleteClaim:
    claimed_at = _parse_iso(row["claimed_at"])
    if claimed_at is None:
        raise CleanupRepositoryError("claim содержит invalid claimed_at")
    return CleanupDeleteClaim(
        claim_id=str(row["claim_id"]),
        run_id=str(row["run_id"]),
        workflow_id=str(row["workflow_id"]),
        ad_id=str(row["ad_id"]),
        ad_name=str(row["ad_name"]),
        adset_id=str(row["adset_id"]),
        purpose="REPLACEMENT_SLOT",
        state=str(row["state"]),  # type: ignore[arg-type]
        capacity_before=int(row["capacity_before"]),
        claimed_at=claimed_at,
    )


def claim_replacement_slot_delete(
    run_id: str,
    workflow_id: str,
    candidate: CleanupCandidateRecord,
    actor: str,
    lease_owner: str,
) -> CleanupDeleteClaim:
    """Атомарно фиксирует claim+candidate+DELETE_ATTEMPT ровно для одного ad."""
    run_id = _required_text(run_id, "run_id")
    workflow_id = _required_text(workflow_id, "workflow_id")
    actor = _required_text(actor, "actor")
    lease_owner = _required_text(lease_owner, "lease_owner")
    normalized = _normalize_candidate(candidate)
    if normalized["state"] != "ELIGIBLE":
        raise CleanupClaimError("claim допускает только ELIGIBLE candidate")
    now = _now()
    now_iso = _iso(now)

    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            "SELECT * FROM ad_cleanup_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise CleanupClaimError("cleanup run не найден")
        lease_expires_at = _parse_iso(run["lease_expires_at"])
        if (
            run["run_kind"] != "REPLACEMENT_SLOT"
            or run["workflow_id"] != workflow_id
            or run["effective_mode"] != "active"
            or run["phase"] != "RUNNING"
            or run["lease_owner"] != lease_owner
            or lease_expires_at is None
            or lease_expires_at <= now
        ):
            raise CleanupClaimError("run/lease не разрешает workflow-bound DELETE")

        _assert_not_referenced_by_open_workflow(conn, normalized["ad_id"])

        workflow = conn.execute(
            """
            SELECT workflow_id, old_ad_id, adset_id, phase,
                   released_ad_id, replacement_ad_id
            FROM ad_replacement_workflows WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        if workflow is None or workflow["phase"] != "WAITING_SLOT":
            raise CleanupClaimError("workflow должен существовать в WAITING_SLOT")
        if workflow["adset_id"] != normalized["adset_id"]:
            raise CleanupClaimError("workflow adset не совпадает с candidate")
        if _optional_text(workflow["released_ad_id"]) or _optional_text(
            workflow["replacement_ad_id"]
        ):
            raise CleanupClaimError("workflow уже release/create linked")

        link = conn.execute(
            "SELECT * FROM ad_replacement_launch_links WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if link is None:
            raise CleanupClaimError("workflow не связан с exact card/media launch link")
        expected_count = _validate_launch_link(link, normalized)

        durable_candidate = conn.execute(
            """
            SELECT * FROM ad_cleanup_candidates WHERE run_id = ? AND ad_id = ?
            """,
            (run_id, normalized["ad_id"]),
        ).fetchone()
        if durable_candidate is None or durable_candidate["state"] != "ELIGIBLE":
            raise CleanupClaimError("durable candidate отсутствует или уже использован")
        immutable_fields = (
            "account_kind",
            "adset_id",
            "ad_name",
            "ordinal",
            "capacity_before",
            "evidence_json",
        )
        if any(
            durable_candidate[field] != normalized[field] for field in immutable_fields
        ):
            raise CleanupClaimError("candidate drift между manifest и claim")

        if (
            conn.execute(
                "SELECT 1 FROM ad_cleanup_delete_claims WHERE ad_id = ? LIMIT 1",
                (normalized["ad_id"],),
            ).fetchone()
            is not None
        ):
            raise CleanupClaimError("prior_delete_claim")
        if (
            conn.execute(
                """
            SELECT 1 FROM ad_cleanup_audit
            WHERE ad_id = ? AND action = 'DELETE_ATTEMPT' LIMIT 1
            """,
                (normalized["ad_id"],),
            ).fetchone()
            is not None
        ):
            raise CleanupClaimError("prior_delete_attempt")

        claims = conn.execute(
            """
            SELECT * FROM ad_cleanup_delete_claims
            WHERE run_id = ? ORDER BY claimed_at, claim_id
            """,
            (run_id,),
        ).fetchall()
        if any(str(claim["state"]) in _UNRESOLVED_CLAIM_STATES for claim in claims):
            raise CleanupClaimError("unresolved claim требует ручного reconcile")
        if any(str(claim["state"]) != "DELETED" for claim in claims):
            raise CleanupClaimError("предыдущий claim не подтверждён как DELETED")

        config = _json_object(run["config_json"], "config_json")
        hard_reserve_slots = _config_int(
            config, "hard_reserve_slots", minimum=1, maximum=5
        )
        max_deletes = _config_int(
            config, "max_deletes_per_workflow", minimum=1, maximum=5
        )
        stale_days = config.get("stale_days")
        if stale_days is None and isinstance(config.get("cleaner"), Mapping):
            stale_days = config["cleaner"].get("stale_days")  # type: ignore[index]
        if stale_days is None:
            stale_days = 15
        if type(stale_days) is not int or not 15 <= stale_days <= 365:
            raise CleanupClaimError("config.stale_days должен быть int 15..365")
        _validate_zero_candidate(normalized, minimum_age_days=stale_days)
        other_reserved = config.get("other_reserved_slots", 0)
        if type(other_reserved) is not int or other_reserved < 0 or other_reserved > 50:
            raise CleanupClaimError("other_reserved_slots должен быть int 0..50")
        if len(claims) >= max_deletes:
            raise CleanupClaimError("max_deletes_per_workflow исчерпан")

        required_slots = expected_count + hard_reserve_slots
        available_after_reservations = max(
            int(normalized["capacity_before"]) - other_reserved,
            0,
        )
        remaining_deficit = max(required_slots - available_after_reservations, 0)
        if remaining_deficit <= 0:
            raise CleanupClaimError("slot deficit уже закрыт")

        run_evidence = _json_object(run["evidence_json"], "evidence_json")
        if not claims:
            initial_deficit = remaining_deficit
            if not 1 <= initial_deficit <= max_deletes:
                raise CleanupClaimError("initial slot deficit вне cap 1..max_deletes")
            run_evidence.update(
                {
                    "initial_available": normalized["capacity_before"],
                    "initial_slot_deficit": initial_deficit,
                    "required_slots": required_slots,
                    "other_reserved_slots": other_reserved,
                }
            )
        else:
            initial_deficit = run_evidence.get("initial_slot_deficit")
            if (
                type(initial_deficit) is not int
                or not 1 <= initial_deficit <= max_deletes
            ):
                raise CleanupClaimError(
                    "initial deficit evidence отсутствует/некорректен"
                )
            last_capacity_after = claims[-1]["capacity_after"]
            if last_capacity_after != normalized["capacity_before"]:
                raise CleanupClaimError(
                    "capacity_before не продолжает последний postcheck"
                )
            if remaining_deficit > initial_deficit - len(claims):
                raise CleanupClaimError("slot deficit вырос после предыдущего DELETE")
        if len(claims) >= int(initial_deficit):
            raise CleanupClaimError("exact initial deficit уже исчерпан")

        eligible_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count FROM ad_cleanup_candidates
                WHERE run_id = ? AND adset_id = ? AND state = 'ELIGIBLE'
                """,
                (run_id, normalized["adset_id"]),
            ).fetchone()["count"]
        )
        if eligible_count < remaining_deficit:
            raise CleanupClaimError(
                "safe candidate set меньше полного remaining deficit"
            )
        oldest = conn.execute(
            """
            SELECT ad_id FROM ad_cleanup_candidates
            WHERE run_id = ? AND adset_id = ? AND state = 'ELIGIBLE'
            ORDER BY ordinal, ad_id LIMIT 1
            """,
            (run_id, normalized["adset_id"]),
        ).fetchone()
        if oldest is None or oldest["ad_id"] != normalized["ad_id"]:
            raise CleanupClaimError("claim должен брать старейший manifest candidate")

        claim_id = f"cleanup-claim-{uuid.uuid4().hex}"
        claim_evidence = _json_dumps(
            {
                "candidate_evidence": json.loads(normalized["evidence_json"]),
                "initial_slot_deficit": initial_deficit,
                "remaining_deficit_before": remaining_deficit,
                "required_slots": required_slots,
            }
        )
        conn.execute(
            """
            INSERT INTO ad_cleanup_delete_claims (
                claim_id, run_id, workflow_id, ad_id, ad_name, adset_id,
                purpose, state, evidence_json, capacity_before,
                claimed_by, claimed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'REPLACEMENT_SLOT', 'CLAIMED', ?, ?, ?, ?, ?, ?)
            """,
            (
                claim_id,
                run_id,
                workflow_id,
                normalized["ad_id"],
                normalized["ad_name"],
                normalized["adset_id"],
                claim_evidence,
                normalized["capacity_before"],
                actor,
                now_iso,
                now_iso,
                now_iso,
            ),
        )
        candidate_cursor = conn.execute(
            """
            UPDATE ad_cleanup_candidates
            SET state = 'CLAIMED', claim_id = ?, updated_at = ?
            WHERE run_id = ? AND ad_id = ? AND state = 'ELIGIBLE' AND claim_id IS NULL
            """,
            (claim_id, now_iso, run_id, normalized["ad_id"]),
        )
        if candidate_cursor.rowcount != 1:
            raise CleanupClaimError("candidate CAS не выполнен")
        conn.execute(
            """
            INSERT INTO ad_cleanup_audit (
                run_id, workflow_id, ad_id, ad_name, adset_id, action, reason,
                configured_status, effective_status, age_days,
                lifetime_spend_usd, local_spend_usd, capacity_before,
                capacity_after, evidence_json, error, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, 'DELETE_ATTEMPT', 'replacement_slot',
                      ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)
            """,
            (
                run_id,
                workflow_id,
                normalized["ad_id"],
                normalized["ad_name"],
                normalized["adset_id"],
                normalized["configured_status"],
                normalized["effective_status"],
                normalized["age_days"],
                normalized["lifetime_spend_usd"],
                normalized["local_spend_usd"],
                normalized["capacity_before"],
                claim_evidence,
                actor,
                now_iso,
            ),
        )
        run_evidence["claims_count"] = len(claims) + 1
        run_evidence["remaining_deficit_before_last_claim"] = remaining_deficit
        run_cursor = conn.execute(
            """
            UPDATE ad_cleanup_runs SET evidence_json = ?, updated_at = ?
            WHERE run_id = ? AND phase = 'RUNNING' AND lease_owner = ?
            """,
            (_json_dumps(run_evidence), now_iso, run_id, lease_owner),
        )
        if run_cursor.rowcount != 1:
            raise CleanupLeaseError("run evidence CAS не выполнен")
        row = conn.execute(
            "SELECT * FROM ad_cleanup_delete_claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        conn.commit()
        return _claim_from_row(row)
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise CleanupClaimError(
            "single-ad claim conflict; automatic retry запрещён"
        ) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _exact_bool(config: Mapping[str, Any], path: tuple[str, ...]) -> bool:
    """Читает literal bool по exact path; truthy значения запрещены."""
    current: Any = config
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            raise CleanupClaimError(f"config.{'.'.join(path)} отсутствует")
        current = current[part]
    if type(current) is not bool:
        raise CleanupClaimError(f"config.{'.'.join(path)} должен быть bool")
    return current


def _assert_irreversible_gates(config: Mapping[str, Any]) -> None:
    """Проверяет все destructive switches непосредственно у DB boundary."""
    cleaner = config.get("cleaner")
    if isinstance(cleaner, Mapping):
        cleaner_enabled = _exact_bool(config, ("cleaner", "enabled"))
        cleaner_dry_run = _exact_bool(config, ("cleaner", "dry_run"))
        irreversible = _exact_bool(
            config, ("cleaner", "allow_irreversible_delete")
        )
    else:
        cleaner_enabled = _exact_bool(config, ("enabled",))
        cleaner_dry_run = _exact_bool(config, ("dry_run",))
        irreversible = _exact_bool(config, ("allow_irreversible_delete",))

    replacement = config.get("replacement")
    replacement_enabled = (
        _exact_bool(config, ("replacement", "enabled"))
        if isinstance(replacement, Mapping)
        else _exact_bool(config, ("replacement_enabled",))
    )
    kill_switch = _exact_bool(config, ("kill_switch",))
    if (
        not cleaner_enabled
        or cleaner_dry_run
        or not irreversible
        or not replacement_enabled
        or kill_switch
    ):
        raise CleanupClaimError("irreversible delete gates закрыты")


def _load_delete_boundary(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    workflow_id: str,
    adset_id: str,
    ad_id: str,
    now: datetime,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    """Загружает и fail-closed проверяет immutable DELETE graph."""
    claim = conn.execute(
        "SELECT * FROM ad_cleanup_delete_claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    if (
        claim is None
        or claim["workflow_id"] != workflow_id
        or claim["adset_id"] != adset_id
        or claim["ad_id"] != ad_id
        or claim["purpose"] != "REPLACEMENT_SLOT"
        or claim["state"] != "CLAIMED"
    ):
        raise CleanupClaimError("durable claim не разрешает DELETE")

    run = conn.execute(
        "SELECT * FROM ad_cleanup_runs WHERE run_id = ?", (claim["run_id"],)
    ).fetchone()
    lease_expires_at = _parse_iso(run["lease_expires_at"]) if run else None
    if (
        run is None
        or run["run_kind"] != "REPLACEMENT_SLOT"
        or run["workflow_id"] != workflow_id
        or run["requested_mode"] != "active"
        or run["effective_mode"] != "active"
        or run["phase"] != "RUNNING"
        or not _optional_text(run["lease_owner"])
        or lease_expires_at is None
        or lease_expires_at <= now
    ):
        raise CleanupClaimError("cleanup run/lease не разрешает DELETE")
    config = _json_object(run["config_json"], "config_json")
    _assert_irreversible_gates(config)

    workflow = conn.execute(
        "SELECT * FROM ad_replacement_workflows WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()
    if (
        workflow is None
        or workflow["phase"] != "WAITING_SLOT"
        or workflow["adset_id"] != adset_id
        or _optional_text(workflow["released_ad_id"])
        or _optional_text(workflow["replacement_ad_id"])
    ):
        raise CleanupClaimError("replacement workflow drifted before DELETE")

    link = conn.execute(
        "SELECT * FROM ad_replacement_launch_links WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()
    if link is None:
        raise CleanupClaimError("replacement launch link отсутствует")

    candidate = conn.execute(
        """
        SELECT * FROM ad_cleanup_candidates
        WHERE run_id = ? AND ad_id = ? AND claim_id = ?
        """,
        (claim["run_id"], ad_id, claim_id),
    ).fetchone()
    if (
        candidate is None
        or candidate["state"] != "CLAIMED"
        or candidate["adset_id"] != adset_id
        or candidate["capacity_before"] != claim["capacity_before"]
    ):
        raise CleanupClaimError("durable candidate drifted before DELETE")
    expected_count = _validate_launch_link(link, candidate)

    cleaner_config = config.get("cleaner")
    stale_days = (
        cleaner_config.get("stale_days")
        if isinstance(cleaner_config, Mapping)
        else config.get("stale_days", 15)
    )
    if type(stale_days) is not int or not 15 <= stale_days <= 365:
        raise CleanupClaimError("config.stale_days должен быть int 15..365")
    _validate_zero_candidate(candidate, minimum_age_days=stale_days)

    hard_reserve_slots = _config_int(
        config, "hard_reserve_slots", minimum=1, maximum=5
    )
    other_reserved = config.get("other_reserved_slots", 0)
    if type(other_reserved) is not int or not 0 <= other_reserved <= 50:
        raise CleanupClaimError("other_reserved_slots должен быть int 0..50")
    live_available = int(claim["capacity_before"])
    remaining_deficit = max(
        expected_count + hard_reserve_slots - max(live_available - other_reserved, 0),
        0,
    )
    if remaining_deficit <= 0:
        raise CleanupClaimError("slot deficit исчез до DELETE")
    claim_evidence = _json_object(claim["evidence_json"], "claim.evidence_json")
    if claim_evidence.get("remaining_deficit_before") != remaining_deficit:
        raise CleanupClaimError("slot deficit drifted before DELETE")
    return claim, run, workflow, link, candidate


def create_cleanup_delete_authorization(
    *,
    token_id: str,
    claim: CleanupDeleteClaim,
    expires_at: datetime,
) -> None:
    """CAS-сохраняет единственную durable authorization для exact claim."""
    token_id = _required_text(token_id, "token_id")
    if type(claim) is not CleanupDeleteClaim:
        raise CleanupClaimError("CleanupDeleteClaim обязателен")
    if claim.state != "CLAIMED" or claim.purpose != "REPLACEMENT_SLOT":
        raise CleanupClaimError("claim нельзя авторизовать")
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("expires_at должен содержать timezone")
    now = _now()
    if expires_at <= now:
        raise CleanupClaimError("authorization уже истекла")
    now_iso = _iso(now)
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        durable_claim, _run, _workflow, _link, _candidate = _load_delete_boundary(
            conn,
            claim_id=claim.claim_id,
            workflow_id=claim.workflow_id,
            adset_id=claim.adset_id,
            ad_id=claim.ad_id,
            now=now,
        )
        if (
            durable_claim["run_id"] != claim.run_id
            or durable_claim["ad_name"] != claim.ad_name
            or int(durable_claim["capacity_before"]) != claim.capacity_before
        ):
            raise CleanupClaimError("claim object не совпадает с durable claim")
        conn.execute(
            """
            INSERT INTO ad_cleanup_delete_authorizations (
                token_id, claim_id, run_id, workflow_id, adset_id, ad_id,
                purpose, state, issued_at, expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'REPLACEMENT_SLOT', 'ISSUED', ?, ?, ?)
            """,
            (
                token_id,
                claim.claim_id,
                claim.run_id,
                claim.workflow_id,
                claim.adset_id,
                claim.ad_id,
                now_iso,
                _iso(expires_at),
                now_iso,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise CleanupClaimError("authorization уже выдавалась для claim") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_cleanup_delete_http_started(
    *,
    token_id: str,
    claim_id: str,
    workflow_id: str,
    adset_id: str,
    ad_id: str,
    live_capacity_before: int,
) -> None:
    """Durable CAS прямо перед HTTP; crash после commit требует reconcile."""
    token_id = _required_text(token_id, "token_id")
    claim_id = _required_text(claim_id, "claim_id")
    workflow_id = _required_text(workflow_id, "workflow_id")
    adset_id = _required_text(adset_id, "adset_id")
    ad_id = _required_text(ad_id, "ad_id")
    live_capacity_before = _nonnegative_int(
        live_capacity_before, "live_capacity_before", maximum=50
    )
    now = _now()
    now_iso = _iso(now)
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        claim, _run, _workflow, _link, _candidate = _load_delete_boundary(
            conn,
            claim_id=claim_id,
            workflow_id=workflow_id,
            adset_id=adset_id,
            ad_id=ad_id,
            now=now,
        )
        if int(claim["capacity_before"]) != live_capacity_before:
            raise CleanupClaimError("live capacity drifted before DELETE")
        authorization = conn.execute(
            """
            SELECT * FROM ad_cleanup_delete_authorizations
            WHERE token_id = ? AND claim_id = ? AND workflow_id = ?
              AND adset_id = ? AND ad_id = ? AND purpose = 'REPLACEMENT_SLOT'
            """,
            (token_id, claim_id, workflow_id, adset_id, ad_id),
        ).fetchone()
        expires_at = _parse_iso(authorization["expires_at"]) if authorization else None
        if (
            authorization is None
            or authorization["state"] != "ISSUED"
            or expires_at is None
            or expires_at <= now
        ):
            raise CleanupClaimError("authorization не ISSUED или истекла")
        auth_cursor = conn.execute(
            """
            UPDATE ad_cleanup_delete_authorizations
            SET state = 'HTTP_STARTED', http_started_at = ?, updated_at = ?
            WHERE token_id = ? AND state = 'ISSUED' AND expires_at = ?
            """,
            (now_iso, now_iso, token_id, authorization["expires_at"]),
        )
        if auth_cursor.rowcount != 1:
            raise CleanupClaimError("authorization HTTP-start CAS не выполнен")
        claim_cursor = conn.execute(
            """
            UPDATE ad_cleanup_delete_claims
            SET state = 'RECONCILE_REQUIRED', updated_at = ?,
                error = 'http_started_reconcile_required'
            WHERE claim_id = ? AND state = 'CLAIMED'
            """,
            (now_iso, claim_id),
        )
        candidate_cursor = conn.execute(
            """
            UPDATE ad_cleanup_candidates
            SET state = 'RECONCILE_REQUIRED', updated_at = ?
            WHERE run_id = ? AND ad_id = ? AND claim_id = ? AND state = 'CLAIMED'
            """,
            (now_iso, claim["run_id"], ad_id, claim_id),
        )
        if claim_cursor.rowcount != 1 or candidate_cursor.rowcount != 1:
            raise CleanupClaimError("claim/candidate HTTP-start CAS не выполнен")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def prepare_cleanup_delete_boundary(
    *,
    claim_id: str,
    workflow_id: str,
    adset_id: str,
    ad_id: str,
    runtime_config_hash: str,
    runtime_config_generation: int | str | None,
    local_zero_evidence: Mapping[str, Any],
) -> str:
    """Связывает свежие runtime/local evidence с ещё не начатым DELETE.

    Вызывается под общим adset lock сразу после финального live lifetime read.
    Любой drift оставляет authorization в ``ISSUED``, а claim в ``CLAIMED``.
    """
    claim_id = _required_text(claim_id, "claim_id")
    workflow_id = _required_text(workflow_id, "workflow_id")
    adset_id = _required_text(adset_id, "adset_id")
    ad_id = _required_text(ad_id, "ad_id")
    if (
        type(runtime_config_hash) is not str
        or re.fullmatch(r"[0-9a-f]{64}", runtime_config_hash) is None
    ):
        raise CleanupClaimError("runtime config hash некорректен")
    if runtime_config_generation is not None and (
        isinstance(runtime_config_generation, bool)
        or not isinstance(runtime_config_generation, (int, str))
        or (
            isinstance(runtime_config_generation, str)
            and not runtime_config_generation.strip()
        )
    ):
        raise CleanupClaimError("runtime config generation некорректен")
    if not isinstance(local_zero_evidence, Mapping):
        raise CleanupClaimError("fresh local zero evidence отсутствует")
    for field in ("complete", "kb_found"):
        if local_zero_evidence.get(field) is not True:
            raise CleanupClaimError(f"fresh local evidence {field} не подтверждён")
    for field in ("any_positive_delivery", "any_positive_outcome"):
        if local_zero_evidence.get(field) is not False:
            raise CleanupClaimError(f"fresh local evidence {field} не равен false")
    for field in (
        "local_spend_usd",
        "local_impressions",
        "local_clicks",
        "local_leads",
        "local_payments",
    ):
        value = local_zero_evidence.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != 0:
            raise CleanupClaimError(f"fresh local evidence {field} не равен zero")
    normalized_local = dict(local_zero_evidence)
    local_json = _json_dumps(normalized_local)
    local_hash = hashlib.sha256(local_json.encode("utf-8")).hexdigest()
    now = _now()
    now_iso = _iso(now)

    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        claim, run, _workflow, _link, _candidate = _load_delete_boundary(
            conn,
            claim_id=claim_id,
            workflow_id=workflow_id,
            adset_id=adset_id,
            ad_id=ad_id,
            now=now,
        )
        config = _json_object(run["config_json"], "config_json")
        if config.get("config_hash") != runtime_config_hash:
            raise CleanupClaimError("runtime config hash drifted before DELETE")
        if config.get("config_generation") != runtime_config_generation:
            raise CleanupClaimError("runtime config generation drifted before DELETE")
        authorization = conn.execute(
            """
            SELECT state FROM ad_cleanup_delete_authorizations
            WHERE claim_id = ? AND workflow_id = ? AND adset_id = ? AND ad_id = ?
            """,
            (claim_id, workflow_id, adset_id, ad_id),
        ).fetchone()
        if authorization is None or authorization["state"] != "ISSUED":
            raise CleanupClaimError("authorization не ISSUED перед fresh evidence")
        claim_evidence = _json_object(claim["evidence_json"], "claim.evidence_json")
        claim_evidence["delete_boundary"] = {
            "runtime_config_hash": runtime_config_hash,
            "runtime_config_generation": runtime_config_generation,
            "local_zero_evidence": normalized_local,
            "local_zero_evidence_sha256": local_hash,
            "checked_at": now_iso,
        }
        evidence_json = _json_dumps(claim_evidence)
        cursor = conn.execute(
            """
            UPDATE ad_cleanup_delete_claims
            SET evidence_json = ?, updated_at = ?
            WHERE claim_id = ? AND state = 'CLAIMED'
            """,
            (evidence_json, now_iso, claim_id),
        )
        audit_cursor = conn.execute(
            """
            UPDATE ad_cleanup_audit
            SET evidence_json = ?
            WHERE run_id = ? AND workflow_id = ? AND ad_id = ?
              AND action = 'DELETE_ATTEMPT'
            """,
            (evidence_json, claim["run_id"], workflow_id, ad_id),
        )
        if cursor.rowcount != 1 or audit_cursor.rowcount != 1:
            raise CleanupClaimError("fresh boundary evidence CAS не выполнен")
        conn.commit()
        return local_hash
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def revoke_cleanup_delete_authorization(claim_id: str) -> None:
    """Durable revoke допустим только пока HTTP ещё не начинался."""
    claim_id = _required_text(claim_id, "claim_id")
    now_iso = _iso(_now())
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE ad_cleanup_delete_authorizations
            SET state = 'REVOKED', revoked_at = ?, updated_at = ?
            WHERE claim_id = ? AND state = 'ISSUED'
            """,
            (now_iso, now_iso, claim_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_cleanup_delete(
    claim_id: str,
    outcome: DeleteOutcome,
    capacity_after: int | None,
    evidence: Mapping[str, Any],
    error: str | None,
) -> None:
    """Фиксирует postcheck outcome; ambiguous результат остаётся unresolved."""
    claim_id = _required_text(claim_id, "claim_id")
    outcome = _required_text(outcome, "outcome")  # type: ignore[assignment]
    if outcome not in _DELETE_OUTCOMES:
        raise ValueError(f"неподдерживаемый delete outcome: {outcome}")
    if not isinstance(evidence, Mapping):
        raise TypeError("evidence должен быть Mapping")
    normalized_capacity = (
        None
        if capacity_after is None
        else _nonnegative_int(capacity_after, "capacity_after", maximum=50)
    )
    evidence_json = _json_dumps({**evidence, "outcome": outcome})
    safe_error = sanitize_text(error) if error else None
    now_iso = _iso(_now())

    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        claim = conn.execute(
            "SELECT * FROM ad_cleanup_delete_claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if claim is None:
            raise CleanupClaimError("claim не найден")
        authorization = conn.execute(
            """
            SELECT * FROM ad_cleanup_delete_authorizations WHERE claim_id = ?
            """,
            (claim_id,),
        ).fetchone()
        final_authorization_state = (
            "CONSUMED" if outcome in {"DELETED", "DELETE_FAILED"}
            else "RECONCILE_REQUIRED"
        )
        if claim["state"] == outcome and (
            authorization is not None
            and authorization["state"] == final_authorization_state
        ):
            if claim["capacity_after"] != normalized_capacity:
                raise CleanupClaimError("повтор finish содержит другой capacity_after")
            conn.commit()
            return
        if (
            claim["state"] != "RECONCILE_REQUIRED"
            or authorization is None
            or authorization["state"] != "HTTP_STARTED"
        ):
            raise CleanupClaimError("finished/unresolved claim нельзя переписать")
        if outcome == "DELETED":
            if normalized_capacity is None:
                raise CleanupClaimError("DELETED требует capacity_after")
            if normalized_capacity != int(claim["capacity_before"]) + 1:
                raise CleanupClaimError("DELETED требует роста available ровно на один")
            if safe_error is not None:
                raise CleanupClaimError("DELETED не допускает error")
        elif safe_error is None:
            raise CleanupClaimError(f"{outcome} требует sanitized error")

        conn.execute(
            """
            UPDATE ad_cleanup_delete_claims
            SET state = ?, evidence_json = ?, capacity_after = ?,
                completed_at = ?, error = ?, updated_at = ?
            WHERE claim_id = ? AND state = 'RECONCILE_REQUIRED'
            """,
            (
                outcome,
                evidence_json,
                normalized_capacity,
                now_iso,
                safe_error,
                now_iso,
                claim_id,
            ),
        )
        candidate_cursor = conn.execute(
            """
            UPDATE ad_cleanup_candidates
            SET state = ?, capacity_after = ?, evidence_json = ?, updated_at = ?
            WHERE run_id = ? AND ad_id = ? AND claim_id = ?
              AND state = 'RECONCILE_REQUIRED'
            """,
            (
                outcome,
                normalized_capacity,
                evidence_json,
                now_iso,
                claim["run_id"],
                claim["ad_id"],
                claim_id,
            ),
        )
        if candidate_cursor.rowcount != 1:
            raise CleanupClaimError("claim candidate CAS не выполнен")
        authorization_state = final_authorization_state
        authorization_cursor = conn.execute(
            """
            UPDATE ad_cleanup_delete_authorizations
            SET state = ?,
                consumed_at = CASE WHEN ? = 'CONSUMED' THEN ? ELSE NULL END,
                reconciled_at = CASE
                    WHEN ? = 'RECONCILE_REQUIRED' THEN ? ELSE NULL
                END,
                updated_at = ?
            WHERE claim_id = ? AND state = 'HTTP_STARTED'
            """,
            (
                authorization_state,
                authorization_state,
                now_iso,
                authorization_state,
                now_iso,
                now_iso,
                claim_id,
            ),
        )
        if authorization_cursor.rowcount != 1:
            raise CleanupClaimError("authorization outcome CAS не выполнен")
        audit_action = "DELETED" if outcome == "DELETED" else "DELETE_FAILED"
        conn.execute(
            """
            INSERT INTO ad_cleanup_audit (
                run_id, workflow_id, ad_id, ad_name, adset_id, action, reason,
                configured_status, effective_status, age_days,
                lifetime_spend_usd, local_spend_usd, capacity_before,
                capacity_after, evidence_json, error, actor, created_at
            )
            SELECT ?, ?, c.ad_id, c.ad_name, c.adset_id, ?, ?,
                   c.configured_status, c.effective_status, c.age_days,
                   c.lifetime_spend_usd, c.local_spend_usd, c.capacity_before,
                   ?, ?, ?, 'cleanup-postcheck', ?
            FROM ad_cleanup_candidates AS c
            WHERE c.run_id = ? AND c.ad_id = ? AND c.claim_id = ?
            """,
            (
                claim["run_id"],
                claim["workflow_id"],
                audit_action,
                outcome.lower(),
                normalized_capacity,
                evidence_json,
                safe_error,
                now_iso,
                claim["run_id"],
                claim["ad_id"],
                claim_id,
            ),
        )
        if outcome != "DELETED":
            conn.execute(
                """
                UPDATE ad_cleanup_runs
                SET phase = 'BLOCKED', lease_owner = NULL, lease_expires_at = NULL,
                    error = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (safe_error, now_iso, claim["run_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalized_counters(counters: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(counters, Mapping):
        raise TypeError("counters должен быть Mapping")
    unknown = set(counters) - set(_COUNTER_COLUMNS)
    if unknown:
        raise ValueError(f"неизвестные counters: {sorted(unknown)}")
    return {
        name: _nonnegative_int(counters.get(name, 0), f"counters.{name}")
        for name in _COUNTER_COLUMNS
    }


def finish_cleanup_run(
    run_id: str,
    phase: CleanupRunPhase,
    counters: Mapping[str, int],
    errors: Sequence[str],
    *,
    lease_owner: str | None = None,
) -> None:
    """Завершает run только живым owner; owner обязателен для CAS."""
    run_id = _required_text(run_id, "run_id")
    phase = _required_text(phase, "phase")  # type: ignore[assignment]
    if phase not in _FINISH_PHASES:
        raise ValueError("finish допускает только terminal phase")
    lease_owner = _required_text(lease_owner, "lease_owner")
    normalized = _normalized_counters(counters)
    if isinstance(errors, (str, bytes)) or not isinstance(errors, Sequence):
        raise TypeError("errors должен быть Sequence[str]")
    safe_errors = [sanitize_text(item) for item in errors if str(item).strip()]
    error_text = "\n".join(safe_errors) or None
    now = _now()
    now_iso = _iso(now)

    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute(
            "SELECT * FROM ad_cleanup_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        expiry = _parse_iso(run["lease_expires_at"]) if run is not None else None
        if (
            run is None
            or run["phase"] != "RUNNING"
            or run["lease_owner"] != lease_owner
            or expiry is None
            or expiry <= now
        ):
            raise CleanupLeaseError("run finish потерял live lease_owner")
        unresolved = _unresolved_claim_count(conn, run_id)
        if unresolved and phase in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}:
            raise CleanupClaimError("unresolved claim нельзя завершить как success")
        values = [normalized[name] for name in _COUNTER_COLUMNS]
        cursor = conn.execute(
            """
            UPDATE ad_cleanup_runs
            SET phase = ?, discovered_count = ?, eligible_count = ?,
                would_delete_count = ?, deleted_count = ?, skipped_count = ?,
                warning_count = ?, error_count = ?, error = ?,
                completed_at = ?, updated_at = ?,
                lease_owner = NULL, lease_expires_at = NULL
            WHERE run_id = ? AND phase = 'RUNNING' AND lease_owner = ?
            """,
            (
                phase,
                *values,
                error_text,
                now_iso,
                now_iso,
                run_id,
                lease_owner,
            ),
        )
        if cursor.rowcount != 1:
            raise CleanupLeaseError("run finish CAS не выполнен")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _claim_public(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["evidence"] = _json_object(result.pop("evidence_json"), "evidence_json")
    return result


def list_unresolved_delete_claims(adset_id: str | None = None) -> list[dict[str, Any]]:
    """Возвращает claims, которые навсегда блокируют автоматический retry."""
    normalized_adset = _optional_text(adset_id)
    conn = _get_connection()
    try:
        sql = (
            "SELECT * FROM ad_cleanup_delete_claims "
            "WHERE state IN ('CLAIMED','DELETE_FAILED','RECONCILE_REQUIRED')"
        )
        params: tuple[Any, ...] = ()
        if normalized_adset is not None:
            sql += " AND adset_id = ?"
            params = (normalized_adset,)
        sql += " ORDER BY claimed_at, claim_id"
        return [_claim_public(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _decoded_row(row: sqlite3.Row, json_fields: Sequence[str]) -> dict[str, Any]:
    value = dict(row)
    for field in json_fields:
        if field in value:
            value[field.removesuffix("_json")] = json.loads(value.pop(field) or "null")
    return value


def _latest_adset_snapshots(
    conn: sqlite3.Connection, run: sqlite3.Row | None
) -> list[dict[str, Any]]:
    if run is None:
        return []
    run_evidence = _json_object(run["evidence_json"], "evidence_json")
    saved_adsets = run_evidence.get("adsets")
    if isinstance(saved_adsets, list) and all(
        isinstance(item, dict) for item in saved_adsets
    ):
        return [dict(item) for item in saved_adsets]
    run_id = str(run["run_id"])
    rows = conn.execute(
        """
        SELECT * FROM ad_cleanup_candidates
        WHERE run_id = ? ORDER BY adset_id, ordinal, ad_id
        """,
        (run_id,),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["adset_id"]), []).append(row)
    result: list[dict[str, Any]] = []
    for adset_rows in grouped.values():
        first = adset_rows[0]
        evidence = _json_object(first["evidence_json"], "evidence_json")
        available = first["capacity_before"]
        safe_count = sum(1 for row in adset_rows if row["state"] == "ELIGIBLE")
        live_status = str(
            evidence.get("live_status")
            or ("ok" if available is not None else "unavailable")
        )
        if available is None:
            severity = "critical" if live_status != "ok" else "ok"
        elif int(available) <= 2:
            severity = "critical"
        elif int(available) <= 5:
            severity = "warning"
        else:
            severity = "ok"
        result.append(
            {
                "account_kind": first["account_kind"],
                "adset_id": first["adset_id"],
                "adset_name": first["adset_name"],
                "live_status": live_status,
                "used": None if available is None else 50 - int(available),
                "available": available,
                "active_count": evidence.get("active_count"),
                "safe_candidate_count": safe_count,
                "severity": evidence.get("severity", severity),
                "reason": evidence.get("reason"),
            }
        )
    return sorted(
        result,
        key=lambda item: (
            item["available"] is None,
            item["available"],
            item["adset_id"],
        ),
    )


def get_cleanup_status(tenant_id: str = "default") -> dict[str, Any]:
    """Читает durable snapshot/claims/workflows/recovery без внешних вызовов."""
    tenant_id = _required_text(tenant_id, "tenant_id")
    conn = _get_connection()
    try:
        last_run_row = conn.execute(
            """
            SELECT * FROM ad_cleanup_runs
            WHERE tenant_id = ? AND run_kind = 'PROACTIVE_DAILY'
            ORDER BY scheduled_date DESC, created_at DESC LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()
        last_run = (
            None
            if last_run_row is None
            else _decoded_row(last_run_row, ("config_json", "evidence_json"))
        )
        unresolved = [
            _claim_public(row)
            for row in conn.execute(
                """
                SELECT * FROM ad_cleanup_delete_claims
                WHERE state IN ('CLAIMED','DELETE_FAILED','RECONCILE_REQUIRED')
                ORDER BY claimed_at, claim_id
                """
            ).fetchall()
        ]
        workflows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM ad_replacement_workflows
                WHERE tenant_id = ? AND phase NOT IN ('COMPLETED','CANCELLED')
                ORDER BY updated_at DESC, workflow_id
                """,
                (tenant_id,),
            ).fetchall()
        ]
        case_rows = conn.execute(
            """
            SELECT * FROM launch_recovery_cases
            WHERE tenant_id = ?
            ORDER BY CASE WHEN phase IN ('NO_ACTION','RECOVERED','CANCELLED')
                          THEN 1 ELSE 0 END,
                     updated_at DESC, case_id
            """,
            (tenant_id,),
        ).fetchall()
        recovery_cases: list[dict[str, Any]] = []
        for case_row in case_rows:
            case = _decoded_row(
                case_row,
                (
                    "target_cities_json",
                    "found_ads_json",
                    "missing_cities_json",
                    "expected_names_json",
                    "media_manifest_json",
                    "fb_evidence_json",
                ),
            )
            plans = [
                _decoded_row(
                    plan,
                    ("expected_ad_names_json", "found_ad_ids_json", "evidence_json"),
                )
                for plan in conn.execute(
                    """
                    SELECT * FROM launch_recovery_city_plans
                    WHERE case_id = ? ORDER BY city, plan_id
                    """,
                    (case_row["case_id"],),
                ).fetchall()
            ]
            case["city_plans"] = plans
            recovery_cases.append(case)
        return {
            "generated_at": _iso(_now()),
            "last_run": last_run,
            "adsets": _latest_adset_snapshots(conn, last_run_row),
            "unresolved_claims": unresolved,
            "replacement_workflows": workflows,
            "recovery_cases": recovery_cases,
        }
    finally:
        conn.close()
