"""Read-only runtime rings и стабильные operational JSON snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import config
from agent.fb_common import read_fb_error_snapshot
from services.approval_checker_models import (
    EvidenceRecord,
    EvidenceRequest,
    EvidenceState,
    FactCategory,
    Metric,
    RuntimeBufferSnapshot,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    canonical_json,
)
from services.notifications import read_notification_runtime_snapshot

_MONITOR_SOURCES = {
    SourceSystem.GUARDIAN_STATE,
    SourceSystem.BRIEF_GENERATOR_STATE,
    SourceSystem.ANOMALY_ALERT_STATE,
    SourceSystem.EXPIRED_OFFER_STATE,
    SourceSystem.COVERAGE_STATE,
    SourceSystem.ADS_WATCHDOG_STATE,
    SourceSystem.ADSET_SPEND_GUARD_STATE,
    SourceSystem.CDP_SPEND_ALERT_STATE,
}


class RuntimeEvidenceError(RuntimeError):
    """Runtime/operational snapshot нельзя считать точным."""


def _error(source: SourceSystem, now: datetime, code: str, state: EvidenceState) -> SourceEvidence:
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


def _runtime_subject(request: EvidenceRequest, snapshot: RuntimeBufferSnapshot) -> SubjectRef:
    candidates = tuple(subject for subject in request.subjects if subject.kind is SubjectKind.RUNTIME)
    if not candidates:
        return SubjectRef(SubjectKind.RUNTIME, snapshot.source_instance_id)
    matching = tuple(
        subject for subject in candidates if snapshot.source_instance_id in subject.subject_id
    )
    if len(matching) != 1:
        raise RuntimeEvidenceError("RUNTIME_INSTANCE_MISMATCH")
    return matching[0]


def _from_buffer(
    request: EvidenceRequest,
    now: datetime,
    source: SourceSystem,
    snapshot: RuntimeBufferSnapshot,
    metric: Metric,
) -> SourceEvidence:
    try:
        age = (now - snapshot.observed_at).total_seconds()
        if age < -1 or age > config.REPORT_CHECKER_RUNTIME_MAX_AGE_SECONDS:
            raise RuntimeEvidenceError("RUNTIME_SNAPSHOT_STALE")
        if not snapshot.complete or snapshot.truncated_in_window:
            raise RuntimeEvidenceError("RUNTIME_BUFFER_TRUNCATED")
        if snapshot.count_in_window != len(snapshot.event_timestamps):
            raise RuntimeEvidenceError("RUNTIME_COUNT_MISMATCH")
        if any(
            timestamp < snapshot.window.start or timestamp >= snapshot.window.end
            for timestamp in snapshot.event_timestamps
        ):
            raise RuntimeEvidenceError("RUNTIME_WINDOW_MISMATCH")
        subject = _runtime_subject(request, snapshot)
    except RuntimeEvidenceError as exc:
        return _error(source, now, str(exc), EvidenceState.INCOMPLETE)
    record = EvidenceRecord(
        category=FactCategory.CHECKER_HEALTH,
        subject=subject,
        metric=metric,
        value=snapshot.count_in_window,
        source=source,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=snapshot.observed_at,
        window=snapshot.window,
        currency=None,
        entity_ids=(
            f"instance:{snapshot.source_instance_id}",
            f"buffer-size:{snapshot.buffer_size}",
            f"capacity:{snapshot.capacity}",
        ),
    )
    return SourceEvidence(
        source=source,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=snapshot.observed_at,
        data_as_of=snapshot.observed_at,
        from_cache=False,
        complete=True,
        records=(record,),
    )


def load_fb_error_ring_evidence(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    """Читает FB ring только в том же процессе и только когда окно не усечено."""

    return _from_buffer(
        request,
        now,
        SourceSystem.FB_ERROR_RING,
        read_fb_error_snapshot(now.timestamp()),
        Metric.ERROR_COUNT,
    )


def load_runtime_evidence(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    """Читает notification runtime без title/detail/meta."""

    return _from_buffer(
        request,
        now,
        SourceSystem.NOTIFICATION_RUNTIME,
        read_notification_runtime_snapshot(now),
        Metric.RUNTIME_COUNT,
    )


def _source_path(source: SourceSystem) -> Path:
    if source is SourceSystem.SETTINGS_FILE:
        return config.REPORT_CHECKER_SETTINGS_PATH
    if source is SourceSystem.CRON_HEARTBEATS:
        return config.REPORT_CHECKER_CRON_HEARTBEATS_PATH
    if source is SourceSystem.CRON_FAILURE_STATE:
        return config.REPORT_CHECKER_CRON_FAILURE_PATH
    if source in _MONITOR_SOURCES:
        raw = config.REPORT_CHECKER_MONITOR_STATE_PATHS.get(source.value)
        if raw is not None:
            return Path(raw)
    raise RuntimeEvidenceError("OPERATIONAL_SOURCE_NOT_ALLOWED")


def _stable_json(path: Path) -> tuple[dict[str, object], str, int, datetime]:
    original = path.expanduser()
    try:
        original_lstat = original.lstat()
    except FileNotFoundError as exc:
        raise RuntimeEvidenceError("OPERATIONAL_STATE_MISSING") from exc
    if stat.S_ISLNK(original_lstat.st_mode) or not stat.S_ISREG(original_lstat.st_mode):
        raise RuntimeEvidenceError("OPERATIONAL_STATE_UNSAFE_PATH")
    if original_lstat.st_size > config.REPORT_CHECKER_MAX_JSON_BYTES:
        raise RuntimeEvidenceError("OPERATIONAL_STATE_TOO_LARGE")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(original, flags)
    except OSError as exc:
        raise RuntimeEvidenceError("OPERATIONAL_STATE_OPEN_FAILED") from exc
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > config.REPORT_CHECKER_MAX_JSON_BYTES:
                raise RuntimeEvidenceError("OPERATIONAL_STATE_TOO_LARGE")
            chunks.append(chunk)
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
    ) or total != before.st_size:
        raise RuntimeEvidenceError("OPERATIONAL_STATE_CHANGED_DURING_READ")
    raw = b"".join(chunks)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeEvidenceError("OPERATIONAL_STATE_JSON_INVALID") from exc
    if not isinstance(payload, dict):
        raise RuntimeEvidenceError("OPERATIONAL_STATE_SCHEMA_INVALID")
    observed_at = datetime.fromtimestamp(before.st_mtime, tz=timezone.utc)
    return payload, hashlib.sha256(raw).hexdigest(), before.st_ino, observed_at


def _validate_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeEvidenceError("OPERATIONAL_STATE_NUMBER_INVALID")
    if isinstance(value, dict):
        for nested in value.values():
            _validate_finite(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_finite(nested)


def _parse_aware(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise RuntimeEvidenceError("OPERATIONAL_STATE_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeEvidenceError("OPERATIONAL_STATE_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeEvidenceError("OPERATIONAL_STATE_TIMESTAMP_NAIVE")
    return parsed


def _validate_schema(source: SourceSystem, payload: dict[str, object]) -> str | None:
    _validate_finite(payload)
    if source is SourceSystem.CRON_HEARTBEATS:
        heartbeats = payload.get("heartbeats")
        if not isinstance(heartbeats, dict):
            raise RuntimeEvidenceError("CRON_HEARTBEATS_SCHEMA_INVALID")
        high: datetime | None = None
        for name, entry in heartbeats.items():
            if not isinstance(name, str) or not name or not isinstance(entry, dict):
                raise RuntimeEvidenceError("CRON_HEARTBEATS_SCHEMA_INVALID")
            at = _parse_aware(entry.get("at"))
            expected = entry.get("expected_minutes")
            if isinstance(expected, bool) or not isinstance(expected, (int, float)) or expected < 0:
                raise RuntimeEvidenceError("CRON_HEARTBEATS_SCHEMA_INVALID")
            if not isinstance(entry.get("critical"), bool):
                raise RuntimeEvidenceError("CRON_HEARTBEATS_SCHEMA_INVALID")
            high = at if high is None else max(high, at)
        return high.isoformat() if high is not None else None
    if source is SourceSystem.CRON_FAILURE_STATE:
        crons = payload.get("crons")
        if not isinstance(crons, dict):
            raise RuntimeEvidenceError("CRON_FAILURE_SCHEMA_INVALID")
        for name, entry in crons.items():
            if not isinstance(name, str) or not name or not isinstance(entry, dict):
                raise RuntimeEvidenceError("CRON_FAILURE_SCHEMA_INVALID")
            failures = entry.get("consecutive_failures", entry.get("count", 0))
            if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
                raise RuntimeEvidenceError("CRON_FAILURE_SCHEMA_INVALID")
            for key in ("last_failure_at", "last_alert_at"):
                if entry.get(key) is not None:
                    _parse_aware(entry[key])
        return None
    if source is SourceSystem.SETTINGS_FILE:
        if not payload:
            raise RuntimeEvidenceError("SETTINGS_SCHEMA_INVALID")
        return None
    # Monitor files подтверждают только dedup/last-run context. Корень обязан быть object.
    for key, value in payload.items():
        if key.endswith("_at") and value is not None:
            _parse_aware(value)
    return None


def load_operational_state_evidence(
    request: EvidenceRequest,
    now: datetime,
) -> tuple[SourceEvidence, ...]:
    """Читает только закрытый enum→path allowlist; caller не передаёт путь."""

    requested = tuple(
        source
        for source in request.required_sources
        if source in _MONITOR_SOURCES
        or source in {
            SourceSystem.SETTINGS_FILE,
            SourceSystem.CRON_HEARTBEATS,
            SourceSystem.CRON_FAILURE_STATE,
        }
    )
    results: list[SourceEvidence] = []
    for source in requested:
        try:
            payload, content_sha256, inode, observed_at = _stable_json(_source_path(source))
            high_watermark = _validate_schema(source, payload)
            schema_sha256 = hashlib.sha256(
                canonical_json({"source": source.value, "keys": sorted(payload)})
            ).hexdigest()
            subject_kind = SubjectKind.CRON if source in {
                SourceSystem.CRON_HEARTBEATS,
                SourceSystem.CRON_FAILURE_STATE,
            } else (SubjectKind.CHECKER if source is SourceSystem.SETTINGS_FILE else SubjectKind.MONITOR)
            record = EvidenceRecord(
                category=FactCategory.HISTORY_AUDIT,
                subject=SubjectRef(subject_kind, source.value),
                metric=Metric.RECORD_STATUS,
                value=content_sha256,
                source=source,
                state=EvidenceState.FRESH_COMPLETE,
                observed_at=observed_at,
                window=None,
                currency=None,
                entity_ids=(
                    f"schema:{schema_sha256}",
                    f"inode:{inode}",
                    *( (f"high-watermark:{high_watermark}",) if high_watermark else () ),
                ),
            )
            results.append(
                SourceEvidence(
                    source=source,
                    state=EvidenceState.FRESH_COMPLETE,
                    fetched_at=now,
                    data_as_of=observed_at,
                    from_cache=False,
                    complete=True,
                    records=(record,),
                )
            )
        except RuntimeEvidenceError as exc:
            missing = str(exc) == "OPERATIONAL_STATE_MISSING"
            results.append(
                _error(
                    source,
                    now,
                    str(exc),
                    EvidenceState.MISSING if missing else EvidenceState.INCOMPLETE,
                )
            )
        except Exception as exc:
            results.append(_error(source, now, type(exc).__name__, EvidenceState.ERROR))
    return tuple(results)
