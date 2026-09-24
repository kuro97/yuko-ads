"""Stable read-only evidence для exact pending brief preview."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timedelta

import config
from services.approval_checker_models import (
    EvidenceRecord,
    EvidenceRequest,
    EvidenceState,
    FactCategory,
    Metric,
    PendingBriefRecord,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    canonical_json,
)

_STATUSES = {"pending", "approved", "rejected", "expired"}
_REQUIRED_KEYS = {
    "id",
    "name",
    "desc",
    "product",
    "signature",
    "status",
    "created_at",
    "decided_at",
    "card_id",
    "card_url",
}


class PendingBriefEvidenceError(RuntimeError):
    """Файл очереди или exact brief нельзя безопасно подтвердить."""


def _aware_datetime(value: object, *, nullable: bool = False) -> datetime | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PendingBriefEvidenceError("PENDING_BRIEF_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PendingBriefEvidenceError("PENDING_BRIEF_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PendingBriefEvidenceError("PENDING_BRIEF_TIMESTAMP_NAIVE")
    return parsed


def _stable_read() -> tuple[dict[str, object], str, datetime]:
    path = config.REPORT_CHECKER_PENDING_BRIEFS_PATH.expanduser()
    try:
        path_lstat = path.lstat()
    except FileNotFoundError as exc:
        raise PendingBriefEvidenceError("PENDING_BRIEFS_MISSING") from exc
    if stat.S_ISLNK(path_lstat.st_mode) or not stat.S_ISREG(path_lstat.st_mode):
        raise PendingBriefEvidenceError("PENDING_BRIEFS_UNSAFE_PATH")
    if path_lstat.st_size > config.REPORT_CHECKER_MAX_JSON_BYTES:
        raise PendingBriefEvidenceError("PENDING_BRIEFS_TOO_LARGE")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PendingBriefEvidenceError("PENDING_BRIEFS_OPEN_FAILED") from exc
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
                raise PendingBriefEvidenceError("PENDING_BRIEFS_TOO_LARGE")
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
        raise PendingBriefEvidenceError("PENDING_BRIEFS_CHANGED_DURING_READ")
    raw = b"".join(chunks)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PendingBriefEvidenceError("PENDING_BRIEFS_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise PendingBriefEvidenceError("PENDING_BRIEFS_SCHEMA_INVALID")
    return value, hashlib.sha256(raw).hexdigest(), datetime.now().astimezone()


def _parse_records(payload: dict[str, object]) -> tuple[PendingBriefRecord, ...]:
    briefs = payload.get("briefs")
    if not isinstance(briefs, list):
        raise PendingBriefEvidenceError("PENDING_BRIEFS_SCHEMA_INVALID")
    result: list[PendingBriefRecord] = []
    seen_ids: set[str] = set()
    seen_signatures: set[str] = set()
    for item in briefs:
        if not isinstance(item, dict) or set(item) != _REQUIRED_KEYS:
            raise PendingBriefEvidenceError("PENDING_BRIEF_RECORD_SCHEMA_INVALID")
        brief_id = item["id"]
        name = item["name"]
        description = item["desc"]
        signature = item["signature"]
        status_value = item["status"]
        product = item["product"]
        card_id = item["card_id"]
        card_url = item["card_url"]
        if any(not isinstance(value, str) or not value for value in (brief_id, name, description, signature)):
            raise PendingBriefEvidenceError("PENDING_BRIEF_RECORD_INVALID")
        if status_value not in _STATUSES:
            raise PendingBriefEvidenceError("PENDING_BRIEF_STATUS_INVALID")
        if product is not None and not isinstance(product, str):
            raise PendingBriefEvidenceError("PENDING_BRIEF_PRODUCT_INVALID")
        if card_id is not None and not isinstance(card_id, str):
            raise PendingBriefEvidenceError("PENDING_BRIEF_CARD_INVALID")
        if card_url is not None and not isinstance(card_url, str):
            raise PendingBriefEvidenceError("PENDING_BRIEF_CARD_INVALID")
        if brief_id in seen_ids or signature in seen_signatures:
            raise PendingBriefEvidenceError("PENDING_BRIEF_DUPLICATE")
        seen_ids.add(brief_id)
        seen_signatures.add(signature)
        created_at = _aware_datetime(item["created_at"])
        decided_at = _aware_datetime(item["decided_at"], nullable=True)
        if not isinstance(created_at, datetime):
            raise PendingBriefEvidenceError("PENDING_BRIEF_TIMESTAMP_INVALID")
        record_sha256 = hashlib.sha256(canonical_json(item)).hexdigest()
        result.append(
            PendingBriefRecord(
                brief_id=brief_id,
                name=name,
                desc=description,
                product=product,
                signature=signature,
                status=status_value,
                created_at=created_at,
                decided_at=decided_at,
                card_id=card_id,
                card_url=card_url,
                record_sha256=record_sha256,
            )
        )
    return tuple(result)


def _error(now: datetime, exc: Exception, *, missing: bool = False) -> SourceEvidence:
    code = str(exc) if isinstance(exc, PendingBriefEvidenceError) else type(exc).__name__
    state = EvidenceState.MISSING if missing else (
        EvidenceState.INCOMPLETE if isinstance(exc, PendingBriefEvidenceError) else EvidenceState.ERROR
    )
    return SourceEvidence(
        source=SourceSystem.PENDING_BRIEFS,
        state=state,
        fetched_at=now,
        data_as_of=None,
        from_cache=False,
        complete=False,
        records=(),
        error_code=code[:120],
    )


def load_pending_brief_evidence(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    """Перечитывает exact record; preview разрешён только для свежего ``pending``."""

    try:
        payload, file_sha256, fetched_at = _stable_read()
        parsed = _parse_records(payload)
        by_id = {record.brief_id: record for record in parsed}
        requested = {
            subject.subject_id
            for subject in request.subjects
            if subject.kind is SubjectKind.BRIEF
        }
        if not requested:
            requested = set(request.card_ids)
        records: list[EvidenceRecord] = []
        latest: datetime | None = None
        for brief_id in sorted(requested):
            record = by_id.get(brief_id)
            if record is None:
                raise PendingBriefEvidenceError("PENDING_BRIEF_NOT_FOUND")
            if record.status != "pending":
                raise PendingBriefEvidenceError("PENDING_BRIEF_NOT_PENDING")
            if record.created_at > now or now - record.created_at > timedelta(
                days=config.REPORT_CHECKER_PENDING_BRIEF_RETENTION_DAYS
            ):
                raise PendingBriefEvidenceError("PENDING_BRIEF_EXPIRED")
            latest = record.created_at if latest is None else max(latest, record.created_at)
            subject = SubjectRef(SubjectKind.BRIEF, brief_id)
            entity_ids = (
                f"record-sha256:{record.record_sha256}",
                f"file-sha256:{file_sha256}",
                f"signature-sha256:{hashlib.sha256(record.signature.encode()).hexdigest()}",
            )
            records.extend(
                (
                    EvidenceRecord(
                        category=FactCategory.ACTION_STATE,
                        subject=subject,
                        metric=Metric.RECORD_STATUS,
                        value=record.status,
                        source=SourceSystem.PENDING_BRIEFS,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=None,
                        currency=None,
                        entity_ids=entity_ids,
                    ),
                    EvidenceRecord(
                        category=FactCategory.MATCH,
                        subject=subject,
                        metric=Metric.MATCH_STATE,
                        value=record.record_sha256,
                        source=SourceSystem.PENDING_BRIEFS,
                        state=EvidenceState.FRESH_COMPLETE,
                        observed_at=now,
                        window=None,
                        currency=None,
                        entity_ids=entity_ids,
                    ),
                )
            )
    except Exception as exc:
        return _error(now, exc, missing=str(exc) in {"PENDING_BRIEFS_MISSING", "PENDING_BRIEF_NOT_FOUND"})

    return SourceEvidence(
        source=SourceSystem.PENDING_BRIEFS,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=fetched_at,
        data_as_of=latest or fetched_at,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )
