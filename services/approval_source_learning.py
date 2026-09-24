"""Read-only evidence для learnings и hypotheses journal."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta

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
from services.approval_source_decisions import (
    LocalSqliteEvidenceError,
    _parse_sqlite_datetime,
    _read_only_rows,
)

_LEARNING_COLUMNS = (
    "id",
    "statement",
    "evidence_ad_ids",
    "confidence",
    "source",
    "tags",
    "created_at",
    "updated_at",
)
_HYPOTHESIS_COLUMNS = (
    "id",
    "ad_ids",
    "expectation_json",
    "status",
    "verdict_at",
    "facts_json",
    "created_at",
)
_CONFIDENCE = {"hypothesis", "probable", "confirmed"}
_LEARNING_SOURCES = {
    "experiment",
    "hypothesis",
    "llm",
    "manual",
    "pattern_engine",
    "pattern_miner",
}
_HYPOTHESIS_STATUSES = {"open", "confirmed", "refuted", "inconclusive"}


def _json_list(raw: object, code: str) -> tuple[str, ...]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LocalSqliteEvidenceError(code) from exc
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise LocalSqliteEvidenceError(code)
    if len(value) != len(set(value)):
        raise LocalSqliteEvidenceError(code)
    return tuple(value)


def _json_object(raw: object, code: str, *, nullable: bool = False) -> dict[str, object] | None:
    if nullable and raw is None:
        return None
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LocalSqliteEvidenceError(code) from exc
    if not isinstance(value, dict):
        raise LocalSqliteEvidenceError(code)
    return value


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


def load_pattern_evidence(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    """Читает pattern learnings, проверяя JSON evidence и свежесть predictor rows."""

    if not request.windows:
        return _error(SourceSystem.PATTERN_LEARNINGS, now, LocalSqliteEvidenceError("LEARNING_WINDOW_MISSING"))
    records: list[EvidenceRecord] = []
    latest: datetime | None = None
    try:
        for window in request.windows:
            snapshot = _read_only_rows(
                config.REPORT_CHECKER_DECISIONS_DB_PATH,
                "learnings",
                _LEARNING_COLUMNS,
                window,
            )
            metadata = (f"schema:{snapshot.schema_sha256}", f"data-version:{snapshot.data_version}")
            confidence_counts: Counter[str] = Counter()
            learning_ids: list[str] = []
            for row in snapshot.rows:
                confidence = str(row["confidence"] or "")
                source_name = str(row["source"] or "")
                statement = str(row["statement"] or "").strip()
                if confidence not in _CONFIDENCE or source_name not in _LEARNING_SOURCES or not statement:
                    raise LocalSqliteEvidenceError("LEARNING_ROW_INVALID")
                evidence_ids = _json_list(row["evidence_ad_ids"], "LEARNING_EVIDENCE_JSON_INVALID")
                tags_raw = str(row["tags"] or "")
                tags = tuple(item.strip() for item in tags_raw.split(",") if item.strip())
                if len(tags) != len(set(tags)):
                    raise LocalSqliteEvidenceError("LEARNING_TAGS_INVALID")
                created_at = _parse_sqlite_datetime(row["created_at"])
                updated_at = _parse_sqlite_datetime(row["updated_at"])
                if not window.start <= created_at < window.end:
                    raise LocalSqliteEvidenceError("LEARNING_WINDOW_MISMATCH")
                if updated_at > now or now - updated_at > timedelta(days=7):
                    raise LocalSqliteEvidenceError("LEARNING_STALE")
                latest = updated_at if latest is None else max(latest, updated_at)
                learning_id = str(row["id"])
                confidence_counts[confidence] += 1
                learning_ids.append(learning_id)
                content_hash = hashlib.sha256(
                    canonical_json(
                        {
                            "statement": statement,
                            "evidence_ad_ids": evidence_ids,
                            "confidence": confidence,
                            "source": source_name,
                            "tags": tags,
                        }
                    )
                ).hexdigest()
                records.append(
                    EvidenceRecord(
                        category=FactCategory.MATCH,
                        subject=SubjectRef(SubjectKind.PATTERN, learning_id),
                        metric=Metric.RECORD_STATUS,
                        value=confidence,
                        source=SourceSystem.PATTERN_LEARNINGS,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=window,
                        currency=None,
                        entity_ids=(
                            f"learning:{learning_id}",
                            f"content-sha256:{content_hash}",
                            *(f"ad:{ad_id}" for ad_id in evidence_ids),
                            *metadata,
                        ),
                    )
                )
            for confidence in sorted(_CONFIDENCE):
                exact_ids = tuple(
                    sorted(
                        str(row["id"])
                        for row in snapshot.rows
                        if str(row["confidence"] or "") == confidence
                    )
                )
                records.append(
                    EvidenceRecord(
                        category=FactCategory.BUSINESS_METRIC,
                        subject=SubjectRef(
                            SubjectKind.PATTERN,
                            f"confidence:{confidence}",
                        ),
                        metric=Metric.PATTERN_COUNT,
                        value=confidence_counts[confidence],
                        source=SourceSystem.PATTERN_LEARNINGS,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=window,
                        currency=None,
                        entity_ids=(
                            *(f"learning:{item}" for item in exact_ids),
                            *metadata,
                        ),
                    )
                )
            records.append(
                EvidenceRecord(
                    category=FactCategory.BUSINESS_METRIC,
                    subject=SubjectRef(SubjectKind.PATTERN, "total"),
                    metric=Metric.PATTERN_COUNT,
                    value=len(snapshot.rows),
                    source=SourceSystem.PATTERN_LEARNINGS,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency=None,
                    entity_ids=(
                        *(f"learning:{item}" for item in sorted(learning_ids)),
                        *metadata,
                    ),
                )
            )
    except Exception as exc:
        return _error(SourceSystem.PATTERN_LEARNINGS, now, exc)
    return SourceEvidence(
        source=SourceSystem.PATTERN_LEARNINGS,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=latest or now,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )


def _hypothesis_in_window(created_at: datetime, verdict_at: datetime | None, window: TimeWindow) -> bool:
    relevant = verdict_at or created_at
    return window.start <= relevant < window.end


def load_hypothesis_evidence(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    """Читает journal; weekly verdict rows фильтруются по exact verdict_at window."""

    if not request.windows:
        return _error(SourceSystem.HYPOTHESIS_JOURNAL, now, LocalSqliteEvidenceError("HYPOTHESIS_WINDOW_MISSING"))
    records: list[EvidenceRecord] = []
    latest: datetime | None = None
    try:
        snapshot = _read_only_rows(
            config.REPORT_CHECKER_DECISIONS_DB_PATH,
            "hypotheses",
            _HYPOTHESIS_COLUMNS,
            None,
        )
        metadata = (f"schema:{snapshot.schema_sha256}", f"data-version:{snapshot.data_version}")
        seen_by_window: dict[TimeWindow, set[int]] = {window: set() for window in request.windows}
        status_by_window: dict[TimeWindow, Counter[str]] = {
            window: Counter() for window in request.windows
        }
        status_by_id: dict[int, str] = {}
        for row in snapshot.rows:
            hypothesis_id = int(row["id"])
            status = str(row["status"] or "")
            if status not in _HYPOTHESIS_STATUSES:
                raise LocalSqliteEvidenceError("HYPOTHESIS_STATUS_INVALID")
            status_by_id[hypothesis_id] = status
            ad_ids = _json_list(row["ad_ids"], "HYPOTHESIS_AD_IDS_INVALID")
            expectation = _json_object(row["expectation_json"], "HYPOTHESIS_EXPECTATION_INVALID")
            facts = _json_object(row["facts_json"], "HYPOTHESIS_FACTS_INVALID", nullable=True)
            created_at = _parse_sqlite_datetime(row["created_at"])
            verdict_at = (
                _parse_sqlite_datetime(row["verdict_at"])
                if row["verdict_at"] not in (None, "")
                else None
            )
            if status != "open" and verdict_at is None:
                raise LocalSqliteEvidenceError("HYPOTHESIS_VERDICT_AT_MISSING")
            latest_value = verdict_at or created_at
            latest = latest_value if latest is None else max(latest, latest_value)
            content_hash = hashlib.sha256(
                canonical_json(
                    {
                        "ad_ids": ad_ids,
                        "expectation": expectation,
                        "status": status,
                        "verdict_at": verdict_at,
                        "facts": facts,
                    }
                )
            ).hexdigest()
            for window in request.windows:
                if not _hypothesis_in_window(created_at, verdict_at, window):
                    continue
                if hypothesis_id in seen_by_window[window]:
                    raise LocalSqliteEvidenceError("HYPOTHESIS_DUPLICATE_ID")
                seen_by_window[window].add(hypothesis_id)
                status_by_window[window][status] += 1
                records.append(
                    EvidenceRecord(
                        category=FactCategory.MATCH,
                        subject=SubjectRef(SubjectKind.HYPOTHESIS, str(hypothesis_id)),
                        metric=Metric.RECORD_STATUS,
                        value=status,
                        source=SourceSystem.HYPOTHESIS_JOURNAL,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=window,
                        currency=None,
                        entity_ids=(
                            f"hypothesis:{hypothesis_id}",
                            f"content-sha256:{content_hash}",
                            *(f"ad:{ad_id}" for ad_id in ad_ids),
                            *metadata,
                        ),
                    )
                )
        for window, ids in seen_by_window.items():
            for status in sorted(_HYPOTHESIS_STATUSES):
                status_ids = tuple(
                    sorted(
                        item
                        for item in ids
                        if status_by_id[item] == status
                    )
                )
                records.append(
                    EvidenceRecord(
                        category=FactCategory.BUSINESS_METRIC,
                        subject=SubjectRef(
                            SubjectKind.HYPOTHESIS,
                            f"status:{status}",
                        ),
                        metric=Metric.HYPOTHESIS_COUNT,
                        value=status_by_window[window][status],
                        source=SourceSystem.HYPOTHESIS_JOURNAL,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=window,
                        currency=None,
                        entity_ids=(
                            *(f"hypothesis:{item}" for item in status_ids),
                            *metadata,
                        ),
                    )
                )
            records.append(
                EvidenceRecord(
                    category=FactCategory.BUSINESS_METRIC,
                    subject=SubjectRef(SubjectKind.HYPOTHESIS, "total"),
                    metric=Metric.HYPOTHESIS_COUNT,
                    value=len(ids),
                    source=SourceSystem.HYPOTHESIS_JOURNAL,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=window,
                    currency=None,
                    entity_ids=(
                        *(f"hypothesis:{item}" for item in sorted(ids)),
                        *metadata,
                    ),
                )
            )
    except Exception as exc:
        return _error(SourceSystem.HYPOTHESIS_JOURNAL, now, exc)
    return SourceEvidence(
        source=SourceSystem.HYPOTHESIS_JOURNAL,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=latest or now,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )
