"""
Почасовой сборщик метрик первых ~48ч жизни объявления → таблица ad_hourly_metrics.

Зачем: два research-цикла «Раннего прогноза» показали, что суточные сигналы дня-1/2
не отличают будущих победителей от сливов. Единственный подтверждённый путь —
почасовые фичи, но FB отдаёт hourly-разбивку задним числом только ~3 недели
(hourly_probe.py). Поэтому копим сами: раз в сутки собираем hourly insights для
объявлений младше 48ч и складываем в ad_hourly_metrics (UPSERT по ad_id+datetime_hour).

Сборщик fail-safe: FBApiError/сеть/битый JSON НИКОГДА не бросаются наружу — крон
не должен падать. Единственная мутация — GET .../insights, никаких решений/пауз.

Мультикабинетность (L2-адсеты мигрировали в «ACME cabinet_b»):
discovery кандидатов ставится на КАЖДЫЙ кабинет карты роутинга через
fb_account(offline_account_context(...)) — как cohort_builder._collect_fb;
сами hourly insights идут на узел объявления {ad_id}/insights и кабинета
не требуют (оффлайн-токен видит все кабинеты карты).

Запускается ночным кроном из web/app.py (_cron_hourly_collector, окно 02-04 по локальному времени).
"""

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agent.fb_common import API, FBApiError, _throttled_get
from services.creative_intelligence import _get_connection
from services.fb_token_provider import (
    fb_account,
    get_fb_account_id,
    get_fb_token,
    offline_account_context,
)
from services.meta_lead_actions import parse_meta_lead_actions

logger = logging.getLogger(__name__)

# Локальное время (UTC+5 по умолчанию, настраивается; единое со всеми модулями проекта)
_TZ_LOCAL = timezone(timedelta(hours=5))
_TZ_ADVERTISER = ZoneInfo("America/Los_Angeles")

# Кабинет FB живёт по времени Лос-Анджелеса (advertiser_time_zone). Пробник и все
# hourly-строки размечены этим ТЗ — границу «часа» и дату мы берём из полей ответа
# FB (date_start + hourly_stats_...), НЕ пересчитываем в локальное время.
# «Младше 48ч» считаем по created_time (UTC от FB) относительно now_local — грубая
# граница в 48ч имеет запас, сдвиг ТЗ на ней некритичен (см. research-v2 §edge cases).


# Потолок кандидатов из FB: даунстрим-обработка всё равно ограничена
# max_ads_per_run (30 FB-запросов/прогон), поэтому нет смысла тянуть больше страниц.
_FB_CANDIDATE_CAP = 30
# Защитный лимит страниц пагинации: если FB проигнорит серверный created_time-фильтр
# и начнёт отдавать весь кабинет — не крутим пагинацию бесконечно ночным кроном.
_FB_MAX_PAGES = 20


def _fetch_recent_ads_from_fb(
    now_utc: datetime,
    max_age_hours: int = 48,
    max_candidates: int = _FB_CANDIDATE_CAP,
) -> list[tuple[str, str]]:
    """Тянет объявления младше max_age_hours НАПРЯМУЮ из FB (account-level /ads).

    Самодостаточный источник кандидатов: не зависит от creative_kb.created_at
    (который регулярный синк не пишет — см. creative_intelligence.py:574-580).

    Запрос: GET /act_{id}/ads с fields=id,name,created_time,effective_status и
    серверным фильтром created_time > (now-max_age_hours) unix-ts. Серверный
    фильтр — оптимизация (меньше страниц); границу «48ч» ВСЕГДА перепроверяем
    локально по created_time (на случай если FB отдаст лишнее). Страничный обход
    через after-курсор (как creative_backfill._fetch_ads_page), стоп при
    достижении max_candidates / конце страниц / потолке страниц _FB_MAX_PAGES.

    Returns: список [(ad_id, launch_date_iso), ...], launch_date_iso = дата
    создания 'YYYY-MM-DD' (для time_range почасового запроса).
    Бросает FBApiError при rate-limit/не-200 — верхний уровень (_select_candidate_ads)
    ловит и уходит на фолбэк через creative_kb.
    """
    account_id = get_fb_account_id()
    token = get_fb_token()

    cutoff_utc = now_utc - timedelta(hours=max_age_hours)
    cutoff_ts = int(cutoff_utc.replace(tzinfo=timezone.utc).timestamp())

    candidates: list[tuple[str, str]] = []
    cursor: str | None = None

    for _page in range(_FB_MAX_PAGES):
        params: dict = {
            "access_token": token,
            "fields": "id,name,created_time,effective_status",
            "filtering": json.dumps(
                [{"field": "created_time", "operator": "GREATER_THAN", "value": cutoff_ts}]
            ),
            "limit": 50,
        }
        if cursor:
            params["after"] = cursor

        resp = _throttled_get(f"{API}/act_{account_id}/ads", params=params)
        if resp.status_code != 200:
            raise FBApiError(
                f"FB /ads ошибка: {resp.status_code} {resp.text[:200]}", resp.status_code
            )

        data = resp.json()
        for ad in data.get("data", []):
            ad_id = ad.get("id")
            created_time = ad.get("created_time")
            if not ad_id or not created_time:
                continue
            try:
                created_dt = datetime.fromisoformat(created_time.replace("+0000", "+00:00"))
                created_utc = created_dt.astimezone(timezone.utc).replace(tzinfo=None)
            except (ValueError, AttributeError):
                continue

            age_hours = (now_utc - created_utc).total_seconds() / 3600
            if age_hours < max_age_hours:
                candidates.append((str(ad_id), created_utc.date().isoformat()))
                if len(candidates) >= max_candidates:
                    return candidates

        paging = data.get("paging", {})
        cursor = paging.get("cursors", {}).get("after")
        if "next" not in paging or not cursor:
            break

    return candidates


def _fetch_recent_ads_all_accounts(
    now_utc: datetime,
    max_age_hours: int = 48,
) -> list[tuple[str, str]]:
    """Мультикабинетный discovery: _fetch_recent_ads_from_fb на КАЖДЫЙ кабинет
    карты роутинга (cabinet_a + cabinet_b), слияние в один список.

    L2-адсеты всех городов живут в «ACME cabinet_b»
    (act_29716040622546856) — discovery только по дефолтному кабинету оставлял
    их без hourly-данных. Паттерн — cohort_builder._collect_fb:
    fb_account(offline_account_context(account_id)).

    Отказ ОДНОГО кабинета не гасит остальные (сборщик fail-safe, в отличие от
    fail-closed когорт: hourly-строки не сливаются в общий агрегат, пропуск
    кабинета — недобор, а не искажение): кандидаты живых кабинетов
    возвращаются, потеря логируется. Упали ВСЕ кабинеты → FBApiError,
    верхний уровень уходит на KB-фолбэк, как раньше при недоступном FB.

    Дубль ad_id из второго кабинета — аномалия (объявление живёт ровно в одном
    кабинете), пропускается: владельцем остаётся первый кабинет.
    """
    from services.launch_routing import accounts_to_scan

    candidates: list[tuple[str, str]] = []
    seen_ads: set[str] = set()
    failed = 0

    accounts = accounts_to_scan()
    for account_id in accounts:
        try:
            with fb_account(offline_account_context(account_id)):
                fetched = _fetch_recent_ads_from_fb(now_utc, max_age_hours=max_age_hours)
        except Exception as exc:
            failed += 1
            logger.warning(
                "_fetch_recent_ads_all_accounts: кабинет %s недоступен (%s) — "
                "его объявления в этом прогоне пропущены",
                account_id, type(exc).__name__,
            )
            continue
        for ad_id, launch_date in fetched:
            if ad_id in seen_ads:
                logger.warning(
                    "_fetch_recent_ads_all_accounts: ad_id=%s пришёл из второго "
                    "кабинета %s — пропущен как аномалия",
                    ad_id, account_id,
                )
                continue
            seen_ads.add(ad_id)
            candidates.append((ad_id, launch_date))

    if not accounts or failed == len(accounts):
        raise FBApiError(
            f"discovery кандидатов: все {len(accounts)} кабинета(ов) карты "
            "роутинга недоступны", 0,
        )

    return candidates


