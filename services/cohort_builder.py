"""
Сборщик недельных когорт объявлений → таблица ad_weekly_cohorts.

Зачем: средние за месяц скрывают траекторию. «ROMI 500% за месяц» одинаково
выглядит у выдыхающейся звезды (первая неделя 1500%, последняя 50%) и у
разгоняющейся (было 50%, стало 200%). Недельная когорта — строка на пару
(объявление, неделя) — даёт решениям динамику вместо усреднения.

Волна 1: только данные. Правил тренда и встраивания в решения здесь НЕТ.

Волна 4 добавила выручку и ROMI. В волне 1 их не было сознательно: не был
известен лаг «лид → оплата», а services/cdp_client.get_payments фильтрует
платежи по doc_date — дате платёжного ДОКУМЕНТА. Приписать кассу недели W
расходу недели W значит вернуть то самое усреднение, от которого уходим:
деньги пришли за лид, приведённый месяцем раньше. Замер лага на исторических
платежах (сопоставление по contract_number) показал: медиана — несколько дней,
к 14-му дню приходит около 90% оплат, к 30-му — почти все. Лаг короткий —
недельный ROMI считать можно, но ТОЛЬКО когортно (см. REVENUE_HORIZON_DAYS ниже).

Источники:
  * Расход/показы/FB-лиды — напрямую из Facebook асинхронным отчётом
    (POST /act_X/insights → report_run_id → GET /{id}/insights). Таблица
    ad_daily_metrics как источник истории НЕПРИГОДНА: в ней дыры почти в
    половину дней, а старые строки записаны legacy-семантикой лидов v1
    (двойной счёт) и несравнимы с v2.
  * AMO-лиды и квалы — integrations.amo.get_leads_window по окну создания
    лида; разметка объявления — _extract_fb_fields, квал — _is_qualified.
  * Выручка — платежи ERP через services.cdp_client.get_payments, привязка к
    объявлению по contract_number = lead_id AMO. Карта «лид → объявление»
    строится ИЗ ТОЙ ЖЕ выгрузки лидов, что даёт amo_leads. Модуль
    services/cdp_payments.py и его дисковый кеш data/cdp_payments_lead_cache
    здесь НЕ используются намеренно: кеш отравлен (большинство записей «не найдено»,
    и код их никогда не перепроверяет), а нам нужна привязка ровно за то
    окно, за которое уже выгружены лиды.
  * Курс USD→LCY — services.exchange_rate.get_usd_to_lcy_on(дата недели):
    строгий датированный вход без подстановок. Нет курса — нет ROMI (NULL),
    но НЕ ROMI по сегодняшнему курсу: историческая метрика обязана быть
    воспроизводимой.

Границы недели. Неделя ISO, начало — понедельник. Лид попадает в неделю по
дате СОЗДАНИЯ лида в локальной таймзоне. Расход запрашивается по тем же
календарным датам, но FB трактует time_range в таймзоне кабинета
(Лос-Анджелес), поэтому сутки расхода сдвинуты относительно суток лидов
примерно на 12 часов. Это известное свойство кабинета, а не ошибка:
недельное окно (7 суток) настолько шире сдвига, что тренд по неделям от него
не переворачивается. Закрытость недели проверяется по календарю кабинета —
пока его день не перевалил за воскресенье, неделя не закрыта.

Полнота — fail-closed. Отсутствующие данные пишутся как NULL, НИКОГДА как 0
(та же дисциплина, что у правила NULL_COERCED_TO_ZERO в
services/approval_rules.py):
  * days_expected — сколько дней недели попало в запрошенный диапазон;
  * days_covered — за сколько из них FB реально отдал данные;
  * дыра (days_covered < days_expected) обнуляет доверие к суммам недели:
    расход/показы/FB-лиды пишутся NULL, а не занижённое число;
  * comparable=1 — только у закрытой недели, целиком попавшей в диапазон и
    целиком полученной (7 = days_covered = days_expected). Иначе 0 и внятная
    причина в not_comparable_reason;
  * выручка недозревшей когорты — NULL при revenue_mature=0, а не «пока ноль
    оплат»; недоступная ERP — NULL при revenue_mature=1 («горизонт закрыт, но
    денег мы не знаем»), эти два состояния различимы намеренно.
Rate limit FB и любой неполный ответ — честная ошибка без записи, а не
частичные данные, записанные как полные. Недоступность ERP — исключение из
этого правила: она гасит только выручку (NULL + счётчик в сводке и warning в
логе) и не отменяет уже посчитанные квалы, ради которых волны 1-2 и делались.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.fb_common import (
    API,
    FBApiError,
    _is_reduce_data_error,
    _throttled_get,
    _throttled_post,
)
from integrations.amo import _extract_fb_fields, _is_qualified, get_leads_window
from services.cdp_client import CdpError, get_payments_strict
from services.creative_briefs import extract_city
from services.creative_intelligence import _get_connection
from services.exchange_rate import get_usd_to_lcy_on
from services.fb_token_provider import get_fb_account_id, get_fb_token
from services.meta_lead_actions import parse_meta_lead_actions
from services.metrics_backfill import _is_reduce_data_exc, _is_true_rate_limit_error

logger = logging.getLogger(__name__)

# Версия сборщика. Повышать при любом изменении смысла колонок — по ней видно,
# какие строки собраны старой логикой и подлежат пересчёту.
# v2 — волна 4: у строки появились выручка когорты, курс и ROMI.
BUILDER_VERSION = 2

# Локальная таймзона (UTC+5 по умолчанию, настраивается этой константой) — в ней считаем
# календарные дни created_at лида.
_TZ_LOCAL = timezone(timedelta(hours=5))

# Таймзона рекламного кабинета: именно в ней FB трактует time_range отчёта.
try:
    _TZ_ACCOUNT = ZoneInfo("America/Los_Angeles")
except ZoneInfoNotFoundError:  # pragma: no cover — на сервере tzdata есть
    _TZ_ACCOUNT = timezone(timedelta(hours=-8))

# Поля асинхронного отчёта. adset_id/adset_name обязательны: привязка
# объявления к адсету фиксируется на пару (объявление, неделя).
_FB_REPORT_FIELDS = (
    "ad_id,ad_name,adset_id,adset_name,date_start,spend,impressions,actions"
)
# Пауза между опросами статуса отчёта и потолок ожидания одного отчёта.
_FB_POLL_INTERVAL_SEC = 10.0
_FB_POLL_MAX_SEC = 900.0
# Размер страницы выдачи готового отчёта.
_FB_PAGE_LIMIT = 500
# Минимальная ширина окна отчёта при дроблении на reduce-data (дни).
_FB_MIN_WINDOW_DAYS = 1
# Ширина одного окна выгрузки AMO (дни): компромисс между числом запросов и
# временем одной выгрузки.
_AMO_WINDOW_DAYS = 30

# ---------------------------------------------------------------------------
# Горизонт когортной выручки
# ---------------------------------------------------------------------------
# Сколько дней после СОЗДАНИЯ ЛИДА собираются его оплаты. Это часть определения
# метрики, а не тюнинг: revenue_lcy за 14 и за 30 дней — разные числа, поэтому
# горизонт пишется в строку недели (revenue_horizon_days) и сравнивать можно
# только недели с одинаковым горизонтом.
#
# Почему 14, а не 30. Замер лага «лид → оплата» на исторических платежах
# (сопоставление по contract_number): к 14-му дню приходит около 90% оплат, к
# 30-му — почти все. То есть 30 дней дают лишь несколько п.п. полноты, но
# переносят зрелость когорты с 20 дней после её понедельника на 36 — почти на
# месяц. Тренд смотрит на две
# соседние закрытые недели; при H=30 сравнивались бы недели полуторамесячной
# давности, и сигнал «объявление выдохлось» приходил бы после того, как деньги
# уже потрачены. Недобор (порядка 10%) системный и одинаковый для всех когорт,
# поэтому СРАВНЕНИЕ недель между собой он не искажает — искажает только
# абсолютный уровень ROMI, который занижен примерно на эту долю.
#
# Известное ограничение горизонта: возврат, пришедший позже H дней от лида, в
# выручку когорты не попадёт. По замеру таких хвостов единицы, но при смене H
# это первое, что стоит перепроверить.
REVENUE_HORIZON_DAYS = 14

# Запас по краям окна запроса платежей: doc_date ERP и created_at AMO живут в
# одном календаре (локальное время), но день на границе не должен теряться из-за
# округления суток на чужой стороне. Всё, что запрошено сверх горизонта,
# отсеивается пер-лидной проверкой и попадает в счётчик payments_outside_horizon.
_REVENUE_QUERY_MARGIN_DAYS = 1

# Коды причин несравнимости. Пишутся в not_comparable_reason через ';'.
REASON_WEEK_NOT_CLOSED = "WEEK_NOT_CLOSED"
REASON_WEEK_PARTIAL_RANGE = "WEEK_PARTIAL_RANGE"
REASON_FB_DAYS_MISSING = "FB_DAYS_MISSING"


class CohortBuildError(RuntimeError):
    """Сборка когорт невозможна — данные неполные, писать нечего."""


class CohortRateLimitError(CohortBuildError):
    """Facebook ограничил запросы. Частичные данные не пишутся."""


@dataclass(slots=True)
class _FbWeekAgg:
    """Агрегат FB по паре (объявление, неделя)."""

    spend_usd: float = 0.0
    impressions: int = 0
    fb_leads: int = 0
    # Дата последнего дня недели с данными — по ней берём привязку к адсету.
    last_day: date | None = None
    adset_id: str | None = None
    adset_name: str | None = None
    ad_name: str | None = None
    days_with_rows: set[date] = field(default_factory=set)


@dataclass(slots=True)
class _AmoWeekAgg:
    """Агрегат AMO по паре (объявление, неделя)."""

    amo_leads: int = 0
    quals: int = 0
    ad_name: str | None = None


@dataclass(slots=True)
class _LeadRef:
    """Куда относится лид: объявление, его неделя и дата создания.

    Дата создания нужна пер-лидно, а не пер-недельно: горизонт выручки
    отсчитывается от каждого лида, а не от понедельника его недели.
    """

    ad_id: str
    week_start: date
    created_day: date


@dataclass(slots=True)
class _RevenueAgg:
    """Агрегат выручки по паре (объявление, неделя создания лида)."""

    revenue_lcy: float = 0.0
    payments: int = 0


# ---------------------------------------------------------------------------
# Календарь
# ---------------------------------------------------------------------------

def _as_date(value: date | str) -> date:
    """Принимает date или ISO-строку 'YYYY-MM-DD'."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def week_start_of(day: date) -> date:
    """Понедельник ISO-недели, в которую попадает day."""
    return day - timedelta(days=day.weekday())


def _week_days(week_start: date) -> list[date]:
    """Семь дат недели от понедельника к воскресенью."""
    return [week_start + timedelta(days=offset) for offset in range(7)]


def _account_today(now: datetime | None = None) -> date:
    """Сегодняшняя дата по календарю рекламного кабинета (Лос-Анджелес)."""
    if now is None:
        return datetime.now(_TZ_ACCOUNT).date()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_LOCAL)
    return now.astimezone(_TZ_ACCOUNT).date()


def _local_today(now: datetime | None = None) -> date:
    """Сегодняшняя дата по локальному календарю — в нём живут лиды и платежи ERP."""
    if now is None:
        return datetime.now(_TZ_LOCAL).date()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_LOCAL)
    return now.astimezone(_TZ_LOCAL).date()


def _is_week_closed(week_start: date, now: datetime | None = None) -> bool:
    """Неделя закрыта, если её воскресенье уже позади по календарю кабинета."""
    return week_start + timedelta(days=6) < _account_today(now)


def _is_revenue_mature(
    week_start: date,
    horizon_days: int,
    now: datetime | None = None,
) -> bool:
    """Прошёл ли горизонт выручки у САМОГО ПОЗДНЕГО лида недели.

    Считается от воскресенья (week_start + 6), а не от понедельника: горизонт
    отсчитывается от каждого лида, и пока у воскресного лида не прошло H дней,
    когорта дозревает. Мерка от понедельника («week_start + H <= сегодня»)
    объявила бы неделю зрелой, когда её последние лиды прожили H − 6 дней, и
    ROMI такой недели был бы систематически занижен — ровно та подмена, ради
    отказа от которой затевалась когортная выручка.

    Календарь — локальный: и created_at лида, и doc_date платежа живут в нём.
    """
    return week_start + timedelta(days=6 + horizon_days) <= _local_today(now)


def _month_windows(since: date, until: date) -> list[tuple[date, date]]:
    """Режет [since, until] на календарно-месячные окна (последнее — остаток)."""
    windows: list[tuple[date, date]] = []
    cursor = since
    while cursor <= until:
        if cursor.month == 12:
            next_month_start = date(cursor.year + 1, 1, 1)
        else:
            next_month_start = date(cursor.year, cursor.month + 1, 1)
        window_end = min(next_month_start - timedelta(days=1), until)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


# ---------------------------------------------------------------------------
# Facebook: асинхронный отчёт уровня объявления с разбивкой по дням
# ---------------------------------------------------------------------------

def _fb_payload(response, context: str) -> dict:
    """Валидирует ответ Graph. Reduce-data отдаёт status_code=1, как в снапшоте."""
    if response.status_code == 200:
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 — важен сам факт битого JSON
            raise FBApiError(f"FB {context}: невалидный JSON") from exc
        if not isinstance(payload, dict):
            raise FBApiError(f"FB {context}: payload должен быть object")
        return payload
    if _is_reduce_data_error(response):
        raise FBApiError(f"FB {context}: слишком большой запрос", 1)
    raise FBApiError(
        f"FB {context}: HTTP {response.status_code}", response.status_code
    )


def _submit_async_report(since: date, until: date) -> str:
    """Ставит асинхронный отчёт insights и возвращает report_run_id."""
    account_id = get_fb_account_id()
    response = _throttled_post(
        f"{API}/act_{account_id}/insights",
        data={
            "access_token": get_fb_token(),
            "level": "ad",
            "time_increment": 1,
            "fields": _FB_REPORT_FIELDS,
            "time_range": json.dumps(
                {"since": since.isoformat(), "until": until.isoformat()}
            ),
        },
    )
    payload = _fb_payload(response, f"постановка отчёта {since}..{until}")
    report_run_id = payload.get("report_run_id")
    if not isinstance(report_run_id, (str, int)) or not str(report_run_id):
        raise FBApiError(f"FB отчёт {since}..{until}: нет report_run_id")
    return str(report_run_id)


def _await_report(report_run_id: str, context: str) -> None:
    """Ждёт 'Job Completed'. Провал, пропуск и таймаут — ошибка, не пустой отчёт."""
    waited = 0.0
    while True:
        response = _throttled_get(
            f"{API}/{report_run_id}",
            params={
                "access_token": get_fb_token(),
                "fields": "async_status,async_percent_completion",
            },
        )
        payload = _fb_payload(response, f"статус отчёта {context}")
        status = payload.get("async_status")
        if status == "Job Completed":
            return
        if status in ("Job Failed", "Job Skipped"):
            raise FBApiError(f"FB отчёт {context}: async_status={status}")
        if waited >= _FB_POLL_MAX_SEC:
            raise FBApiError(
                f"FB отчёт {context}: не завершился за {_FB_POLL_MAX_SEC:.0f}с"
            )
        time.sleep(_FB_POLL_INTERVAL_SEC)
        waited += _FB_POLL_INTERVAL_SEC


def _fetch_report_rows(report_run_id: str, context: str) -> list[dict]:
    """Читает готовый отчёт целиком. Частичная выдача недопустима."""
    url = f"{API}/{report_run_id}/insights"
    params: dict | None = {
        "access_token": get_fb_token(),
        "limit": _FB_PAGE_LIMIT,
    }
    rows: list[dict] = []
    page = 1
    while True:
        response = (
            _throttled_get(url, params=params)
            if params is not None
            else _throttled_get(url)
        )
        payload = _fb_payload(response, f"выдача отчёта {context}, page={page}")
        data = payload.get("data")
        if not isinstance(data, list):
            raise FBApiError(f"FB отчёт {context}: data должен быть list")
        for index, row in enumerate(data):
            if not isinstance(row, dict):
                raise FBApiError(
                    f"FB отчёт {context}: строка {index} page {page} не object"
                )
            rows.append(row)
        paging = payload.get("paging") or {}
        if not isinstance(paging, dict):
            raise FBApiError(f"FB отчёт {context}: paging должен быть object")
        next_url = paging.get("next")
        if not next_url:
            return rows
        if not isinstance(next_url, str):
            raise FBApiError(f"FB отчёт {context}: paging.next должен быть string")
        url = next_url
        params = None
        page += 1


def _parse_report_row(row: dict, since: date, until: date, context: str) -> dict:
    """Разбирает строку отчёта. Любая кривизна — ошибка, а не молчаливый ноль."""
    ad_id = row.get("ad_id")
    if not isinstance(ad_id, str) or not ad_id.strip():
        raise FBApiError(f"FB отчёт {context}: строка без ad_id")
    raw_date = row.get("date_start")
    if not isinstance(raw_date, str) or not raw_date:
        raise FBApiError(f"FB отчёт {context}: строка без date_start")
    try:
        row_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise FBApiError(
            f"FB отчёт {context}: невалидный date_start {raw_date}"
        ) from exc
    if not since <= row_date <= until:
        raise FBApiError(
            f"FB отчёт {context}: date_start {raw_date} вне запрошенного окна"
        )
    try:
        spend = float(row.get("spend") or 0)
        impressions = int(row.get("impressions") or 0)
    except (TypeError, ValueError) as exc:
        raise FBApiError(
            f"FB отчёт {context}: нечисловые spend/impressions у {ad_id}"
        ) from exc
    if spend < 0 or impressions < 0:
        raise FBApiError(f"FB отчёт {context}: отрицательные метрики у {ad_id}")

    lead_result = parse_meta_lead_actions(row.get("actions"))
    if lead_result.canonical_total is None:
        raise FBApiError(
            f"FB отчёт {context}: битая семантика лидов у {ad_id} "
            f"({','.join(lead_result.problems)})"
        )
    if lead_result.problems:
        logger.warning(
            "cohort_builder: расхождение lead-компонентов у %s %s — %s",
            ad_id, raw_date, ",".join(lead_result.problems),
        )

    def _text(key: str) -> str | None:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    return {
        "ad_id": ad_id.strip(),
        "date": row_date,
        "spend": spend,
        "impressions": impressions,
        "leads": lead_result.canonical_total,
        "adset_id": _text("adset_id"),
        "adset_name": _text("adset_name"),
        "ad_name": _text("ad_name"),
    }


