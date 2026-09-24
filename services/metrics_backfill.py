"""
Резюмируемый бэкфилл дневных метрик объявлений → ad_daily_metrics.

Курсор прогресса — поле cursor_date (YYYY-MM-DD, следующая дата к обработке).
Состояние хранится в backfill_state[key='metrics_backfill'] и зеркалится в
data/metrics_backfill_state.json (для удобства просмотра вайб-кодером).

При rate-limit или любом неполном Graph-ответе курсор НЕ двигается — следующий
вызов повторит тот же диапазон.
Один HTTP-вызов обрабатывает не более max_windows окон ИЛИ max_seconds секунд —
это держит запрос коротким (без блокировки единственного воркера/502).

Legacy-строки с lead_semantics_version=1 не переинтерпретируются локальным SQL.
Повторный запуск заново получает actions из Graph API, разбирает их обычным
парсером metrics_snapshot и UPSERT-ом заменяет точную пару (ad_id, date) на v2.
Курсор делает этот refetch резюмируемым. Запуск миграции и production-backfill —
отдельные операционные действия, этот модуль сам их не инициирует.
"""

import calendar
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from agent.fb_common import FBApiError
from services.fb_token_provider import fb_account, offline_account_context
from services.metrics_snapshot import _fetch_created_times, _fetch_daily_rows, _upsert_daily, _TZ_LOCAL
from services.creative_intelligence import _get_connection
from services.creative_backfill import _get_state_by_key, _save_state_by_key

logger = logging.getLogger(__name__)

# Начало исторического диапазона (включительно). Зафиксировано задачей.
BACKFILL_START_DATE = date(2025, 11, 1)

# Старт для маршрутизированных кабинетов карты роутинга: раньше этой даты
# в кабинете тянуть нечего. Значение — пример: дата первых расходов
# кабинета — задайте свою.
_ACCOUNT_START_DATES = {
    "29716040622546856": date(2026, 1, 1),
}

# Ключ в таблице backfill_state
_STATE_KEY = "metrics_backfill"

# Путь к зеркальному JSON-снимку состояния (рядом с data/decisions.db)
_STATE_JSON_PATH = Path(__file__).parent.parent / "data" / "metrics_backfill_state.json"


def _normalize_account_id(account_id: str | None) -> str | None:
    """None или дефолтный кабинет (config.FB_ACCOUNT_ID) → None (legacy-путь),
    иначе — цифровой account_id без префикса act_."""
    normalized = str(account_id or "").replace("act_", "").strip()
    if not normalized:
        return None
    try:
        from config import FB_ACCOUNT_ID

        default_account = str(FB_ACCOUNT_ID or "").replace("act_", "").strip()
    except ImportError:
        default_account = ""
    if normalized == default_account:
        return None
    return normalized


def _state_key_for(account_id: str | None) -> str:
    """Ключ backfill_state: legacy «metrics_backfill» у дефолтного кабинета,
    «metrics_backfill:<account_id>» у маршрутизированного."""
    return _STATE_KEY if account_id is None else f"{_STATE_KEY}:{account_id}"


def _state_json_path_for(account_id: str | None) -> Path:
    """JSON-зеркало состояния: свой файл на маршрутизированный кабинет."""
    if account_id is None:
        return _STATE_JSON_PATH
    return _STATE_JSON_PATH.with_name(f"metrics_backfill_state_{account_id}.json")

# Дефолтное состояние бэкфилла метрик.
# cursor_date — следующая дата к обработке (YYYY-MM-DD); None = не начато.
# months_done оставляем для обратной совместимости со старым /status-эндпоинтом.
_DEFAULT_STATE = {
    "months_done": [],
    "cursor_date": None,
    "last_run_at": None,
    "rate_limited_at": None,
}

# Размер одного окна (дней) при курсорном бэкфилле
_CURSOR_WINDOW_DAYS = 5
# Лимит времени одного вызова run_metrics_backfill_increment (секунды)
_DEFAULT_MAX_SECONDS = 120
# Лимит окон одного вызова run_metrics_backfill_increment
_DEFAULT_MAX_WINDOWS = 3


def _month_key(d: date) -> str:
    """Ключ месяца формата 'YYYY-MM'. Пример: date(2025,11,1) -> '2025-11'."""
    return d.strftime("%Y-%m")


