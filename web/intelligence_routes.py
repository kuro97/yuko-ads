"""
FastAPI роутер для Creative Intelligence API.
Эндпоинты: синхронизация KB, список креативов, диагностика одного/всех.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from services.creative_intelligence import (
    sync_knowledge_base,
    get_kb_creatives,
    diagnose_creative,
    diagnose_all,
    find_similar_winners,
    _get_connection,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])


# --- Pydantic-модели ответов ---

class SyncResponse(BaseModel):
    synced: int
    new: int
    updated: int
    sources: dict
    scoring: dict | None = None  # заполняется если auto_score=True


# --- Эндпоинты ---

@router.post("/sync", response_model=SyncResponse)
async def sync_kb(auto_score: bool = False):
    """Запустить синхронизацию Knowledge Base из learner_results, amo_data, vision_analysis.

    Query params:
        auto_score: если True — после синка автоматически скорит все новые/устаревшие креативы.
    """
    try:
        result = await asyncio.to_thread(sync_knowledge_base, auto_score)
        return SyncResponse(**result)
    except Exception as exc:
        logger.error("Ошибка синхронизации KB: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


class KBListResponse(BaseModel):
    total: int
    creatives: list[dict]


@router.get("/knowledge-base", response_model=KBListResponse)
async def list_kb_creatives(
    creative_class: str | None = Query(default=None, description="Winner / Clickbait / Hidden Gem / Dead"),
    city: str | None = Query(default=None),
    grade: str | None = Query(default=None, description="excellent / good / mediocre / poor"),
    score_min: int | None = Query(default=None, ge=0, le=100, description="Минимальный total_score"),
    score_max: int | None = Query(default=None, ge=0, le=100, description="Максимальный total_score"),
    sort_by: str = Query(default="spend", description="Поле сортировки: spend | total_score | cpl | romi | hook_rate | leads | ctr | scored_at"),
    sort_order: str = Query(default="desc", description="Направление: asc / desc"),
    limit: int = Query(default=100, ge=1),
):
    """Список креативов из Knowledge Base с опциональной фильтрацией и сортировкой."""
    # Принудительное ограничение — не более 500
    effective_limit = min(limit, 500)
    try:
        creatives = await asyncio.to_thread(
            get_kb_creatives,
            creative_class,
            city,
            grade,
            score_min,
            score_max,
            sort_by,
            sort_order,
            effective_limit,
        )
        return {"total": len(creatives), "creatives": creatives}
    except Exception as exc:
        logger.error("Ошибка получения KB: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/diagnose/{ad_id}")
async def diagnose_one(ad_id: str):
    """Диагностика одного объявления из Knowledge Base.

    Загружает ad из creative_kb, запускает каскадную диагностику,
    подтягивает топ-3 похожих winners.
    """
    # Загружаем ad из KB напрямую по ad_id
    def _fetch_ad(ad_id: str) -> dict | None:
        conn = _get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM creative_kb WHERE ad_id = ?", (ad_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    try:
        ad = await asyncio.to_thread(_fetch_ad, ad_id)
    except RuntimeError as exc:
        # KB не инициализирована
        raise HTTPException(status_code=503, detail=str(exc))

    if ad is None:
        raise HTTPException(status_code=404, detail="Объявление не найдено в Knowledge Base")

    # Диагностика и поиск похожих winners — оба блокирующие, запускаем последовательно
    # (find_similar_winners зависит от результата diagnose только косвенно, но выполняются быстро)
    try:
        diagnosis = await asyncio.to_thread(diagnose_creative, ad)
        similar = await asyncio.to_thread(find_similar_winners, ad_id, 3)
    except Exception as exc:
        logger.error("Ошибка диагностики %s: %s", ad_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "ad_id": ad_id,
        "ad_name": ad.get("ad_name", ""),
        "level": diagnosis["level"],
        "message": diagnosis["message"],
        "recommendation": diagnosis["recommendation"],
        "metrics": diagnosis["metrics"],
        "similar_winners": similar,
    }


@router.get("/diagnose-all")
async def diagnose_all_creatives(
    only_active: bool = Query(default=True, description="Диагностировать только ACTIVE объявления"),
):
    """Диагностика всех объявлений из Knowledge Base.

    Возвращает итог по уровням и список диагнозов, отсортированных по приоритету.
    """
    try:
        result = await asyncio.to_thread(diagnose_all, only_active)
        return result
    except Exception as exc:
        logger.error("Ошибка diagnose_all: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/adsets-discovered")
async def get_discovered_adsets(force_refresh: bool = False):
    """Что система обнаружила в FB сейчас (для дебага).
    force_refresh=true — сбросить кеш и перезапросить FB API.
    """
    from agent.adset_discovery import discover_adsets
    try:
        result = await asyncio.to_thread(discover_adsets, force_refresh)
        return result
    except Exception as exc:
        logger.error("Ошибка discover_adsets: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
