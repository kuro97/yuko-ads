"""
Общие функции для работы с FB Ads API.
Используется в analyzer.py и learner.py.
"""

import json
import logging
import threading
import time as _time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
import requests

from services.approval_checker_models import RuntimeBufferSnapshot, TimeWindow

from services.fb_token_provider import get_fb_token, get_fb_account_id

API = "https://graph.facebook.com/v21.0"

# Connection pooling — переиспользуем TCP соединения
session = requests.Session()
# Таймаут по умолчанию для всех запросов к FB API (connect, read).
# session.timeout requests игнорирует, поэтому monkey-patch request() —
# чтобы ни один вызов session.get/post/delete не висел бесконечно.
_DEFAULT_TIMEOUT = (10, 60)
_orig_session_request = session.request
def _session_request_with_timeout(method, url, **kwargs):
    kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
    return _orig_session_request(method, url, **kwargs)
session.request = _session_request_with_timeout

# Глобальный троттлинг FB API — минимум 5 сек между запросами
_fb_lock = threading.Lock()
_fb_last_call = 0.0
_FB_MIN_INTERVAL = 5.0  # секунд между запросами при высокой загрузке лимита (потолок обычного режима)

# Адаптивный троттлинг по реальной загрузке лимитов, которую FB присылает в каждом ответе
# (x-business-use-case-usage / x-app-usage / x-ad-account-usage / x-fb-ads-insights-throttle).
# При загрузке лимита в единицы процентов фиксированные 5 секунд превращали одну паузу
# (десятки чтений FB) в минуты ожидания, и очередь одобренных пауз расходилась сутками.
_FB_USAGE_TTL_SECONDS = 300.0
_FB_INTERVAL_UNKNOWN = 1.0     # загрузка ещё не наблюдалась (старт процесса)
_FB_INTERVAL_STEPS = ((50.0, 0.3), (75.0, 2.0), (90.0, _FB_MIN_INTERVAL))  # (загрузка ниже %, интервал)
_FB_INTERVAL_SATURATED = 15.0  # 90%+ — почти потолок, даём лимиту восстановиться
_fb_usage: dict = {}           # ключ наблюдения → (процент, время)


def _usage_percents(headers) -> dict:
    """Проценты загрузки из заголовков ответа FB. Нестроковые значения (моки) игнорируются."""
    found: dict = {}
    getter = getattr(headers, "get", None)
    if getter is None:
        return found
    for name in ("x-business-use-case-usage", "x-app-usage", "x-ad-account-usage", "x-fb-ads-insights-throttle"):
        raw = getter(name)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        blocks = []
        if name == "x-business-use-case-usage" and isinstance(data, dict):
            for account, items in data.items():
                for item in items if isinstance(items, list) else []:
                    blocks.append((f"buc:{account}:{(item or {}).get('type')}", item))
        elif isinstance(data, dict):
            blocks.append((name, data))
        for key, block in blocks:
            if not isinstance(block, dict):
                continue
            values = [
                float(block[field]) for field in (
                    "call_count", "total_cputime", "total_time", "acc_id_util_pct", "app_id_util_pct",
                ) if isinstance(block.get(field), (int, float)) and not isinstance(block.get(field), bool)
            ]
            if isinstance(block.get("estimated_time_to_regain_access"), (int, float)) and block["estimated_time_to_regain_access"] > 0:
                values.append(100.0)
            if values:
                found[key] = max(values)
    return found


def _observe_usage(resp) -> None:
    try:
        percents = _usage_percents(getattr(resp, "headers", None))
    except Exception:  # noqa: BLE001 — наблюдение загрузки не роняет запрос
        return
    now = _time.time()
    for key, pct in percents.items():
        _fb_usage[key] = (pct, now)


def _throttle_interval() -> float:
    """Минимальная пауза между запросами FB под текущую загрузку лимита."""
    if _FB_MIN_INTERVAL <= 0:
        return 0.0
    now = _time.time()
    fresh = [pct for pct, seen in _fb_usage.values() if now - seen <= _FB_USAGE_TTL_SECONDS]
    if not fresh:
        return min(_FB_INTERVAL_UNKNOWN, _FB_MIN_INTERVAL)
    usage = max(fresh)
    for below, interval in _FB_INTERVAL_STEPS:
        if usage < below:
            return min(interval, _FB_MIN_INTERVAL)
    return _FB_INTERVAL_SATURATED

# Кэш последнего успешного page_limit — чтобы не тратить 2 лишних запроса
# после предыдущего понижения (500→250→125: при следующем вызове сразу 125)
_last_good_page_limit: dict = {"value": 500}

# Retry при rate limit — короткий backoff чтобы UI не висел
# Было 3×(30→60→120)=210с, стало 2×(5→10)=15с
_RATE_LIMIT_MAX_RETRIES = 2
_RATE_LIMIT_BASE_DELAY = 5  # секунд (5 → 10)

