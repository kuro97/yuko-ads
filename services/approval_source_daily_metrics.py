"""Read-only evidence из complete daily metrics manifests и SQLite rows."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import string
from datetime import date, datetime, timedelta
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

_REQUIRED_COLUMNS = (
    "ad_id",
    "date",
    "spend",
    "impressions",
    "clicks",
    "ctr",
    "leads",
    "cpl",
    "hook_rate",
    "hold_rate",
    "video_views_3s",
    "day_since_launch",
)


class DailyMetricsEvidenceError(RuntimeError):
    """Persisted daily snapshot не доказывает полный числовой ряд."""


def _stable_manifest(path: Path) -> dict[str, object]:
    original = Path(path).expanduser()
    try:
        path_stat = original.lstat()
    except FileNotFoundError as exc:
        raise DailyMetricsEvidenceError("DAILY_MANIFEST_MISSING") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise DailyMetricsEvidenceError("DAILY_MANIFEST_UNSAFE_PATH")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(original, flags)
    try:
        before = os.fstat(descriptor)
        raw = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > config.REPORT_CHECKER_MAX_JSON_BYTES:
                raise DailyMetricsEvidenceError("DAILY_MANIFEST_TOO_LARGE")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or len(raw) != before.st_size:
        raise DailyMetricsEvidenceError("DAILY_MANIFEST_CHANGED_DURING_READ")
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DailyMetricsEvidenceError("DAILY_MANIFEST_JSON_INVALID") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise DailyMetricsEvidenceError("DAILY_MANIFEST_SCHEMA_INVALID")
    return payload


def _parse_aware(raw: object, code: str) -> datetime:
    if not isinstance(raw, str):
        raise DailyMetricsEvidenceError(code)
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DailyMetricsEvidenceError(code) from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise DailyMetricsEvidenceError(code)
    return value


def _required_dates(request: EvidenceRequest) -> tuple[date, ...]:
    result: set[date] = set()
    for window in request.windows:
        cursor = window.start.date()
        end_date = (window.end - timedelta(microseconds=1)).date()
        while cursor <= end_date:
            result.add(cursor)
            cursor += timedelta(days=1)
    return tuple(sorted(result))


def _complete_runs(payload: dict[str, object], dates: tuple[date, ...], now: datetime) -> dict[str, dict[str, object]]:
    runs = payload.get("runs")
    if not isinstance(runs, list) or len(runs) > 32:
        raise DailyMetricsEvidenceError("DAILY_MANIFEST_RUNS_INVALID")
    selected: dict[str, dict[str, object]] = {}
    for raw_run in runs:
        if not isinstance(raw_run, dict) or not isinstance(raw_run.get("result"), dict):
            raise DailyMetricsEvidenceError("DAILY_MANIFEST_RUN_INVALID")
        result = raw_run["result"]
        target = result.get("target_date")
        if target not in {item.isoformat() for item in dates} or result.get("complete") is not True:
            continue
        if target in selected:
            previous_at = _parse_aware(selected[target].get("persisted_at"), "DAILY_PERSISTED_AT_INVALID")
            current_at = _parse_aware(raw_run.get("persisted_at"), "DAILY_PERSISTED_AT_INVALID")
            if current_at <= previous_at:
                continue
        selected[str(target)] = raw_run
    if set(selected) != {item.isoformat() for item in dates}:
        raise DailyMetricsEvidenceError("DAILY_COMPLETE_MANIFEST_GAP")
    yesterday = now.date() - timedelta(days=1)
    if yesterday in dates:
        persisted = _parse_aware(
            selected[yesterday.isoformat()].get("persisted_at"),
            "DAILY_PERSISTED_AT_INVALID",
        )
        if now - persisted > timedelta(hours=36) or persisted > now:
            raise DailyMetricsEvidenceError("DAILY_YESTERDAY_MANIFEST_STALE")
    return selected


def _validate_run(run: dict[str, object]) -> tuple[str, tuple[str, ...], str]:
    result = run["result"]
    if not isinstance(result, dict):
        raise DailyMetricsEvidenceError("DAILY_MANIFEST_RUN_INVALID")
    inventory = result.get("inventory")
    if not isinstance(inventory, dict) or inventory.get("complete") is not True or inventory.get("fresh") is not True:
        raise DailyMetricsEvidenceError("DAILY_INVENTORY_INCOMPLETE")
    eligible_campaign_ids = inventory.get("eligible_campaign_ids")
    campaign_by_ad_sha256 = inventory.get("campaign_by_ad_sha256")
    if (
        not isinstance(eligible_campaign_ids, list)
        or not all(isinstance(item, str) and item for item in eligible_campaign_ids)
        or eligible_campaign_ids != sorted(set(eligible_campaign_ids))
        or not isinstance(campaign_by_ad_sha256, str)
        or len(campaign_by_ad_sha256) != 64
        or any(character not in string.hexdigits.lower() for character in campaign_by_ad_sha256)
    ):
        raise DailyMetricsEvidenceError("DAILY_CAMPAIGN_MAPPING_INVALID")
    requested = result.get("requested_ad_ids")
    fetched = result.get("fetched_ad_ids")
    upserted = result.get("upserted_ad_ids")
    if not all(isinstance(value, list) and all(isinstance(item, str) for item in value) for value in (requested, fetched, upserted)):
        raise DailyMetricsEvidenceError("DAILY_ID_SET_INVALID")
    if len(requested) != len(set(requested)) or set(requested) != set(fetched) or set(requested) != set(upserted):
        raise DailyMetricsEvidenceError("DAILY_ID_SET_MISMATCH")
    fetch_path = result.get("fetch_path")
    if fetch_path == "CAMPAIGN_CHUNKS":
        pagination = result.get("campaign_pagination")
        chunks = result.get("campaign_chunks")
        if (
            not isinstance(pagination, dict)
            or pagination.get("endpoint_kind") != "CAMPAIGNS"
            or not isinstance(pagination.get("page_count"), int)
            or pagination["page_count"] < 1
            or pagination.get("pagination_complete") is not True
            or pagination.get("failed_page_index") is not None
            or pagination.get("item_ids") != eligible_campaign_ids
            or not isinstance(chunks, list)
        ):
            raise DailyMetricsEvidenceError("DAILY_CAMPAIGN_COVERAGE_INVALID")
        flattened: list[str] = []
        for expected_index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                raise DailyMetricsEvidenceError("DAILY_CAMPAIGN_CHUNK_INVALID")
            chunk_ids = chunk.get("campaign_ids")
            chunk_pagination = chunk.get("pagination")
            if (
                chunk.get("chunk_index") != expected_index
                or chunk.get("complete") is not True
                or not isinstance(chunk_ids, list)
                or not chunk_ids
                or not all(isinstance(item, str) and item for item in chunk_ids)
                or chunk_ids != sorted(set(chunk_ids))
                or len(chunk_ids) > 50
                or not isinstance(chunk_pagination, dict)
                or chunk_pagination.get("endpoint_kind") != "CHUNK_INSIGHTS"
                or not isinstance(chunk_pagination.get("page_count"), int)
                or chunk_pagination["page_count"] < 1
                or chunk_pagination.get("pagination_complete") is not True
                or chunk_pagination.get("failed_page_index") is not None
            ):
                raise DailyMetricsEvidenceError("DAILY_CAMPAIGN_CHUNK_INVALID")
            flattened.extend(chunk_ids)
        if flattened != eligible_campaign_ids or len(flattened) != len(set(flattened)):
            raise DailyMetricsEvidenceError("DAILY_CAMPAIGN_COVERAGE_MISMATCH")
    elif fetch_path != "ACCOUNT":
        raise DailyMetricsEvidenceError("DAILY_FETCH_PATH_INVALID")
    inventory_fetched = _parse_aware(inventory.get("fetched_at"), "DAILY_INVENTORY_AT_INVALID")
    persisted = _parse_aware(run.get("persisted_at"), "DAILY_PERSISTED_AT_INVALID")
    if persisted < inventory_fetched or persisted - inventory_fetched > timedelta(minutes=15):
        raise DailyMetricsEvidenceError("DAILY_INVENTORY_STALE_AT_PERSIST")
    account_id = inventory.get("account_id")
    db_hash = run.get("db_rows_state_sha256")
    if not isinstance(account_id, str) or not account_id or not isinstance(db_hash, str) or len(db_hash) != 64:
        raise DailyMetricsEvidenceError("DAILY_MANIFEST_IDENTITY_INVALID")
    return account_id, tuple(sorted(requested)), db_hash


def _read_db_rows(path: Path, dates: tuple[date, ...]) -> tuple[tuple[sqlite3.Row, ...], str]:
    original = Path(path).expanduser()
    try:
        file_stat = original.lstat()
    except FileNotFoundError as exc:
        raise DailyMetricsEvidenceError("DAILY_DB_MISSING") from exc
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise DailyMetricsEvidenceError("DAILY_DB_UNSAFE_PATH")
    uri = f"{original.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        schema = connection.execute('PRAGMA table_info("ad_daily_metrics")').fetchall()
        columns = {str(row[1]) for row in schema}
        if not set(_REQUIRED_COLUMNS).issubset(columns):
            raise DailyMetricsEvidenceError("DAILY_DB_COLUMNS_MISSING")
        placeholders = ",".join("?" for _ in dates)
        selected = ",".join(f'"{column}"' for column in _REQUIRED_COLUMNS)
        rows = tuple(
            connection.execute(
                f'SELECT {selected} FROM "ad_daily_metrics" WHERE date IN ({placeholders}) ORDER BY date,ad_id',
                tuple(item.isoformat() for item in dates),
            ).fetchall()
        )
        connection.rollback()
    except sqlite3.Error as exc:
        raise DailyMetricsEvidenceError("DAILY_DB_READ_FAILED") from exc
    finally:
        connection.close()
    identities = [(str(row["ad_id"]), str(row["date"])) for row in rows]
    if len(identities) != len(set(identities)):
        raise DailyMetricsEvidenceError("DAILY_DB_DUPLICATE_ROW")
    for row in rows:
        for key in ("spend", "impressions", "clicks", "ctr", "leads", "cpl", "video_views_3s", "day_since_launch"):
            value = row[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                raise DailyMetricsEvidenceError("DAILY_DB_NUMBER_INVALID")
    db_hash = hashlib.sha256(canonical_json([tuple(row) for row in rows])).hexdigest()
    return rows, db_hash


def _window_for_date(request: EvidenceRequest, target: date) -> TimeWindow:
    matches = [
        window
        for window in request.windows
        if window.start.date() <= target <= (window.end - timedelta(microseconds=1)).date()
    ]
    if not matches:
        raise DailyMetricsEvidenceError("DAILY_WINDOW_MISSING")
    return matches[0]


def _error(now: datetime, exc: Exception) -> SourceEvidence:
    code = str(exc) if isinstance(exc, DailyMetricsEvidenceError) else type(exc).__name__
    state = EvidenceState.INCOMPLETE if isinstance(exc, DailyMetricsEvidenceError) else EvidenceState.ERROR
    return SourceEvidence(
        source=SourceSystem.AD_DAILY_METRICS,
        state=state,
        fetched_at=now,
        data_as_of=None,
        from_cache=False,
        complete=False,
        records=(),
        error_code=code[:120],
    )


def load_daily_metrics_evidence(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    """Читает уже persisted complete manifest; writer/network никогда не вызывает."""

    try:
        dates = _required_dates(request)
        if not dates:
            raise DailyMetricsEvidenceError("DAILY_DATES_MISSING")
        manifest = _stable_manifest(config.REPORT_CHECKER_METRICS_MANIFEST_PATH)
        runs = _complete_runs(manifest, dates, now)
        expected_by_date: dict[str, tuple[str, tuple[str, ...], str]] = {
            day: _validate_run(run) for day, run in runs.items()
        }
        rows, db_hash = _read_db_rows(config.REPORT_CHECKER_DECISIONS_DB_PATH, dates)
        rows_by_date: dict[str, list[sqlite3.Row]] = {item.isoformat(): [] for item in dates}
        for row in rows:
            rows_by_date[str(row["date"])].append(row)
        records: list[EvidenceRecord] = []
        data_as_of: datetime | None = None
        for day, (account_id, expected_ids, expected_db_hash) in expected_by_date.items():
            actual = rows_by_date[day]
            actual_ids = tuple(sorted(str(row["ad_id"]) for row in actual))
            if actual_ids != expected_ids:
                raise DailyMetricsEvidenceError("DAILY_DB_MANIFEST_ID_MISMATCH")
            # Writer hashes all rows for one target date; adapter does the same for one-day requests.
            if len(dates) == 1 and db_hash != expected_db_hash:
                raise DailyMetricsEvidenceError("DAILY_DB_HASH_MISMATCH")
            window = _window_for_date(request, date.fromisoformat(day))
            for row in actual:
                ad_id = str(row["ad_id"])
                subject = SubjectRef(
                    SubjectKind.DAILY_METRIC,
                    f"{account_id}:{ad_id}:{day}",
                    parent_id=ad_id,
                )
                common = {
                    "category": FactCategory.BUSINESS_METRIC,
                    "subject": subject,
                    "source": SourceSystem.AD_DAILY_METRICS,
                    "state": EvidenceState.FRESH_COMPLETE,
                    "observed_at": now,
                    "window": window,
                    "entity_ids": (f"ad:{ad_id}", f"date:{day}", f"account:{account_id}"),
                }
                records.extend(
                    (
                        EvidenceRecord(metric=Metric.SPEND, value=Decimal(str(row["spend"])), currency="USD", **common),
                        EvidenceRecord(metric=Metric.LEADS, value=int(row["leads"]), currency=None, **common),
                        EvidenceRecord(metric=Metric.CPL, value=Decimal(str(row["cpl"])), currency="USD", **common),
                    )
                )
            persisted_at = _parse_aware(runs[day]["persisted_at"], "DAILY_PERSISTED_AT_INVALID")
            data_as_of = persisted_at if data_as_of is None else max(data_as_of, persisted_at)
    except Exception as exc:
        return _error(now, exc)
    return SourceEvidence(
        source=SourceSystem.AD_DAILY_METRICS,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=data_as_of or now,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )
