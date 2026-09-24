"""
FastAPI роутер для Creative Brain API.
Тонкие обёртки над сервисами: бэкфилл, статус, pattern-miner, разметка.
Все POST автоматически защищены X-API-Key middleware (web/app.py).
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brain", tags=["brain"])


# ---------------------------------------------------------------------------
# Pydantic-модели запросов и ответов
# ---------------------------------------------------------------------------

class BackfillRequest(BaseModel):
    max_ads: int = Field(default=25, ge=1, le=50)  # размер порции (DEV-режим: ≤50)
    reset: bool = Field(default=False)              # сбросить курсор и начать заново


class BackfillResponse(BaseModel):
    fetched: int           # объявлений скачано за этот вызов
    upserted: int          # новых INSERT
    rate_limited: bool     # True если FB ограничил — курсор не сдвинулся
    cursor_after: str | None
    done: bool             # True если весь кабинет уже скачан


class BrainStatusResponse(BaseModel):
    backfill: dict    # fetched_total, upserted_total, done, cursor_set, last_run_at, rate_limited_at
    labeling: dict    # total_with_body, labeled, pending
    outcomes: dict    # total, matched, pending
    learnings: dict   # total, by_confidence, pattern_miner, manual


class MineResponse(BaseModel):
    learnings_written: int
    slices_evaluated: int
    baseline: dict    # avg_qual_pct, avg_cpl, n_ads


class LabelRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


# ---------------------------------------------------------------------------
# POST /api/brain/backfill
# ---------------------------------------------------------------------------

@router.post("/backfill", response_model=BackfillResponse)
async def backfill_endpoint(body: BackfillRequest):
    """Один инкремент резюмируемого бэкфилла всего рекламного кабинета.

    reset=True — сбросить курсор и начать заново.
    Возвращает fetched/upserted/rate_limited/cursor_after/done.
    """
    try:
        from services.creative_backfill import sync_backfill_increment, reset_backfill_state
    except RuntimeError as exc:
        # KB не инициализирована
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        if body.reset:
            await asyncio.to_thread(reset_backfill_state)

        result = await asyncio.to_thread(sync_backfill_increment, body.max_ads)
        return BackfillResponse(**result)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("backfill_endpoint: ошибка — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /api/brain/status  (открытый — GET, без X-API-Key)
# ---------------------------------------------------------------------------

@router.get("/status", response_model=BrainStatusResponse)
async def status_endpoint():
    """Прогресс по всем фазам Creative Brain: бэкфилл, разметка, исходы, уроки."""

    def _collect_status() -> dict:
        from services.creative_intelligence import _get_connection
        from services.creative_backfill import get_backfill_state

        conn = _get_connection()
        try:
            # --- Бэкфилл ---
            bf_state = get_backfill_state()
            backfill = {
                "fetched_total": bf_state.get("fetched_total", 0),
                "upserted_total": bf_state.get("upserted_total", 0),
                "done": bf_state.get("done", False),
                "cursor_set": bf_state.get("cursor") is not None,
                "last_run_at": bf_state.get("last_run_at"),
                "rate_limited_at": bf_state.get("rate_limited_at"),
            }

            # --- Разметка (labeling) ---
            row = conn.execute(
                "SELECT COUNT(*) FROM creative_kb WHERE ad_body != '' AND ad_body IS NOT NULL"
            ).fetchone()
            total_with_body = row[0] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) FROM creative_kb WHERE labeled_at IS NOT NULL"
            ).fetchone()
            labeled_count = row[0] if row else 0

            labeling = {
                "total_with_body": total_with_body,
                "labeled": labeled_count,
                "pending": max(0, total_with_body - labeled_count),
            }

            # --- AMO-исходы (outcomes) ---
            row = conn.execute("SELECT COUNT(*) FROM creative_kb").fetchone()
            total_ads = row[0] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) FROM creative_kb WHERE outcomes_matched_at IS NOT NULL"
            ).fetchone()
            matched_count = row[0] if row else 0

            outcomes = {
                "total": total_ads,
                "matched": matched_count,
                "pending": max(0, total_ads - matched_count),
            }

            # --- Уроки (learnings) ---
            row = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()
            total_learnings = row[0] if row else 0

            # По уверенности
            conf_rows = conn.execute(
                """
                SELECT confidence, COUNT(*) FROM learnings
                GROUP BY confidence
                """
            ).fetchall()
            by_conf = {"confirmed": 0, "probable": 0, "hypothesis": 0}
            for conf, cnt in conf_rows:
                if conf in by_conf:
                    by_conf[conf] = cnt

            row = conn.execute(
                "SELECT COUNT(*) FROM learnings WHERE source='pattern_miner'"
            ).fetchone()
            pm_count = row[0] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) FROM learnings WHERE source='manual'"
            ).fetchone()
            manual_count = row[0] if row else 0

            learnings = {
                "total": total_learnings,
                "by_confidence": by_conf,
                "pattern_miner": pm_count,
                "manual": manual_count,
            }

            return {
                "backfill": backfill,
                "labeling": labeling,
                "outcomes": outcomes,
                "learnings": learnings,
            }
        finally:
            conn.close()

    try:
        result = await asyncio.to_thread(_collect_status)
        return BrainStatusResponse(**result)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("status_endpoint: ошибка — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# POST /api/brain/mine
# ---------------------------------------------------------------------------

@router.post("/mine", response_model=MineResponse)
async def mine_endpoint():
    """Запустить pattern-miner: агрегации срезов → уроки в таблицу learnings."""
    try:
        from services.pattern_miner import mine_patterns
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        result = await asyncio.to_thread(mine_patterns)
        return MineResponse(**result)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("mine_endpoint: ошибка — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# POST /api/brain/label  (бонус — ручной запуск разметки)
# ---------------------------------------------------------------------------

@router.post("/label")
async def label_endpoint(body: LabelRequest):
    """Ручной запуск инкрементальной LLM-разметки текстов креативов (Gemini)."""
    try:
        from services.creative_labeler import label_unlabeled
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        result = await asyncio.to_thread(label_unlabeled, body.limit)
        return result
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("label_endpoint: ошибка — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Часть A: бэкфилл дневных метрик
# ---------------------------------------------------------------------------

class BackfillMetricsRequest(BaseModel):
    months_back: int | None = Field(default=None, ge=1, le=12)  # сколько месяцев докрутить
    date_from: str | None = Field(default=None)   # 'YYYY-MM-DD' — прямой диапазон
    date_to: str | None = Field(default=None)
    reset: bool = Field(default=False)            # сбросить курсор месяцев


class BackfillMetricsResponse(BaseModel):
    mode: str                      # "range" | "increment"
    increments_run: int = 0
    months_filled: list[str] = []
    fetched_total: int = 0
    upserted_total: int = 0
    rate_limited: bool = False
    done: bool = False
    complete: bool = True
    error: str | None = None


class BackfillMetricsStatusResponse(BaseModel):
    # Курсорный прогресс (основное)
    cursor_date: str | None      # следующая дата к обработке (YYYY-MM-DD)
    done: bool
    rows_in_table: int
    date_span: dict              # {"min": str|None, "max": str|None}
    last_run_at: str | None
    rate_limited_at: str | None
    hourly_lead_semantics: dict
    # Обратная совместимость — старые поля месячного курсора
    months_total: int
    months_done_count: int
    months_done: list[str]
    next_month: str | None


@router.post("/backfill-metrics", response_model=BackfillMetricsResponse)
async def backfill_metrics_endpoint(body: BackfillMetricsRequest):
    """Старт порции исторического бэкфилла day-метрик.
    Режим 'range' если заданы date_from и date_to; иначе помесячный инкремент.
    Защищён X-API-Key (POST). 400 при невалидном диапазоне.
    """
    try:
        from services.metrics_backfill import (
            backfill_range,
            reset_backfill_metrics_state,
            run_metrics_backfill,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    # Режим range: оба поля должны быть заданы вместе или ни один
    has_from = body.date_from is not None
    has_to = body.date_to is not None

    if has_from != has_to:
        raise HTTPException(
            status_code=400,
            detail="Необходимо задать оба поля date_from и date_to, или ни одного.",
        )

    if has_from and has_to:
        # Проверяем формат дат
        from datetime import date as _date
        try:
            d_from = _date.fromisoformat(body.date_from)
            d_to = _date.fromisoformat(body.date_to)
        except ValueError:
            raise HTTPException(status_code=400, detail="Некорректный формат даты. Используйте YYYY-MM-DD.")

        if d_from > d_to:
            raise HTTPException(status_code=400, detail="date_from не может быть позже date_to.")

        # Режим range — прямой диапазон без курсора
        try:
            r = await asyncio.to_thread(backfill_range, body.date_from, body.date_to)
            return BackfillMetricsResponse(
                mode="range",
                fetched_total=r.get("fetched", 0),
                upserted_total=r.get("upserted", 0),
                rate_limited=r.get("rate_limited", False),
                done=False,
                complete=r.get("complete", False),
                error=r.get("error"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception as exc:
            logger.error("backfill_metrics_endpoint (range): ошибка — %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))
    else:
        # Режим increment — помесячный резюмируемый бэкфилл
        try:
            if body.reset:
                await asyncio.to_thread(reset_backfill_metrics_state)

            r = await asyncio.to_thread(run_metrics_backfill, body.months_back)
            return BackfillMetricsResponse(
                mode="increment",
                increments_run=r.get("increments_run", 0),
                months_filled=r.get("months_filled", []),
                fetched_total=r.get("fetched_total", 0),
                upserted_total=r.get("upserted_total", 0),
                rate_limited=r.get("rate_limited", False),
                done=r.get("done", False),
                complete=r.get("complete", False),
                error=r.get("error"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception as exc:
            logger.error("backfill_metrics_endpoint (increment): ошибка — %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))


@router.get("/backfill-metrics/status", response_model=BackfillMetricsStatusResponse)
async def backfill_metrics_status_endpoint():
    """Прогресс помесячного бэкфилла (без X-API-Key, GET)."""

    def _collect_status() -> dict:
        from services.metrics_backfill import (
            get_backfill_metrics_state,
            _all_months,
            _get_latest_date,
        )
        from services.creative_intelligence import _get_connection
        from services.hourly_collector import get_hourly_lead_semantics_status

        state = get_backfill_metrics_state()
        all_months = _all_months()
        months_done = state.get("months_done", [])
        done_set = set(months_done)

        months_total = len(all_months)
        months_done_count = len(months_done)

        # Следующий незалитый месяц (обратная совместимость)
        next_month = None
        for m in all_months:
            if m not in done_set:
                next_month = m
                break

        # Курсор и done по курсорной логике
        cursor_date = state.get("cursor_date")
        try:
            latest = _get_latest_date()
            if cursor_date:
                from datetime import date as _date
                cursor_d = _date.fromisoformat(cursor_date)
                done = cursor_d > latest
            else:
                done = False
        except Exception:
            done = False

        # Кол-во строк и диапазон дат в таблице
        conn = _get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM ad_daily_metrics "
                "WHERE lead_semantics_version = 2 "
                "AND lead_parse_status IN ('ok', 'component_mismatch')"
            ).fetchone()
            rows_in_table = int(row["cnt"] if row else 0)

            span_row = conn.execute(
                "SELECT MIN(date) AS min_d, MAX(date) AS max_d FROM ad_daily_metrics "
                "WHERE lead_semantics_version = 2 "
                "AND lead_parse_status IN ('ok', 'component_mismatch')"
            ).fetchone()
            date_span = {
                "min": span_row["min_d"] if span_row else None,
                "max": span_row["max_d"] if span_row else None,
            }
            hourly_lead_semantics = get_hourly_lead_semantics_status(conn)
        finally:
            conn.close()

        return {
            "cursor_date": cursor_date,
            "done": done,
            "rows_in_table": rows_in_table,
            "date_span": date_span,
            "last_run_at": state.get("last_run_at"),
            "rate_limited_at": state.get("rate_limited_at"),
            "hourly_lead_semantics": hourly_lead_semantics,
            # Обратная совместимость
            "months_total": months_total,
            "months_done_count": months_done_count,
            "months_done": months_done,
            "next_month": next_month,
        }

    try:
        result = await asyncio.to_thread(_collect_status)
        return BackfillMetricsStatusResponse(**result)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("backfill_metrics_status_endpoint: ошибка — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Часть B: движок паттернов
# ---------------------------------------------------------------------------

class PatternsResponse(BaseModel):
    predictors: list[dict]
    data_sufficiency: str        # "ok" | "insufficient"
    n_success: int = 0
    n_fail: int = 0
    learnings_written: int = 0


@router.post("/patterns", response_model=PatternsResponse)
async def run_patterns_endpoint():
    """Ручной запуск движка паттернов (пересчёт + запись learnings). Защищён X-API-Key."""
    try:
        from services.pattern_engine import run_pattern_engine
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        result = await asyncio.to_thread(run_pattern_engine)
        return PatternsResponse(**result)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("run_patterns_endpoint: ошибка — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/patterns", response_model=PatternsResponse)
async def get_patterns_endpoint():
    """Сводка предикторов из последних learnings (без X-API-Key, GET)."""
    try:
        from services.pattern_engine import get_patterns_summary
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        result = await asyncio.to_thread(get_patterns_summary)
        return PatternsResponse(**result)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("get_patterns_endpoint: ошибка — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# POST /api/brain/match-outcomes  — ручной запуск сопоставления AMO-исходов
# ---------------------------------------------------------------------------

class MatchOutcomesRequest(BaseModel):
    # months_back=2 → последние ~60 дней (лёгкий проход, НЕ вся история)
    months_back: int = Field(default=2, ge=1, le=12)


class MatchOutcomesResponse(BaseModel):
    windows: int
    leads_fetched: int
    ads_matched: int
    ads_updated: int


@router.post("/match-outcomes", response_model=MatchOutcomesResponse)
async def match_outcomes_endpoint(body: MatchOutcomesRequest):
    """Ручной запуск сопоставления AMO-исходов к объявлениям creative_kb.

    По умолчанию months_back=2 — последние ~60 дней.
    Полный проход (months_back=9) тяжёлый (десятки минут на большом кабинете) — используй только вручную.
    Защищён X-API-Key (POST).
    """
    try:
        from services.amo_outcomes import attach_amo_outcomes
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        result = await asyncio.to_thread(
            attach_amo_outcomes, body.months_back, 30
        )
        # attach_amo_outcomes возвращает {"error": ...} при недоступности KB или AMO
        if "error" in result:
            raise HTTPException(status_code=503, detail=result["error"])
        return MatchOutcomesResponse(**result)
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("match_outcomes_endpoint: ошибка — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