# Счётчик ошибок FB API для детектора аномалий (services/anomaly_alerts.py).
# Кольцевой буфер таймстампов не-2xx ответов и устойчивых rate-limit — под
# отдельным локом, не пересекается с _fb_lock (троттлинг запросов).
_FB_ERROR_TIMES: deque = deque(maxlen=200)
_fb_error_lock = threading.Lock()
_FB_ERROR_WINDOW_SEC = 3600  # окно для fb_error_count_last_hour
_FB_RUNTIME_INSTANCE_ID = uuid.uuid4().hex

_logger = logging.getLogger(__name__)


def record_fb_error(now: float | None = None) -> None:
    """Кладёт таймстамп ошибки FB API в кольцевой буфер (deque, maxlen=200).

    Вызывается из _do_throttled_request при не-2xx ответе (кроме штатного
    rate-limit throttle) и при исчерпании retry (устойчивый rate-limit).
    """
    ts = now if now is not None else _time.time()
    with _fb_error_lock:
        _FB_ERROR_TIMES.append(ts)


def fb_error_count_last_hour(now: float | None = None) -> int:
    """Число ошибок FB API за последние 3600 сек (для anomaly_alerts)."""
    ts = now if now is not None else _time.time()
    cutoff = ts - _FB_ERROR_WINDOW_SEC
    with _fb_error_lock:
        return sum(1 for t in _FB_ERROR_TIMES if t >= cutoff)


def read_fb_error_snapshot(now: float | None = None) -> RuntimeBufferSnapshot:
    """Возвращает lock-safe snapshot кольца без очистки и изменения счётчика.

    Полнота fail-closed: если заполненный ring начинается внутри окна, более
    ранние релевантные события могли быть вытеснены и точное число неизвестно.
    """

    timestamp = now if now is not None else _time.time()
    cutoff = timestamp - _FB_ERROR_WINDOW_SEC
    with _fb_error_lock:
        retained = tuple(float(value) for value in _FB_ERROR_TIMES)
        capacity = int(_FB_ERROR_TIMES.maxlen or 0)
    relevant = tuple(value for value in retained if value >= cutoff)
    truncated = bool(
        capacity > 0
        and len(retained) >= capacity
        and retained
        and retained[0] >= cutoff
    )
    observed_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    window = TimeWindow(
        start=observed_at - timedelta(seconds=_FB_ERROR_WINDOW_SEC),
        end=observed_at,
        timezone_name="UTC",
        semantic="FB_ERROR_LAST_HOUR",
    )
    return RuntimeBufferSnapshot(
        source_instance_id=_FB_RUNTIME_INSTANCE_ID,
        observed_at=observed_at,
        window=window,
        event_timestamps=tuple(
            datetime.fromtimestamp(value, tz=timezone.utc) for value in relevant
        ),
        count_in_window=len(relevant),
        buffer_size=len(retained),
        capacity=capacity,
        truncated_in_window=truncated,
        complete=not truncated,
    )


def _is_rate_limit(resp) -> bool:
    """Проверяет, является ли ответ rate limit ошибкой.

    FB шлёт rate limit разными способами:
    - 400 + "too many calls" — старый формат
    - 400 + code 17 — "User request limit reached"
    - 400 + code 4 — "Application request limit reached"
    - 400 + code 32 — "Page request limit reached"
    - 400 + code 80003/80004 — Business Use Case rate limit
    - 429 — стандартный HTTP rate limit
    """
    if resp.status_code == 429:
        return True
    if resp.status_code != 400:
        return False
    text = resp.text.lower()
    if "too many calls" in text or "request limit reached" in text:
        return True
    try:
        code = (resp.json().get("error") or {}).get("code", 0)
        return code in (4, 17, 32, 80003, 80004)
    except Exception:
        return False