def _month_bounds(month_key: str) -> tuple[str, str]:
    """Границы календарного месяца [first_day, last_day] как ISO-строки 'YYYY-MM-DD'.

    Последний день месяца обрезается до 'сегодня по локальному времени' если месяц текущий
    (нельзя тянуть будущие дни). Пример: '2025-11' -> ('2025-11-01', '2025-11-30').
    """
    year, month = map(int, month_key.split("-"))
    first_day = date(year, month, 1)
    # Последний день месяца по календарю
    last_day_of_month = date(year, month, calendar.monthrange(year, month)[1])
    # Сегодня по локальному времени — нельзя тянуть будущие дни
    today_local = datetime.now(_TZ_LOCAL).date()
    last_day = min(last_day_of_month, today_local)
    return first_day.isoformat(), last_day.isoformat()


def _all_months() -> list[str]:
    """Список ключей месяцев от BACKFILL_START_DATE до текущего месяца (по локальному времени) включительно,
    от старого к свежему. Пример на 2026-06-16: ['2025-11','2025-12',...,'2026-06'].
    """
    today_local = datetime.now(_TZ_LOCAL).date()
    result = []
    current = date(BACKFILL_START_DATE.year, BACKFILL_START_DATE.month, 1)
    end = date(today_local.year, today_local.month, 1)
    while current <= end:
        result.append(_month_key(current))
        # Следующий месяц
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return result


def get_backfill_metrics_state(account_id: str | None = None) -> dict:
    """Читает состояние из backfill_state по ключу кабинета. Дефолт если нет строки:
        {"months_done": [], "last_run_at": None, "rate_limited_at": None}.
    months_done — список залитых month_key.
    account_id=None или дефолтный кабинет → legacy-ключ «metrics_backfill».
    """
    raw = _get_state_by_key(_state_key_for(_normalize_account_id(account_id)))
    if not raw:
        return dict(_DEFAULT_STATE)
    # Заполняем недостающие ключи дефолтами
    state = dict(_DEFAULT_STATE)
    state.update(raw)
    # Убеждаемся что months_done — список
    if not isinstance(state.get("months_done"), list):
        state["months_done"] = []
    return state


def save_backfill_metrics_state(state: dict, account_id: str | None = None) -> None:
    """UPSERT состояния в backfill_state + зеркалит в JSON рядом с data/decisions.db.
    JSON пишется атомарно (во временный файл рядом, затем os.replace). Ошибку записи JSON
    логируем как warning — БД-состояние первично, JSON только зеркало.
    """
    normalized = _normalize_account_id(account_id)
    # Сохраняем в БД через существующий хелпер
    _save_state_by_key(_state_key_for(normalized), state)

    # Атомарная запись JSON-зеркала
    json_path = _state_json_path_for(normalized)
    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = json_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(json_path))
    except Exception as exc:
        logger.warning("save_backfill_metrics_state: не удалось записать JSON-зеркало — %s", exc)


def reset_backfill_metrics_state(account_id: str | None = None) -> None:
    """Сбрасывает months_done=[]. Для ручного полного перезапуска бэкфилла."""
    save_backfill_metrics_state(dict(_DEFAULT_STATE), account_id=account_id)
    logger.info("metrics_backfill_state сброшен (account=%s)", account_id or "default")


# Размер подокна по умолчанию (дней). 5 дней = 20-25% месяца — проверено что FB отдаёт.
_WINDOW_SIZE_DEFAULT = 5
# Минимальный размер подокна; если и оно не читается, диапазон incomplete.
_WINDOW_SIZE_MIN = 1


def _is_true_rate_limit_error(exc: FBApiError) -> bool:
    """Настоящий rate-limit: 429 или FB code 4/17/32/80003/80004.
    Код 1 (reduce data) — НЕ rate-limit, это слишком большой запрос.
    """
    return exc.status_code in (429, 4, 17, 32, 80003, 80004)


def _is_reduce_data_exc(exc: FBApiError) -> bool:
    """FB 'reduce the amount of data' — status_code=1 (как кидает _fetch_daily_rows)."""
    return exc.status_code == 1