def _fetch_fb_window(since: date, until: date) -> list[dict]:
    """Одно окно отчёта с дроблением при reduce-data.

    Настоящий rate limit (429/4/17/32/80003/80004) — CohortRateLimitError и
    остановка: половина данных, записанная как целое, хуже отсутствия данных.
    Reduce-data (code 1) — не лимит, а слишком большой запрос: окно делится
    пополам до одного дня.
    """
    context = f"{since}..{until}"
    try:
        report_run_id = _submit_async_report(since, until)
        _await_report(report_run_id, context)
        raw_rows = _fetch_report_rows(report_run_id, context)
    except FBApiError as exc:
        if _is_true_rate_limit_error(exc):
            raise CohortRateLimitError(
                f"FB ограничил запросы на окне {context}: {exc}"
            ) from exc
        if _is_reduce_data_exc(exc):
            span_days = (until - since).days + 1
            if span_days <= _FB_MIN_WINDOW_DAYS:
                raise CohortBuildError(
                    f"FB не отдаёт даже одиночный день {context}: {exc}"
                ) from exc
            middle = since + timedelta(days=span_days // 2 - 1)
            logger.info(
                "cohort_builder: reduce-data на %s — дробим на %s..%s и %s..%s",
                context, since, middle, middle + timedelta(days=1), until,
            )
            return [
                *_fetch_fb_window(since, middle),
                *_fetch_fb_window(middle + timedelta(days=1), until),
            ]
        raise CohortBuildError(f"FB отчёт {context} не получен: {exc}") from exc

    try:
        return [_parse_report_row(row, since, until, context) for row in raw_rows]
    except FBApiError as exc:
        raise CohortBuildError(f"FB отчёт {context} не разобран: {exc}") from exc


def _collect_fb(
    since: date,
    until: date,
) -> tuple[dict[tuple[str, date], _FbWeekAgg], set[date]]:
    """Собирает FB-агрегаты по неделям и множество реально покрытых дней.

    Покрытый день — день, за который отчёт вернул хотя бы одну строку по
    кабинету. Дня нет в выдаче — считаем, что данных за него НЕТ, и неделя
    теряет сравнимость. Отличить «кабинет молчал» от «FB не отдал» на уровне
    отчёта нельзя, поэтому выбор консервативный.
    """
    weeks: dict[tuple[str, date], _FbWeekAgg] = {}
    covered_days: set[date] = set()

    # Отчёт ставится на КАЖДЫЙ оффлайн-кабинет карты роутинга (cabinet_a +
    # cabinet_b): ad_id между кабинетами не пересекаются, агрегаты сливаются
    # в одну карту. Отказ любого кабинета — исключение и стоп всего билда
    # (fail-closed): половина данных, записанная как целое, хуже отсутствия.
    # covered_days — объединение по кабинетам: день без строк ОБОИХ кабинетов
    # считаем непокрытым.
    from services.fb_token_provider import fb_account, offline_account_context
    from services.launch_routing import accounts_to_scan

    # Страж двойного счёта: объявление живёт ровно в одном кабинете, поэтому
    # (ad_id, день), уже учтённый ДРУГИМ кабинетом, — аномалия отчёта, а не
    # данные. Пропускаем, иначе расход/лиды задвоятся.
    day_owner: dict[tuple[str, date], str] = {}
    cross_account_dupes = 0

    for account_id in accounts_to_scan():
        with fb_account(offline_account_context(account_id)):
            for window_since, window_until in _month_windows(since, until):
                for row in _fetch_fb_window(window_since, window_until):
                    row_date = row["date"]
                    owner_key = (row["ad_id"], row_date)
                    owner = day_owner.get(owner_key)
                    if owner is not None and owner != account_id:
                        cross_account_dupes += 1
                        continue
                    day_owner[owner_key] = account_id
                    covered_days.add(row_date)
                    key = (row["ad_id"], week_start_of(row_date))
                    agg = weeks.get(key)
                    if agg is None:
                        agg = _FbWeekAgg()
                        weeks[key] = agg
                    agg.spend_usd += row["spend"]
                    agg.impressions += row["impressions"]
                    agg.fb_leads += row["leads"]
                    agg.days_with_rows.add(row_date)
                    # Привязка к адсету — по последнему дню недели с данными:
                    # объявление может переезжать между адсетами, и историческая
                    # привязка недели не должна зависеть от порядка страниц.
                    if agg.last_day is None or row_date >= agg.last_day:
                        agg.last_day = row_date
                        agg.adset_id = row["adset_id"]
                        agg.adset_name = row["adset_name"]
                        agg.ad_name = row["ad_name"]

    if cross_account_dupes:
        logger.warning(
            "cohort_builder: %d строк (ad_id, день) пришли из второго кабинета — "
            "пропущены как аномалия (объявление живёт в одном кабинете)",
            cross_account_dupes,
        )

    return weeks, covered_days


# ---------------------------------------------------------------------------
# AMO: лиды и квалы по неделе создания лида
# ---------------------------------------------------------------------------

def _amo_windows(since: date, until: date) -> list[tuple[int, int]]:
    """Окна unix-времени по _AMO_WINDOW_DAYS дней внутри [since, until] (локальное время)."""
    windows: list[tuple[int, int]] = []
    cursor = since
    while cursor <= until:
        window_end = min(cursor + timedelta(days=_AMO_WINDOW_DAYS - 1), until)
        from_ts = int(
            datetime.combine(cursor, datetime.min.time(), _TZ_LOCAL).timestamp()
        )
        to_ts = int(
            datetime.combine(
                window_end, datetime.max.time(), _TZ_LOCAL
            ).timestamp()
        )
        windows.append((from_ts, to_ts))
        cursor = window_end + timedelta(days=1)
    return windows


def _collect_amo(
    since: date,
    until: date,
) -> tuple[dict[tuple[str, date], _AmoWeekAgg], dict[str, _LeadRef], dict]:
    """Считает лиды и квалы по паре (объявление, неделя создания лида).

    Возвращает вторым элементом карту lead_id → _LeadRef: по ней платежи ERP
    (contract_number = lead_id) привязываются к объявлению и его неделе. Карта
    строится ИЗ ЭТОЙ ЖЕ выгрузки намеренно — services/cdp_payments.py с его
    дисковым кешем сюда не привлекается: кеш отравлен (большинство записей «не
    найдено», и код их не перепроверяет), а нужна привязка ровно за то окно,
    за которое лиды уже выгружены.

    Лиды без FB-разметки не теряются молча: они попадают в отдельный счётчик,
    их доля логируется и возвращается в сводке. Разметка есть лишь у части
    лидов — остальное сайт и органика, это не дыра в данных, а другой канал.
    """
    weeks: dict[tuple[str, date], _AmoWeekAgg] = {}
    lead_index: dict[str, _LeadRef] = {}
    total = 0
    marked = 0
    unmarked = 0
    out_of_range = 0
    bad_created_at = 0

    for from_ts, to_ts in _amo_windows(since, until):
        leads = get_leads_window(from_ts, to_ts)
        for lead in leads:
            total += 1
            created_at = lead.get("created_at")
            if not isinstance(created_at, (int, float)) or isinstance(
                created_at, bool
            ):
                # Лид без внятной даты создания нельзя отнести к неделе. Не
                # бросаем весь бэкфилл из-за одной кривой записи, но и не
                # прячем её: счётчик уходит в сводку и в лог.
                bad_created_at += 1
                continue
            created_day = datetime.fromtimestamp(
                int(created_at), tz=_TZ_LOCAL
            ).date()
            if not since <= created_day <= until:
                out_of_range += 1
                continue
            fb_fields = _extract_fb_fields(lead)
            ad_id = (fb_fields.get("ad_id") or "").strip()
            if not ad_id:
                unmarked += 1
                continue
            marked += 1
            week_start = week_start_of(created_day)
            key = (ad_id, week_start)
            agg = weeks.get(key)
            if agg is None:
                agg = _AmoWeekAgg()
                weeks[key] = agg
            agg.amo_leads += 1
            if _is_qualified(lead):
                agg.quals += 1
            if agg.ad_name is None:
                ad_name = (fb_fields.get("ad_name") or "").strip()
                agg.ad_name = ad_name or None
            lead_id = lead.get("id")
            if lead_id is not None:
                # Ключ платежа — contract_number ERP, а это строковый lead_id AMO.
                lead_index[str(lead_id)] = _LeadRef(
                    ad_id=ad_id,
                    week_start=week_start,
                    created_day=created_day,
                )

    marked_share = round(marked / total * 100, 1) if total else 0.0
    logger.info(
        "cohort_builder: AMO %s..%s — лидов %d, с разметкой %d (%.1f%%), "
        "без разметки %d, вне окна %d, без даты создания %d",
        since, until, total, marked, marked_share, unmarked, out_of_range,
        bad_created_at,
    )
    stats = {
        "amo_leads_total": total,
        "amo_leads_marked": marked,
        "amo_leads_unmarked": unmarked,
        "amo_leads_out_of_range": out_of_range,
        "amo_leads_bad_created_at": bad_created_at,
        "amo_marked_share_pct": marked_share,
    }
    return weeks, lead_index, stats


# ---------------------------------------------------------------------------
# ERP: когортная выручка по неделе создания лида
# ---------------------------------------------------------------------------

def _revenue_days(query_from: date, query_to: date) -> list[date]:
    """Дни окна выгрузки платежей — по одному, включая обе границы.

    Почему по дням, а не длинными окнами: пагинация /revenue устойчива только
    на коротких выборках. Проверка на исторических данных показала:
      - выгрузка одного дня (несколько страниц): все записи уникальны, повторов нет;
      - выгрузка за месяц (десятки страниц): **заметная доля записей теряется**,
        вместо них приходят повторы.
    Глубокая пагинация на стороне CDP уезжает, и длинное окно молча возвращает
    неполный список с правильным на вид счётчиком. День — проверяемая единица:
    заявленный total сходится с числом уникальных id, и это доказательство
    полноты, а не предположение.
    """
    if query_from > query_to:
        return []
    days: list[date] = []
    day = query_from
    while day <= query_to:
        days.append(day)
        day += timedelta(days=1)
    return days


def _read_day_payments(day: date) -> tuple[list[dict], bool]:
    """Платежи одного дня с доказательством полноты выгрузки.

    Читает по направлениям (income и refund отдельно): так сервер сам отсекает
    внутренний кэшфлоу компании, и день почти всегда укладывается в одну
    страницу. Возвращает (платежи, полнота). Полнота — это strict.complete:
    заявленный сервером total сошёлся с числом собранных уникальных записей.

    Returns:
        ([], False) если хоть одно направление не подтвердило полноту.
    """
    collected: list[dict] = []
    for direction in ("income", "refund"):
        read = get_payments_strict(day, day, direction)
        if not read.complete:
            logger.warning(
                "cohort_builder: выгрузка %s (%s) не подтвердила полноту "
                "(заявлено %d, собрано %d) — день считаем невыгруженным",
                day, direction, read.declared_total, read.collected_total,
            )
            return [], False
        collected.extend(read.items)
    return collected, True


def _revenue_window_covered(
    week_start: date,
    horizon_days: int,
    covered_days: set[date] | None,
    since: date | None = None,
    until: date | None = None,
) -> bool:
    """Все ли дни, куда могут упасть оплаты недели, реально выгружены из ERP.

    Окно недели — от её первого дня до последнего дня горизонта самого позднего
    её лида. Дыра внутри означает, что часть оплат невидима, и ноль был бы
    враньём — такую неделю оставляем NULL.

    Границы недели урезаются запрошенным диапазоном [since, until]: в индексе
    лидов есть только лиды оттуда, значит и оплатить могли только они. Без
    урезки неделя на краю усечённого диапазона требовала бы дней, которые
    никогда не запрашивались, и молча теряла бы честно посчитанные деньги.

    covered_days=None означает «покрытие не отслеживалось» (старые вызовы и
    тесты) — тогда решает общий флаг доступности источника.
    """
    if covered_days is None:
        return True
    first_lead_day = week_start if since is None else max(week_start, since)
    week_end = week_start + timedelta(days=6)
    last_lead_day = week_end if until is None else min(week_end, until)
    if last_lead_day < first_lead_day:
        return False
    day = first_lead_day
    last_day = last_lead_day + timedelta(days=horizon_days)
    while day <= last_day:
        if day not in covered_days:
            return False
        day += timedelta(days=1)
    return True


def _collect_revenue(
    lead_index: dict[str, _LeadRef],
    since: date,
    until: date,
    horizon_days: int,
    today: date | None = None,
) -> tuple[dict[tuple[str, date], _RevenueAgg], dict]:
    """Деньги от лидов недели за horizon_days после создания КАЖДОГО лида.

    Это главное отличие от кассы недели. get_payments отдаёт платежи по
    doc_date — дате документа; если сложить их по неделе документа и поделить
    на расход той же недели, получится ROMI недели, к которой деньги отношения
    не имеют. Здесь наоборот: платёж сначала находит свой лид
    (contract_number = lead_id), лид — свою неделю создания, и только потом
    сумма попадает в когорту. Окно запроса поэтому шире диапазона когорт: до
    until + horizon_days, иначе хвост оплат последней недели был бы потерян.

    Нетто считается ПО СДЕЛКЕ (лиду), а не по документу — как в
    services/cdp_payments.compute_ad_payments_erp: рассрочка банка-партнёра даёт
    отдельный документ на каждый месяц, и одна оплатившая сделка иначе
    превратилась бы в N оплат. Возврат вычитается; сделка, съеденная рефандом,
    уменьшает выручку когорты, а не обнуляется.

    Платежи, не нашедшие свой лид (чужие / лид старше окна выгрузки), и
    платежи вне горизонта не исчезают: они попадают в счётчики сводки и в лог.

    Returns:
        ({(ad_id, week_start): _RevenueAgg}, статистика). Ключ
        stats["revenue_source_ok"]=False означает «ERP недоступна» — выручка не
        просто пустая, а НЕИЗВЕСТНАЯ, и записывать её нулями нельзя.
    """
    stats = {
        "revenue_source_ok": True,
        # Дни, за которые ERP реально ответила. Неделя получает деньги, только
        # если ВЕСЬ её горизонт оплат лежит внутри этого множества.
        "revenue_covered_days": set(),
        "revenue_failed_spans": [],
        "payments_total": 0,
        "payments_matched": 0,
        "payments_duplicate": 0,
        "payments_unmatched_lead": 0,
        "payments_outside_horizon": 0,
        "payments_bad_contract": 0,
        "payments_bad_doc_date": 0,
        "payments_bad_amount": 0,
        "payments_bad_direction": 0,
    }
    if not lead_index:
        logger.info(
            "cohort_builder: выручка %s..%s — ни одного размеченного лида, "
            "привязывать платежи не к чему",
            since, until,
        )
        # Полнота выгрузки здесь ни при чём: платить некому, и ноль — честный
        # ноль, а не дыра. None означает «покрытие не отслеживалось».
        stats["revenue_covered_days"] = None
        return {}, stats

    query_from = since - timedelta(days=_REVENUE_QUERY_MARGIN_DAYS)
    query_to = until + timedelta(
        days=horizon_days + _REVENUE_QUERY_MARGIN_DAYS
    )
    # Дальше сегодняшнего дня спрашивать нечего: платёж с датой документа из
    # будущего ERP не выдаст. В кроне until = сегодня, и без этой обрезки треть
    # запросов прогона (15 дней горизонта × 2 направления) уходила в пустоту.
    # Неделю это не обедняет: выручка пишется только у дозревших недель, а у
    # них последний день горизонта — не позже сегодняшнего.
    horizon_today = today or datetime.now(_TZ_LOCAL).date()
    query_to = min(query_to, horizon_today)
    if query_to < query_from:
        query_to = query_from
    # По дням, а не одним окном: длинная пагинация теряет записи молча
    # (см. _revenue_days). Плюс изоляция — упавший день гасит деньги только у
    # своих недель, остальные считаются как обычно.
    payments: list = []
    for day in _revenue_days(query_from, query_to):
        try:
            day_payments, complete = _read_day_payments(day)
        except CdpError as exc:
            # Текст CdpError уже маскирует ключ (services/cdp_client._request).
            logger.warning(
                "cohort_builder: ERP не отдала день %s — недели, чьи оплаты "
                "попадают в этот день, останутся с NULL (не ноль): %s",
                day, exc,
            )
            stats["revenue_failed_spans"].append((day.isoformat(), day.isoformat()))
            continue
        if not complete:
            stats["revenue_failed_spans"].append((day.isoformat(), day.isoformat()))
            continue
        payments.extend(day_payments)
        stats["revenue_covered_days"].add(day)

    if not stats["revenue_covered_days"]:
        logger.warning(
            "cohort_builder: ERP недоступна на всём окне %s..%s — выручка "
            "когорт останется NULL (не ноль)",
            query_from, query_to,
        )
        stats["revenue_source_ok"] = False
        return {}, stats

    # Нетто по сделке: лид → сумма всех его документов в горизонте.
    deal_net: dict[str, float] = {}
    seen_payment_ids: set = set()
    for payment in payments:
        stats["payments_total"] += 1
        if not isinstance(payment, dict):
            stats["payments_bad_contract"] += 1
            continue
        payment_id = payment.get("id")
        if payment_id is not None:
            if payment_id in seen_payment_ids:
                stats["payments_duplicate"] += 1
                continue
            seen_payment_ids.add(payment_id)
        try:
            lead_id = str(int(payment.get("contract_number")))
        except (TypeError, ValueError):
            stats["payments_bad_contract"] += 1
            continue
        ref = lead_index.get(lead_id)
        if ref is None:
            # Платёж по лиду, которого нет в выгрузке: не наш канал, лид старше
            # окна или сделка без FB-разметки. Не ноль и не молча — счётчик.
            stats["payments_unmatched_lead"] += 1
            continue
        try:
            doc_date = date.fromisoformat(str(payment.get("doc_date")))
        except (TypeError, ValueError):
            stats["payments_bad_doc_date"] += 1
            continue
        horizon_end = ref.created_day + timedelta(days=horizon_days)
        if not ref.created_day <= doc_date <= horizon_end:
            stats["payments_outside_horizon"] += 1
            continue
        try:
            amount = float(payment.get("amount") or 0)
        except (TypeError, ValueError):
            stats["payments_bad_amount"] += 1
            continue
        direction = payment.get("direction")
        if direction == "income":
            deal_net[lead_id] = deal_net.get(lead_id, 0.0) + amount
        elif direction == "refund":
            deal_net[lead_id] = deal_net.get(lead_id, 0.0) - amount
        else:
            # get_payments возвращает только income/refund; сюда попасть можно
            # лишь при изменении контракта CDP — тогда это не «ноль», а сигнал.
            stats["payments_bad_direction"] += 1
            continue
        stats["payments_matched"] += 1

    weeks: dict[tuple[str, date], _RevenueAgg] = {}
    for lead_id, net in deal_net.items():
        ref = lead_index[lead_id]
        key = (ref.ad_id, ref.week_start)
        agg = weeks.get(key)
        if agg is None:
            agg = _RevenueAgg()
            weeks[key] = agg
        agg.revenue_lcy += net
        # Оплаченной считается сделка с положительным нетто — как в ERP-агрегате
        # creative_kb: возвращённая полностью сделка оплатой не является.
        if net > 0:
            agg.payments += 1

    logger.info(
        "cohort_builder: выручка %s..%s (горизонт %d дн) — платежей %d, "
        "сопоставлено %d, чужих лидов %d, вне горизонта %d, дублей %d, "
        "битых %d, сделок с деньгами %d",
        since, until, horizon_days, stats["payments_total"],
        stats["payments_matched"], stats["payments_unmatched_lead"],
        stats["payments_outside_horizon"], stats["payments_duplicate"],
        stats["payments_bad_contract"] + stats["payments_bad_doc_date"]
        + stats["payments_bad_amount"] + stats["payments_bad_direction"],
        len(deal_net),
    )
    return weeks, stats


# ---------------------------------------------------------------------------
# Сборка строк и запись
# ---------------------------------------------------------------------------

def _resolve_week_rate(week_start: date, memo: dict[date, float | None]) -> float | None:
    """Курс USD→LCY на понедельник недели, один запрос на неделю за прогон.

    Понедельник, а не дата платежа: ROMI сравнивает расход недели (доллары) с
    выручкой её когорты (ед.), и курс берётся на момент траты. Внутринедельное
    движение курса (доли процента) на порядок меньше шума самого ROMI, а
    понедельник — гарантированно рабочий день с опубликованным курсом.
    """
    if week_start in memo:
        return memo[week_start]
    try:
        rate = get_usd_to_lcy_on(week_start)
    except Exception as exc:  # noqa: BLE001 — курс не должен ронять сборку
        logger.warning(
            "cohort_builder: курс на %s не получен (%s) — ROMI недели будет NULL",
            week_start, exc,
        )
        rate = None
    memo[week_start] = rate
    return rate


def _build_rows(
    since: date,
    until: date,
    fb_weeks: dict[tuple[str, date], _FbWeekAgg],
    fb_covered_days: set[date],
    amo_weeks: dict[tuple[str, date], _AmoWeekAgg],
    now: datetime | None,
    revenue_weeks: dict[tuple[str, date], _RevenueAgg] | None = None,
    revenue_source_ok: bool = False,
    revenue_covered_days: set[date] | None = None,
    horizon_days: int = REVENUE_HORIZON_DAYS,
) -> list[dict]:
    """Складывает FB, AMO и ERP в строки когорт с честной оценкой полноты."""
    computed_at = datetime.now(timezone.utc).isoformat()
    revenue_weeks = revenue_weeks or {}
    rate_memo: dict[date, float | None] = {}
    keys = sorted(set(fb_weeks) | set(amo_weeks), key=lambda k: (k[1], k[0]))

    # Полнота считается на неделю, а не на объявление: она свойство выгрузки.
    week_state: dict[date, dict] = {}
    for _, week_start in keys:
        if week_start in week_state:
            continue
        days_in_range = [
            day for day in _week_days(week_start) if since <= day <= until
        ]
        missing = [day for day in days_in_range if day not in fb_covered_days]
        days_expected = len(days_in_range)
        days_covered = days_expected - len(missing)
        reasons: list[str] = []
        if days_expected < 7:
            reasons.append(REASON_WEEK_PARTIAL_RANGE)
        if missing:
            reasons.append(
                f"{REASON_FB_DAYS_MISSING}:"
                + ",".join(day.isoformat() for day in missing)
            )
        if not _is_week_closed(week_start, now):
            reasons.append(REASON_WEEK_NOT_CLOSED)
        # Дыра в выгрузке (день запрошен, но не получен) обесценивает суммы:
        # расход недели оказался бы занижен на невидимый день. В этом случае
        # метрики FB пишутся как NULL. Урезанный диапазон дырой не считается —
        # там сумма честная по известным дням, и days_covered/days_expected
        # прямо говорят, за какую долю недели она посчитана.
        week_state[week_start] = {
            "days_expected": days_expected,
            "days_covered": days_covered,
            "reasons": reasons,
            "fb_metrics_known": days_covered == days_expected,
            "comparable": bool(
                days_expected == 7 and days_covered == 7 and not reasons
            ),
            # Зрелость выручки — свойство КАЛЕНДАРЯ, а не выгрузки: она равна 1
            # и тогда, когда ERP недоступна (см. revenue_source_ok ниже). Это
            # различает «горизонт ещё идёт» и «горизонт закрыт, но денег мы не
            # знаем» — оба дают NULL, но по разным причинам.
            "revenue_mature": _is_revenue_mature(week_start, horizon_days, now),
            # Дыра в выгрузке платежей делает ноль недостоверным: часть оплат
            # просто не видна. Такая неделя получает NULL, как при полном
            # отказе ERP, — но только она, а не весь диапазон.
            "revenue_window_covered": _revenue_window_covered(
                week_start, horizon_days, revenue_covered_days, since, until
            ),
        }

    rows: list[dict] = []
    for ad_id, week_start in keys:
        state = week_state[week_start]
        fb_agg = fb_weeks.get((ad_id, week_start))
        amo_agg = amo_weeks.get((ad_id, week_start))

        if state["fb_metrics_known"]:
            # Все запрошенные дни недели получены: отсутствие объявления в
            # отчёте — доказанный ноль показов, а не пропуск данных.
            spend_usd = round(fb_agg.spend_usd, 4) if fb_agg else 0.0
            impressions = fb_agg.impressions if fb_agg else 0
            fb_leads = fb_agg.fb_leads if fb_agg else 0
        else:
            spend_usd = impressions = fb_leads = None

        # AMO выгружается ровно по [since, until] и падает целиком при ошибке,
        # поэтому по любой строке его счётчики известны за те же дни недели.
        amo_leads = amo_agg.amo_leads if amo_agg else 0
        quals = amo_agg.quals if amo_agg else 0

        # Выручка. Пишется ТОЛЬКО когда горизонт закрыт и ERP ответила: тогда
        # отсутствие платежей по лидам недели — доказанный ноль (лиды были,
        # денег нет), а не «ещё не пришли». В остальных случаях NULL.
        revenue_mature = bool(state["revenue_mature"])
        if revenue_mature and revenue_source_ok and state["revenue_window_covered"]:
            revenue_agg = revenue_weeks.get((ad_id, week_start))
            revenue_lcy = (
                round(revenue_agg.revenue_lcy, 2) if revenue_agg else 0.0
            )
            payments = revenue_agg.payments if revenue_agg else 0
            usd_lcy_rate = _resolve_week_rate(week_start, rate_memo)
        else:
            revenue_lcy = payments = usd_lcy_rate = None

        # ROMI, % = выручка / расход в ед. × 100 (конвенция проекта, см.
        # integrations/amo.calc_ad_metrics: 100% — вышли в ноль). Без курса
        # ROMI не считается — по сегодняшнему курсу считать нельзя, иначе одна
        # и та же неделя завтра даст другое число.
        if (
            revenue_mature
            and revenue_lcy is not None
            and spend_usd is not None
            and spend_usd > 0
            and usd_lcy_rate is not None
        ):
            romi_pct = round(revenue_lcy / (spend_usd * usd_lcy_rate) * 100, 1)
        else:
            romi_pct = None

        ad_name = (fb_agg.ad_name if fb_agg else None) or (
            amo_agg.ad_name if amo_agg else None
        )
        city = extract_city(ad_name) if ad_name else None
        if city == "?":
            city = None

        reason = ";".join(state["reasons"]) or None
        comparable = 1 if state["comparable"] else 0
        rows.append({
            "ad_id": ad_id,
            "week_start": week_start.isoformat(),
            # adset_id/adset_name — строго из FB-отчёта той недели: объявление
            # могло переехать, историческая привязка не переписывается.
            "adset_id": fb_agg.adset_id if fb_agg else None,
            "adset_name": fb_agg.adset_name if fb_agg else None,
            "ad_name": ad_name,
            "city": city,
            "spend_usd": spend_usd,
            "impressions": impressions,
            "fb_leads": fb_leads,
            "amo_leads": amo_leads,
            "quals": quals,
            "days_covered": state["days_covered"],
            "days_expected": state["days_expected"],
            "comparable": comparable,
            "not_comparable_reason": reason,
            "builder_version": BUILDER_VERSION,
            "computed_at": computed_at,
            "revenue_lcy": revenue_lcy,
            "payments": payments,
            # Горизонт пишется только вместе с выручкой: он часть её
            # определения, а не настройка строки.
            "revenue_horizon_days": horizon_days if revenue_lcy is not None else None,
            "revenue_mature": 1 if revenue_mature else 0,
            "usd_lcy_rate": usd_lcy_rate,
            "romi_pct": romi_pct,
        })
    return rows


_UPSERT_SQL = """
    INSERT INTO ad_weekly_cohorts (
        ad_id, week_start, adset_id, adset_name, ad_name, city,
        spend_usd, impressions, fb_leads, amo_leads, quals,
        days_covered, days_expected, comparable, not_comparable_reason,
        builder_version, computed_at,
        revenue_lcy, payments, revenue_horizon_days, revenue_mature,
        usd_lcy_rate, romi_pct
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?)
    ON CONFLICT(ad_id, week_start) DO UPDATE SET
        adset_id              = excluded.adset_id,
        adset_name            = excluded.adset_name,
        ad_name               = excluded.ad_name,
        city                  = excluded.city,
        spend_usd             = excluded.spend_usd,
        impressions           = excluded.impressions,
        fb_leads              = excluded.fb_leads,
        amo_leads             = excluded.amo_leads,
        quals                 = excluded.quals,
        days_covered          = excluded.days_covered,
        days_expected         = excluded.days_expected,
        comparable            = excluded.comparable,
        not_comparable_reason = excluded.not_comparable_reason,
        builder_version       = excluded.builder_version,
        computed_at           = excluded.computed_at,
        revenue_lcy           = COALESCE(excluded.revenue_lcy,
                                         ad_weekly_cohorts.revenue_lcy),
        payments              = COALESCE(excluded.payments,
                                         ad_weekly_cohorts.payments),
        revenue_horizon_days  = COALESCE(excluded.revenue_horizon_days,
                                         ad_weekly_cohorts.revenue_horizon_days),
        revenue_mature        = MAX(excluded.revenue_mature,
                                    ad_weekly_cohorts.revenue_mature),
        usd_lcy_rate          = COALESCE(excluded.usd_lcy_rate,
                                         ad_weekly_cohorts.usd_lcy_rate),
        romi_pct              = COALESCE(excluded.romi_pct,
                                         ad_weekly_cohorts.romi_pct)
    WHERE excluded.comparable = 1 OR ad_weekly_cohorts.comparable = 0
"""


def _write_rows(rows: list[dict]) -> dict:
    """UPSERT строк одной транзакцией. Возвращает {written, kept}.

    Полная строка (comparable=1) не затирается неполной: узкий пересчёт
    диапазона не должен обесценивать уже собранную неделю. Обратное
    направление свободно — неполная строка обновляется всегда.

    Денежные колонки обновляются через COALESCE: известная выручка не
    затирается NULL. Иначе разовая недоступность ERP стирала бы уже посчитанный
    ROMI недели — а «не знаем сейчас» не отменяет того, что знали вчера.
    Зрелость только растёт (MAX): календарь назад не идёт.
    """
    if not rows:
        return {"written": 0, "kept": 0}

    conn = _get_connection()
    try:
        written = 0
        for row in rows:
            cursor = conn.execute(_UPSERT_SQL, (
                row["ad_id"],
                row["week_start"],
                row["adset_id"],
                row["adset_name"],
                row["ad_name"],
                row["city"],
                row["spend_usd"],
                row["impressions"],
                row["fb_leads"],
                row["amo_leads"],
                row["quals"],
                row["days_covered"],
                row["days_expected"],
                row["comparable"],
                row["not_comparable_reason"],
                row["builder_version"],
                row["computed_at"],
                row["revenue_lcy"],
                row["payments"],
                row["revenue_horizon_days"],
                row["revenue_mature"],
                row["usd_lcy_rate"],
                row["romi_pct"],
            ))
            written += cursor.rowcount if cursor.rowcount > 0 else 0
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"written": written, "kept": len(rows) - written}


# ---------------------------------------------------------------------------
# Публичный вход
# ---------------------------------------------------------------------------

def build_cohorts(
    since: date | str,
    until: date | str,
    *,
    now: datetime | None = None,
    horizon_days: int = REVENUE_HORIZON_DAYS,
) -> dict:
    """Собирает недельные когорты за [since, until] включительно.

    Сначала полностью выгружаются все источники, и только потом идёт запись:
    при rate limit или неполном ответе FB не остаётся частично записанного
    диапазона. Повторный вызов на том же диапазоне идемпотентен — строка
    пары (объявление, неделя) перезаписывается, дублей не появляется.

    Args:
        since/until: границы диапазона по дате создания лида (локальное время).
        now: «сейчас» для проверок закрытости недели и зрелости выручки.
        horizon_days: горизонт сбора выручки от создания лида. Меняется только
            осознанно: он часть определения revenue_lcy и пишется в строку.

    Returns: {"since","until","weeks","rows","written","kept","comparable",
              "not_comparable","reasons":{code:count},"fb_days_expected",
              "fb_days_covered","revenue_horizon_days","weeks_revenue_mature",
              "rows_with_revenue","rows_with_romi","revenue_rate_missing",
              ...amo- и ERP-статистика}.
    """
    since_date = _as_date(since)
    until_date = _as_date(until)
    if since_date > until_date:
        raise ValueError(f"Пустой диапазон когорт: {since_date} > {until_date}")
    if not isinstance(horizon_days, int) or isinstance(horizon_days, bool):
        raise ValueError("horizon_days должен быть int")
    if horizon_days < 1:
        raise ValueError(f"horizon_days должен быть >= 1, получено {horizon_days}")

    fb_weeks, fb_covered_days = _collect_fb(since_date, until_date)
    amo_weeks, lead_index, amo_stats = _collect_amo(since_date, until_date)
    revenue_weeks, revenue_stats = _collect_revenue(
        lead_index,
        since_date,
        until_date,
        horizon_days,
        today=(now.astimezone(_TZ_LOCAL).date() if now is not None else None),
    )
    rows = _build_rows(
        since_date,
        until_date,
        fb_weeks,
        fb_covered_days,
        amo_weeks,
        now,
        revenue_weeks=revenue_weeks,
        revenue_source_ok=bool(revenue_stats["revenue_source_ok"]),
        revenue_covered_days=revenue_stats.get("revenue_covered_days"),
        horizon_days=horizon_days,
    )
    write_stats = _write_rows(rows)

    reasons: dict[str, int] = {}
    for row in rows:
        if not row["not_comparable_reason"]:
            continue
        for chunk in row["not_comparable_reason"].split(";"):
            code = chunk.split(":", 1)[0]
            reasons[code] = reasons.get(code, 0) + 1

    comparable = sum(1 for row in rows if row["comparable"] == 1)
    requested_days = (until_date - since_date).days + 1
    mature_weeks = {
        row["week_start"] for row in rows if row["revenue_mature"] == 1
    }
    rows_with_revenue = sum(1 for row in rows if row["revenue_lcy"] is not None)
    rows_with_romi = sum(1 for row in rows if row["romi_pct"] is not None)
    # Строки, где горизонт закрыт и деньги известны, но курса нет — ROMI
    # посчитать нечем. Отдельный счётчик: молчаливый NULL здесь неотличим от
    # «денег не было», а это разные вещи.
    revenue_rate_missing = sum(
        1
        for row in rows
        if row["revenue_lcy"] is not None and row["usd_lcy_rate"] is None
    )
    summary = {
        "since": since_date.isoformat(),
        "until": until_date.isoformat(),
        "weeks": len({row["week_start"] for row in rows}),
        "rows": len(rows),
        "written": write_stats["written"],
        "kept": write_stats["kept"],
        "comparable": comparable,
        "not_comparable": len(rows) - comparable,
        "reasons": reasons,
        "fb_days_expected": requested_days,
        "fb_days_covered": len(fb_covered_days),
        "revenue_horizon_days": horizon_days,
        "weeks_revenue_mature": len(mature_weeks),
        "rows_with_revenue": rows_with_revenue,
        "rows_with_romi": rows_with_romi,
        "revenue_rate_missing": revenue_rate_missing,
        **amo_stats,
        # Множество дней в сводку не отдаём — она уходит в JSON и в отчёты;
        # наружу нужны число покрытых дней и список непрочитанных окон.
        **{
            key: value
            for key, value in revenue_stats.items()
            if key != "revenue_covered_days"
        },
        "revenue_days_covered": len(revenue_stats.get("revenue_covered_days") or ()),
    }
    logger.info(
        "cohort_builder: %s..%s — недель %d, строк %d (записано %d, сохранено "
        "прежних %d), сравнимых %d, причины %s, дней FB %d/%d; выручка: "
        "горизонт %d дн, дозревших недель %d, строк с деньгами %d, с ROMI %d, "
        "без курса %d, источник %s",
        summary["since"], summary["until"], summary["weeks"], summary["rows"],
        summary["written"], summary["kept"], summary["comparable"],
        reasons, summary["fb_days_covered"], summary["fb_days_expected"],
        horizon_days, len(mature_weeks), rows_with_revenue, rows_with_romi,
        revenue_rate_missing,
        "ERP" if revenue_stats["revenue_source_ok"] else "НЕДОСТУПЕН",
    )
    return summary


def refresh_recent_weeks(weeks: int = 5, *, now: datetime | None = None) -> dict:
    """Пересчитывает последние `weeks` недель, включая текущую (для крона).

    Диапазон выравнивается на понедельник, чтобы узкий пересчёт не резал
    неделю пополам. Свежие недели пересчитываются намеренно: квалы в AMO
    дозревают, и вчерашняя когорта завтра выглядит иначе.

    Дефолт 5, а не 3: неделя дозревает по выручке через 6 + REVENUE_HORIZON_DAYS
    = 20 дней после своего понедельника. Окно из трёх недель (14 дней назад)
    заканчивается раньше — дозревшая неделя оказывалась бы уже за его краем, и
    выручка не проставилась бы В НЕЙ НИКОГДА. Пяти недель хватает с запасом на
    пропущенный день крона (минимально достаточно четырёх).
    """
    if weeks < 1:
        raise ValueError(f"weeks должно быть >= 1, получено {weeks}")
    today_local = _local_today(now)
    since = week_start_of(today_local) - timedelta(days=7 * (weeks - 1))
    return build_cohorts(since, today_local, now=now)
