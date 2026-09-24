"""Тонкий read-only клиент CDP Acme (https://cdp.example.com/api/v1).

Только GET (ключ readonly). Никаких мутаций. Ключ из окружения CDP_API_KEY —
НЕ логируем его значение. На неизвестных путях/без трейлинг-слэша сервер CDP
отдаёт SPA-HTML со СТАТУСОМ 200 — поэтому ПЕРЕД .json() ОБЯЗАТЕЛЬНО проверяем
Content-Type: application/json, иначе молча распарсили бы мусор.

Кеш в память процесса (TTL 600с) — чтобы крон бюджет-пилота (тики в 13:xx)
не долбил чужой сервис. На диск ничего не пишем.

Комментарии на русском.
"""

import logging
import os
import time
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests

from services.approval_checker_models import CdpResponse, canonical_json

logger = logging.getLogger(__name__)

_BASE_URL = "https://cdp.example.com/api/v1"

# Таймаут (connect, read). Сервис друга может лежать — не висим.
_TIMEOUT = (5, 15)

# TTL кеша в память (сек). Крон тикает раз в 15 мин, но в 13:xx может быть
# несколько тиков — кеш гасит повторные обращения к API в пределах прогона/окна.
_CACHE_TTL_SEC = 600

# TTL кеша для budget-context (сек). Прогнозный движок пересчитывается раз в
# сутки (ночью, к 09:30 CityA) — данные внутри дня не меняются, но час TTL —
# консервативный запас на случай раннего пересчёта. Гасит повторные тики крона.
_BUDGET_CTX_TTL_SEC = 3600

# TTL кеша платежей (сек). Синк ERP ~вечером того же дня; в пределах одного
# прогона крона повторные запросы гасим, но час-получас достаточно (данные дня
# стабильны). Умеренный: гасит тики, но не держит устаревшее сутки.
_PAYMENTS_TTL_SEC = 1800

# 429 от CDP — не отказ, а «подожди»: длинная пагинация (полугодовая выгрузка
# платежей — полторы сотни страниц) упирается в лимит чужого сервиса. Ждём и
# повторяем, а не роняем весь прогон (иначе длинный бэкфилл может потерять
# выручку целиком из-за одного 429).
_RATE_LIMIT_RETRIES = 4
_RATE_LIMIT_BASE_SLEEP_SEC = 5.0
_RATE_LIMIT_MAX_SLEEP_SEC = 30.0
# Потолок суммарного ожидания на ОДИН вызов. Нужен из-за жёсткого внешнего
# таймаута бюджет-скейлера (web/app.py: _SCALER_TIMEOUT_SEC = 20 мин): его
# прогон делает до пяти обращений в CDP, и без потолка одни только выдержки
# могли бы выбрать весь таймаут, а поток при этом не убивается и продолжает
# писать бюджеты уже вне контроля крона.
_RATE_LIMIT_TOTAL_SLEEP_SEC = 45.0


def _rate_limit_delay(resp, attempt: int, slept: float = 0.0) -> float:
    """Сколько ждать после 429: Retry-After сервера, иначе удвоение от базы.

    Заголовку верим, но в разумных пределах: отрицательные и абсурдные
    значения (как и мусор вместо числа) заменяем своей выдержкой. Результат
    урезается остатком общего бюджета ожидания; 0 означает «ждать больше
    нельзя, отдавай ошибку».
    """
    remaining = _RATE_LIMIT_TOTAL_SLEEP_SEC - slept
    if remaining <= 0:
        return 0.0
    header = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
    wait = None
    if header is not None:
        try:
            parsed = float(str(header).strip())
        except (TypeError, ValueError):
            parsed = -1.0
        if 0 < parsed <= _RATE_LIMIT_MAX_SLEEP_SEC:
            wait = parsed
    if wait is None:
        wait = min(
            _RATE_LIMIT_BASE_SLEEP_SEC * (2 ** attempt), _RATE_LIMIT_MAX_SLEEP_SEC
        )
    return min(wait, remaining)


# Кеш: {(path, frozenset(params.items())): (expires_at_epoch, payload)}
_cache: dict = {}

# Метаданные живут отдельно: формат старого кеша менять нельзя, его напрямую
# проверяют legacy-тесты и используют уже запущенные процессы.
_cache_metadata: dict[tuple[str, frozenset], tuple[datetime, datetime | None, str]] = {}


@dataclass(frozen=True, slots=True)
class StrictPaymentsRead:
    """Полный checker-only снимок пагинации платежей."""

    items: tuple[dict[str, object], ...]
    declared_total: int
    collected_total: int
    page_item_counts: tuple[int, ...]
    fetched_at: datetime
    data_as_of: datetime | None
    from_cache: bool
    complete: bool


class CdpError(Exception):
    """Любая ошибка обращения к CDP (сеть, не-JSON, HTTP != 200, 401/403/422)."""


def _get_api_key() -> str:
    """Ключ из окружения. Пусто/None → CdpError (fail-closed на уровне вызова).
    Значение ключа НИКОГДА не попадает в текст исключения/лога."""
    key = os.environ.get("CDP_API_KEY")
    if not key:
        raise CdpError("CDP_API_KEY не задан в окружении")
    return key


def _mask(text: str) -> str:
    """Маскирует значение CDP_API_KEY в произвольной строке (для безопасного лога).
    Если ключ пуст — возвращает text как есть."""
    key = os.environ.get("CDP_API_KEY") or ""
    if not key:
        return text
    return text.replace(key, "***")


def _request(path: str, params: dict, ttl: int | None = None) -> object:
    """GET {_BASE_URL}{path} с ключом, таймаутом, 1 ретраем на сеть, проверкой
    content-type и TTL-кешем.

    Args:
        ttl: срок жизни записи кеша в секундах. None → используется дефолт
            _CACHE_TTL_SEC (600). Передаётся вызывающей функцией под конкретный
            эндпоинт (напр. get_budget_context передаёт _BUDGET_CTX_TTL_SEC=3600).
            Ключ кеша НЕ включает ttl — повторный вызов того же пути/params
            попадает в кеш независимо от того, каким ttl он был туда положен.

    Алгоритм:
      1. Кеш-хит (не истёк) → вернуть закешированный payload.
      2. requests.get(url, params, headers={'X-API-Key': key}, timeout=_TIMEOUT).
         На ConnectionError/Timeout → один повтор; второй провал → CdpError.
      3. resp.status_code != 200 → CdpError (текст ошибки маскирует ключ, тело
         обрезаем до 200 символов).
      4. 'application/json' NOT IN resp.headers.get('Content-Type','') → CdpError
         («CDP вернул не-JSON (SPA-HTML?) — путь неверный или сервис лежит»).
      5. payload = resp.json(); положить в кеш с expires_at = now + effective_ttl.
      6. Вернуть payload.

    path — с ведущим слэшем; для /channels/ трейлинг-слэш ОБЯЗАТЕЛЕН
    (в шаге A /channels/ не используется, но правило зафиксировано в докстринге).
    """
    key = _get_api_key()  # бросит CdpError если пусто
    cache_key = (path, frozenset(params.items()))
    hit = _cache.get(cache_key)
    now_epoch = time.time()
    if hit and hit[0] > now_epoch:
        return hit[1]

    effective_ttl = ttl if ttl is not None else _CACHE_TTL_SEC

    url = f"{_BASE_URL}{path}"
    headers = {"X-API-Key": key}
    last_exc = None
    network_attempts = 0
    rate_limit_waits = 0
    slept = 0.0
    while network_attempts < 2:  # 1 основной + 1 ретрай на сеть
        network_attempts += 1
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            continue  # ретраим только сеть
        # 429 — просьба подождать, а не отказ: пережидаем и повторяем.
        if resp.status_code == 429 and rate_limit_waits < _RATE_LIMIT_RETRIES:
            delay = _rate_limit_delay(resp, rate_limit_waits, slept)
            if delay > 0:
                rate_limit_waits += 1
                slept += delay
                logger.warning(
                    "CDP %s: лимит запросов (429), ждём %.0fс — попытка %d из %d",
                    path, delay, rate_limit_waits, _RATE_LIMIT_RETRIES,
                )
                time.sleep(delay)
                network_attempts -= 1  # ожидание лимита не тратит сетевые попытки
                continue
        # HTTP-статус — не сетевая ошибка, не ретраим
        if resp.status_code != 200:
            raise CdpError(_mask(f"CDP {path}: HTTP {resp.status_code} — {resp.text[:200]}"))
        ctype = resp.headers.get("Content-Type", "")
        if "application/json" not in ctype:
            # SPA-HTML со статусом 200 — путь неверный или сервис отдаёт заглушку
            raise CdpError(
                f"CDP {path}: ответ не JSON (Content-Type={ctype!r}) — путь неверный или сервис лежит"
            )
        payload = resp.json()
        _cache[cache_key] = (now_epoch + effective_ttl, payload)
        return payload
    raise CdpError(_mask(f"CDP {path}: сеть недоступна после ретрая — {last_exc}"))


def _parse_aware_datetime(value: object) -> datetime | None:
    """Парсит provider timestamp, не придумывая timezone для naive значений."""

    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _response_data_as_of(payload: object, headers: object) -> datetime | None:
    """Извлекает фактическую дату данных только из ответа провайдера."""

    header_map = headers if isinstance(headers, dict) else {}
    for name in ("X-Data-As-Of", "Last-Modified", "Date"):
        parsed = _parse_aware_datetime(header_map.get(name))
        if parsed is not None:
            return parsed
    if isinstance(payload, dict):
        for name in ("data_as_of", "as_of", "updated_at", "synced_at"):
            parsed = _parse_aware_datetime(payload.get(name))
            if parsed is not None:
                return parsed
        raw_items = payload.get("items") or payload.get("data") or []
        if isinstance(raw_items, list):
            candidates = [
                parsed
                for item in raw_items
                if isinstance(item, dict)
                for parsed in (_parse_aware_datetime(item.get("synced_at")),)
                if parsed is not None
            ]
            if candidates:
                return max(candidates)
    return None


def _request_with_meta(
    path: str,
    params: dict[str, object],
    ttl: int | None = None,
    *,
    force_live: bool = False,
) -> CdpResponse:
    """GET с truthful metadata; force_live никогда не читает TTL-кеш."""

    key = _get_api_key()
    cache_key = (path, frozenset(params.items()))
    now_epoch = time.time()
    cached = _cache.get(cache_key)
    cached_meta = _cache_metadata.get(cache_key)
    if not force_live and cached and cached[0] > now_epoch and cached_meta is not None:
        fetched_at, data_as_of, headers_sha256 = cached_meta
        return CdpResponse(
            payload=cached[1],
            status_code=200,
            fetched_at=fetched_at,
            data_as_of=data_as_of,
            from_cache=True,
            cache_age_seconds=max(0.0, now_epoch - fetched_at.timestamp()),
            response_headers_sha256=headers_sha256,
        )

    effective_ttl = ttl if ttl is not None else _CACHE_TTL_SEC
    url = f"{_BASE_URL}{path}"
    headers = {"X-API-Key": key}
    last_exc: Exception | None = None
    network_attempts = 0
    rate_limit_waits = 0
    slept = 0.0
    while network_attempts < 2:
        network_attempts += 1
        try:
            response = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            continue
        # 429 — просьба подождать, а не отказ (см. _rate_limit_delay).
        if response.status_code == 429 and rate_limit_waits < _RATE_LIMIT_RETRIES:
            delay = _rate_limit_delay(response, rate_limit_waits, slept)
            if delay <= 0:
                # Бюджет ожидания исчерпан — отдаём ошибку, а не спим дальше.
                raise CdpError(
                    _mask(f"CDP {path}: HTTP 429 — {response.text[:200]}")
                )
            rate_limit_waits += 1
            slept += delay
            logger.warning(
                "CDP %s: лимит запросов (429), ждём %.0fс — попытка %d из %d",
                path, delay, rate_limit_waits, _RATE_LIMIT_RETRIES,
            )
            time.sleep(delay)
            network_attempts -= 1
            continue
        if response.status_code != 200:
            raise CdpError(
                _mask(f"CDP {path}: HTTP {response.status_code} — {response.text[:200]}")
            )
        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise CdpError(
                f"CDP {path}: ответ не JSON (Content-Type={content_type!r}) — "
                "путь неверный или сервис лежит"
            )
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise CdpError(f"CDP {path}: битый JSON") from exc
        fetched_at = datetime.now(timezone.utc)
        data_as_of = _response_data_as_of(payload, response.headers)
        public_headers = {
            str(name).lower(): str(value)
            for name, value in response.headers.items()
            if str(name).lower() not in {"authorization", "x-api-key", "set-cookie"}
        }
        headers_sha256 = hashlib.sha256(canonical_json(public_headers)).hexdigest()
        _cache[cache_key] = (now_epoch + effective_ttl, payload)
        _cache_metadata[cache_key] = (fetched_at, data_as_of, headers_sha256)
        return CdpResponse(
            payload=payload,
            status_code=response.status_code,
            fetched_at=fetched_at,
            data_as_of=data_as_of,
            from_cache=False,
            cache_age_seconds=0.0,
            response_headers_sha256=headers_sha256,
        )
    raise CdpError(_mask(f"CDP {path}: сеть недоступна после ретрая — {last_exc}"))


def _strict_payment_is_valid(payment: object) -> bool:
    """Годна ли запись страницы, чтобы считать выгрузку целой.

    Требования предъявляются только к тому, что мы реально потребляем — к
    income/refund. Внутренний кэшфлоу компании (expense/transfer) в выручку не
    идёт и договора не имеет ПО ПРИРОДЕ: перевод между своими счетами не
    привязан к сделке. Требовать от него contract_number — значит объявлять
    нормальную выгрузку битой (замер показал, что большинство записей на
    странице — transfer/expense с пустым договором; из-за этого
    get_payments_strict возвращал complete=False и НОЛЬ записей на любом окне,
    а проверка денежных фактов не могла подтвердить ни одного отчёта).
    """
    if not isinstance(payment, dict):
        return False
    payment_id = payment.get("id")
    direction = payment.get("direction")
    if isinstance(payment_id, bool) or payment_id in (None, ""):
        return False
    if direction not in {"income", "refund", "expense", "transfer"}:
        return False
    if direction not in {"income", "refund"}:
        # Чужая нам строка: в выручку не попадёт, дальше не проверяем.
        return True
    # Номер договора здесь НЕ требуется. Эта проверка отвечает на вопрос
    # «не порвалась ли выгрузка», а не «к кому отнести платёж». В ERP
    # встречаются приходы и возвраты без договора (например, приход
    # 100 000 ¤ без номера) — они просто ни к какому объявлению не
    # относятся. Считать из-за них всю страницу битой
    # значит объявлять целую выгрузку неполной. Привязка — забота вызывающего:
    # cohort_builder считает такой платёж чужим лидом, approval_source_cdp
    # отказывается закрывать цепочку.
    try:
        date.fromisoformat(str(payment.get("doc_date")))
        amount = Decimal(str(payment.get("amount")))
    except (TypeError, ValueError, InvalidOperation):
        return False
    return amount.is_finite() and amount >= 0


def get_payments_strict(
    date_from: date,
    date_to: date,
    direction: str | None = None,
    *,
    force_live: bool = False,
) -> StrictPaymentsRead:
    """Читает все страницы и никогда не выдаёт частичный список как полный."""

    if not isinstance(date_from, date) or not isinstance(date_to, date) or date_from > date_to:
        raise ValueError("Некорректное окно платежей")
    if direction not in {None, "income", "refund"}:
        raise ValueError("direction должен быть income, refund или None")

    server_to = date_to + timedelta(days=1)
    collected: list[dict[str, object]] = []
    page_counts: list[int] = []
    declared_total: int | None = None
    totals_stable = True
    pages_complete = False
    fetched_at: datetime | None = None
    data_as_of_values: list[datetime] = []
    any_cache = False

    for page in range(1, 1001):
        params: dict[str, object] = {
            "date_from": date_from.isoformat(),
            "date_to": server_to.isoformat(),
            "page": page,
            "page_size": 100,
        }
        if direction:
            params["direction"] = direction
        response = _request_with_meta(
            "/revenue",
            params,
            ttl=_PAYMENTS_TTL_SEC,
            force_live=force_live,
        )
        any_cache = any_cache or response.from_cache
        fetched_at = response.fetched_at if fetched_at is None else max(fetched_at, response.fetched_at)
        if response.data_as_of is not None:
            data_as_of_values.append(response.data_as_of)

        payload = response.payload
        if not isinstance(payload, dict):
            page_counts.append(0)
            break
        raw_total = payload.get("total")
        if isinstance(raw_total, bool):
            page_counts.append(0)
            break
        try:
            current_total = int(raw_total)
        except (TypeError, ValueError):
            page_counts.append(0)
            break
        if current_total < 0:
            page_counts.append(0)
            break
        if declared_total is None:
            declared_total = current_total
        elif current_total != declared_total:
            totals_stable = False

        raw_items = payload.get("items")
        if raw_items is None:
            raw_items = payload.get("data")
        if not isinstance(raw_items, list):
            page_counts.append(0)
            break
        page_counts.append(len(raw_items))
        if not raw_items:
            pages_complete = declared_total == len(collected)
            break
        if any(not _strict_payment_is_valid(item) for item in raw_items):
            break
        collected.extend(dict(item) for item in raw_items)
        if declared_total is not None and len(collected) >= declared_total:
            pages_complete = len(collected) == declared_total
            break

    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc)
    if declared_total is None:
        declared_total = -1

    seen_ids: set[str] = set()
    unique_ids = True
    filtered: list[dict[str, object]] = []
    for payment in collected:
        payment_id = str(payment.get("id"))
        if payment_id in seen_ids:
            unique_ids = False
            continue
        seen_ids.add(payment_id)
        payment_direction = payment.get("direction")
        if payment_direction not in {"income", "refund"}:
            continue
        if direction and payment_direction != direction:
            continue
        try:
            document_date = date.fromisoformat(str(payment.get("doc_date")))
        except ValueError:
            continue
        if date_from <= document_date <= date_to:
            filtered.append(payment)

    complete = (
        declared_total >= 0
        and totals_stable
        and pages_complete
        and unique_ids
        and len(collected) == declared_total
        and all(_strict_payment_is_valid(item) for item in collected)
        and bool(data_as_of_values or declared_total == 0)
    )
    return StrictPaymentsRead(
        items=tuple(filtered),
        declared_total=declared_total,
        collected_total=len(collected),
        page_item_counts=tuple(page_counts),
        fetched_at=fetched_at,
        data_as_of=max(data_as_of_values) if data_as_of_values else None,
        from_cache=any_cache,
        complete=complete,
    )


def get_daily_report(
    date_from: date,
    date_to: date,
    city: str | None = None,
) -> list[dict]:
    """GET /analytics/daily-report?date_from&date_to(&city).

    Возвращает СПИСОК items (см. поля в §6), уже отфильтрованный по окну:
    элементы, чей report_date вне [date_from, date_to], ОТБРАСЫВАЮТСЯ
    (семантика границ CDP не подтверждена — не доверяем, режем сами).

    Args:
        date_from: начало окна (включительно).
        date_to: конец окна (включительно).
        city: город (опционально; None = все города).

    Returns:
        list[dict] items. Пустой список — валидный результат (нет данных).

    Raises:
        CdpError: сеть/HTTP/не-JSON/нет ключа.
    """
    params = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    if city:
        params["city"] = city
    payload = _request("/analytics/daily-report", params)
    items = (payload or {}).get("items") or []

    filtered = []
    for item in items:
        raw_date = item.get("report_date")
        try:
            report_date = date.fromisoformat(raw_date)
        except (TypeError, ValueError):
            # битый/отсутствующий report_date — не парсится → не в окне → drop
            continue
        if date_from <= report_date <= date_to:
            filtered.append(item)
    return filtered


def get_plan_fact_summary(month: date) -> dict:
    """GET /plans/plan-fact-summary?month=YYYY-MM-01.

    ВАЖНО: month отправляется как ПОЛНАЯ дата первого числа месяца
    (month.replace(day=1).isoformat()). '2026-07' → 422 на стороне CDP.

    Returns:
        dict с ключами month, time_pct, days_in_month, days_elapsed, cities[]
        (см. §6). Форма не проверяется на полноту — вызывающий берёт нужное
        через .get() с дефолтами.

    Raises:
        CdpError: сеть/HTTP/не-JSON/нет ключа.
    """
    params = {"month": month.replace(day=1).isoformat()}
    payload = _request("/plans/plan-fact-summary", params)
    return payload or {}


def get_budget_context(city: str | None = None) -> dict:
    """GET /analytics/budget-context (+city если задан). Read-only, TTL-кеш 3600 с.

    Данные движка меняются раз в сутки (ночной пересчёт к 09:30 CityA),
    крон бюджет-пилота тикает в 13:xx — TTL 3600 с гасит повторные тики одного
    прогона и не долбит чужой сервис (данные внутри дня не меняются, но 1 ч
    TTL — консервативный запас на случай раннего пересчёта). Фильтрация не нужна
    (эндпоинт отдаёт готовый агрегат). Форма не проверяется на полноту —
    вызывающий берёт нужное через .get() с дефолтами.

    Args:
        city: город (опционально; None = агрегат по всем городам + разбивка).

    Returns:
        dict (см. §6.1 спеки ARCH-cdp-budget-context). Может содержать пустой
        cities — валидный ответ.

    Raises:
        CdpError: сеть/HTTP/не-JSON/нет ключа (наружу, как get_daily_report).
    """
    params: dict = {}
    if city:
        params["city"] = city
    payload = _request("/analytics/budget-context", params, ttl=_BUDGET_CTX_TTL_SEC)
    return payload or {}


def get_payments(
    date_from: date,
    date_to: date,
    direction: str | None = None,
) -> list[dict]:
    """GET /revenue?direction&date_from&date_to&page_size=100&page=N — платежи ERP.

    Пагинация ДО КОНЦА: идём page=1,2,... пока накопленных записей < payload["total"]
    И текущая страница непустая (защита от бесконечного цикла, если total врёт).

    ЭКСКЛЮЗИВНЫЙ date_to (проверено: 0 записей на from==to при существующих
    платежах): запрашиваем у CDP date_to+1 день, затем на клиенте фильтруем по
    doc_date ∈ [date_from, date_to] включительно. Не доверяем границам сервера.

    Направления: возвращаем ТОЛЬКО income/refund (expense/transfer — внутренний
    кэшфлоу компании, игнорируем всегда). Если direction задан ("income"/"refund")
    — дополнительно оставляем только его; None → и income, и refund.

    Args:
        date_from: начало окна (включительно, по doc_date).
        date_to: конец окна (включительно, по doc_date).
        direction: "income" | "refund" | None (None = оба клиентских направления).

    Returns:
        list[dict] платежей (см. поля §6.1 ARCH-cdp-payments). Пустой список —
        валидный результат.

    Raises:
        CdpError: сеть/HTTP/не-JSON/нет ключа.
    """
    # date_to эксклюзивен на сервере → запрашиваем +1 день, режем клиентом.
    server_to = date_to + timedelta(days=1)
    accumulated: list[dict] = []
    page = 1
    total: int | None = None
    while page <= 1000:  # защита от бесконечного цикла, если total врёт
        params = {
            "date_from": date_from.isoformat(),
            "date_to": server_to.isoformat(),
            "page": page,
            "page_size": 100,
        }
        if direction:
            params["direction"] = direction
        payload = _request("/revenue", params, ttl=_PAYMENTS_TTL_SEC)
        items = (payload or {}).get("items") or (payload or {}).get("data") or []
        if total is None:
            total = int((payload or {}).get("total") or 0)
        if not items:
            break  # пустая страница → конец (total мог врать)
        accumulated.extend(items)
        if total and len(accumulated) >= total:
            break
        page += 1

    # Фильтр окна по doc_date + отсев expense/transfer + direction на клиенте.
    result: list[dict] = []
    for payment in accumulated:
        payment_direction = payment.get("direction")
        if payment_direction not in ("income", "refund"):
            continue  # expense/transfer — внутренний кэшфлоу, игнорируем всегда
        if direction and payment_direction != direction:
            continue
        raw_doc_date = payment.get("doc_date")
        try:
            doc_date = date.fromisoformat(raw_doc_date)
        except (TypeError, ValueError):
            continue  # битая/отсутствующая дата → вне окна → drop
        if date_from <= doc_date <= date_to:
            result.append(payment)
    return result