def _do_throttled_request(method_fn, url, **kwargs):
    """Запрос с троттлингом + retry при rate limit (exponential backoff).

    Дефолтный timeout (connect, read) — (10, 60) сек, чтобы FB API не висел
    бесконечно при сетевых проблемах. session.timeout requests игнорирует,
    нужно передавать в каждый вызов явно.
    """
    global _fb_last_call
    kwargs.setdefault("timeout", (10, 60))

    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        # Троттлинг — пауза зависит от реальной загрузки лимита FB (см. _throttle_interval)
        with _fb_lock:
            interval = _throttle_interval()
            elapsed = _time.time() - _fb_last_call
            if elapsed < interval:
                wait = interval - elapsed
                _logger.debug("FB API throttle: wait %.1fs", wait)
                _time.sleep(wait)
            _fb_last_call = _time.time()

        resp = method_fn(url, **kwargs)
        _observe_usage(resp)

        # Если не rate limit — возвращаем сразу (успех или другая ошибка)
        if not _is_rate_limit(resp):
            # Не-2xx ответ (кроме штатного rate-limit throttle, у него свой retry
            # ниже) — считаем как ошибку FB API для детектора аномалий.
            if resp.status_code >= 400:
                record_fb_error()
            return resp

        # Rate limit — retry с backoff
        if attempt < _RATE_LIMIT_MAX_RETRIES:
            delay = _RATE_LIMIT_BASE_DELAY * (2 ** attempt)  # 30, 60, 120
            _logger.warning(
                "FB API rate limit (попытка %d/%d), retry через %ds",
                attempt + 1, _RATE_LIMIT_MAX_RETRIES + 1, delay,
            )
            _time.sleep(delay)
        else:
            # Все retry исчерпаны — устойчивый rate-limit, реальная проблема
            record_fb_error()
            raise FBApiError(
                "Facebook временно ограничил запросы к рекламному кабинету "
                "(слишком много вызовов API). Подождите 15-20 минут — "
                "лимит сбросится автоматически.",
                status_code=429,
            )

    # Недостижимо, но на всякий случай
    return resp


def _throttled_get(url, **kwargs):
    """GET с троттлингом + retry при rate limit."""
    return _do_throttled_request(session.get, url, **kwargs)


def _throttled_post(url, **kwargs):
    """POST с троттлингом + retry при rate limit.

    Зеркало _throttled_get: нужен для постановки асинхронных отчётов insights
    (POST /act_X/insights возвращает report_run_id), которые нельзя запросить GET-ом.
    """
    return _do_throttled_request(session.post, url, **kwargs)


class FBApiError(Exception):
    """Ошибка FB API — бросаем вместо тихого return {}."""
    def __init__(self, message: str, status_code: int = 0):
        self.status_code = status_code
        super().__init__(message)


def _is_reduce_data_error(resp) -> bool:
    """Проверяет, является ли ответ ошибкой FB 'Please reduce the amount of data' (код 1).
    Дублирует аналогичный хелпер из analyzer.py — циклический импорт не позволяет переиспользовать."""
    if resp.status_code not in (400, 500):
        return False
    try:
        err = (resp.json().get("error") or {})
        code = err.get("code", 0)
        msg = (err.get("message") or "").lower()
        return code == 1 and "reduce" in msg
    except Exception:
        return False


def build_adset_map() -> dict:
    """Карта adset_id → (city, type). Источник: discover_adsets() с fallback на config."""
    from agent.adset_discovery import discover_adsets

    discovered = discover_adsets()
    adset_map = {}

    # Текущие активные адсеты
    for city, types in discovered["leadgen"].items():
        for atype, aid in types.items():
            adset_map[aid] = (city, atype)

    # Старые адсеты (из discover) — для исторической аналитики
    for city, types in discovered.get("old", {}).items():
        for atype, ids in types.items():
            for aid in ids:
                if aid not in adset_map:
                    adset_map[aid] = (city, atype)

    # ADSETS_OLD из config (legacy support — старые адсеты до autodiscovery)
    try:
        from config import ADSETS_OLD
        for city, adsets in ADSETS_OLD.items():
            for adset_type, adset_id in adsets.items():
                if adset_id not in adset_map:
                    adset_map[adset_id] = (city, adset_type)
    except (ImportError, AttributeError):
        pass

    # ADSETS_EXTRA (ADV+ и т.д.) — всегда перезаписывают
    try:
        from config import ADSETS_EXTRA
        for city, adsets in ADSETS_EXTRA.items():
            for adset_type, adset_id in adsets.items():
                adset_map[adset_id] = (city, adset_type)
    except (ImportError, AttributeError):
        pass

    return adset_map


def get_all_ads(adset_map: dict, extra_fields: str = "") -> dict:
    """Все объявления из наших адсетов (1 запрос + пагинация).
    extra_fields: дополнительные поля, например 'campaign{name},adset{name}'.

    Адаптивный page_limit: при ошибке FB «reduce the amount of data» (code 1)
    уменьшаем limit вдвое и повторяем ту же страницу. Пагинация идёт через
    cursor (paging.cursors.after), а не по paging.next — это позволяет менять
    limit между страницами.
    """
    _PAGE_LIMIT_MIN = 10      # минимум перед FBApiError

    ads_by_id = {}
    base_fields = "id,name,status,effective_status,created_time,adset_id"
    fields = f"{base_fields},{extra_fields}" if extra_fields else base_fields
    base_url = f"{API}/act_{get_fb_account_id()}/ads"
    access_token = get_fb_token()

    # Стартуем с последнего успешного limit — экономим запросы в выжатых кабинетах
    page_limit = _last_good_page_limit["value"]
    cursor_after = None  # курсор для следующей страницы

    while True:
        # Параметры запроса: cursor-based пагинация вместо paging.next
        params = {
            "access_token": access_token,
            "fields": fields,
            "effective_status": json.dumps(["ACTIVE", "PAUSED"]),
            "limit": page_limit,
        }
        if cursor_after:
            params["after"] = cursor_after

        resp = _throttled_get(base_url, params=params)

        # Ошибка «слишком много данных» — снижаем limit и повторяем страницу
        if _is_reduce_data_error(resp):
            new_limit = page_limit // 2
            if new_limit < _PAGE_LIMIT_MIN:
                raise FBApiError(
                    f"FB API ошибка: {resp.status_code} {resp.text[:200]}",
                    resp.status_code,
                )
            _logger.warning(
                "get_all_ads: FB отбил страницу (reduce data) — снижаю limit до %d",
                new_limit,
            )
            page_limit = new_limit
            continue  # повторяем ту же страницу с меньшим limit

        if resp.status_code != 200:
            # Первая страница — фатальная ошибка; остальные — логируем и прерываем
            if cursor_after is None and not ads_by_id:
                raise FBApiError(
                    f"FB API ошибка: {resp.status_code} {resp.text[:200]}",
                    resp.status_code,
                )
            _logger.warning(
                "get_all_ads: FB API пагинация %d — возвращаем %d объявлений",
                resp.status_code,
                len(ads_by_id),
            )
            break

        data = resp.json()
        _collect_ads(data, ads_by_id, adset_map, parse_extra=bool(extra_fields))

        # Запоминаем текущий limit как успешный — следующий вызов стартует с него
        _last_good_page_limit["value"] = page_limit

        # Получаем cursor для следующей страницы
        paging = data.get("paging", {})
        cursors = paging.get("cursors", {})
        next_cursor = cursors.get("after")

        # Если нет next или нет следующего курсора — пагинация завершена
        if not paging.get("next") or not next_cursor:
            break

        cursor_after = next_cursor

    return ads_by_id


def _classify_objective(optimization_goal: str, destination_type: str) -> str:
    """Классифицирует тип цели объявления: 'leadform' или 'site' (или '').

    Лид-форма определяется по destination_type=ON_AD (форма прямо в объявлении)
    или optimization_goal in (LEAD_GENERATION, QUALITY_LEAD).
    Сайт — когда объявление ведёт на внешний URL.
    """
    goal = (optimization_goal or "").upper()
    dest = (destination_type or "").upper()

    # ON_AD = форма прямо в объявлении (FB Instant Form), независимо от goal
    if dest == "ON_AD":
        return "leadform"

    # FB-новые цели для лидогенерации
    if goal in {"LEAD_GENERATION", "QUALITY_LEAD"}:
        return "leadform"

    # Ведёт на внешний URL / сайт
    if dest in {"WEBSITE", "WEBSITE_LANDING_PAGE", "UNDEFINED"} and goal:
        return "site"

    # Прочие цели без явного destination_type (LINK_CLICKS, LANDING_PAGE_VIEWS, OFFSITE_CONVERSIONS)
    if goal:
        return "site"

    return ""


def _collect_ads(data: dict, ads_by_id: dict, adset_map: dict, parse_extra: bool = False):
    """Собирает объявления из ответа API, фильтруя по нашим адсетам."""
    for ad in data.get("data", []):
        adset_id = ad.get("adset_id")
        if adset_id not in adset_map:
            continue
        city, adset_type = adset_map[adset_id]
        entry = {
            "name": ad["name"],
            "status": ad["status"],
            "effective_status": ad.get("effective_status", ad["status"]),  # реальный статус с учётом родителей
            "created_time": ad["created_time"],
            "city": city,
            "adset_type": adset_type,
        }
        if parse_extra:
            entry["adset_id"] = adset_id
            entry["campaign_name"] = (ad.get("campaign") or {}).get("name", "")
            adset_obj = ad.get("adset") or {}
            entry["adset_name"] = adset_obj.get("name", "")
            entry["ad_objective"] = _classify_objective(
                adset_obj.get("optimization_goal", ""),
                adset_obj.get("destination_type", ""),
            )
            creative = ad.get("creative") or {}
            entry["creative_id"] = creative.get("id", "")
            # HD картинка: image_url (оригинал) > object_story_spec > thumbnail_url (маленький)
            spec = creative.get("object_story_spec") or {}
            video_img = (spec.get("video_data") or {}).get("image_url", "")
            link_img = (spec.get("link_data") or {}).get("picture", "")
            entry["thumbnail_url"] = (
                creative.get("image_url")
                or video_img
                or link_img
                or creative.get("thumbnail_url", "")
            )
            entry["image_url"] = creative.get("image_url", "")
            entry["body"] = creative.get("body", "")
        ads_by_id[ad["id"]] = entry
