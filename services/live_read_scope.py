"""Кеш живых чтений на один прогон исполнения одобренных действий.

Зачем. Исполнение одного одобренного действия читает живые источники ТРИ раза:
сборка манифеста (``owner_action_live_manifest._load_live_sources``), живая
сводка gateway (``approval_sources.load_action_item_evidence``) и precondition
адаптера (``action_adapter_pause._five_source_observation``). Все три чтения
идут с ОДНИМ и тем же ``now`` — то есть по контракту это одно наблюдение в один
момент времени, а не три разных. Прогон очереди тоже фиксирует ``checked_at``
один раз и передаёт его каждому заданию, поэтому окно решения (30 дней) у всех
заданий прогона совпадает байт в байт.

Без кеша это превращалось в 3 × N одинаковых запросов к AMO/CDP за прогон и
съедало бюджет задания. Здесь заводится явная область видимости: внутри неё
одинаковый ключ читается один раз, снаружи кеша нет вообще.

Чего кеш НЕ делает:

* не живёт дольше открытой области — новый прогон читает заново;
* не кеширует исключения — сбой источника обязан повториться, а не залипнуть;
* не подменяет ``fetched_at``/``from_cache`` источника: наружу отдаётся тот же
  объект, что вернул живое чтение, поэтому проверки свежести и запрет кеша для
  действий продолжают судить по настоящим метаданным;
* не касается Facebook — детект дрейфа FB между чтениями обязан оставаться
  живым, иначе исчезнет защита «состояние не изменилось с момента одобрения».
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Callable, Iterator, TypeVar

_SCOPE: ContextVar[dict[object, object] | None] = ContextVar(
    "live_read_scope",
    default=None,
)

_T = TypeVar("_T")


@contextlib.contextmanager
def live_read_scope() -> Iterator[None]:
    """Открывает область переиспользования живых чтений.

    Вложенный вход переиспользует внешнюю область: прогон очереди открывает её
    на весь проход, задание — на себя, и второй вход не должен обнулять то, что
    уже прочитано.
    """

    if _SCOPE.get() is not None:
        yield
        return
    token = _SCOPE.set({})
    try:
        yield
    finally:
        _SCOPE.reset(token)


def scope_is_open() -> bool:
    """True если чтения сейчас переиспользуются (нужно тестам и логам)."""

    return _SCOPE.get() is not None


def scoped_read(key: object, loader: Callable[[], _T]) -> _T:
    """Читает ``loader()`` один раз на область; вне области — всегда заново.

    ``key`` обязан быть hashable и полностью описывать запрос: разные окна,
    разные ad_id и разный ``force_live`` — это разные ключи.
    """

    memo = _SCOPE.get()
    if memo is None:
        return loader()
    if key in memo:
        return memo[key]  # type: ignore[return-value]
    value = loader()
    memo[key] = value
    return value
