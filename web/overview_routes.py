"""FastAPI роутер для обзорного дашборда."""
import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException

from services.overview import get_overview, build_overview_from_ads
from services.meta_lead_actions import parse_meta_lead_actions
from agent.fb_common import FBApiError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["overview"])

# Простой кэш в памяти: {days: {"data": result, "ts": float}}
_cache: dict = {}

# Время жизни кэша — 25 минут (фоновый прогрев каждые 20 мин держит его тёплым)
_CACHE_TTL = 1500


@router.get("/api/overview")
async def api_overview(days: int = 14):
    """Обзор по городам + алерты за период."""
    if days not in (7, 14, 30, 90):
        raise HTTPException(status_code=400, detail="days должен быть 7, 14, 30 или 90")

    # Проверяем кэш overview
    cached = _cache.get(days)
    if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
        return cached["data"]

    try:
        result = await asyncio.to_thread(get_overview, days)
        _cache[days] = {"data": result, "ts": time.time()}
        return result
    except (FBApiError, Exception) as e:
        logger.warning("FB API overview error: %s — пробуем из analytics кеша", e)
        # Fallback 1: устаревший overview кеш
        if cached:
            return cached["data"]
        # Fallback 2: строим overview из analytics кеша
        try:
            from web.app import get_cached_analytics
            from datetime import datetime, timedelta
            date_to = datetime.now().strftime("%Y-%m-%d")
            date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            ads = get_cached_analytics(date_from, date_to)
            if ads:
                result = build_overview_from_ads(ads, days)
                _cache[days] = {"data": result, "ts": time.time()}
                return result
        except Exception:
            pass
        # Fallback 3: любой overview кеш
        for v in _cache.values():
            if v.get("data"):
                return v["data"]
        raise HTTPException(status_code=502, detail=f"FB API недоступен: {e}")


_trends_cache: dict = {}
_TRENDS_TTL = 1800  # 30 мин — тренды меняются медленно


@router.get("/api/overview/trends")
async def api_overview_trends(days: int = 14):
    """Динамика расход/лиды по дням (1 запрос FB insights с time_increment=1).
    Агрессивный кеш 30 мин — тренды меняются медленно, экономим rate limit."""
    if days not in (7, 14, 30, 90):
        raise HTTPException(status_code=400, detail="days должен быть 7, 14, 30 или 90")

    cached = _trends_cache.get(days)
    if cached and (time.time() - cached["ts"]) < _TRENDS_TTL:
        return cached["data"]

    try:
        result = await asyncio.to_thread(_fetch_trends, days)
        _trends_cache[days] = {"data": result, "ts": time.time()}
        return result
    except Exception as e:
        logger.warning("trends error: %s", e)
        if cached:
            return cached["data"]
        # Пустой — фронт покажет заглушку, не ломаемся
        return {"dates": [], "spend": [], "leads": []}


def _fetch_trends(days: int) -> dict:
    """Один запрос account-insights с разбивкой по дням."""
    import json as _json
    from datetime import datetime, timedelta
    from agent.fb_common import _throttled_get, API
    from services.fb_token_provider import get_fb_token, get_fb_account_id

    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    resp = _throttled_get(
        f"{API}/act_{get_fb_account_id()}/insights",
        params={
            "access_token": get_fb_token(),
            "level": "account",
            "fields": "spend,actions",
            "time_range": _json.dumps({"since": date_from, "until": date_to}),
            "time_increment": 1,  # разбивка по дням
            "limit": 100,
        },
    )
    if resp.status_code != 200:
        raise FBApiError(f"trends insights {resp.status_code}", resp.status_code)

    dates, spend, leads = [], [], []
    for row in resp.json().get("data", []):
        dates.append(row.get("date_start", "")[5:])  # MM-DD
        spend.append(round(float(row.get("spend", 0)), 2))
        lead_result = parse_meta_lead_actions(row.get("actions"))
        if lead_result.canonical_total is None:
            raise ValueError(f"invalid Meta lead actions: {','.join(lead_result.problems)}")
        if lead_result.problems:
            logger.warning("Meta lead components расходятся: %s", ",".join(lead_result.problems))
        # Этот график исторически показывает только лид-формы. Website/MQL
        # компоненты здесь намеренно не расширяют продуктовую семантику.
        leads.append(lead_result.instant_form or 0)

    # Квал-лиды по дням из AMO (по дате создания лида). Не критично —
    # если AMO недоступна, просто вернём нули, график не сломается.
    qual_by_day: dict = {}
    try:
        from integrations.amo import get_leads, classify_lead
        for l in get_leads(days):
            if classify_lead(l) in ("квал", "оплата"):
                ts = l.get("created_at") or 0
                day = datetime.fromtimestamp(ts).strftime("%m-%d")
                qual_by_day[day] = qual_by_day.get(day, 0) + 1
    except Exception as e:
        logger.warning("trends qual leads failed: %s", e)
    qual = [qual_by_day.get(d, 0) for d in dates]

    return {"dates": dates, "spend": spend, "leads": leads, "qual": qual}


@router.post("/api/overview/refresh")
async def api_overview_refresh(days: int = 14):
    """Сбрасывает кэш и принудительно загружает свежие данные."""
    if days not in (7, 14, 30, 90):
        raise HTTPException(status_code=400, detail="days должен быть 7, 14, 30 или 90")

    _cache.pop(days, None)
    logger.info("Кэш сброшен для days=%d, загружаем свежие данные", days)

    try:
        result = await asyncio.to_thread(get_overview, days)
        _cache[days] = {"data": result, "ts": time.time()}
        return result
    except FBApiError as e:
        raise HTTPException(status_code=502, detail=f"FB API недоступен: {e}")
    except Exception as e:
        logger.exception("Ошибка в /api/overview/refresh")
        raise HTTPException(status_code=500, detail=str(e))
