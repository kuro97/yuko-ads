"""Read-only evidence из decisions.db без инициализации и скрытых fallback."""

from __future__ import annotations

import hashlib
import sqlite3
import stat
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import config
from services.approval_checker_models import (
    EvidenceRecord,
    EvidenceRequest,
    EvidenceState,
    FactCategory,
    Metric,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    canonical_json,
)

_REQUIRED_DECISIONS = ("id", "ad_id", "action", "confirmed_by", "created_at")
_REQUIRED_FEEDBACK = ("id", "ad_id", "action", "verdict", "created_at")


class LocalSqliteEvidenceError(RuntimeError):
    """Локальный SQLite-снимок нельзя считать полным."""


@dataclass(frozen=True, slots=True)
class ReadOnlyRows:
    rows: tuple[sqlite3.Row, ...]
    fetched_at: datetime
    data_version: int
    schema_sha256: str
    high_watermark: str


def _parse_sqlite_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LocalSqliteEvidenceError("SQLITE_TIMESTAMP_INVALID")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise LocalSqliteEvidenceError("SQLITE_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sqlite_window_bounds(window: TimeWindow) -> tuple[str, str]:
    start = window.start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end = window.end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return start, end


def _read_only_rows(
    path: Path,
    table: str,
    required_columns: Sequence[str],
    window: TimeWindow | None,
    *,
    order_by: str = "id",
    where_sql: str = "",
    where_params: Sequence[object] = (),
) -> ReadOnlyRows:
    """Читает одну стабильную транзакцию через SQLite ``mode=ro``/query_only."""

    original = path.expanduser()
    try:
        original_lstat = original.lstat()
    except FileNotFoundError as exc:
        raise LocalSqliteEvidenceError("SQLITE_DB_MISSING") from exc
    if stat.S_ISLNK(original_lstat.st_mode) or not stat.S_ISREG(original_lstat.st_mode):
        raise LocalSqliteEvidenceError("SQLITE_DB_UNSAFE_PATH")
    absolute = original.resolve()
    if not absolute.is_file():
        raise LocalSqliteEvidenceError("SQLITE_DB_MISSING")
    before = absolute.stat()
    uri = f"{absolute.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error as exc:
        raise LocalSqliteEvidenceError("SQLITE_OPEN_FAILED") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        if query_only != 1:
            raise LocalSqliteEvidenceError("SQLITE_QUERY_ONLY_DISABLED")
        data_version_before = int(connection.execute("PRAGMA data_version").fetchone()[0])
        schema_rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        columns = tuple(str(row[1]) for row in schema_rows)
        if not schema_rows:
            raise LocalSqliteEvidenceError("SQLITE_TABLE_MISSING")
        missing = set(required_columns) - set(columns)
        if missing:
            raise LocalSqliteEvidenceError("SQLITE_COLUMNS_MISSING")

        clauses: list[str] = []
        parameters: list[object] = []
        if window is not None:
            start, end = _sqlite_window_bounds(window)
            clauses.append("created_at >= ? AND created_at < ?")
            parameters.extend((start, end))
        if where_sql:
            clauses.append(f"({where_sql})")
            parameters.extend(where_params)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        selected = ", ".join(f'"{column}"' for column in required_columns)
        rows = tuple(
            connection.execute(
                f'SELECT {selected} FROM "{table}"{where} ORDER BY "{order_by}"',
                tuple(parameters),
            ).fetchall()
        )
        data_version_after = int(connection.execute("PRAGMA data_version").fetchone()[0])
        connection.rollback()
    except sqlite3.Error as exc:
        raise LocalSqliteEvidenceError("SQLITE_READ_FAILED") from exc
    finally:
        connection.close()

    after = absolute.stat()
    if data_version_before != data_version_after:
        raise LocalSqliteEvidenceError("SQLITE_CHANGED_DURING_READ")
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise LocalSqliteEvidenceError("SQLITE_FILE_REPLACED")

    identities = [str(row["id"] if "id" in required_columns else row[order_by]) for row in rows]
    if len(identities) != len(set(identities)):
        raise LocalSqliteEvidenceError("SQLITE_DUPLICATE_ID")
    schema_sha256 = hashlib.sha256(canonical_json({"table": table, "columns": columns})).hexdigest()
    timestamps = [str(row["created_at"]) for row in rows] if "created_at" in required_columns else []
    high_watermark = canonical_json(
        {
            "count": len(rows),
            "min_id": min(identities) if identities else None,
            "max_id": max(identities) if identities else None,
            "max_timestamp": max(timestamps) if timestamps else None,
            "data_version": data_version_after,
        }
    ).decode("utf-8")
    return ReadOnlyRows(
        rows=rows,
        fetched_at=datetime.now(timezone.utc),
        data_version=data_version_after,
        schema_sha256=schema_sha256,
        high_watermark=high_watermark,
    )


def _error(source: SourceSystem, now: datetime, exc: Exception) -> SourceEvidence:
    code = str(exc) if isinstance(exc, LocalSqliteEvidenceError) else type(exc).__name__
    state = EvidenceState.INCOMPLETE if isinstance(exc, LocalSqliteEvidenceError) else EvidenceState.ERROR
    return SourceEvidence(
        source=source,
        state=state,
        fetched_at=now,
        data_as_of=None,
        from_cache=False,
        complete=False,
        records=(),
        error_code=code[:120],
    )


def _load_each_window(
    request: EvidenceRequest,
    now: datetime,
    source: SourceSystem,
    loader: Callable[[TimeWindow], tuple[list[EvidenceRecord], datetime | None]],
) -> SourceEvidence:
    if not request.windows:
        return _error(source, now, LocalSqliteEvidenceError("SQLITE_WINDOW_MISSING"))
    records: list[EvidenceRecord] = []
    data_as_of: datetime | None = None
    try:
        for window in request.windows:
            window_records, window_as_of = loader(window)
            records.extend(window_records)
            if window_as_of is not None:
                data_as_of = window_as_of if data_as_of is None else max(data_as_of, window_as_of)
    except Exception as exc:
        return _error(source, now, exc)
    return SourceEvidence(
        source=source,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=data_as_of or now,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )


def load_decisions_evidence(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    """Читает decisions в точных полуинтервалах и не создаёт БД/таблицу."""

    def load(window: TimeWindow) -> tuple[list[EvidenceRecord], datetime | None]:
        snapshot = _read_only_rows(
            config.REPORT_CHECKER_DECISIONS_DB_PATH,
            "decisions",
            _REQUIRED_DECISIONS,
            window,
        )
        records: list[EvidenceRecord] = []
        per_ad: Counter[str] = Counter()
        confirmed_ids: dict[str, list[str]] = {"paused": [], "scaled": []}
        latest: datetime | None = None
        metadata = (f"schema:{snapshot.schema_sha256}", f"data-version:{snapshot.data_version}")
        for row in snapshot.rows:
            created_at = _parse_sqlite_datetime(row["created_at"])
            if not window.start <= created_at < window.end:
                raise LocalSqliteEvidenceError("SQLITE_ROW_OUTSIDE_WINDOW")
            ad_id = str(row["ad_id"] or "").strip()
            action = str(row["action"] or "").strip()
            confirmed_by = str(row["confirmed_by"] or "").strip()
            if not ad_id or not action or not confirmed_by:
                raise LocalSqliteEvidenceError("DECISION_ROW_INVALID")
            decision_id = str(row["id"])
            per_ad[ad_id] += 1
            if confirmed_by.casefold().startswith("autopilot"):
                if action == "PAUSED":
                    confirmed_ids["paused"].append(decision_id)
                if "scale" in action.casefold():
                    confirmed_ids["scaled"].append(decision_id)
            latest = created_at if latest is None else max(latest, created_at)
            records.append(
                EvidenceRecord(
                    category=FactCategory.HISTORY_AUDIT,
                    subject=SubjectRef(SubjectKind.DECISION, decision_id, parent_id=ad_id),
                    metric=Metric.RECORD_STATUS,
                    value=action,
                    source=SourceSystem.DECISIONS_DB,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency=None,
                    entity_ids=(f"ad:{ad_id}", f"confirmed-by:{confirmed_by}", *metadata),
                )
            )
        for ad_id, count in sorted(per_ad.items()):
            records.append(
                EvidenceRecord(
                    category=FactCategory.HISTORY_AUDIT,
                    subject=SubjectRef(SubjectKind.AD, ad_id),
                    metric=Metric.DECISION_COUNT,
                    value=count,
                    source=SourceSystem.DECISIONS_DB,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency=None,
                    entity_ids=metadata,
                )
            )
        for action_name in ("paused", "scaled"):
            decision_ids = tuple(sorted(confirmed_ids[action_name]))
            records.append(
                EvidenceRecord(
                    category=FactCategory.BUSINESS_METRIC,
                    subject=SubjectRef(SubjectKind.DECISION, action_name),
                    metric=Metric.DECISION_COUNT,
                    value=len(decision_ids),
                    source=SourceSystem.DECISIONS_DB,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency=None,
                    entity_ids=(*(f"decision:{item}" for item in decision_ids), *metadata),
                )
            )
        return records, latest

    return _load_each_window(request, now, SourceSystem.DECISIONS_DB, load)


def load_feedback_evidence(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    """Читает owner feedback и независимо считает agreement=up/(up+down)."""

    def load(window: TimeWindow) -> tuple[list[EvidenceRecord], datetime | None]:
        snapshot = _read_only_rows(
            config.REPORT_CHECKER_DECISIONS_DB_PATH,
            "autopilot_feedback",
            _REQUIRED_FEEDBACK,
            window,
        )
        counts: Counter[str] = Counter()
        records: list[EvidenceRecord] = []
        latest: datetime | None = None
        metadata = (f"schema:{snapshot.schema_sha256}", f"data-version:{snapshot.data_version}")
        for row in snapshot.rows:
            verdict = str(row["verdict"] or "")
            if verdict not in {"up", "down"}:
                raise LocalSqliteEvidenceError("FEEDBACK_VERDICT_INVALID")
            ad_id = str(row["ad_id"] or "").strip()
            action = str(row["action"] or "").strip()
            if not ad_id or not action:
                raise LocalSqliteEvidenceError("FEEDBACK_ROW_INVALID")
            created_at = _parse_sqlite_datetime(row["created_at"])
            if not window.start <= created_at < window.end:
                raise LocalSqliteEvidenceError("SQLITE_ROW_OUTSIDE_WINDOW")
            latest = created_at if latest is None else max(latest, created_at)
            counts[verdict] += 1
            records.append(
                EvidenceRecord(
                    category=FactCategory.HISTORY_AUDIT,
                    subject=SubjectRef(SubjectKind.FEEDBACK, str(row["id"]), parent_id=ad_id),
                    metric=Metric.RECORD_STATUS,
                    value=verdict,
                    source=SourceSystem.AUTOPILOT_FEEDBACK,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency=None,
                    entity_ids=(f"ad:{ad_id}", f"action:{action}", *metadata),
                )
            )
        ids_by_verdict = {
            verdict: tuple(
                sorted(
                    str(row["id"])
                    for row in snapshot.rows
                    if str(row["verdict"] or "") == verdict
                )
            )
            for verdict in ("up", "down")
        }
        # Согласие владельца считается по РЕАЛЬНЫМ решениям owner_action_decisions
        # (кнопок 👍/👎 больше нет). Тот же источник и та же формула, что у
        # services/autopilot_feedback.get_feedback_stats, иначе кросс-проверка
        # отчёта разошлась бы сама с собой. Наследие остаётся fallback'ом.
        from services.autopilot_feedback import owner_decision_counts

        approve, reject, approve_ids, reject_ids = owner_decision_counts(
            config.REPORT_CHECKER_DECISIONS_DB_PATH,
            window_start=window.start,
            window_end=window.end,
        )
        if approve + reject > 0:
            counts = Counter({"up": approve, "down": reject})
            ids_by_verdict = {"up": approve_ids, "down": reject_ids}
        total = counts["up"] + counts["down"]
        for label, count in (("up", counts["up"]), ("down", counts["down"]), ("total", total)):
            exact_ids = (
                ids_by_verdict["up"] + ids_by_verdict["down"]
                if label == "total"
                else ids_by_verdict[label]
            )
            records.append(
                EvidenceRecord(
                    category=FactCategory.BUSINESS_METRIC,
                    subject=SubjectRef(SubjectKind.FEEDBACK, label),
                    metric=Metric.FEEDBACK_COUNT,
                    value=count,
                    source=SourceSystem.AUTOPILOT_FEEDBACK,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency=None,
                    entity_ids=(*(f"feedback:{item}" for item in exact_ids), *metadata),
                )
            )
        agreement = (
            Decimal(str(round(counts["up"] / total * 100, 1)))
            if total
            else None
        )
        records.append(
            EvidenceRecord(
                category=FactCategory.BUSINESS_METRIC,
                subject=SubjectRef(SubjectKind.FEEDBACK, "agreement"),
                metric=Metric.AGREEMENT_PCT,
                value=agreement,
                source=SourceSystem.AUTOPILOT_FEEDBACK,
                state=EvidenceState.FRESH_COMPLETE,
                observed_at=now,
                window=window,
                currency=None,
                entity_ids=(
                    f"numerator-up:{counts['up']}",
                    f"denominator-total:{total}",
                    *(f"feedback:{item}" for item in ids_by_verdict["up"]),
                    *(f"feedback:{item}" for item in ids_by_verdict["down"]),
                    *metadata,
                ),
            )
        )
        return records, latest

    return _load_each_window(request, now, SourceSystem.AUTOPILOT_FEEDBACK, load)
