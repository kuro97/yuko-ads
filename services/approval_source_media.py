"""Read-only rehash фактических staged media/text bytes."""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from config import REPORT_CHECKER_STAGING_ROOT
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
)

_READ_CHUNK_BYTES = 1024 * 1024


class MediaEvidenceError(RuntimeError):
    """Staged input нельзя безопасно и полностью перечитать."""


@dataclass(frozen=True, slots=True)
class StagedFileDigest:
    relative_path: str
    order_index: int
    size_bytes: int
    content_sha256: str
    normalized_text_sha256: str | None


def normalize_ad_text(value: str) -> str:
    """Единая нормализация текста: NFC и LF, без скрытого trim."""

    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _safe_relative_path(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise MediaEvidenceError("MEDIA_PATH_INVALID")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise MediaEvidenceError("MEDIA_PATH_ESCAPE")
    return relative


def _safe_root(root: Path) -> Path:
    try:
        root_lstat = root.lstat()
    except OSError as exc:
        raise MediaEvidenceError("MEDIA_ROOT_MISSING") from exc
    if stat.S_ISLNK(root_lstat.st_mode) or not stat.S_ISDIR(root_lstat.st_mode):
        raise MediaEvidenceError("MEDIA_ROOT_INVALID")
    return root.resolve(strict=True)


def _open_safe_file(root: Path, relative: PurePosixPath) -> tuple[int, Path, os.stat_result]:
    path = root.joinpath(*relative.parts)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise MediaEvidenceError("MEDIA_ROOT_OPEN_FAILED") from exc
    try:
        for part in relative.parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            except OSError as exc:
                raise MediaEvidenceError("MEDIA_PARENT_UNSAFE") from exc
            os.close(directory_fd)
            directory_fd = next_fd
        file_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise MediaEvidenceError("MEDIA_FILE_UNSAFE") from exc
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            os.close(descriptor)
            code = "MEDIA_HARDLINK_UNSAFE" if opened.st_nlink != 1 else "MEDIA_FILE_UNSAFE"
            raise MediaEvidenceError(code)
        return descriptor, path, opened
    finally:
        os.close(directory_fd)


def _hash_one(root: Path, raw_path: str, order_index: int) -> StagedFileDigest:
    relative = _safe_relative_path(raw_path)
    descriptor, path, before = _open_safe_file(root, relative)
    digest = hashlib.sha256()
    content = bytearray() if path.suffix.lower() == ".txt" else None
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            if content is not None:
                content.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        total != before.st_size
        or after.st_ino != before.st_ino
        or after.st_dev != before.st_dev
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise MediaEvidenceError("MEDIA_FILE_RACE")
    if total <= 0:
        raise MediaEvidenceError("MEDIA_FILE_EMPTY")

    text_sha256: str | None = None
    if content is not None:
        try:
            normalized = normalize_ad_text(bytes(content).decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise MediaEvidenceError("MEDIA_TEXT_UTF8_INVALID") from exc
        text_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return StagedFileDigest(
        relative_path=relative.as_posix(),
        order_index=order_index,
        size_bytes=total,
        content_sha256=digest.hexdigest(),
        normalized_text_sha256=text_sha256,
    )


def rehash_staged_files(
    staged_relative_paths: tuple[str, ...],
    *,
    staging_root: Path | None = None,
) -> tuple[StagedFileDigest, ...]:
    """Хеширует файлы ровно в manifest order; дубликаты запрещены."""

    if not staged_relative_paths or len(staged_relative_paths) != len(set(staged_relative_paths)):
        raise MediaEvidenceError("MEDIA_ORDER_INVALID")
    root = _safe_root(staging_root or REPORT_CHECKER_STAGING_ROOT)
    return tuple(
        _hash_one(root, relative_path, order_index)
        for order_index, relative_path in enumerate(staged_relative_paths)
    )


def _error(now: datetime, code: str, *, state: EvidenceState = EvidenceState.ERROR) -> SourceEvidence:
    return SourceEvidence(
        source=SourceSystem.MEDIA_BYTES,
        state=state,
        fetched_at=now,
        data_as_of=None,
        from_cache=False,
        complete=False,
        records=(),
        error_code=code,
    )


def load_media_evidence(request: EvidenceRequest, now: datetime) -> SourceEvidence:
    """Перечитывает actual bytes; manifest hash из памяти не считается доказательством."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    if not request.staged_relative_paths:
        return SourceEvidence(
            source=SourceSystem.MEDIA_BYTES,
            state=EvidenceState.FRESH_COMPLETE,
            fetched_at=now,
            data_as_of=now,
            from_cache=False,
            complete=True,
            records=(),
        )
    try:
        digests = rehash_staged_files(request.staged_relative_paths)
    except Exception as exc:
        code = str(exc) if isinstance(exc, MediaEvidenceError) else type(exc).__name__
        state = EvidenceState.INCOMPLETE if isinstance(exc, MediaEvidenceError) else EvidenceState.ERROR
        return _error(now, code[:120], state=state)

    media_subjects = tuple(subject for subject in request.subjects if subject.kind is SubjectKind.MEDIA)
    records: list[EvidenceRecord] = []
    for index, digest in enumerate(digests):
        subject = (
            media_subjects[index]
            if len(media_subjects) == len(digests)
            else SubjectRef(SubjectKind.MEDIA, digest.relative_path)
        )
        entity_ids = (
            digest.relative_path,
            f"order:{digest.order_index}",
            f"size:{digest.size_bytes}",
        )
        records.append(
            EvidenceRecord(
                category=FactCategory.MATCH,
                subject=subject,
                metric=Metric.MATCH_STATE,
                value=digest.content_sha256,
                source=SourceSystem.MEDIA_BYTES,
                state=EvidenceState.FRESH_COMPLETE,
                observed_at=now,
                window=None,
                currency=None,
                entity_ids=entity_ids,
            )
        )
        if digest.normalized_text_sha256 is not None:
            records.append(
                EvidenceRecord(
                    category=FactCategory.MATCH,
                    subject=subject,
                    metric=Metric.DISPLAY_CONTEXT,
                    value=digest.normalized_text_sha256,
                    source=SourceSystem.MEDIA_BYTES,
                    state=EvidenceState.FRESH_COMPLETE,
                    observed_at=now,
                    window=None,
                    currency=None,
                    entity_ids=(*entity_ids, "normalized-text:NFC-LF"),
                )
            )

    return SourceEvidence(
        source=SourceSystem.MEDIA_BYTES,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )
