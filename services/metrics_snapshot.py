"""
Дневной снапшот метрик объявлений → таблица ad_daily_metrics.

Логика:
- capture_daily_snapshot() берёт метрики за вчера (по локальному времени) и делает UPSERT по (ad_id, date).
- Резюмируемый: при FBApiError молча возвращает {"rate_limited": True} — следующий прогон догонит.
- Запускается ночным кроном из web/app.py (_cron_metrics_snapshot).
"""

import json
import hashlib
import logging
import os
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.fb_common import API, FBApiError, _throttled_get
from services.creative_intelligence import _get_connection
from services.fb_token_provider import get_fb_account_id, get_fb_token
from services.meta_lead_actions import parse_meta_lead_actions
from services.approval_checker_models import (
    CampaignChunkCoverage,
    DailyInventorySnapshot,
    DailyMetricsSnapshotResult,
    PaginationCoverage,
    TimeWindow,
    canonical_json,
)

logger = logging.getLogger(__name__)

# Локальное время (UTC+5 по умолчанию, настраивается)
_TZ_LOCAL = timezone(timedelta(hours=5))

# Путь к кешу created_times (рядом с data/decisions.db)
_CREATED_TIMES_CACHE_PATH = Path(__file__).parent.parent / "data" / "ad_created_times.json"
# Считать кеш «свежим» если файл не старше N дней
_CREATED_TIMES_CACHE_TTL_DAYS = 7


def _created_times_cache_path() -> Path:
    """Путь кеша created_times активного кабинета.

    Дефолтный кабинет (config.FB_ACCOUNT_ID) хранит legacy-файл
    ad_created_times.json; маршрутизированный кабинет карты роутинга — свой
    файл с суффиксом account_id. Иначе кабинеты подменяют инвентарь друг
    друга: свежий кеш cabinet_a отдаёт пустые created_time для cabinet_b, и
    day_since_launch у всего кабинета обнуляется.
    """
    try:
        account_id = get_fb_account_id()
        from config import FB_ACCOUNT_ID

        default_account = str(FB_ACCOUNT_ID or "").replace("act_", "").strip()
    except Exception:
        return _CREATED_TIMES_CACHE_PATH
    if not default_account or account_id == default_account:
        return _CREATED_TIMES_CACHE_PATH
    return _CREATED_TIMES_CACHE_PATH.with_name(f"ad_created_times_{account_id}.json")


def _compute_day_since_launch(created_time: str, on_date: str) -> int:
    """(on_date - created_time.date()).days, не меньше 0. created_time — ISO от FB."""
    try:
        # FB возвращает формат: '2024-01-01T12:00:00+0000'
        # Убираем временну́ю зону и парсим только дату
        created_dt = datetime.fromisoformat(created_time.replace("+0000", "+00:00"))
        created_date = created_dt.date()
        target_date = date.fromisoformat(on_date)
        delta = (target_date - created_date).days
        return max(0, delta)
    except Exception:
        return 0


def _parse_daily_row(row: dict, created_times: dict, on_date: str) -> dict:
    """Парсит одну строку FB insights (level=ad, time_increment=1) в словарь для UPSERT."""
    ad_id = row.get("ad_id", "")
    row_date = row.get("date_start") or on_date
    spend = float(row.get("spend") or 0)
    impressions = int(row.get("impressions") or 0)
    clicks = int(row.get("clicks") or 0)
    ctr = float(row.get("ctr") or 0)

    lead_result = parse_meta_lead_actions(row.get("actions"))
    if lead_result.canonical_total is None:
        raise ValueError(f"invalid Meta lead actions: {','.join(lead_result.problems)}")
    if lead_result.problems:
        logger.warning("Meta lead components расходятся: %s", ",".join(lead_result.problems))
    leads = lead_result.canonical_total

    # Считаем 3-секундные просмотры из actions (как в parse_video_insight_row)
    video_views_3s = 0
    for action in row.get("actions") or []:
        atype = action.get("action_type", "")
        val = int(action.get("value", 0))
        if atype == "video_view":
            video_views_3s += val

    cpl = spend / leads if leads > 0 else 0.0

    # Видео-метрики: hook_rate и hold_rate (None для не-видео)
    # thruplay берём из video_thruplay_watched_actions (валидное поле FB API)
    thruplay = 0
    for action in row.get("video_thruplay_watched_actions", []):
        thruplay += int(action.get("value", 0))

    hook_rate = None
    hold_rate = None
    if video_views_3s > 0 or thruplay > 0:
        # Есть видео-метрики → считаем по той же формуле что в creative_backfill.py:
        # hook_rate = 3s-просмотры / показы * 100
        # hold_rate = thruplay / 3s-просмотры * 100
        if impressions > 0:
            hook_rate = video_views_3s / impressions * 100
        else:
            hook_rate = 0.0
        hold_rate = thruplay / video_views_3s * 100 if video_views_3s > 0 else 0.0

    # day_since_launch
    created_time = created_times.get(ad_id, "")
    day_since_launch = _compute_day_since_launch(created_time, row_date) if created_time else 0

    return {
        "ad_id": ad_id,
        "date": row_date,
        "spend": round(spend, 4),
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(ctr, 4),
        "leads": leads,
        "lead_semantics_version": 2,
        "lead_parse_status": lead_result.status,
        "cpl": round(cpl, 4),
        "hook_rate": hook_rate,
        "hold_rate": hold_rate,
        "video_views_3s": video_views_3s,
        "day_since_launch": day_since_launch,
    }


def _load_created_times_cache(cache_path: Path | None = None) -> dict | None:
    """Читает кеш created_times из JSON-файла если он существует и не устарел.

    Возвращает словарь {ad_id: created_time} или None если кеш отсутствует/устарел.
    cache_path=None → использует дефолтный _CREATED_TIMES_CACHE_PATH.
    """
    path = cache_path or _CREATED_TIMES_CACHE_PATH
    try:
        if not path.exists():
            return None
        # Проверяем возраст файла
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age_days = (datetime.now(timezone.utc) - mtime).total_seconds() / 86400
        if age_days > _CREATED_TIMES_CACHE_TTL_DAYS:
            logger.info("_load_created_times_cache: кеш устарел (%.1f дн.) — перетянем из FB", age_days)
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        logger.info("_load_created_times_cache: загружено %d записей из кеша (возраст %.1f дн.)", len(data), age_days)
        return data
    except Exception as exc:
        logger.warning("_load_created_times_cache: ошибка чтения — %s", exc)
        return None


def _save_created_times_cache(data: dict, cache_path: Path | None = None) -> None:
    """Атомарно сохраняет created_times в JSON-файл кеша.

    Атомарность: пишем во временный файл → os.replace.
    Ошибки логируем как warning (кеш некритичен).
    cache_path=None → использует дефолтный _CREATED_TIMES_CACHE_PATH.
    """
    path = cache_path or _CREATED_TIMES_CACHE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))
        logger.info("_save_created_times_cache: сохранено %d записей → %s", len(data), path)
    except Exception as exc:
        logger.warning("_save_created_times_cache: ошибка записи — %s", exc)


def _fetch_created_times(cache_path: Path | None = None) -> dict:
    """Тянет карту {ad_id: created_time} для всех объявлений кабинета.

    Сначала проверяет дисковый кеш (data/ad_created_times.json):
    - если файл свежее _CREATED_TIMES_CACHE_TTL_DAYS дней — возвращает его (~0с)
    - иначе тянет из FB (~145с) и сохраняет кеш атомарно

    Вызывать ОДИН РАЗ на бэкфилл-сессию — не на каждое подокно.
    При ошибке возвращает пустой словарь (day_since_launch будет 0).
    cache_path — для тестов, переопределяет путь к кешу.
    """
    # Кеш выбирается по активному кабинету — у маршрутизированного свой файл
    resolved_cache_path = cache_path or _created_times_cache_path()

    # Пробуем загрузить из кеша
    cached = _load_created_times_cache(cache_path=resolved_cache_path)
    if cached is not None:
        return cached

    # Кеша нет или устарел — тянем из FB
    account_id = get_fb_account_id()
    token = get_fb_token()
    created_times: dict = {}
    try:
        ads_resp = _throttled_get(
            f"{API}/act_{account_id}/ads",
            params={
                "access_token": token,
                "fields": "id,created_time",
                "limit": 500,
            },
        )
        if ads_resp.status_code != 200:
            logger.warning("_fetch_created_times: FB вернул %d", ads_resp.status_code)
            return created_times
        ads_data = ads_resp.json()
        for a in ads_data.get("data", []):
            created_times[a["id"]] = a.get("created_time", "")
        # Пагинация
        while ads_data.get("paging", {}).get("next"):
            r2 = _throttled_get(ads_data["paging"]["next"])
            if r2.status_code != 200:
                break
            ads_data = r2.json()
            for a in ads_data.get("data", []):
                created_times[a["id"]] = a.get("created_time", "")
        # Сохраняем кеш на диск
        _save_created_times_cache(created_times, cache_path=resolved_cache_path)
    except Exception as e:
        logger.warning("_fetch_created_times: ошибка — %s", e)
    return created_times


