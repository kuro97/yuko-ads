"""FastAPI роутер для сезонов и языковой аналитики."""
import asyncio
import logging

from fastapi import APIRouter, HTTPException

from services.seasons import get_current_season, get_all_seasons
from services.language import get_language_config, get_analytics_by_language
from agent.analyzer import analyze_all
from agent.fb_common import FBApiError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["seasons"])


@router.get("/api/seasons")
async def seasons():
    """Текущий сезон + все сезоны."""
    return {
        "current_season": get_current_season(),
        "all_seasons": get_all_seasons(),
    }


@router.get("/api/analytics/by-language")
async def analytics_by_language():
    """Метрики в разрезе L2/L1."""
    try:
        ads = await asyncio.to_thread(analyze_all)
    except FBApiError as e:
        raise HTTPException(502, detail=f"Ошибка FB API: {e}")

    languages = get_analytics_by_language(ads)

    # Добавляем display_name из конфига
    lang_config = get_language_config()
    for lang_id, metrics in languages.items():
        metrics["display_name"] = lang_config.get(lang_id, {}).get("display_name", lang_id)

    return {
        "languages": languages,
        "config": lang_config,
    }
