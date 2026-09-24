"""Ad Generator — бизнес-логика генерации, черновиков, таксономии, learnings."""

import json
import logging
import sqlite3
from datetime import datetime

from agent.copywriter_v2 import AdBrief, generate_ad_batch
from services.creative_intelligence import get_few_shot_examples
from services import llm_logger

logger = logging.getLogger(__name__)


def _get_connection() -> sqlite3.Connection:
    """Подключение к БД через creative_intelligence.DB_PATH."""
    from services.creative_intelligence import DB_PATH
    if DB_PATH is None:
        raise RuntimeError("KB не инициализирована")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def generate_drafts(brief: AdBrief) -> dict:
    """Полный пайплайн: few-shot → генерация → сохранение черновиков → лог LLM.

    1. Подтягивает few-shot из KB (get_few_shot_examples)
    2. Вызывает generate_ad_batch()
    3. Сохраняет каждый variant в ad_drafts (status='pending_review')
    4. Логирует LLM-вызов через llm_logger

    Returns:
        {
            "drafts": [dict],  # сохранённые черновики с id
            "model": str,
            "llm_call_id": int,
            "input_tokens": int,
            "output_tokens": int,
            "latency_ms": int,
            "few_shot_count": int,
        }

    Raises:
        ValueError: невалидный brief
        RuntimeError: ошибка Claude API
    """
    # 1. Подтягиваем few-shot примеры из KB
    winners, losers = get_few_shot_examples(
        product=brief.product_line, city=brief.city, language=brief.language
    )

    # Подтягиваем top-N подтверждённых уроков для промпта (пустой список → поведение прежнее)
    top_learnings = _get_top_learnings(limit=8)

    # 2. Генерируем варианты через copywriter_v2
    batch, usage = generate_ad_batch(brief, winners, losers, learnings=top_learnings)

    # 3. Сохраняем черновики в ad_drafts
    conn = _get_connection()
    saved_drafts = []
    try:
        for variant in batch.variants:
            cur = conn.execute(
                """INSERT INTO ad_drafts
                   (hook, body, cta, angle, format, target_persona,
                    primary_language, rationale, brief_json, model, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review')""",
                (
                    variant.hook,
                    variant.body,
                    variant.cta,
                    variant.angle,
                    variant.format,
                    variant.target_persona,
                    variant.primary_language,
                    variant.rationale,
                    brief.model_dump_json(),
                    usage.get("model", ""),
                ),
            )
            draft_id = cur.lastrowid
            saved_drafts.append({
                "id": draft_id,
                "hook": variant.hook,
                "body": variant.body,
                "cta": variant.cta,
                "angle": variant.angle,
                "hook_type": getattr(variant, "hook_type", ""),
                "format": variant.format,
                "target_persona": variant.target_persona,
                "primary_language": variant.primary_language,
                "rationale": variant.rationale,
                "status": "pending_review",
            })
        conn.commit()
    finally:
        conn.close()

    # 4. Логируем LLM-вызов
    llm_call_id = llm_logger.log_llm_call(
        model=usage.get("model", ""),
        purpose="generate_ad_batch",
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        latency_ms=usage.get("latency_ms", 0),
        cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
    )

    # Привязываем llm_call_id к сохранённым черновикам
    conn = _get_connection()
    try:
        for d in saved_drafts:
            conn.execute(
                "UPDATE ad_drafts SET llm_call_id = ? WHERE id = ?",
                (llm_call_id, d["id"]),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "drafts": saved_drafts,
        "model": usage.get("model", ""),
        "llm_call_id": llm_call_id,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "latency_ms": usage.get("latency_ms", 0),
        "few_shot_count": len(winners) + len(losers),
    }