def _fetch_daily_rows(
    date_from: str,
    date_to: str,
    created_times: dict | None = None,
) -> dict[tuple[str, str], dict]:
    """Тянет дневные метрики (level=ad, time_increment=1) за период.

    Возвращает {(ad_id, date_start): {date, spend, impressions, clicks, ctr,
              cpl, leads, hook_rate, hold_rate, video_views_3s}}. Составной
    ключ обязателен: один ad_id имеет отдельную строку на каждый день диапазона.
    При reduce-data ошибке FB деградирует на чанки по кампаниям.
    Бросает FBApiError если даже чанки не удались.

    created_times: карта {ad_id: created_time} для day_since_launch.
    Если None — тянется самостоятельно (для ночного снапшота, где вызов одиночный).
    Передавать снаружи при бэкфилле чтобы не дёргать список объявлений на каждое подокно.
    """
    account_id = get_fb_account_id()
    token = get_fb_token()

    base_params = {
        "access_token": token,
        "level": "ad",
        "time_increment": 1,
        "fields": (
            "ad_id,date_start,spend,impressions,clicks,ctr,actions,"
            "video_thruplay_watched_actions"
        ),
        "time_range": json.dumps({"since": date_from, "until": date_to}),
        "limit": 200,
    }

    # Тянем created_time только если не передана снаружи
    if created_times is None:
        created_times = _fetch_created_times()

    def _is_reduce_data_error(resp) -> bool:
        """Проверяет ошибку FB 'reduce the amount of data'."""
        if resp.status_code in (400, 500):
            try:
                err = resp.json().get("error") or {}
                code = err.get("code", 0)
                msg = (err.get("message") or "").lower()
                return code == 1 and "reduce" in msg
            except Exception:
                pass
        return False

    def _validated_payload(response, context: str) -> dict:
        """Валидирует полную страницу Graph; partial payload недопустим."""
        if response.status_code != 200:
            raise FBApiError(
                f"FB API {context} вернул HTTP {response.status_code}",
                response.status_code,
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise FBApiError(f"FB API {context}: невалидный JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise FBApiError(f"FB API {context}: payload.data должен быть list")
        return payload

    def _process_pages(response, context: str) -> dict[tuple[str, str], dict]:
        """Атомарно обрабатывает все страницы insights без частичного успеха."""
        result: dict[tuple[str, str], dict] = {}
        page_number = 1
        while True:
            payload = _validated_payload(response, f"{context}, page={page_number}")
            for row_index, row in enumerate(payload["data"]):
                if not isinstance(row, dict):
                    raise FBApiError(
                        f"FB API {context}: row {row_index} на page {page_number} не object"
                    )
                ad_id = row.get("ad_id")
                row_date = row.get("date_start")
                if not isinstance(ad_id, str) or not ad_id:
                    raise FBApiError(f"FB API {context}: строка без ad_id")
                if not isinstance(row_date, str) or not row_date:
                    raise FBApiError(f"FB API {context}: строка без date_start")
                try:
                    parsed_date = date.fromisoformat(row_date)
                except ValueError as exc:
                    raise FBApiError(
                        f"FB API {context}: невалидный date_start {row_date}"
                    ) from exc
                if not date.fromisoformat(date_from) <= parsed_date <= date.fromisoformat(date_to):
                    raise FBApiError(
                        f"FB API {context}: date_start {row_date} вне запрошенного диапазона"
                    )
                try:
                    parsed_row = _parse_daily_row(row, created_times, row_date)
                except Exception as exc:
                    raise FBApiError(
                        f"FB API {context}: parser error для ({ad_id}, {row_date})"
                    ) from exc
                key = (ad_id, row_date)
                if key in result:
                    raise FBApiError(f"FB API {context}: duplicate row {key}")
                result[key] = parsed_row

            paging = payload.get("paging") or {}
            if not isinstance(paging, dict):
                raise FBApiError(f"FB API {context}: paging должен быть object")
            next_url = paging.get("next")
            if not next_url:
                return result
            if not isinstance(next_url, str):
                raise FBApiError(f"FB API {context}: paging.next должен быть string")
            response = _throttled_get(next_url)
            page_number += 1

    def _fetch_campaign_ids(response) -> list[str]:
        """Атомарно получает все страницы кампаний для chunk fallback."""
        campaign_ids: list[str] = []
        seen_ids: set[str] = set()
        page_number = 1
        while True:
            payload = _validated_payload(response, f"campaigns, page={page_number}")
            for campaign in payload["data"]:
                if not isinstance(campaign, dict):
                    raise FBApiError("FB API campaigns: строка не object")
                campaign_id = campaign.get("id")
                if not isinstance(campaign_id, str) or not campaign_id:
                    raise FBApiError("FB API campaigns: строка без id")
                if campaign_id in seen_ids:
                    raise FBApiError(f"FB API campaigns: duplicate id {campaign_id}")
                seen_ids.add(campaign_id)
                campaign_ids.append(campaign_id)

            paging = payload.get("paging") or {}
            if not isinstance(paging, dict):
                raise FBApiError("FB API campaigns: paging должен быть object")
            next_url = paging.get("next")
            if not next_url:
                return campaign_ids
            if not isinstance(next_url, str):
                raise FBApiError("FB API campaigns: paging.next должен быть string")
            response = _throttled_get(next_url)
            page_number += 1

    # Попытка 1: полный account-level запрос
    resp = _throttled_get(f"{API}/act_{account_id}/insights", params=base_params)

    if resp.status_code == 200:
        return _process_pages(resp, "account insights")

    if not _is_reduce_data_error(resp):
        raise FBApiError(
            f"FB API daily insights ошибка: {resp.status_code} {resp.text[:200]}",
            resp.status_code,
        )

    # Попытка 2: деградация на чанки по кампаниям
    logger.warning("_fetch_daily_rows: деградируем на чанки по кампаниям")

    campaigns_resp = _throttled_get(
        f"{API}/act_{account_id}/campaigns",
        params={"access_token": token, "fields": "id,name", "limit": 200},
    )
    if campaigns_resp.status_code != 200:
        raise FBApiError(
            f"FB API campaigns ошибка: {campaigns_resp.status_code} {campaigns_resp.text[:200]}",
            campaigns_resp.status_code,
        )

    campaign_ids = _fetch_campaign_ids(campaigns_resp)
    merged: dict[tuple[str, str], dict] = {}

    # Обрабатываем чанками по 50 кампаний
    chunk_size = 50
    for i in range(0, len(campaign_ids), chunk_size):
        chunk_campaign_ids = campaign_ids[i: i + chunk_size]
        chunk_params = {
            **base_params,
            "filtering": json.dumps([{
                "field": "campaign.id",
                "operator": "IN",
                "value": chunk_campaign_ids,
            }]),
        }
        chunk_resp = _throttled_get(f"{API}/act_{account_id}/insights", params=chunk_params)
        chunk_rows = _process_pages(
            chunk_resp,
            f"campaign chunk {i // chunk_size + 1}",
        )
        duplicate_keys = merged.keys() & chunk_rows.keys()
        if duplicate_keys:
            duplicate_key = sorted(duplicate_keys)[0]
            raise FBApiError(f"FB API campaign chunks: duplicate row {duplicate_key}")
        merged.update(chunk_rows)

    return merged


def _upsert_daily(conn: sqlite3.Connection, rows: list) -> int:
    """UPSERT строк в ad_daily_metrics (ON CONFLICT(ad_id,date) DO UPDATE). Возвращает число строк."""
    if not rows:
        return 0

    sql = """
        INSERT INTO ad_daily_metrics
            (ad_id, date, spend, impressions, clicks, ctr, leads,
             lead_semantics_version, lead_parse_status, cpl,
             hook_rate, hold_rate, video_views_3s, day_since_launch)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ad_id, date) DO UPDATE SET
            spend            = excluded.spend,
            impressions      = excluded.impressions,
            clicks           = excluded.clicks,
            ctr              = excluded.ctr,
            leads            = excluded.leads,
            lead_semantics_version = excluded.lead_semantics_version,
            lead_parse_status = excluded.lead_parse_status,
            cpl              = excluded.cpl,
            hook_rate        = excluded.hook_rate,
            hold_rate        = excluded.hold_rate,
            video_views_3s   = excluded.video_views_3s,
            day_since_launch = excluded.day_since_launch
    """

    count = 0
    for row in rows:
        conn.execute(sql, (
            row["ad_id"],
            row["date"],
            row["spend"],
            row["impressions"],
            row["clicks"],
            row["ctr"],
            row["leads"],
            row["lead_semantics_version"],
            row["lead_parse_status"],
            row["cpl"],
            row.get("hook_rate"),    # может быть None
            row.get("hold_rate"),    # может быть None
            row.get("video_views_3s", 0),
            row.get("day_since_launch", 0),
        ))
        count += 1

    conn.commit()
    return count


def capture_daily_snapshot(target_date: str | None = None) -> dict:
    """Снимает дневные метрики всех объявлений за target_date (по умолчанию вчера по локальному времени)
    и делает UPSERT в ad_daily_metrics по (ad_id, date).

    Returns dict:
      {"date": "YYYY-MM-DD", "fetched": int, "upserted": int,
       "rate_limited": bool, "skipped_reason": str | None}

    НИКОГДА не бросает FBApiError наружу — rate-limit это штатная ситуация
    (resumable по (ad_id,date): пропущенный день догонится при следующем прогоне).
    """
    # Вычисляем дату (вчера по локальному времени, если не задана)
    if target_date is None:
        now_local = datetime.now(_TZ_LOCAL)
        target_date = (now_local - timedelta(days=1)).date().isoformat()

    result_base = {
        "date": target_date,
        "fetched": 0,
        "upserted": 0,
        "rate_limited": False,
        "skipped_reason": None,
    }

    try:
        rows_dict = _fetch_daily_rows(target_date, target_date)
        rows = list(rows_dict.values())
        fetched = len(rows)

        if fetched == 0:
            return {**result_base, "fetched": 0, "upserted": 0}

        conn = _get_connection()
        try:
            upserted = _upsert_daily(conn, rows)
        finally:
            conn.close()

        logger.info("capture_daily_snapshot: %s — fetched=%d upserted=%d", target_date, fetched, upserted)
        return {**result_base, "fetched": fetched, "upserted": upserted}

    except FBApiError as exc:
        is_rate_limit = exc.status_code in (429, 4, 17, 32, 80003, 80004)
        logger.warning("capture_daily_snapshot: FB incomplete/error — %s", exc)
        return {
            **result_base,
            "rate_limited": is_rate_limit,
            "skipped_reason": "rate_limit" if is_rate_limit else "fb_error",
        }
    except Exception as exc:
        logger.error("capture_daily_snapshot: неожиданная ошибка — %s", exc)
        return {
            **result_base,
            "skipped_reason": str(exc),
        }


# ---------------------------------------------------------------------------
# Strict checker evidence path. Он намеренно не вызывает legacy функции выше.
# ---------------------------------------------------------------------------


class DailyMetricsCompletenessError(RuntimeError):
    """Строгий дневной snapshot не смог доказать полное покрытие."""


def _strict_payload(response, context: str) -> dict:
    if response.status_code != 200:
        raise DailyMetricsCompletenessError(f"{context.upper()}_HTTP_{response.status_code}")
    try:
        payload = response.json()
    except Exception as exc:
        raise DailyMetricsCompletenessError(f"{context.upper()}_JSON_INVALID") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise DailyMetricsCompletenessError(f"{context.upper()}_PAYLOAD_INVALID")
    return payload


def _strict_pages(response, context: str) -> tuple[list[dict], PaginationCoverage]:
    items: list[dict] = []
    item_ids: list[str] = []
    seen_ids: set[str] = set()
    page_count = 0
    current = response
    while True:
        page_count += 1
        payload = _strict_payload(current, context)
        for raw in payload["data"]:
            if not isinstance(raw, dict):
                raise DailyMetricsCompletenessError(f"{context.upper()}_ROW_INVALID")
            item_id = raw.get("id") or raw.get("ad_id")
            if not isinstance(item_id, str) or not item_id:
                raise DailyMetricsCompletenessError(f"{context.upper()}_ID_INVALID")
            if item_id in seen_ids:
                raise DailyMetricsCompletenessError(f"{context.upper()}_DUPLICATE_ID")
            seen_ids.add(item_id)
            item_ids.append(item_id)
            items.append(raw)
        paging = payload.get("paging") or {}
        if not isinstance(paging, dict):
            raise DailyMetricsCompletenessError(f"{context.upper()}_PAGING_INVALID")
        next_url = paging.get("next")
        if not next_url:
            break
        if not isinstance(next_url, str):
            raise DailyMetricsCompletenessError(f"{context.upper()}_NEXT_INVALID")
        current = _throttled_get(next_url)
    state_sha256 = hashlib.sha256(canonical_json(items)).hexdigest()
    return items, PaginationCoverage(
        endpoint_kind=context.upper(),
        page_count=page_count,
        item_ids=tuple(item_ids),
        pagination_complete=True,
        failed_page_index=None,
        response_state_sha256=state_sha256,
    )


def _fetch_account_metadata(account_id: str) -> tuple[int, str, str, datetime]:
    response = _throttled_get(
        f"{API}/act_{account_id}",
        params={
            "access_token": get_fb_token(),
            "fields": "id,account_status,currency,timezone_name",
        },
    )
    if response.status_code != 200:
        raise DailyMetricsCompletenessError(f"ACCOUNT_HTTP_{response.status_code}")
    try:
        payload = response.json()
    except Exception as exc:
        raise DailyMetricsCompletenessError("ACCOUNT_JSON_INVALID") from exc
    if not isinstance(payload, dict) or str(payload.get("id", "")).removeprefix("act_") != account_id:
        raise DailyMetricsCompletenessError("ACCOUNT_ID_MISMATCH")
    account_status = payload.get("account_status")
    currency = payload.get("currency")
    timezone_name = payload.get("timezone_name")
    if isinstance(account_status, bool) or not isinstance(account_status, int):
        raise DailyMetricsCompletenessError("ACCOUNT_STATUS_INVALID")
    if not isinstance(currency, str) or not currency:
        raise DailyMetricsCompletenessError("ACCOUNT_CURRENCY_INVALID")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise DailyMetricsCompletenessError("ACCOUNT_TIMEZONE_INVALID")
    return account_status, currency, timezone_name, datetime.now(timezone.utc)


def _strict_write_created_times(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.strict.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        payload = canonical_json(dict(sorted(values.items())))
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _fetch_live_daily_inventory(
    account_id: str,
    target_window: TimeWindow,
    now: datetime,
    created_times_cache_path: Path | None = None,
) -> DailyInventorySnapshot:
    """Force-live account + полный ads inventory; cache никогда не задаёт universe."""

    account_status, currency, timezone_name, fetched_at = _fetch_account_metadata(account_id)
    response = _throttled_get(
        f"{API}/act_{account_id}/ads",
        params={
            "access_token": get_fb_token(),
            "fields": "id,account_id,campaign_id,created_time,status,effective_status",
            "limit": 500,
        },
    )
    ads, coverage = _strict_pages(response, "ACCOUNT_ADS")
    accessible: list[str] = []
    eligible: list[str] = []
    created_times: dict[str, str] = {}
    status_values: dict[str, tuple[str, str]] = {}
    campaign_by_ad: dict[str, str] = {}
    for ad in ads:
        ad_id = str(ad["id"])
        raw_account = str(ad.get("account_id", "")).removeprefix("act_")
        if raw_account and raw_account != account_id:
            raise DailyMetricsCompletenessError("INVENTORY_ACCOUNT_MISMATCH")
        campaign_id = ad.get("campaign_id")
        if not isinstance(campaign_id, str) or not campaign_id:
            raise DailyMetricsCompletenessError("INVENTORY_CAMPAIGN_ID_MISSING")
        created_raw = ad.get("created_time")
        if not isinstance(created_raw, str) or not created_raw:
            raise DailyMetricsCompletenessError("INVENTORY_CREATED_TIME_MISSING")
        try:
            created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00").replace("+0000", "+00:00"))
        except ValueError as exc:
            raise DailyMetricsCompletenessError("INVENTORY_CREATED_TIME_INVALID") from exc
        if created_at.tzinfo is None:
            raise DailyMetricsCompletenessError("INVENTORY_CREATED_TIME_NAIVE")
        status = ad.get("status")
        effective_status = ad.get("effective_status")
        if not isinstance(status, str) or not isinstance(effective_status, str):
            raise DailyMetricsCompletenessError("INVENTORY_STATUS_INVALID")
        accessible.append(ad_id)
        created_times[ad_id] = created_raw
        status_values[ad_id] = (status, effective_status)
        campaign_by_ad[ad_id] = campaign_id
        if created_at < target_window.end:
            eligible.append(ad_id)

    cache_path = created_times_cache_path or _CREATED_TIMES_CACHE_PATH
    cached = _load_created_times_cache(cache_path=cache_path) or {}
    cache_missing = sorted(set(created_times) - set(cached))
    cache_mismatched = sorted(
        ad_id for ad_id, created in created_times.items() if ad_id in cached and cached[ad_id] != created
    )
    merged_cache = dict(cached)
    merged_cache.update(created_times)
    _strict_write_created_times(cache_path, merged_cache)

    age_seconds = (now - fetched_at).total_seconds()
    fresh = -1 <= age_seconds <= 15 * 60
    complete = account_status == 1 and coverage.pagination_complete and fresh
    eligible_campaign_ids = tuple(sorted({campaign_by_ad[ad_id] for ad_id in eligible}))
    return DailyInventorySnapshot(
        account_id=account_id,
        account_status=account_status,
        currency=currency,
        timezone_name=timezone_name,
        target_window=target_window,
        fetched_at=fetched_at,
        max_age_seconds=15 * 60,
        ads_pagination=coverage,
        accessible_ad_ids=tuple(sorted(accessible)),
        eligible_ad_ids=tuple(sorted(eligible)),
        eligible_campaign_ids=eligible_campaign_ids,
        exact_lookup_ad_ids=tuple(sorted(set(cache_missing) | set(cache_mismatched))),
        created_time_by_ad_sha256=hashlib.sha256(canonical_json(created_times)).hexdigest(),
        status_by_ad_sha256=hashlib.sha256(canonical_json(status_values)).hexdigest(),
        campaign_by_ad_sha256=hashlib.sha256(canonical_json(campaign_by_ad)).hexdigest(),
        cache_missing_ad_ids=tuple(cache_missing),
        cache_mismatched_ad_ids=tuple(cache_mismatched),
        fresh=fresh,
        complete=complete,
    )


def _strict_insight_params(target_window: TimeWindow) -> dict[str, object]:
    day = target_window.start.date().isoformat()
    return {
        "access_token": get_fb_token(),
        "level": "ad",
        "time_increment": 1,
        "fields": (
            "ad_id,date_start,spend,impressions,clicks,ctr,actions,"
            "video_thruplay_watched_actions"
        ),
        "time_range": json.dumps({"since": day, "until": day}),
        "limit": 200,
    }


def _strict_parse_insights(
    raw_rows: list[dict],
    inventory: DailyInventorySnapshot,
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    target_date = inventory.target_window.start.date().isoformat()
    for raw in raw_rows:
        ad_id = raw.get("ad_id")
        if not isinstance(ad_id, str) or not ad_id:
            raise DailyMetricsCompletenessError("INSIGHTS_AD_ID_INVALID")
        if raw.get("date_start") != target_date:
            raise DailyMetricsCompletenessError("INSIGHTS_DATE_MISMATCH")
        if ad_id in result:
            raise DailyMetricsCompletenessError("INSIGHTS_DUPLICATE_AD")
        try:
            result[ad_id] = _parse_daily_row(raw, {}, target_date)
        except Exception as exc:
            raise DailyMetricsCompletenessError("INSIGHTS_ROW_INVALID") from exc
    return result


def _strict_is_reduce_data_error(response) -> bool:
    if response.status_code not in (400, 500):
        return False
    try:
        error = response.json().get("error") or {}
    except Exception:
        return False
    return error.get("code") == 1 and "reduce" in str(error.get("message") or "").lower()


def _fetch_account_daily_rows(
    account_id: str,
    target_window: TimeWindow,
    inventory: DailyInventorySnapshot,
) -> tuple[dict[str, dict[str, object]], PaginationCoverage]:
    response = _throttled_get(
        f"{API}/act_{account_id}/insights",
        params=_strict_insight_params(target_window),
    )
    if _strict_is_reduce_data_error(response):
        raise FBApiError("strict account insights reduce data", status_code=1)
    rows, coverage = _strict_pages(response, "ACCOUNT_INSIGHTS")
    return _strict_parse_insights(rows, inventory), coverage


def _fetch_campaign_daily_rows(
    account_id: str,
    target_window: TimeWindow,
    inventory: DailyInventorySnapshot,
) -> tuple[
    dict[str, dict[str, object]],
    PaginationCoverage,
    tuple[CampaignChunkCoverage, ...],
]:
    campaigns_response = _throttled_get(
        f"{API}/act_{account_id}/campaigns",
        params={"access_token": get_fb_token(), "fields": "id", "limit": 200},
    )
    campaigns, campaign_coverage = _strict_pages(campaigns_response, "CAMPAIGNS")
    campaign_ids = tuple(sorted(str(item["id"]) for item in campaigns))
    if campaign_ids != inventory.eligible_campaign_ids:
        raise DailyMetricsCompletenessError("CAMPAIGN_ID_COVERAGE_MISMATCH")
    # В manifest сохраняется канонический sorted набор, а response hash по-прежнему
    # привязан к исходному полностью прочитанному ответу Graph API.
    campaign_coverage = replace(campaign_coverage, item_ids=campaign_ids)
    merged: dict[str, dict[str, object]] = {}
    chunk_coverages: list[CampaignChunkCoverage] = []
    for chunk_index, offset in enumerate(range(0, len(campaign_ids), 50)):
        chunk = tuple(campaign_ids[offset : offset + 50])
        params = _strict_insight_params(target_window)
        params["filtering"] = json.dumps(
            [{"field": "campaign.id", "operator": "IN", "value": list(chunk)}]
        )
        response = _throttled_get(f"{API}/act_{account_id}/insights", params=params)
        rows, pagination = _strict_pages(response, "CHUNK_INSIGHTS")
        parsed = _strict_parse_insights(rows, inventory)
        duplicate = set(merged) & set(parsed)
        if duplicate:
            raise DailyMetricsCompletenessError("CAMPAIGN_CHUNK_DUPLICATE_AD")
        merged.update(parsed)
        chunk_coverages.append(
            CampaignChunkCoverage(
                chunk_index=chunk_index,
                campaign_ids=chunk,
                pagination=pagination,
                returned_ad_ids=tuple(sorted(parsed)),
                complete=pagination.pagination_complete,
            )
        )
    chunk_campaign_ids = tuple(
        campaign_id
        for coverage in chunk_coverages
        for campaign_id in coverage.campaign_ids
    )
    if (
        chunk_campaign_ids != inventory.eligible_campaign_ids
        or len(chunk_campaign_ids) != len(set(chunk_campaign_ids))
        or not all(coverage.complete for coverage in chunk_coverages)
    ):
        raise DailyMetricsCompletenessError("CAMPAIGN_CHUNK_COVERAGE_MISMATCH")
    return merged, campaign_coverage, tuple(chunk_coverages)


def _verified_zero_row(ad_id: str, target_date: str) -> dict[str, object]:
    return {
        "ad_id": ad_id,
        "date": target_date,
        "spend": 0.0,
        "impressions": 0,
        "clicks": 0,
        "ctr": 0.0,
        "leads": 0,
        "lead_semantics_version": 2,
        "lead_parse_status": "ok",
        "cpl": 0.0,
        "hook_rate": None,
        "hold_rate": None,
        "video_views_3s": 0,
        "day_since_launch": 0,
    }


def _resolve_extra_insight_ads(
    account_id: str,
    ad_ids: set[str],
    target_window: TimeWindow,
) -> dict[str, dict[str, str]]:
    """Force-live exact lookup для insight IDs, которых не было в inventory page set."""

    resolved: dict[str, dict[str, str]] = {}
    for ad_id in sorted(ad_ids):
        response = _throttled_get(
            f"{API}/{ad_id}",
            params={
                "access_token": get_fb_token(),
                "fields": "id,account_id,campaign_id,created_time,status,effective_status",
            },
        )
        if response.status_code != 200:
            raise DailyMetricsCompletenessError("INSIGHT_EXTRA_LOOKUP_FAILED")
        try:
            payload = response.json()
        except Exception as exc:
            raise DailyMetricsCompletenessError("INSIGHT_EXTRA_LOOKUP_JSON_INVALID") from exc
        if not isinstance(payload, dict) or payload.get("id") != ad_id:
            raise DailyMetricsCompletenessError("INSIGHT_EXTRA_LOOKUP_ID_MISMATCH")
        raw_account = str(payload.get("account_id", "")).removeprefix("act_")
        if raw_account != account_id:
            raise DailyMetricsCompletenessError("INSIGHT_EXTRA_ACCOUNT_MISMATCH")
        created_raw = payload.get("created_time")
        if not isinstance(created_raw, str):
            raise DailyMetricsCompletenessError("INSIGHT_EXTRA_CREATED_MISSING")
        try:
            created_at = datetime.fromisoformat(
                created_raw.replace("Z", "+00:00").replace("+0000", "+00:00")
            )
        except ValueError as exc:
            raise DailyMetricsCompletenessError("INSIGHT_EXTRA_CREATED_INVALID") from exc
        if created_at.tzinfo is None or created_at >= target_window.end:
            raise DailyMetricsCompletenessError("INSIGHT_EXTRA_DATE_MISMATCH")
        status = payload.get("status")
        effective_status = payload.get("effective_status")
        if not isinstance(status, str) or not isinstance(effective_status, str):
            raise DailyMetricsCompletenessError("INSIGHT_EXTRA_STATUS_INVALID")
        campaign_id = payload.get("campaign_id")
        if not isinstance(campaign_id, str) or not campaign_id:
            raise DailyMetricsCompletenessError("INSIGHT_EXTRA_CAMPAIGN_MISSING")
        resolved[ad_id] = {
            "created_time": created_raw,
            "status": status,
            "effective_status": effective_status,
            "campaign_id": campaign_id,
        }
    return resolved


def _result_payload(result: DailyMetricsSnapshotResult) -> dict[str, object]:
    return json.loads(canonical_json(result).decode("utf-8"))


def _persist_daily_manifest(
    result: DailyMetricsSnapshotResult,
    db_rows_state_sha256: str,
    *,
    path: Path | None = None,
) -> None:
    import config as runtime_config

    manifest_path = path or runtime_config.REPORT_CHECKER_METRICS_MANIFEST_PATH
    existing_runs: list[object] = []
    if manifest_path.exists():
        try:
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(current, dict) and isinstance(current.get("runs"), list):
                existing_runs = current["runs"]
        except (OSError, json.JSONDecodeError):
            existing_runs = []
    run = {
        "result": _result_payload(result),
        "persisted_at": result.completed_at.astimezone(timezone.utc).isoformat(),
        "db_rows_state_sha256": db_rows_state_sha256,
    }
    retained = [item for item in existing_runs if isinstance(item, dict)]
    retained.append(run)
    retained = retained[-32:]
    payload = canonical_json({"schema_version": 1, "runs": retained})
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(f"{manifest_path.suffix}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, manifest_path)
    directory_fd = os.open(manifest_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _incomplete_result(
    target: date,
    inventory: DailyInventorySnapshot,
    started_at: datetime,
    reason_codes: set[str],
    *,
    fetch_path: str = "ACCOUNT",
    account_coverage: PaginationCoverage | None = None,
    campaign_coverage: PaginationCoverage | None = None,
    chunks: tuple[CampaignChunkCoverage, ...] = (),
    requested: tuple[str, ...] = (),
    returned: tuple[str, ...] = (),
    fetched: tuple[str, ...] = (),
    upserted: tuple[str, ...] = (),
) -> DailyMetricsSnapshotResult:
    completed_at = max(datetime.now(timezone.utc), started_at.astimezone(timezone.utc))
    payload = {
        "target_date": target.isoformat(),
        "requested": requested,
        "returned": returned,
        "fetched": fetched,
        "upserted": upserted,
        "reasons": sorted(reason_codes),
    }
    return DailyMetricsSnapshotResult(
        target_date=target,
        inventory=inventory,
        fetch_path=fetch_path,
        account_insights=account_coverage,
        campaign_pagination=campaign_coverage,
        campaign_chunks=chunks,
        requested_ad_ids=requested,
        insights_returned_ad_ids=returned,
        verified_zero_ad_ids=(),
        fetched_ad_ids=fetched,
        upserted_ad_ids=upserted,
        started_at=started_at,
        completed_at=completed_at,
        complete=False,
        incomplete_reason_codes=tuple(sorted(reason_codes)),
        state_sha256=hashlib.sha256(canonical_json(payload)).hexdigest(),
    )


def capture_daily_snapshot_complete(
    target_date: str | None = None,
    now: datetime | None = None,
) -> DailyMetricsSnapshotResult:
    """Cron-only strict writer; checker adapters никогда не вызывают эту функцию."""

    started_at = now or datetime.now(timezone.utc)
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    account_id = get_fb_account_id()
    try:
        account_status, _currency, timezone_name, _ = _fetch_account_metadata(account_id)
        account_timezone = ZoneInfo(timezone_name)
        metadata_error: str | None = None
    except Exception as exc:
        account_status = 0
        _currency = "UNKNOWN"
        timezone_name = "UTC"
        account_timezone = timezone.utc
        metadata_error = (
            "ACCOUNT_TIMEZONE_UNKNOWN"
            if isinstance(exc, ZoneInfoNotFoundError)
            else str(exc)[:120] or type(exc).__name__
        )
    try:
        target = date.fromisoformat(target_date) if target_date is not None else (
            started_at.astimezone(account_timezone).date() - timedelta(days=1)
        )
    except ValueError as exc:
        raise ValueError("target_date должен быть YYYY-MM-DD") from exc
    window_start = datetime.combine(target, datetime.min.time(), tzinfo=account_timezone)
    window_end = window_start + timedelta(days=1)
    target_window = TimeWindow(
        start=window_start,
        end=window_end,
        timezone_name=timezone_name,
        semantic="CLOSED_ACCOUNT_DAY",
    )
    # Placeholder нужен только для fail-closed результата до inventory; обычный путь
    # немедленно заменяет его реальным snapshot.
    empty_coverage = PaginationCoverage("ACCOUNT_ADS", 0, (), False, 0, "0" * 64)
    inventory = DailyInventorySnapshot(
        account_id=account_id,
        account_status=account_status,
        currency=_currency,
        timezone_name=timezone_name,
        target_window=target_window,
        fetched_at=started_at,
        max_age_seconds=15 * 60,
        ads_pagination=empty_coverage,
        accessible_ad_ids=(),
        eligible_ad_ids=(),
        eligible_campaign_ids=(),
        exact_lookup_ad_ids=(),
        created_time_by_ad_sha256="0" * 64,
        status_by_ad_sha256="0" * 64,
        campaign_by_ad_sha256="0" * 64,
        cache_missing_ad_ids=(),
        cache_mismatched_ad_ids=(),
        fresh=False,
        complete=False,
    )
    if metadata_error is not None:
        result = _incomplete_result(target, inventory, started_at, {metadata_error})
        _persist_daily_manifest(result, "0" * 64)
        return result
    if window_end > started_at.astimezone(account_timezone):
        result = _incomplete_result(target, inventory, started_at, {"DAY_NOT_CLOSED"})
        _persist_daily_manifest(result, "0" * 64)
        return result

    reasons: set[str] = set()
    fetch_path = "ACCOUNT"
    account_coverage: PaginationCoverage | None = None
    campaign_coverage: PaginationCoverage | None = None
    chunks: tuple[CampaignChunkCoverage, ...] = ()
    rows: dict[str, dict[str, object]] = {}
    requested: tuple[str, ...] = ()
    returned: tuple[str, ...] = ()
    fetched: tuple[str, ...] = ()
    upserted: tuple[str, ...] = ()
    db_hash = "0" * 64
    try:
        inventory = _fetch_live_daily_inventory(account_id, target_window, started_at)
        requested = tuple(sorted(inventory.eligible_ad_ids))
        if not inventory.complete:
            reasons.add("INVENTORY_INCOMPLETE")
        try:
            rows, account_coverage = _fetch_account_daily_rows(account_id, target_window, inventory)
        except FBApiError as exc:
            if exc.status_code != 1:
                raise
            fetch_path = "CAMPAIGN_CHUNKS"
            rows, campaign_coverage, chunks = _fetch_campaign_daily_rows(
                account_id, target_window, inventory
            )
        returned = tuple(sorted(rows))
        extras = set(returned) - set(inventory.accessible_ad_ids)
        if extras:
            resolved_extras = _resolve_extra_insight_ads(account_id, extras, target_window)
            strict_cache = _load_created_times_cache(cache_path=_CREATED_TIMES_CACHE_PATH) or {}
            strict_cache.update(
                {ad_id: value["created_time"] for ad_id, value in resolved_extras.items()}
            )
            _strict_write_created_times(_CREATED_TIMES_CACHE_PATH, strict_cache)
            requested = tuple(sorted(set(requested) | set(resolved_extras)))
            inventory = replace(
                inventory,
                accessible_ad_ids=tuple(sorted(set(inventory.accessible_ad_ids) | set(resolved_extras))),
                eligible_ad_ids=tuple(sorted(set(inventory.eligible_ad_ids) | set(resolved_extras))),
                eligible_campaign_ids=tuple(
                    sorted(
                        set(inventory.eligible_campaign_ids)
                        | {value["campaign_id"] for value in resolved_extras.values()}
                    )
                ),
                exact_lookup_ad_ids=tuple(sorted(set(inventory.exact_lookup_ad_ids) | set(resolved_extras))),
                created_time_by_ad_sha256=hashlib.sha256(
                    canonical_json(
                        {
                            "prior": inventory.created_time_by_ad_sha256,
                            "resolved": {
                                ad_id: value["created_time"] for ad_id, value in resolved_extras.items()
                            },
                        }
                    )
                ).hexdigest(),
                status_by_ad_sha256=hashlib.sha256(
                    canonical_json(
                        {
                            "prior": inventory.status_by_ad_sha256,
                            "resolved": {
                                ad_id: (value["status"], value["effective_status"])
                                for ad_id, value in resolved_extras.items()
                            },
                        }
                    )
                ).hexdigest(),
                # Snapshot хранит commitment, а не саму карту. Для exact lookup
                # расширение канонически привязывается к исходному commitment.
                campaign_by_ad_sha256=hashlib.sha256(
                    canonical_json(
                        {
                            "prior": inventory.campaign_by_ad_sha256,
                            "resolved": {
                                ad_id: value["campaign_id"]
                                for ad_id, value in resolved_extras.items()
                            },
                        }
                    )
                ).hexdigest(),
            )
        verified_zero = tuple(sorted(set(requested) - set(returned)))
        target_iso = target.isoformat()
        for ad_id in verified_zero:
            rows[ad_id] = _verified_zero_row(ad_id, target_iso)
        fetched = tuple(sorted(rows))
        if set(fetched) != set(requested):
            reasons.add("REQUESTED_FETCHED_ID_MISMATCH")
        if reasons:
            result = _incomplete_result(
                target,
                inventory,
                started_at,
                reasons,
                fetch_path=fetch_path,
                account_coverage=account_coverage,
                campaign_coverage=campaign_coverage,
                chunks=chunks,
                requested=requested,
                returned=returned,
                fetched=fetched,
            )
            _persist_daily_manifest(result, db_hash)
            return result
        connection = _get_connection()
        try:
            _upsert_daily(connection, [rows[ad_id] for ad_id in fetched])
            db_rows = connection.execute(
                "SELECT ad_id,date,spend,impressions,clicks,ctr,leads,cpl,"
                "hook_rate,hold_rate,video_views_3s,day_since_launch "
                "FROM ad_daily_metrics WHERE date=? ORDER BY ad_id",
                (target_iso,),
            ).fetchall()
            # Берём фактическое множество строк дня целиком. Старый/лишний ID в БД
            # нельзя скрыть фильтром requested — такой run остаётся incomplete.
            upserted = tuple(sorted(str(row[0]) for row in db_rows))
            db_hash = hashlib.sha256(canonical_json([tuple(row) for row in db_rows])).hexdigest()
        finally:
            connection.close()
        if set(upserted) != set(requested):
            reasons.add("FETCHED_UPSERTED_ID_MISMATCH")
        completed_at = max(datetime.now(timezone.utc), started_at.astimezone(timezone.utc))
        complete = not reasons and inventory.complete and set(requested) == set(fetched) == set(upserted)
        state_payload = {
            "target_date": target_iso,
            "inventory": inventory,
            "fetch_path": fetch_path,
            "requested": requested,
            "returned": returned,
            "verified_zero": verified_zero,
            "fetched": fetched,
            "upserted": upserted,
            "complete": complete,
            "reasons": sorted(reasons),
        }
        result = DailyMetricsSnapshotResult(
            target_date=target,
            inventory=inventory,
            fetch_path=fetch_path,
            account_insights=account_coverage,
            campaign_pagination=campaign_coverage,
            campaign_chunks=chunks,
            requested_ad_ids=requested,
            insights_returned_ad_ids=returned,
            verified_zero_ad_ids=verified_zero,
            fetched_ad_ids=fetched,
            upserted_ad_ids=upserted,
            started_at=started_at,
            completed_at=completed_at,
            complete=complete,
            incomplete_reason_codes=tuple(sorted(reasons)),
            state_sha256=hashlib.sha256(canonical_json(state_payload)).hexdigest(),
        )
    except Exception as exc:
        code = str(exc) if isinstance(exc, (DailyMetricsCompletenessError, FBApiError)) else type(exc).__name__
        reasons.add(code[:120])
        result = _incomplete_result(
            target,
            inventory,
            started_at,
            reasons,
            fetch_path=fetch_path,
            account_coverage=account_coverage,
            campaign_coverage=campaign_coverage,
            chunks=chunks,
            requested=requested,
            returned=returned,
            fetched=fetched,
            upserted=upserted,
        )
    _persist_daily_manifest(result, db_hash)
    return result
