"""Provider-neutral межпроцессные lock-и approval gateway."""

from __future__ import annotations

import fcntl
import os
import re
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator


_LOCK_STATE = threading.local()
_THREAD_GUARD = threading.RLock()
_RANK_OPERATION = 0
_RANK_LAUNCH = 1
_RANK_ADSET = 2


class ActionLockOrderError(RuntimeError):
    """Нарушен единый порядок или сделана повторная попытка захвата."""


def _operation_lock_path() -> Path:
    from config import REPORT_CHECKER_OPERATION_LOCK_PATH

    return Path(REPORT_CHECKER_OPERATION_LOCK_PATH)


def _lock_root() -> Path:
    # Все provider-mutations обязаны делить тот же физический namespace,
    # который используют pause guard и cleaner.
    return Path(__file__).resolve().parent.parent / "data" / "locks"


def _stack() -> list[tuple[int, str]]:
    held = getattr(_LOCK_STATE, "held", None)
    if held is None:
        held = []
        _LOCK_STATE.held = held
    return held


def _validate_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{field_name} должен быть непустым ID")
    return value


def _assert_can_acquire(
    rank: int, key: str, *, allow_sorted_peer: bool = False
) -> None:
    held = _stack()
    if any(existing_key == key for _, existing_key in held):
        raise ActionLockOrderError(f"Повторный захват lock запрещён: {key}")
    if held and rank < held[-1][0]:
        raise ActionLockOrderError(
            "Нарушен порядок operation -> launch -> sorted adset"
        )
    if held and rank == held[-1][0]:
        previous_key = held[-1][1]
        if not allow_sorted_peer or not (
            previous_key.startswith("adset:")
            and key.startswith("adset:")
            and _adset_sort_key(previous_key.removeprefix("adset:"))
            < _adset_sort_key(key.removeprefix("adset:"))
        ):
            raise ActionLockOrderError(
                "Повторный или несортированный peer lock запрещён"
            )


@contextmanager
def _file_lock(
    path: Path,
    *,
    rank: int,
    key: str,
    allow_sorted_peer: bool = False,
) -> Iterator[None]:
    _assert_can_acquire(rank, key, allow_sorted_peer=allow_sorted_peer)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    # Thread guard нужен потому, что flock одного процесса не сериализует все
    # платформенные комбинации разных file descriptors между потоками.
    with _THREAD_GUARD:
        fd = os.open(path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _stack().append((rank, key))
            try:
                yield
            finally:
                popped = _stack().pop()
                if popped != (rank, key):
                    raise ActionLockOrderError("Стек action locks повреждён")
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def operation_lock() -> Iterator[None]:
    """Сериализует reserve/recovery одного gateway процесса."""

    with _file_lock(
        _operation_lock_path(),
        rank=_RANK_OPERATION,
        key="operation",
    ):
        yield


@contextmanager
def launch_execution_lease(operation_id: str) -> Iterator[None]:
    """Глобально сериализует LAUNCH, не импортируя auto_launch."""

    _validate_id(operation_id, "operation_id")
    with _file_lock(
        _lock_root() / "launch-execution.lock",
        rank=_RANK_LAUNCH,
        key="launch",
    ):
        yield


def _adset_lock_path(adset_id: str) -> Path:
    normalized = _validate_id(adset_id, "adset_id")
    if re.fullmatch(r"[A-Za-z0-9_-]+", normalized) is None:
        raise ValueError("adset_id содержит недопустимые символы")
    return _lock_root() / f"adset-{normalized}.lock"


def _adset_sort_key(adset_id: str) -> tuple[int, int | str]:
    """Meta IDs сортируются численно; legacy opaque IDs — лексикографически."""

    return (0, int(adset_id)) if adset_id.isdigit() else (1, adset_id)


@contextmanager
def adset_locks(adset_ids: tuple[str, ...]) -> Iterator[None]:
    """Берёт unique adset lock-и ровно один раз в лексикографическом порядке."""

    normalized = tuple(_validate_id(item, "adset_id") for item in adset_ids)
    if not normalized:
        raise ValueError("adset_locks требует хотя бы один adset_id")
    if len(normalized) != len(set(normalized)):
        raise ActionLockOrderError("Повторный adset_id запрещён")
    ordered = tuple(sorted(normalized, key=_adset_sort_key))
    if any(rank >= _RANK_ADSET for rank, _ in _stack()):
        raise ActionLockOrderError("Вложенный adset_locks запрещён")

    # Все adset lock-и логически имеют один rank; берём их через внутренний
    # цикл, чтобы публичная проверка не сочла второй sorted lock инверсией.
    with ExitStack() as stack:
        for adset_id in ordered:
            key = f"adset:{adset_id}"
            path = _adset_lock_path(adset_id)
            stack.enter_context(
                _file_lock(
                    path,
                    rank=_RANK_ADSET,
                    key=key,
                    allow_sorted_peer=True,
                )
            )
        yield
