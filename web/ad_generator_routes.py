"""FastAPI роутер для Ad Generator V2 — генерация текстов, черновики, таксономия, learnings."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent.copywriter_v2 import AdBrief
from services import ad_generator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ad-generator", tags=["ad-generator"])


# --- Pydantic-модели запросов ---

class ApproveRequest(BaseModel):
    """Запрос на одобрение черновика."""
    approved_by: str = Field(default="user", description="Кто одобрил")


class RejectRequest(BaseModel):
    """Запрос на отклонение черновика."""
    feedback: str = Field(description="Причина отклонения / что исправить")


class AddLearningRequest(BaseModel):
    """Запрос на добавление нового learning в KB."""
    statement: str = Field(description="Вывод / гипотеза")
    evidence_ad_ids: list[str] = Field(default=[], description="Список ad_id как доказательства")
    confidence: str = Field(default="hypothesis", description="hypothesis / probable / confirmed")
    tags: str = Field(default="", description="Теги через запятую")


# --- Вспомогательная функция маппинга ValueError → HTTP ---

def _handle_service_error(e: ValueError) -> HTTPException:
    """Конвертирует ValueError из сервисного слоя в HTTP-исключение по правилам спеки."""
    msg = str(e)
    if "не найден" in msg:
        return HTTPException(status_code=404, detail=msg)
    elif "уже обработан" in msg:
        return HTTPException(status_code=409, detail=msg)
    elif "API_KEY" in msg:
        return HTTPException(status_code=503, detail=msg)
    else:
        return HTTPException(status_code=400, detail=msg)


# --- 6.1 POST /api/ad-generator/generate ---

@router.post("/generate")
async def generate_ads(brief: AdBrief):
    """Генерация рекламных текстов через Claude Sonnet со structured output.

    Принимает бриф, запускает full pipeline:
    few-shot из KB → generate_ad_batch → сохранение черновиков → лог LLM.
    """
    try:
        result = await asyncio.to_thread(ad_generator.generate_drafts, brief)
        return result
    except ValueError as e:
        raise _handle_service_error(e)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# --- 6.2 GET /api/ad-generator/drafts ---

@router.get("/drafts")
async def list_drafts(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Список черновиков с пагинацией и фильтрацией по статусу.

    status: pending_review / approved / rejected / None (все)
    """
    try:
        result = await asyncio.to_thread(ad_generator.get_drafts, status, limit, offset)
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# --- 6.3 POST /api/ad-generator/drafts/{draft_id}/approve ---

@router.post("/drafts/{draft_id}/approve")
async def approve_draft(draft_id: int, body: ApproveRequest):
    """Одобряет черновик — меняет status на 'approved'."""
    try:
        result = await asyncio.to_thread(
            ad_generator.approve_draft, draft_id, body.approved_by
        )
        return result
    except ValueError as e:
        raise _handle_service_error(e)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# --- 6.4 POST /api/ad-generator/drafts/{draft_id}/reject ---

@router.post("/drafts/{draft_id}/reject")
async def reject_draft(draft_id: int, body: RejectRequest):
    """Отклоняет черновик с обязательной причиной (feedback)."""
    try:
        result = await asyncio.to_thread(
            ad_generator.reject_draft, draft_id, body.feedback
        )
        return result
    except ValueError as e:
        raise _handle_service_error(e)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# --- 6.5 GET /api/ad-generator/taxonomy ---

@router.get("/taxonomy")
async def get_taxonomy():
    """Таксономия из KB: типы хуков, углы подачи, типы офферов."""
    try:
        result = await asyncio.to_thread(ad_generator.get_taxonomy)
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# --- 6.6 GET /api/ad-generator/learnings ---

@router.get("/learnings")
async def list_learnings(limit: int = 50, offset: int = 0):
    """Список learnings (выводов из экспериментов) с пагинацией."""
    try:
        result = await asyncio.to_thread(ad_generator.get_learnings, limit, offset)
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# --- 6.7 POST /api/ad-generator/learnings ---

@router.post("/learnings")
async def add_learning(body: AddLearningRequest):
    """Добавляет новый learning в таблицу learnings."""
    try:
        result = await asyncio.to_thread(
            ad_generator.add_learning,
            body.statement,
            body.evidence_ad_ids,
            body.confidence,
            body.tags,
        )
        return result
    except ValueError as e:
        raise _handle_service_error(e)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