def _select_candidate_ads(
    conn: sqlite3.Connection,
    now_local: datetime,
    max_age_hours: int = 48,
) -> list[tuple[str, str]]:
    """Кандидаты (объявления младше max_age_hours) НАПРЯМУЮ из FB.

    Основной путь — _fetch_recent_ads_all_accounts (account-level /ads по
    created_time на каждый кабинет карты роутинга).
    Фолбэк: все кабинеты легли / нет токена / битая карта — старый путь через
    creative_kb.created_at (_select_candidates_from_kb). Фолбэк хуже
    (created_at регулярный синк не пишет → может быть пусто), но «хуже, чем ничего»
    не будет — крон не должен падать из-за FB.

    Returns: список [(ad_id, launch_date_iso), ...]. Сортировка НЕ здесь —
    приоритезация в collect_hourly_metrics (_prioritize_candidates).
    """
    # now_local может быть aware — сравниваем в UTC, приводим к naive UTC.
    now_utc = now_local.astimezone(timezone.utc).replace(tzinfo=None)

    try:
        candidates = _fetch_recent_ads_all_accounts(now_utc, max_age_hours=max_age_hours)
        logger.info(
            "_select_candidate_ads: %d кандидатов из FB (created_time < %dч, все кабинеты)",
            len(candidates), max_age_hours,
        )
        return candidates
    except Exception as exc:
        logger.warning(
            "_select_candidate_ads: FB недоступен (%s) — фолбэк на creative_kb",
            type(exc).__name__,
        )
        return _select_candidates_from_kb(conn, now_utc, max_age_hours)


def _select_candidates_from_kb(
    conn: sqlite3.Connection,
    now_utc: datetime,
    max_age_hours: int = 48,
) -> list[tuple[str, str]]:
    """Фолбэк-путь: кандидаты из creative_kb.created_at (как до самодостаточного сборщика).

    Кандидат = ad_id с непустым created_at, у которого (now_utc - created_at) < max_age_hours.
    created_at — ISO-строка FB ('YYYY-MM-DDTHH:MM:SS+0000'). now_utc — naive UTC.
    """
    rows = conn.execute(
        "SELECT ad_id, created_at FROM creative_kb WHERE created_at IS NOT NULL AND created_at != ''"
    ).fetchall()

    candidates: list[tuple[str, str]] = []
    for row in rows:
        ad_id = row["ad_id"]
        created_at = row["created_at"]
        try:
            created_dt = datetime.fromisoformat(created_at.replace("+0000", "+00:00"))
            created_utc = created_dt.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, AttributeError):
            # Битый/пустой created_at — объявление не кандидат, без падения.
            continue

        age_hours = (now_utc - created_utc).total_seconds() / 3600
        if age_hours < max_age_hours:
            candidates.append((ad_id, created_utc.date().isoformat()))

    return candidates


def _prioritize_candidates(
    conn: sqlite3.Connection,
    candidates: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Ставит вперёд объявления без доверенных v2-строк в ad_hourly_metrics.

    Legacy и invalid строки требуют повторного Graph refetch, поэтому считаются
    отсутствующими. Возвращает переупорядоченный список того же состава.
    """
    if not candidates:
        return []

    rows = conn.execute(
        "SELECT DISTINCT ad_id FROM ad_hourly_metrics "
        "WHERE lead_semantics_version = 2 "
        "AND lead_parse_status IN ('ok', 'component_mismatch')"
    ).fetchall()
    ads_with_data = {row["ad_id"] for row in rows}

    without_data = [c for c in candidates if c[0] not in ads_with_data]
    with_data = [c for c in candidates if c[0] in ads_with_data]

    return without_data + with_data


def get_hourly_lead_semantics_status(
    conn: sqlite3.Connection | None = None,
    now_local: datetime | None = None,
) -> dict:
    """Возвращает операторский статус legacy hourly rows без удаления данных.

    Строки за последние 48 часов ещё можно заменить обычным collector Graph
    refetch. Более старые legacy-строки остаются явным fail-closed veto cleaner.
    """
    if now_local is None:
        now_local = datetime.now(_TZ_LOCAL)
    cutoff = (
        now_local.astimezone(_TZ_ADVERTISER) - timedelta(hours=48)
    ).replace(minute=0, second=0, microsecond=0, tzinfo=None).isoformat()

    owns_connection = conn is None
    if conn is None:
        conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN lead_semantics_version = 1 OR lead_parse_status = 'legacy'
                         THEN 1 ELSE 0 END) AS legacy_rows,
                SUM(CASE WHEN (lead_semantics_version = 1 OR lead_parse_status = 'legacy')
                              AND datetime_hour >= ?
                         THEN 1 ELSE 0 END) AS refetchable_48h_rows,
                SUM(CASE WHEN (lead_semantics_version = 1 OR lead_parse_status = 'legacy')
                              AND datetime_hour < ?
                         THEN 1 ELSE 0 END) AS older_fail_closed_rows,
                SUM(CASE WHEN lead_semantics_version != 2 OR lead_parse_status != 'ok'
                         THEN 1 ELSE 0 END) AS cleaner_veto_rows
            FROM ad_hourly_metrics
            """,
            (cutoff, cutoff),
        ).fetchone()
    finally:
        if owns_connection:
            conn.close()

    legacy_rows = int(row["legacy_rows"] or 0)
    refetchable_rows = int(row["refetchable_48h_rows"] or 0)
    older_rows = int(row["older_fail_closed_rows"] or 0)
    return {
        "legacy_rows": legacy_rows,
        "refetchable_48h_rows": refetchable_rows,
        "older_fail_closed_rows": older_rows,
        "cleaner_veto_rows": int(row["cleaner_veto_rows"] or 0),
        "blocked_reason": (
            "hourly_legacy_outside_48h_requires_explicit_provider_refetch"
            if older_rows
            else None
        ),
    }


def _fetch_hourly_for_ad(
    ad_id: str,
    launch_date: str,
) -> list[dict]:
    """Делает ОДИН FB-запрос level=ad + breakdowns=hourly за [launch_date, launch_date+2дня].

    Запрос идёт на УЗЕЛ объявления {ad_id}/insights, а не act_.../insights:
    узлу кабинет не нужен, поэтому один и тот же вызов работает для объявлений
    cabinet_a и «ACME cabinet_b» (оффлайн-токен имеет ads_management во всех
    кабинетах карты роутинга) и для KB-фолбэка, где кабинет кандидата неизвестен.

    Диапазон = дата запуска + 2 дня (охватывает первые 48ч по любой границе суток).
    Возвращает сырой список row-dict из data[] ответа FB. Бросает FBApiError при устойчивом
    rate-limit (как _throttled_get). Пустой ответ → [].
    ВАЖНО: hourly_stats_aggregated_by_advertiser_time_zone указывается ТОЛЬКО в breakdowns,
    НЕ в fields (иначе FB #100 'is not valid for fields param').
    """
    token = get_fb_token()

    since = launch_date
    until = (date.fromisoformat(launch_date) + timedelta(days=2)).isoformat()

    params = {
        "access_token": token,
        "level": "ad",
        "fields": "ad_id,date_start,spend,impressions,clicks,actions",
        "breakdowns": "hourly_stats_aggregated_by_advertiser_time_zone",
        "time_range": json.dumps({"since": since, "until": until}),
        # БЕЗ time_increment=1 FB складывает весь time_range в 24 строки «час
        # суток» с date_start=since: «первые 6 часов» получались суммой этого
        # часа за три дня (проверено: без параметра 24 строки одной
        # даты, с ним — строки по каждой дате окна). Только так datetime_hour —
        # настоящий час жизни объявления.
        "time_increment": 1,
        "limit": 300,
    }

    resp = _throttled_get(f"{API}/{ad_id}/insights", params=params)

    if resp.status_code != 200:
        logger.warning(
            "_fetch_hourly_for_ad: FB вернул %d для ad_id=%s — пропускаем", resp.status_code, ad_id
        )
        return []

    data = resp.json()
    return data.get("data", [])


def _parse_hourly_rows(rows: list[dict], ad_id: str) -> list[dict]:
    """Парсит сырые FB-строки в записи для UPSERT.

    Из каждой строки берёт:
      - hourly_stats_aggregated_by_advertiser_time_zone: 'HH:MM:SS - HH:MM:SS' → час (int 0..23)
      - date_start: 'YYYY-MM-DD' (день по advertiser_time_zone)
      - datetime_hour: f'{date_start}T{hour:02d}:00:00' (ключ идемпотентности)
      - spend (float), impressions (int), clicks (int)
      - actions_lead: канонический Meta lead total без двойного счёта компонентов
      - video_3s: сумма value по action_type == 'video_view' из actions
    Строки без валидного часа пропускает. Returns: список dict, готовых для _upsert_hourly.
    """
    result: list[dict] = []

    for row in rows:
        hourly_str = row.get("hourly_stats_aggregated_by_advertiser_time_zone")
        if not hourly_str:
            continue
        try:
            hour = int(hourly_str.split(":")[0].split(" ")[0])
        except (ValueError, IndexError, AttributeError):
            continue

        date_start = row.get("date_start")
        if not date_start:
            continue

        lead_result = parse_meta_lead_actions(row.get("actions"))
        if lead_result.canonical_total is None:
            raise ValueError(f"invalid Meta lead actions: {','.join(lead_result.problems)}")
        if lead_result.problems:
            logger.warning("Meta lead components расходятся: %s", ",".join(lead_result.problems))
        actions_lead = lead_result.canonical_total

        video_3s = 0
        for action in row.get("actions") or []:
            atype = action.get("action_type", "")
            val = int(float(action.get("value") or 0))
            if atype == "video_view":
                video_3s += val

        result.append({
            "ad_id": row.get("ad_id") or ad_id,
            "datetime_hour": f"{date_start}T{hour:02d}:00:00",
            "spend": float(row.get("spend") or 0),
            "impressions": int(row.get("impressions") or 0),
            "clicks": int(row.get("clicks") or 0),
            "actions_lead": actions_lead,
            "lead_semantics_version": 2,
            "lead_parse_status": lead_result.status,
            "video_3s": video_3s,
        })

    return result


