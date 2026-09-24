"""CoW-кэш скачанных медиа: один экземпляр байт на весь staging.

Зачем: один и тот же файл с Google Drive попадает в staging каждой заявки
отдельной копией (~240 МБ на прогон), и обратно этот диск не отдаётся.
Кэш держит ровно один экземпляр содержимого (ключ — sha256), а staging
получает reflink-копию: на XFS/btrfs это copy-on-write, физически файл
на диске один.

Почему reflink, а не жёсткая ссылка: staging-контракт требует
`st_nlink == 1` (`services/launch_staging.py`, `services/approval_source_media.py`),
иначе `verify_staged_launch` вернёт MEDIA_HARDLINK_UNSAFE перед каждой
мутацией в Facebook. Reflink даёт отдельный inode с `nlink == 1` и общими
экстентами — контракт цел, диск экономится.

Чего НЕ делает: не ходит в сеть, не решает что качать, не удаляет staging
и не трогает файлы вне своего корня. Если ФС не умеет reflink, кэш молча
выключается и вызывающий код копирует по-старому.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    REPORT_CHECKER_MEDIA_CACHE_ENABLED,
    REPORT_CHECKER_MEDIA_CACHE_ROOT,
    REPORT_CHECKER_STAGING_DIR_MODE,
)

logger = logging.getLogger(__name__)

# FICLONE = _IOW(0x94, 9, int). В Python 3.12+ константа есть в fcntl,
# на 3.11 (прод) её нет — держим литерал как фолбэк.
_FICLONE = getattr(fcntl, "FICLONE", 0x40049409)

# Кэш-файл только на чтение: его разделяют все заявки, менять его нельзя.
_CACHE_FILE_MODE = 0o400

_SHA256_LENGTH = 64
_COPY_CHUNK_BYTES = 1024 * 1024

# Ошибки, означающие «эта ФС не умеет reflink» — не повод падать.
_UNSUPPORTED_ERRNOS = frozenset(
    {errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV, errno.EINVAL, errno.ENOSYS}
)

# Результат пробы reflink на процесс: None — ещё не проверяли.
_reflink_supported: bool | None = None


class MediaCacheError(RuntimeError):
    """Кэш не смог выполнить операцию; вызывающий код обязан скопировать сам."""


@dataclass(frozen=True)
class PurgeStats:
    """Итог уборки кэша."""

    scanned: int
    removed: int
    removed_bytes: int
    kept: int
    errors: int


def _cache_root() -> Path:
    return Path(REPORT_CHECKER_MEDIA_CACHE_ROOT)


def _validate_digest(sha256: str) -> str:
    if len(sha256) != _SHA256_LENGTH or any(
        char not in "0123456789abcdef" for char in sha256
    ):
        raise MediaCacheError("MEDIA_CACHE_DIGEST_INVALID")
    return sha256


def cache_path(sha256: str) -> Path:
    """Путь файла в кэше: двухуровневый шардинг, чтобы каталог не разрастался."""

    digest = _validate_digest(sha256)
    return _cache_root() / digest[:2] / digest


def _ficlone(destination_fd: int, source_fd: int) -> None:
    fcntl.ioctl(destination_fd, _FICLONE, source_fd)


def reflink_supported() -> bool:
    """Проверяет reflink одной пробой на процесс: без него кэш бесполезен."""

    global _reflink_supported
    if _reflink_supported is not None:
        return _reflink_supported
    if not REPORT_CHECKER_MEDIA_CACHE_ENABLED:
        _reflink_supported = False
        return False
    root = _cache_root()
    probe_source: Path | None = None
    probe_target: Path | None = None
    try:
        root.mkdir(mode=REPORT_CHECKER_STAGING_DIR_MODE, parents=True, exist_ok=True)
        probe_source = root / f".reflink-probe-{uuid.uuid4().hex}.src"
        probe_target = root / f".reflink-probe-{uuid.uuid4().hex}.dst"
        probe_source.write_bytes(b"reflink-probe")
        source_fd = os.open(probe_source, os.O_RDONLY)
        try:
            destination_fd = os.open(
                probe_target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _CACHE_FILE_MODE
            )
            try:
                _ficlone(destination_fd, source_fd)
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)
        _reflink_supported = True
    except OSError as exc:
        logger.info(
            "media_cache: reflink недоступен (%s), кэш выключен", errno.errorcode.get(
                exc.errno, exc.errno
            )
        )
        _reflink_supported = False
    finally:
        for probe in (probe_source, probe_target):
            if probe is None:
                continue
            try:
                probe.unlink()
            except OSError:
                pass
    return _reflink_supported


def store(source_fd: int, sha256: str, size: int) -> Path | None:
    """Кладёт содержимое `source_fd` в кэш под ключом sha256.

    Возвращает путь кэш-файла или None, если кэш недоступен. Запись атомарна:
    временный файл в том же каталоге, fsync, os.replace. Гонка двух заявок с
    одинаковым содержимым безопасна — побеждает любая, байты одинаковые.
    """

    if not reflink_supported():
        return None
    target = cache_path(sha256)
    try:
        existing = target.stat()
        if stat.S_ISREG(existing.st_mode) and existing.st_size == size:
            _touch(target)
            return target
        # Размер разошёлся с ключом — кэш-файл битый, перезаписываем.
        logger.warning("media_cache: битый кэш-файл %s, перезапись", sha256[:12])
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("media_cache: stat %s не удался: %s", sha256[:12], type(exc).__name__)
        return None
    temporary: Path | None = None
    try:
        target.parent.mkdir(mode=REPORT_CHECKER_STAGING_DIR_MODE, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{sha256[:16]}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            os.lseek(source_fd, 0, os.SEEK_SET)
            written = 0
            while True:
                chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                offset = 0
                while offset < len(chunk):
                    step = os.write(descriptor, chunk[offset:])
                    if step <= 0:
                        # Короткая запись не считается успехом: иначе цикл вечный.
                        raise MediaCacheError("MEDIA_CACHE_WRITE_STALLED")
                    offset += step
                written += len(chunk)
            if written != size:
                raise MediaCacheError("MEDIA_CACHE_SIZE_MISMATCH")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(temporary, _CACHE_FILE_MODE)
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(target.parent)
        return target
    except (OSError, MediaCacheError) as exc:
        logger.warning(
            "media_cache: запись %s не удалась: %s", sha256[:12], type(exc).__name__
        )
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        return None


def clone_into(cached: Path, destination: Path, *, file_mode: int) -> bool:
    """Материализует кэш-файл в destination через reflink.

    True — файл создан (nlink == 1, экстенты общие с кэшем).
    False — ФС не смогла; вызывающий код обязан скопировать байты сам.
    """

    if not reflink_supported():
        return False
    try:
        source_fd = os.open(cached, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    destination_fd: int | None = None
    created = False
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            return False
        destination_fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, file_mode
        )
        created = True
        _ficlone(destination_fd, source_fd)
        os.fsync(destination_fd)
        return True
    except OSError as exc:
        if destination_fd is not None:
            os.close(destination_fd)
            destination_fd = None
        # Удаляем только то, что создали сами: чужой файл трогать нельзя.
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        if exc.errno in _UNSUPPORTED_ERRNOS:
            logger.info("media_cache: reflink отклонён ФС, копируем напрямую")
            return False
        logger.warning("media_cache: clone не удался: %s", type(exc).__name__)
        return False
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


def _touch(path: Path) -> None:
    """Отмечает обращение: TTL кэша считается от последнего использования."""

    try:
        os.utime(path, None)
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def purge_unused(
    *,
    max_age_days: int,
    now: datetime | None = None,
    apply: bool = False,
) -> PurgeStats:
    """Удаляет кэш-файлы, к которым не обращались `max_age_days` дней.

    По умолчанию — сухой прогон: считает, но ничего не удаляет.
    """

    if max_age_days < 1:
        raise ValueError("max_age_days должен быть >= 1")
    moment = now or datetime.now(timezone.utc)
    threshold = (moment - timedelta(days=max_age_days)).timestamp()
    root = _cache_root()
    scanned = removed = removed_bytes = kept = errors = 0
    if not root.is_dir():
        return PurgeStats(0, 0, 0, 0, 0)
    for shard in sorted(root.iterdir()):
        if not shard.is_dir() or shard.is_symlink():
            continue
        for entry in sorted(shard.iterdir()):
            if entry.is_symlink() or not entry.is_file():
                continue
            scanned += 1
            try:
                info = entry.stat()
                if info.st_mtime >= threshold:
                    kept += 1
                    continue
                if apply:
                    entry.unlink()
                removed += 1
                removed_bytes += info.st_size
            except OSError as exc:
                errors += 1
                logger.warning(
                    "media_cache: не удалось убрать %s: %s", entry.name[:12], type(exc).__name__
                )
    if apply:
        _drop_empty_shards(root)
    return PurgeStats(scanned, removed, removed_bytes, kept, errors)


def _drop_empty_shards(root: Path) -> None:
    for shard in sorted(root.iterdir()):
        if not shard.is_dir() or shard.is_symlink():
            continue
        try:
            next(shard.iterdir())
        except StopIteration:
            try:
                shutil.rmtree(shard)
            except OSError:
                pass
        except OSError:
            pass


__all__ = [
    "MediaCacheError",
    "PurgeStats",
    "cache_path",
    "clone_into",
    "purge_unused",
    "reflink_supported",
    "store",
]