def backfill_range(date_from: str, date_to: str, window_size: int = _WINDOW_SIZE_DEFAULT) -> dict:
    """Догружает дневные метрики за период [date_from, date_to] (ISO 'YYYY-MM-DD', включительно).

    Разбивает диапазон на подокна по window_size дней (последнее — остаток).
    Для каждого подокна вызывает _fetch_daily_rows и делает UPSERT сразу —
    так частичный прогресс сохраняется при прерывании. Это именно Graph refetch:
    _fetch_daily_rows применяет текущий Meta lead parser, а _upsert_daily записывает
    lead_semantics_version=2 и lead_parse_status. Legacy-числа из БД не используются.

    При ошибке reduce-data (code 1) — уменьшает текущее подокно вдвое и повторяет.
    При настоящем rate-limit (code 17/429 и т.д.) — rate_limited=True, останавливается.
    Если даже 1-дневное подокно даёт reduce-data — диапазон incomplete; день
    не пропускается и внешний курсор остаётся на начале окна.

    Returns: {"date_from","date_to","days":N,"fetched":int,"upserted":int,
              "rate_limited":bool,"complete":bool,"error":str|None}.
    """
    try:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
    except ValueError:
        # Некорректный формат — возвращаем пустой результат
        return {
            "date_from": date_from,
            "date_to": date_to,
            "days": 0,
            "fetched": 0,
            "upserted": 0,
            "rate_limited": False,
            "complete": False,
            "error": "invalid_date_range",
        }

    # Пустой период — не ошибка, просто no-op
    if d_from > d_to:
        return {
            "date_from": date_from,
            "date_to": date_to,
            "days": 0,
            "fetched": 0,
            "upserted": 0,
            "rate_limited": False,
            "complete": True,
            "error": None,
        }

    total_fetched = 0
    total_upserted = 0
    days_count = 0

    # Получаем карту created_time ОДИН РАЗ на весь диапазон —
    # не тянем список объявлений заново на каждое подокно (перформанс-фикс)
    created_times = _fetch_created_times()

    # Итерируем подокнами. current_win_size адаптируется при reduce-data.
    current_win_size = window_size
    window_start = d_from

    while window_start <= d_to:
        window_end = min(window_start + timedelta(days=current_win_size - 1), d_to)

        try:
            rows_dict = _fetch_daily_rows(
                window_start.isoformat(), window_end.isoformat(), created_times=created_times
            )

            # Успех — сохраняем и двигаем курсор
            rows = list(rows_dict.values())
            total_fetched += len(rows)
            if rows:
                conn = _get_connection()
                try:
                    upserted = _upsert_daily(conn, rows)
                    total_upserted += upserted
                finally:
                    conn.close()

            days_count += (window_end - window_start).days + 1
            window_start = window_end + timedelta(days=1)
            # После успеха восстанавливаем размер окна
            current_win_size = window_size

        except FBApiError as exc:
            if _is_true_rate_limit_error(exc):
                # Настоящий rate-limit — останавливаемся; частичный прогресс уже в БД
                logger.warning(
                    "backfill_range: настоящий rate-limit на %s…%s — %s",
                    window_start, window_end, exc,
                )
                return {
                    "date_from": date_from,
                    "date_to": date_to,
                    "days": days_count,
                    "fetched": total_fetched,
                    "upserted": total_upserted,
                    "rate_limited": True,
                    "complete": False,
                    "error": str(exc),
                }

            if not _is_reduce_data_exc(exc):
                # Неожиданная ошибка (напр. 400 code 100 — неверное поле, или любой FBApiError
                # не относящийся к rate-limit и reduce-data) — логируем и останавливаем
                # подокно/месяц. НЕ зацикливаемся, НЕ дробим окно впустую.
                logger.error(
                    "backfill_range: неожиданная ошибка FB (code=%s) на %s…%s — %s. "
                    "Останавливаем обработку месяца.",
                    exc.status_code, window_start, window_end, exc,
                )
                return {
                    "date_from": date_from,
                    "date_to": date_to,
                    "days": days_count,
                    "fetched": total_fetched,
                    "upserted": total_upserted,
                    "rate_limited": False,
                    "complete": False,
                    "error": str(exc),
                }

            # Ошибка reduce-data (code 1) — уменьшаем окно вдвое
            span = (window_end - window_start).days + 1
            new_size = max(_WINDOW_SIZE_MIN, span // 2)

            if new_size >= span:
                # Уже минимальное окно: не выдаём частичный диапазон за complete.
                logger.error(
                    "backfill_range: reduce-data на %s за 1 день — incomplete без продвижения",
                    window_start,
                )
                return {
                    "date_from": date_from,
                    "date_to": date_to,
                    "days": days_count,
                    "fetched": total_fetched,
                    "upserted": total_upserted,
                    "rate_limited": False,
                    "complete": False,
                    "error": str(exc),
                }
            else:
                logger.warning(
                    "backfill_range: reduce-data на %s…%s → уменьшаем окно %d→%d дн.",
                    window_start, window_end, span, new_size,
                )
                current_win_size = new_size
                # window_start не меняем — повторяем тот же старт с меньшим окном

        except Exception as exc:
            logger.error(
                "backfill_range: incomplete на %s…%s — %s: %s",
                window_start,
                window_end,
                type(exc).__name__,
                exc,
            )
            return {
                "date_from": date_from,
                "date_to": date_to,
                "days": days_count,
                "fetched": total_fetched,
                "upserted": total_upserted,
                "rate_limited": False,
                "complete": False,
                "error": str(exc),
            }

    return {
        "date_from": date_from,
        "date_to": date_to,
        "days": days_count,
        "fetched": total_fetched,
        "upserted": total_upserted,
        "rate_limited": False,
        "complete": True,
        "error": None,
    }


def _get_earliest_date(account_id: str | None = None) -> date:
    """Самая ранняя дата диапазона бэкфилла: BACKFILL_START_DATE для дефолтного
    кабинета, у маршрутизированного — своя дата из _ACCOUNT_START_DATES."""
    if account_id is not None and account_id in _ACCOUNT_START_DATES:
        return _ACCOUNT_START_DATES[account_id]
    return BACKFILL_START_DATE


def _get_latest_date() -> date:
    """Возвращает вчера по локальному времени (верхняя граница бэкфилла).
    Не тянем сегодня — данные за текущий день неполные.
    """
    today_local = datetime.now(_TZ_LOCAL).date()
    return today_local - timedelta(days=1)


def run_metrics_backfill_increment(
    max_seconds: float = _DEFAULT_MAX_SECONDS,
    max_windows: int = _DEFAULT_MAX_WINDOWS,
    window_days: int = _CURSOR_WINDOW_DAYS,
    account_id: str | None = None,
) -> dict:
    """Курсорный инкремент: обрабатывает окна дат от cursor_date вперёд.

    За один вызов обрабатывает не более max_windows окон ИЛИ max_seconds секунд —
    это держит HTTP-запрос коротким и не блокирует единственный воркер.

    account_id — кабинет карты роутинга: None/дефолтный → legacy-поведение
    (cabinet_a, ключ «metrics_backfill»), иначе свой курсор
    «metrics_backfill:<account_id>» и FB-запросы под контекстом кабинета.

    cursor_date инициализируется в earliest (_get_earliest_date кабинета) при
    первом запуске. После каждого complete окна (включая валидно пустое)
    cursor_date двигается вперёд и state сохраняется немедленно. Любой partial
    page/chunk/parser failure оставляет курсор на начале окна и завершает
    вызов с stopped_reason="fb_error".

    Returns: {"cursor_date","fetched","upserted","rate_limited","done",
              "windows_processed","stopped_reason"}.
    stopped_reason: "done" | "rate_limited" | "max_windows" | "max_seconds" | "fb_error"
    """
    normalized_account = _normalize_account_id(account_id)
    # Fail-closed до любых FB-вызовов: незарегистрированный кабинет — отказ.
    account_context = (
        offline_account_context(normalized_account) if normalized_account else None
    )
    state = get_backfill_metrics_state(normalized_account)

    earliest = _get_earliest_date(normalized_account)
    latest = _get_latest_date()

    # Инициализируем курсор если не задан
    cursor_str = state.get("cursor_date")
    if not cursor_str:
        cursor = earliest
    else:
        try:
            cursor = date.fromisoformat(cursor_str)
        except ValueError:
            logger.warning("run_metrics_backfill_increment: некорректный cursor_date='%s', сбрасываем", cursor_str)
            cursor = earliest

    # Уже всё сделано
    if cursor > latest:
        return {
            "cursor_date": cursor.isoformat(),
            "fetched": 0,
            "upserted": 0,
            "rate_limited": False,
            "done": True,
            "windows_processed": 0,
            "stopped_reason": "done",
        }

    start_mono = time.monotonic()
    total_fetched = 0
    total_upserted = 0
    windows_processed = 0
    stopped_reason = "max_windows"
    while cursor <= latest:
        # Проверяем лимиты перед каждым окном
        elapsed = time.monotonic() - start_mono
        if elapsed >= max_seconds:
            stopped_reason = "max_seconds"
            break
        if windows_processed >= max_windows:
            stopped_reason = "max_windows"
            break

        # Границы текущего окна
        window_end = min(cursor + timedelta(days=window_days - 1), latest)

        try:
            # Используем backfill_range — он устойчив к reduce-data, пустым
            # диапазонам и нефатальным ошибкам FB. FB-запросы идут под
            # контекстом кабинета (None = дефолтный оффлайн cabinet_a).
            with fb_account(account_context):
                r = backfill_range(cursor.isoformat(), window_end.isoformat())

            if r["rate_limited"]:
                # Настоящий rate-limit — курсор НЕ двигаем, повторим позже
                now_iso = datetime.now(timezone.utc).isoformat()
                state["rate_limited_at"] = now_iso
                save_backfill_metrics_state(state, account_id=normalized_account)
                logger.warning(
                    "run_metrics_backfill_increment: rate-limit на %s..%s",
                    cursor, window_end,
                )
                stopped_reason = "rate_limited"
                return {
                    "cursor_date": cursor.isoformat(),
                    "fetched": total_fetched,
                    "upserted": total_upserted,
                    "rate_limited": True,
                    "done": False,
                    "windows_processed": windows_processed,
                    "stopped_reason": "rate_limited",
                }

            if not r.get("complete", True):
                logger.error(
                    "run_metrics_backfill_increment: incomplete окно %s..%s — %s. "
                    "Курсор не изменён.",
                    cursor,
                    window_end,
                    r.get("error") or "unknown_error",
                )
                return {
                    "cursor_date": cursor.isoformat(),
                    "fetched": total_fetched + r.get("fetched", 0),
                    "upserted": total_upserted + r.get("upserted", 0),
                    "rate_limited": False,
                    "done": False,
                    "windows_processed": windows_processed,
                    "stopped_reason": "fb_error",
                    "error": r.get("error"),
                }

            # Успех или пустое окно (fetched=0 при праздниках — тоже легитимно).
            # В ЛЮБОМ случае двигаем курсор вперёд.
            total_fetched += r["fetched"]
            total_upserted += r["upserted"]

            cursor = window_end + timedelta(days=1)
            windows_processed += 1

            now_iso = datetime.now(timezone.utc).isoformat()
            state["cursor_date"] = cursor.isoformat()
            state["last_run_at"] = now_iso
            state["rate_limited_at"] = None
            save_backfill_metrics_state(state, account_id=normalized_account)

            logger.info(
                "run_metrics_backfill_increment: окно %s..%s → fetched=%d upserted=%d cursor→%s",
                (cursor - timedelta(days=window_days)).isoformat(),
                window_end.isoformat(),
                r["fetched"], r["upserted"], cursor.isoformat(),
            )

        except Exception as exc:
            # Неожиданное исключение тоже не даёт права продвигать курсор.
            logger.error(
                "run_metrics_backfill_increment: неожиданное исключение на %s..%s — %s: %s. "
                "Курсор не изменён.",
                cursor,
                window_end,
                type(exc).__name__,
                exc,
            )
            return {
                "cursor_date": cursor.isoformat(),
                "fetched": total_fetched,
                "upserted": total_upserted,
                "rate_limited": False,
                "done": False,
                "windows_processed": windows_processed,
                "stopped_reason": "fb_error",
                "error": str(exc),
            }

    # Проверяем: всё ли обработано после цикла
    done = cursor > latest
    if done:
        stopped_reason = "done"

    return {
        "cursor_date": cursor.isoformat(),
        "fetched": total_fetched,
        "upserted": total_upserted,
        "rate_limited": False,
        "done": done,
        "windows_processed": windows_processed,
        "stopped_reason": stopped_reason,
    }


def run_metrics_backfill(months_back: int | None = None) -> dict:
    """Вызывает run_metrics_backfill_increment пока не done или не rate_limited.

    months_back — сохранён для обратной совместимости эндпоинта (теперь игнорируется,
    логика управляется max_seconds/max_windows внутри increment).
    Останавливается после первого вызова — серия вызовов эндпоинта докрутит остальное.

    Returns: {"increments_run","fetched_total","upserted_total","rate_limited","done",
              "cursor_date","months_filled"}.
    """
    r = run_metrics_backfill_increment()
    return {
        "increments_run": 1,
        "months_filled": [],        # курсорная логика не оперирует месяцами
        "fetched_total": r.get("fetched", 0),
        "upserted_total": r.get("upserted", 0),
        "rate_limited": r.get("rate_limited", False),
        "done": r.get("done", False),
        "cursor_date": r.get("cursor_date"),
        "stopped_reason": r.get("stopped_reason"),
        "complete": r.get("stopped_reason") not in ("fb_error", "rate_limited"),
        "error": r.get("error"),
    }