def _upsert_hourly(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """UPSERT строк в ad_hourly_metrics (ON CONFLICT(ad_id, datetime_hour) DO UPDATE).
    Делает conn.commit(). Возвращает число обработанных строк."""
    if not rows:
        return 0

    sql = """
        INSERT INTO ad_hourly_metrics
            (ad_id, datetime_hour, spend, impressions, clicks, actions_lead,
             lead_semantics_version, lead_parse_status, video_3s)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ad_id, datetime_hour) DO UPDATE SET
            spend        = excluded.spend,
            impressions  = excluded.impressions,
            clicks       = excluded.clicks,
            actions_lead = excluded.actions_lead,
            lead_semantics_version = excluded.lead_semantics_version,
            lead_parse_status = excluded.lead_parse_status,
            video_3s     = excluded.video_3s
    """

    count = 0
    for row in rows:
        conn.execute(sql, (
            row["ad_id"],
            row["datetime_hour"],
            row["spend"],
            row["impressions"],
            row["clicks"],
            row["actions_lead"],
            row["lead_semantics_version"],
            row["lead_parse_status"],
            row["video_3s"],
        ))
        count += 1

    conn.commit()
    return count


def collect_hourly_metrics(
    now_local: datetime | None = None,
    max_ads_per_run: int = 30,
) -> dict:
    """Собирает hourly-метрики объявлений младше 48ч и делает UPSERT в ad_hourly_metrics.

    now_local=None → datetime.now(_TZ_LOCAL).
    Потолок: не более max_ads_per_run FB-запросов (по одному на объявление), приоритет —
    объявления без данных. При упоре в потолок логирует warning и обрабатывает первые
    max_ads_per_run.

    НИКОГДА не бросает наружу: FBApiError/сеть/битый JSON ловятся, возвращается отчёт.

    Returns dict:
      {"candidates": int, "processed_ads": int, "upserted_rows": int,
       "capped": bool, "rate_limited": bool, "error": str | None}
    """
    if now_local is None:
        now_local = datetime.now(_TZ_LOCAL)

    result = {
        "candidates": 0,
        "processed_ads": 0,
        "upserted_rows": 0,
        "capped": False,
        "rate_limited": False,
        "error": None,
        "lead_semantics_status": None,
    }

    conn = None
    try:
        conn = _get_connection()
        result["lead_semantics_status"] = get_hourly_lead_semantics_status(conn, now_local)

        candidates = _select_candidate_ads(conn, now_local)
        result["candidates"] = len(candidates)

        if not candidates:
            return result

        prioritized = _prioritize_candidates(conn, candidates)

        capped = len(prioritized) > max_ads_per_run
        if capped:
            logger.warning(
                "collect_hourly_metrics: кандидатов %d > потолка %d — обрабатываем первые %d",
                len(prioritized), max_ads_per_run, max_ads_per_run,
            )
        result["capped"] = capped

        to_process = prioritized[:max_ads_per_run]

        upserted_total = 0
        processed = 0
        for ad_id, launch_date in to_process:
            raw_rows = _fetch_hourly_for_ad(ad_id, launch_date)
            processed += 1
            if not raw_rows:
                continue
            parsed_rows = _parse_hourly_rows(raw_rows, ad_id)
            upserted_total += _upsert_hourly(conn, parsed_rows)

        result["processed_ads"] = processed
        result["upserted_rows"] = upserted_total
        result["lead_semantics_status"] = get_hourly_lead_semantics_status(conn, now_local)
        return result

    except FBApiError as exc:
        logger.warning("collect_hourly_metrics: FB rate-limit / ошибка — %s", exc)
        result["rate_limited"] = True
        return result
    except Exception as exc:
        logger.error("collect_hourly_metrics: неожиданная ошибка — %s", exc)
        result["error"] = str(exc)
        return result
    finally:
        if conn is not None:
            conn.close()