def get_drafts(status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    """Список черновиков с пагинацией.

    Args:
        status: фильтр по статусу ('pending_review', 'approved', 'rejected') или None
        limit: максимальное количество записей
        offset: смещение для пагинации

    Returns:
        {"total": int, "drafts": list[dict]}
    """
    conn = _get_connection()
    try:
        if status:
            total = conn.execute(
                "SELECT COUNT(*) FROM ad_drafts WHERE status = ?", (status,)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM ad_drafts WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) FROM ad_drafts").fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM ad_drafts ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return {"total": total, "drafts": [dict(r) for r in rows]}
    finally:
        conn.close()


def approve_draft(draft_id: int, approved_by: str = "user") -> dict:
    """Одобряет черновик — меняет status на 'approved'.

    Args:
        draft_id: идентификатор черновика
        approved_by: кто одобрил (имя пользователя или роль)

    Returns:
        {"status": "approved", "draft_id": int, "approved_at": str}

    Raises:
        ValueError: черновик не найден или уже обработан
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM ad_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Черновик не найден: {draft_id}")
        if row["status"] != "pending_review":
            raise ValueError(f"Черновик уже обработан (status={row['status']})")

        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE ad_drafts SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?",
            (approved_by, now, draft_id),
        )
        conn.commit()
        return {"status": "approved", "draft_id": draft_id, "approved_at": now}
    finally:
        conn.close()


def reject_draft(draft_id: int, feedback: str) -> dict:
    """Отклоняет черновик с обратной связью.

    Args:
        draft_id: идентификатор черновика
        feedback: причина отклонения (обязательно)

    Returns:
        {"status": "rejected", "draft_id": int, "rejected_at": str}

    Raises:
        ValueError: черновик не найден, уже обработан, или feedback пустой
    """
    if not feedback or not feedback.strip():
        raise ValueError("feedback обязателен")

    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM ad_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Черновик не найден: {draft_id}")
        if row["status"] != "pending_review":
            raise ValueError(f"Черновик уже обработан (status={row['status']})")

        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE ad_drafts SET status = 'rejected', feedback = ?, rejected_at = ? WHERE id = ?",
            (feedback.strip(), now, draft_id),
        )
        conn.commit()
        return {"status": "rejected", "draft_id": draft_id, "rejected_at": now}
    finally:
        conn.close()


def get_taxonomy() -> dict:
    """Читает таксономию из БД (типы хуков, углы, типы офферов).

    Returns:
        {"hook_types": [...], "angles": [...], "offer_types": [...]}
    """
    conn = _get_connection()
    try:
        hooks = [dict(r) for r in conn.execute(
            "SELECT * FROM hook_types ORDER BY id"
        ).fetchall()]
        angles = [dict(r) for r in conn.execute(
            "SELECT * FROM angles ORDER BY id"
        ).fetchall()]
        offers = [dict(r) for r in conn.execute(
            "SELECT * FROM offer_types ORDER BY id"
        ).fetchall()]
        return {"hook_types": hooks, "angles": angles, "offer_types": offers}
    finally:
        conn.close()


def get_learnings(limit: int = 50, offset: int = 0) -> dict:
    """Список learnings с пагинацией.

    Args:
        limit: максимальное количество записей
        offset: смещение для пагинации

    Returns:
        {"total": int, "learnings": list[dict]}
    """
    conn = _get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM learnings ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        learnings = []
        for r in rows:
            d = dict(r)
            # Десериализуем evidence_ad_ids из JSON-строки
            try:
                d["evidence_ad_ids"] = json.loads(d.get("evidence_ad_ids", "[]"))
            except (json.JSONDecodeError, TypeError):
                d["evidence_ad_ids"] = []
            learnings.append(d)
        return {"total": total, "learnings": learnings}
    finally:
        conn.close()


def get_active_learnings(limit: int = 8) -> list[dict]:
    """Top-N активных уроков для подключения к контуру (генератор, отчёт).

    Берёт уроки и автоматические (source='pattern_miner'), и ручные (source='manual').
    Сортировка по уверенности: confirmed → probable → hypothesis (через CASE), внутри — свежие
    первыми. evidence_ad_ids десериализуется из JSON.

    Args:
        limit: максимум уроков (дефолт 8).

    Returns: list[dict] с полями id, statement, evidence_ad_ids(list), confidence, source,
             tags, created_at.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM learnings
            WHERE source IN ('pattern_miner', 'manual')
            ORDER BY
                CASE confidence
                    WHEN 'confirmed'  THEN 0
                    WHEN 'probable'   THEN 1
                    WHEN 'hypothesis' THEN 2
                    ELSE 3
                END,
                created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["evidence_ad_ids"] = json.loads(d.get("evidence_ad_ids", "[]"))
            except (json.JSONDecodeError, TypeError):
                d["evidence_ad_ids"] = []
            out.append(d)
        return out
    finally:
        conn.close()


def _get_top_learnings(limit: int = 8) -> list[str]:
    """Возвращает statement-строки уроков с confidence='confirmed' для промпта генератора.

    Args:
        limit: максимум уроков (дефолт 8).

    Returns: список statement-строк (только confirmed, не более limit штук).
    """
    all_learnings = get_active_learnings(limit=limit)
    # Фильтруем — только подтверждённые уроки попадают в промпт
    return [
        d["statement"]
        for d in all_learnings
        if d.get("confidence") == "confirmed"
    ]


def add_learning(
    statement: str,
    evidence_ad_ids: list[str] | None = None,
    confidence: str = "hypothesis",
    tags: str = "",
) -> dict:
    """Добавляет новый learning в таблицу learnings.

    Args:
        statement: текст вывода/инсайта (обязательно)
        evidence_ad_ids: список id объявлений-доказательств
        confidence: уровень уверенности ('hypothesis', 'tested', 'proven')
        tags: теги через запятую

    Returns:
        {"status": "created", "learning_id": int}

    Raises:
        ValueError: statement пустой
    """
    if not statement or not statement.strip():
        raise ValueError("statement обязателен")

    evidence_json = json.dumps(evidence_ad_ids or [], ensure_ascii=False)

    conn = _get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO learnings (statement, evidence_ad_ids, confidence, source, tags)
               VALUES (?, ?, ?, 'manual', ?)""",
            (statement.strip(), evidence_json, confidence, tags),
        )
        conn.commit()
        return {"status": "created", "learning_id": cur.lastrowid}
    finally:
        conn.close()
