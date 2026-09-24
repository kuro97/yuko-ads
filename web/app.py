"""
Веб-сервер Yuko.
FastAPI + SSE для управления рекламой через браузер.
Аутентификация через Supabase Auth (JWT).
"""

import json
import logging
import os
import re
import sqlite3
import time
import uuid
import hmac
import hashlib

import psutil
import asyncio
import threading
from contextlib import asynccontextmanager
from importlib import import_module

logger = logging.getLogger(__name__)
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Request, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

# Добавляем корень проекта в path для импортов
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

# Root-логгер БЕЗ хендлеров = все logger.info/warning/exception сервисов уходят
# в никуда: диагностика показала, что в journald никогда не попадала ни
# одна строка модульных логгеров (казавшиеся «журнальными» строки были print-ами
# discovery и выводом ручных прогонов). Уровень INFO, поток stdout — journald
# подхватывает через systemd. force=False: uvicorn-логгеры со своими хендлерами
# не трогаются, дублей access-логов нет.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(levelname)s %(name)s: %(message)s",
)

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from integrations.trello import (
    get_done_list_id,
    get_unlaunched_cards,
    get_card,
    get_card_drive_link,
    detect_language,
    publish_hypotheses,
    redact_trello_secrets,
)
from integrations.gdrive import detect_media_hint
from integrations.facebook import (
    get_adset_capacity,
)
from integrations.amo import sync_amo_data, get_qualified_leads_with_fb_id
from integrations.fb_capi import send_mql_batch
import config
from config import ADSETS, AD_BODY
from agent.analyzer import analyze_all, apply_decision_tree, apply_portfolio_decisions, get_ads_with_metrics, get_daily_insights, get_statuses_age_seconds  # noqa: F401 — analyze_all реэкспорт для web.app.analyze_all (патчат тесты и services/analytics_cache._resolve_from_web_app)
from agent.learner import run_learner
from agent.launcher import launch_single
from agent.fb_common import FBApiError
from agent.copywriter import generate_ad_texts
from agent.scorer import score_creative
from agent.repositories import (
    decisions_repo,
    history_repo,
    amo_repo,
    learner_repo,
    winners_repo,
)
from agent.database import init_db
from web.overview_routes import router as overview_router
from web.intelligence_routes import router as intelligence_router
from web.ad_generator_routes import router as ad_generator_router
from web.amo_webhook_routes import router as amo_webhook_router
from web.brain_routes import router as brain_router
from web.sources_routes import sources_router
from web.seasons_routes import router as seasons_router
from services.creative_intelligence import get_kb_creatives
from services.cron_heartbeat import heartbeat, report_cron_failure, report_cron_success
from services.replacement_orchestrator import safe_pause_or_enqueue_replacement

_cleanup_schemas = import_module("web.cleanup_schemas")
CleanerStatusResponse = _cleanup_schemas.CleanerStatusResponse
DisabledCleanupResponse = _cleanup_schemas.DisabledCleanupResponse

# Старое имя оставлено только как monkeypatch-point для regression-тестов.
# Production endpoint всегда получает replacement-aware facade.
_ORIGINAL_WEB_PAUSE_FACADE = safe_pause_or_enqueue_replacement
safe_pause_ad = safe_pause_or_enqueue_replacement


def _require_idempotency_key(request: Request) -> str:
    """HTTP mutations принимают только client-owned canonical UUID4."""
    from services.action_producer_gateway import require_uuid4

    raw = request.headers.get("Idempotency-Key")
    try:
        return require_uuid4(raw)  # type: ignore[arg-type]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _optional_json_object(request: Request) -> dict:
    """Пустое тело допустимо; malformed/non-object JSON отклоняется явно."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Тело должно быть valid JSON object") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Тело должно быть JSON object")
    return value


def _approval_action_payload(run) -> dict[str, object]:
    """Единый typed HTTP-снимок durable approval operation."""

    reviews = tuple(getattr(run, "reviews", ()))
    executions = tuple(getattr(run, "executions", ()))
    result = getattr(run, "result", None)
    state = getattr(run, "state", None)
    decision = getattr(run, "decision", None)
    shadow_evaluation = getattr(run, "shadow_evaluation", None)
    shadow_items = tuple(getattr(run, "shadow_items", ()))
    return {
        "operation_id": run.operation_id,
        "idempotency_key": run.idempotency_key,
        "batch_manifest_id": run.batch_manifest_id,
        "check_ids": list(
            getattr(run, "check_ids", ())
            or tuple(review.check_id for review in reviews)
        ),
        "decision": getattr(decision, "value", decision),
        "state": getattr(state, "value", state),
        "result": getattr(result, "value", result),
        "shadow_evaluation": getattr(shadow_evaluation, "value", shadow_evaluation),
        "shadow_items": [getattr(item, "value", item) for item in shadow_items],
        "completed_items": getattr(
            run,
            "completed_items",
            sum(execution.result is not None for execution in executions),
        ),
        "total_items": getattr(run, "total_items", len(reviews)),
        "first_unprocessed_index": run.first_unprocessed_index,
        "dry_run": run.dry_run,
        "provider_mutation_count": run.provider_mutation_count,
        "reconciliation_required": run.reconciliation_required,
        "stop_reason_code": run.stop_reason_code,
    }


def _owner_proposal_response(outcome: object) -> JSONResponse | None:
    """202-ответ producer-границы: предложение владельцу создано, действия нет.

    Producer только предлагает действие, поэтому здесь нет ни записи decisions,
    ни уведомления об исполнении — их делает execution boundary уже после
    одобрения владельца. ``None`` означает, что outcome не содержит receipt.
    """

    receipt = getattr(outcome, "receipt", None)
    if receipt is None:
        return None
    return JSONResponse(
        status_code=202,
        content={
            "status": "proposal_created",
            "proposal_id": receipt.proposal_id,
            "state": receipt.state,
            "deduplicated": bool(receipt.deduplicated),
            "valid_until": receipt.valid_until.isoformat(),
        },
    )


def _approval_gateway_http_error(exc: Exception) -> HTTPException:
    """Не раскрывает provider/DB детали и сохраняет typed error code."""

    from services.action_producer_gateway import ProducerIdempotencyConflict

    if isinstance(exc, ProducerIdempotencyConflict):
        return HTTPException(
            status_code=409,
            detail={"code": "IDEMPOTENCY_CONFLICT", "message": "Ключ уже связан с другой командой"},
        )
    return HTTPException(
        status_code=502,
        detail={"code": "CHECKER_UNAVAILABLE", "message": "Approval gateway временно недоступен"},
    )


def _deliver_in_app_action_outbox(effect_id: str) -> bool:
    """Доставляет durable notification at-least-once после commit UoW."""

    from agent.database import claim_action_outbox, mark_action_outbox_sent

    try:
        claimed = claim_action_outbox(effect_id)
    except Exception as exc:
        logger.warning("action outbox claim недоступен: %s", type(exc).__name__)
        return False
    if claimed is None:
        return False
    if claimed["channel"] != "IN_APP":
        raise RuntimeError("Неподдерживаемый action outbox channel")
    payload = claimed["payload"]
    try:
        notify(
            payload["event_type"],
            payload["title"],
            detail=payload["detail"],
            level=payload["level"],
            meta=payload["meta"],
        )
        mark_action_outbox_sent(effect_id)
    except Exception as exc:
        logger.warning("action outbox delivery не завершена: %s", type(exc).__name__)
        return False
    return True


def _web_pause_facade(ad_id: str, source: str):
    facade = safe_pause_or_enqueue_replacement
    if safe_pause_ad is not _ORIGINAL_WEB_PAUSE_FACADE:
        facade = safe_pause_ad
    return facade(ad_id, source)


@heartbeat("_cron_sync_mql", 30, critical=True)
def _cron_sync_mql() -> None:
    """Отправка новых MQL-событий в Facebook CAPI + GA4 MP (каждые 30 минут)."""
    from integrations.amo import get_qualified_leads_with_fb_id
    from integrations.fb_capi import send_mql_batch
    from integrations.ga_mp import send_mql_batch_to_ga4

    try:
        # days=2: берём лидов за последние 2 дня — достаточно для 30-минутного крона
        leads = get_qualified_leads_with_fb_id(days=2)

        # Facebook CAPI
        fb_result = send_mql_batch(leads)
        if fb_result["sent"] > 0 or fb_result["errors"] > 0:
            logger.info(
                "CAPI cron: FB отправлено=%d пропущено=%d ошибок=%d",
                fb_result["sent"], fb_result["skipped"], fb_result["errors"],
            )

        # Google Analytics 4 Measurement Protocol
        ga_result = send_mql_batch_to_ga4(leads)
        if ga_result["sent"] > 0 or ga_result["errors"] > 0:
            logger.info(
                "CAPI cron: GA4 отправлено=%d пропущено=%d ошибок=%d",
                ga_result["sent"], ga_result["skipped"], ga_result["errors"],
            )
        report_cron_success("_cron_sync_mql")
    except Exception as e:
        logger.error("CAPI cron: ошибка — %s", e)
        report_cron_failure("_cron_sync_mql", e)


# Локальный часовой пояс (UTC+5 по умолчанию, настраивается здесь; используется в крон-функциях)
_TZ_LOCAL = timezone(timedelta(hours=5))

# Сколько последних недель пересобирает крон когорт. Текущая неделя входит,
# двух закрытых хватает, чтобы дозревшие квалы AMO попали в строку.
# Пять, а не три, из-за выручки (волна 4): когорта дозревает через
# 6 + REVENUE_HORIZON_DAYS = 20 дней после своего понедельника, и в окне из трёх
# недель неделя дозревала бы уже за его краем — выручка не проставилась бы
# никогда. Минимально достаточно четырёх, пятая — запас на пропущенный день.
_COHORT_REFRESH_WEEKS = 5

# Состояние сторожа FB-лидов:
#   alerted      — был ли уже отправлен первый алерт за текущий простой
#   last_alert_at — datetime последнего отправленного алерта (или None)
# Повторный алерт шлётся не чаще чем раз в 3 часа, пока простой продолжается.
_fb_watchdog_state: dict = {"alerted": False, "last_alert_at": None}

# Состояние пульса FB-лидов — datetime последней отправки (или None).
_fb_heartbeat_state = {"last_sent_at": None}

# Лок для thread-safe чтения/записи состояния сторожа и пульса
_fb_state_lock = threading.Lock()

# Пороги тишины FB-лидов в зависимости от времени суток (локальное время)
_FB_LEAD_DAY_SILENCE_MIN = 60    # день 08:00–21:59 → 1 час
_FB_LEAD_NIGHT_SILENCE_MIN = 180  # ночь 22:00–07:59 → 3 часа

# Интервалы пульса «всё норм» (независимы от порогов тревог — совпадают случайно)
_FB_PULSE_DAY_INTERVAL_MIN = 60    # день 08:00–21:59 → раз в час
_FB_PULSE_NIGHT_INTERVAL_MIN = 180  # ночь 22:00–07:59 → раз в 3 часа


def _should_send_heartbeat(now: datetime, last_sent_at) -> bool:
    """Пора ли слать пульс «всё норм».

    День 08:00–21:59 (локальное время) — раз в час; ночь 22:00–07:59 — раз в 3 часа.
    last_sent_at — datetime последней отправки (или None).
    """
    interval_min = _FB_PULSE_DAY_INTERVAL_MIN if 8 <= now.hour < 22 else _FB_PULSE_NIGHT_INTERVAL_MIN
    if last_sent_at is None:
        return True
    return (now - last_sent_at).total_seconds() >= interval_min * 60


def _fb_silence_threshold_min(hour: int) -> int:
    """Порог тишины FB-лидов в минутах по часу суток (локальное время).

    День 08:00–21:59 → 60 мин, ночь 22:00–07:59 → 180 мин.
    """
    return _FB_LEAD_DAY_SILENCE_MIN if 8 <= hour < 22 else _FB_LEAD_NIGHT_SILENCE_MIN


@heartbeat("_cron_fb_lead_watchdog", 15)
def _cron_fb_lead_watchdog() -> None:
    """Следит, что FB-лиды продолжают падать в AMO. Работает круглосуточно.

    Цепочка FB→Zapier→amoconnect→AMO иногда тихо ломается.
    Порог тишины зависит от времени суток (локальное время):
      - День 08:00–21:59 → 60 мин без лида = тревога
      - Ночь 22:00–07:59 → 180 мин без лида = тревога
    Эскалация с повторами: первый алерт — сразу, повторные — не чаще раз в 3 часа
    пока простой продолжается. Восстановление — сбрасываем флаг.
    Если get_latest_fb_lead_ts вернул None и ранее был алерт — тоже шлём повторы
    (видимо поиск вышел за окно 48 ч, что говорит о длительном простое).
    """
    # Интервал повторных напоминаний в секундах
    REPEAT_INTERVAL_SEC = 3 * 3600

    try:
        now = datetime.now(_TZ_LOCAL)

        # Динамический порог: день — 60 мин, ночь — 180 мин
        threshold = _fb_silence_threshold_min(now.hour)

        from integrations.amo import get_latest_fb_lead_ts
        ts = get_latest_fb_lead_ts()

        from services.notifications import send_critical_alert

        # Читаем состояние под локом
        with _fb_state_lock:
            alerted = _fb_watchdog_state["alerted"]
            last_alert_at = _fb_watchdog_state["last_alert_at"]

        if ts is None:
            if not alerted:
                # ts=None при первом запуске означает что FB-лидов нет за последние 48ч.
                # Это может быть: рестарт сервера во время длящегося сбоя FB, или реальный
                # первый сбой при котором поиск сразу ничего не нашёл.
                # Ложная тревога при рестарте лучше полной слепоты — шлём алерт с пометкой.
                send_critical_alert(
                    "FB-лиды не падают в AMO",
                    "⚠️ Нет FB-лидов за последние 48ч. "
                    "Возможно сбой FB/Zapier или процесс перезапустился в момент длительного простоя. "
                    "Проверь amoconnect/Zapier вручную.",
                    channel="health",
                )
                with _fb_state_lock:
                    _fb_watchdog_state["alerted"] = True
                    _fb_watchdog_state["last_alert_at"] = now
                logger.warning("Сторож FB: ТРЕВОГА — нет данных за 48ч (первый запуск или рестарт)")
                return
            # Уже был алерт, но теперь ts=None — поиск вышел за окно 48ч,
            # что подтверждает длительный простой. Шлём повтор по тем же правилам.
            should_repeat = (
                last_alert_at is None
                or (now - last_alert_at).total_seconds() >= REPEAT_INTERVAL_SEC
            )
            if should_repeat:
                send_critical_alert(
                    "FB-лиды не падают в AMO",
                    "⏳ FB-лиды всё ещё не падают — не вижу FB-лидов уже >48ч окна поиска. "
                    "Проверь amoconnect/Zapier.",
                    channel="health",
                )
                with _fb_state_lock:
                    _fb_watchdog_state["last_alert_at"] = now
                logger.warning("Сторож FB: повтор — нет данных >48ч")
            return

        last_dt = datetime.fromtimestamp(ts, _TZ_LOCAL)
        gap_min = (now - last_dt).total_seconds() / 60

        # Определяем суфикс даты последнего лида: «вчера» или ничего
        today = now.date()
        lead_date = last_dt.date()
        date_suffix = " вчера" if lead_date < today else ""

        if gap_min > threshold:
            gap_h = int(gap_min) // 60
            gap_m = int(gap_min) % 60
            gap_str = f"{gap_h}ч {gap_m}м" if gap_h else f"{gap_m}м"

            if not alerted:
                # Первый алерт за этот простой
                title = "FB-лиды не падают в AMO"
                detail = (
                    f"Последний FB-лид был в {last_dt.strftime('%H:%M')}{date_suffix} — "
                    f"это {gap_str} назад. "
                    f"(порог сейчас {threshold} мин) "
                    "Проверь Zapier (зап «FB-лиды → CRM») — "
                    "возможно отвалился доступ Facebook."
                )
                send_critical_alert(title, detail, meta={
                    "gap_min": int(gap_min),
                    "last_lead": last_dt.isoformat(),
                }, channel="health")
                with _fb_state_lock:
                    _fb_watchdog_state["alerted"] = True
                    _fb_watchdog_state["last_alert_at"] = now
                logger.warning("Сторож FB: ТРЕВОГА — %d мин без лидов (порог %d)", gap_min, threshold)

            else:
                # Уже был алерт — проверяем пора ли слать повтор (раз в 3 часа)
                should_repeat = (
                    last_alert_at is None
                    or (now - last_alert_at).total_seconds() >= REPEAT_INTERVAL_SEC
                )
                if should_repeat:
                    send_critical_alert(
                        "FB-лиды не падают в AMO",
                        f"⏳ FB-лиды всё ещё не падают — уже {gap_str} "
                        f"(последний в {last_dt.strftime('%H:%M')}{date_suffix}). "
                        "Проверь amoconnect/Zapier.",
                        channel="health",
                    )
                    with _fb_state_lock:
                        _fb_watchdog_state["last_alert_at"] = now
                    logger.warning("Сторож FB: повтор — %d мин без лидов", gap_min)

        elif gap_min <= threshold and alerted:
            # Лиды пошли снова — отправляем уведомление о восстановлении
            send_critical_alert(
                "FB-лиды снова идут ✅",
                f"Поток восстановился, последний лид в {last_dt.strftime('%H:%M')}.",
                channel="health",
            )
            with _fb_state_lock:
                _fb_watchdog_state["alerted"] = False
                _fb_watchdog_state["last_alert_at"] = None
            logger.info("Сторож FB: восстановилось")

    except Exception as exc:
        logger.warning("Сторож FB: неожиданная ошибка — %s", exc)


@heartbeat("_cron_fb_lead_heartbeat", 20)
def _cron_fb_lead_heartbeat() -> None:
    """Позитивный пульс FB-лидов — каждый час днём (08:00–21:59) и раз в 3 часа ночью (22:00–07:59, локальное время).

    Шлёт в Telegram «✅ всё норм» со статистикой: время последнего лида
    и количество FB-лидов за сегодня. Если нет данных — молчим (ложный пульс
    хуже пропущенного). Не мешает тревогам _cron_fb_lead_watchdog.
    Если сторож в тревоге — не шлём «всё норм» (противоречило бы тревоге).
    """
    try:
        now = datetime.now(_TZ_LOCAL)

        # Читаем состояние под локом
        with _fb_state_lock:
            last_sent_at = _fb_heartbeat_state["last_sent_at"]
            watchdog_alerted = _fb_watchdog_state["alerted"]

        # Проверяем интервал: день — раз в час, ночь — раз в 3 часа
        if not _should_send_heartbeat(now, last_sent_at):
            return

        # Если сторож в тревоге — не слать «всё норм» (это было бы ложью)
        if watchdog_alerted:
            logger.info("Пульс FB: пропускаем — сторож в тревоге")
            return

        from integrations.amo import get_latest_fb_lead_ts, get_leads, _extract_fb_lead_id

        # Время последнего FB-лида
        ts = get_latest_fb_lead_ts()
        if ts is None:
            # Нет данных — не шлём ложный пульс
            logger.warning("Пульс FB: не удалось получить время последнего лида, пропускаем")
            return

        last_dt = datetime.fromtimestamp(ts, _TZ_LOCAL)
        gap_min = (now - last_dt).total_seconds() / 60

        # Считаем FB-лиды за сегодня через get_leads(1) — данные за последние ~24ч.
        # Фильтруем по дате создания в локальной TZ, чтобы считать именно сегодня.
        today = now.date()
        leads_today = get_leads(days=1)
        count_today = 0
        for lead in leads_today:
            created_ts = lead.get("created_at")
            if created_ts:
                lead_date = datetime.fromtimestamp(created_ts, _TZ_LOCAL).date()
                if lead_date == today and _extract_fb_lead_id(lead) is not None:
                    count_today += 1

        from services.notifications import send_telegram
        text = (
            "✅ <b>FB-лиды идут нормально</b>\n"
            f"Последний лид: {last_dt:%H:%M} ({int(gap_min)} мин назад)\n"
            f"Сегодня FB-лидов: {count_today}\n"
            "Сторож на связи."
        )
        send_telegram(text, channel="health")

        # Фиксируем datetime последней отправки под локом
        with _fb_state_lock:
            _fb_heartbeat_state["last_sent_at"] = now
        logger.info("Пульс FB: отправлен (лидов сегодня: %d, последний %d мин назад)", count_today, int(gap_min))

    except Exception as exc:
        logger.warning("Пульс FB: неожиданная ошибка — %s", exc)


@heartbeat("_cron_autopilot", 15, critical=True)
def _cron_autopilot() -> None:
    """Крон автопилота — запускается каждые 15 минут, гейт по окнам 10/15/20 ч по локальному времени.

    Окно определяется как "{дата}-{час}", чтобы не запускаться дважды в одном часу.
    Состояние (last_run_window) хранится в data/autopilot_state.json.
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        if now.hour not in (10, 15, 20):
            return  # не в окне запуска

        window_key = f"{now.date().isoformat()}-{now.hour}"

        # Читаем state через функции autopilot-модуля (импорт ленивый — зависимости могут не быть готовы при старте)
        from services.autopilot import _load_state, _save_state, run_autopilot

        state = _load_state()
        if state.get("last_run_window") == window_key:
            return  # окно уже обработано

        # Помечаем окно ДО запуска — чтобы повторный крон-тик в том же часу не дублировал
        state["last_run_window"] = window_key
        _save_state(state)

        run_autopilot("cron")
        report_cron_success("_cron_autopilot")
    except Exception as exc:
        logger.warning("_cron_autopilot: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_autopilot", exc)


@heartbeat("_cron_evening_report", 10)
def _cron_evening_report() -> None:
    """Крон вечернего отчёта — каждые 10 минут; реальная отправка в 21:00–21:59 по локальному времени.

    Состояние (last_report_date) хранится в data/autopilot_state.json.
    """
    try:
        from services.autopilot import _load_state, _save_state
        from services.evening_report import should_send_report, send_evening_report
        from datetime import date

        now = datetime.now(_TZ_LOCAL)
        state = _load_state()

        # last_report_date — строка "YYYY-MM-DD" или None
        last_date_str = state.get("last_report_date")
        last_report_date = None
        if last_date_str:
            try:
                last_report_date = date.fromisoformat(last_date_str)
            except ValueError:
                pass

        if not should_send_report(now, last_report_date):
            return

        sent = send_evening_report()
        if sent:
            state["last_report_date"] = now.date().isoformat()
            _save_state(state)
            logger.info("Вечерний отчёт отправлен (%s)", now.date())
        else:
            logger.warning("Вечерний отчёт: send_evening_report вернул False")
    except Exception as exc:
        logger.warning("_cron_evening_report: неожиданная ошибка — %s", exc)


# Утро владельца — 9:00 по локальному времени (решение владельца). Сводка покрывает окно
# «с прошлой сводки», а не календарный день:
# слоты пауз идут каждые 2 часа 08–22, и вечерние иначе не попадали бы никуда.
_AUTONOMOUS_SUMMARY_HOUR_LOCAL = 9


@heartbeat("_cron_autonomy_invariant", 15)
def _cron_autonomy_invariant() -> None:
    """Сторож результата полной автономии — каждые 15 минут.

    Раньше регрессии владелец находил сам по карточкам в телеге. Теперь
    инвариант «полная автономия включена → PAUSE-карточек у владельца нет»
    проверяется системой; нарушение будит критическим алертом немедленно.
    """
    try:
        from services.autonomy_invariant import check_and_alert
        from services.creative_intelligence import DB_PATH

        if DB_PATH is not None:
            check_and_alert(DB_PATH)
        report_cron_success("_cron_autonomy_invariant")
    except Exception as exc:
        logger.warning("_cron_autonomy_invariant: ошибка — %s", exc)
        report_cron_failure("_cron_autonomy_invariant", exc)


@heartbeat("_cron_autonomous_summary", 10)
def _cron_autonomous_summary() -> None:
    """Сводка автономных пауз — одно сообщение в день, слот 9:00 по локальному времени.

    Если бот за окно ничего не выключил сам, сообщение не уходит вообще:
    «сегодня автономных пауз не было» — это шум, из-за которого перестают
    читать и настоящие сводки. Состояние слота — отдельное от вечернего
    отчёта, чтобы пропущенная сводка не блокировала отчёт и наоборот.
    """
    try:
        from datetime import date, timedelta as _td

        from services.autonomous_pause import send_daily_summary
        from services.autopilot import _load_state, _save_state

        now = datetime.now(_TZ_LOCAL)
        state = _load_state()
        last_date_str = state.get("last_autonomous_summary_date")
        last_sent = None
        if last_date_str:
            try:
                last_sent = date.fromisoformat(last_date_str)
            except ValueError:
                pass

        if now.hour < _AUTONOMOUS_SUMMARY_HOUR_LOCAL or last_sent == now.date():
            return

        # Окно сводки: с прошлой отправки; если метки нет — последние сутки.
        since = state.get("last_autonomous_summary_at")
        if not isinstance(since, str) or not since:
            since = (now.astimezone(timezone.utc) - _td(days=1)).isoformat()

        # Слот считаем закрытым в любом случае: и когда сводка ушла, и когда
        # отправлять было нечего. Иначе крон каждые 10 минут до полуночи
        # заново проверял бы пустой журнал.
        send_daily_summary(now, since=since)
        state["last_autonomous_summary_date"] = now.date().isoformat()
        state["last_autonomous_summary_at"] = now.astimezone(timezone.utc).isoformat()
        _save_state(state)
    except Exception as exc:
        logger.warning("_cron_autonomous_summary: неожиданная ошибка — %s", exc)


# Вечер владельца: к 20:00 по локальному времени дневной прогон auto_launch (≈10:30) и его
# ретраи исполнения уже отработали — сводка показывает завершённый день.
_LAUNCH_SUMMARY_HOUR_LOCAL = 20


@heartbeat("_cron_launch_summary", 10)
def _cron_launch_summary() -> None:
    """Сводка-воронка запусков — одно сообщение в день, слот 20:00 по локальному времени.

    В отличие от сводки автономных пауз, нулевая воронка ОТПРАВЛЯЕТСЯ:
    целый месяц, когда до эфира доходила лишь малая доля предложений, прошёл
    незамеченным именно потому, что сбой конвейера выглядел как тишина.
    """
    try:
        from datetime import date, timedelta as _td

        from services.autopilot import _load_state, _save_state
        from services.launch_summary import send_launch_summary

        now = datetime.now(_TZ_LOCAL)
        state = _load_state()
        last_date_str = state.get("last_launch_summary_date")
        last_sent = None
        if last_date_str:
            try:
                last_sent = date.fromisoformat(last_date_str)
            except ValueError:
                pass

        if now.hour < _LAUNCH_SUMMARY_HOUR_LOCAL or last_sent == now.date():
            return

        # Окно сводки: с прошлой отправки; если метки нет — последние сутки.
        since = state.get("last_launch_summary_at")
        if not isinstance(since, str) or not since:
            since = (now.astimezone(timezone.utc) - _td(days=1)).isoformat()

        # Слот закрываем в любом случае: сводка шлётся и при нулевой воронке,
        # а при ошибке отправки повтор пойдёт только завтра — лучше пропуск,
        # чем шторм повторов каждые 10 минут.
        send_launch_summary(now.astimezone(timezone.utc), since=since)
        state["last_launch_summary_date"] = now.date().isoformat()
        state["last_launch_summary_at"] = now.astimezone(timezone.utc).isoformat()
        _save_state(state)
    except Exception as exc:
        logger.warning("_cron_launch_summary: неожиданная ошибка — %s", exc)


@heartbeat("_cron_telegram_poll", 5)  # add_job=60сек, но 5 мин порог (3×5=15) — компромисс против джиттера секундного крона
def _cron_telegram_poll() -> None:
    """Polling Telegram-обновлений — каждые 60 секунд.

    Обрабатывает callback_query от inline-кнопок вечернего отчёта, а также
    текстовые команды «пульта» владельца (/status, /queue, /ads, /scale,
    /launch, /help) и кнопку паузы pause:<ad_id> из /ads.
    """
    try:
        from services.telegram_bot import poll_updates
        poll_updates()
    except Exception as exc:
        logger.warning("_cron_telegram_poll: неожиданная ошибка — %s", exc)


@heartbeat("_cron_prewarm_overview", 120)
def _cron_prewarm_overview() -> None:
    """Фоновый прогрев кеша обзора за 14 дней + аналитики за период автопилота (каждые 120 мин).

    Чтобы первый заход пользователя не упирался в холодный (медленный) FB-запрос
    и не ловил 502 при rate limit — держим кеш тёплым.
    120 мин < 180 мин порога свежести → fallback автопилота и сторожа всегда принимает кеш.

    Прогрев аналитики за today-7 → today обеспечивает:
    - fallback автопилота найдёт свежий кеш с date_to=сегодня;
    - сторож (ads_watchdog) переиспользует тот же кеш для 7д-среднего.
    """
    try:
        import time as _t
        from services.overview import get_overview
        from web.overview_routes import _cache
        result = get_overview(14)
        _cache[14] = {"data": result, "ts": _t.time()}
        logger.info("Prewarm: кеш обзора (14 дн) обновлён в фоне")
    except Exception as e:
        # Не критично — пользователь просто увидит обычную загрузку
        logger.warning("Prewarm обзора не удался: %s", e)

    # Прогрев аналитики за период автопилота (today-7 → today)
    # Каждый час обновляет analytics_cache.json с date_to=сегодня,
    # устраняя причину «прогон пропущен» при рейт-лимите FB.
    try:
        _ap_date_to = datetime.now().strftime("%Y-%m-%d")
        _ap_date_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        get_cached_analytics(date_from=_ap_date_from, date_to=_ap_date_to)
        logger.info(
            "Prewarm: аналитика автопилота %s–%s обновлена в фоне",
            _ap_date_from, _ap_date_to,
        )
    except Exception as e:
        # Не критично — автопилот попробует live-запрос или устаревший кеш
        logger.warning("Prewarm аналитики автопилота не удался: %s", e)


@heartbeat("_cron_brain_backfill", 20)
def _cron_brain_backfill() -> None:
    """Ночной резюмируемый бэкфилл всего кабинета FB → creative_kb.

    Интервал 20 мин, гейт 02:00–05:59 по локальному времени.
    Дедупликация по часовому окну через backfill_state key='cron_window'.
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        if now.hour not in (2, 3, 4, 5):
            return  # не в ночном окне запуска

        window_key = f"{now.date()}-{now.hour}"

        from services.creative_backfill import (
            sync_backfill_increment,
            _get_state_by_key,
            _save_state_by_key,
        )

        # Проверяем, не запускались ли в этом часовом окне
        cron_state = _get_state_by_key("cron_window")
        if cron_state.get("last_window") == window_key:
            return  # окно уже обработано в этом часу

        # Помечаем окно ДО запуска — чтобы повторный тик не дублировал
        _save_state_by_key("cron_window", {"last_window": window_key})

        result = sync_backfill_increment(max_ads=25)
        logger.info(
            "_cron_brain_backfill: окно %s — fetched=%d upserted=%d rate_limited=%s done=%s",
            window_key,
            result.get("fetched", 0),
            result.get("upserted", 0),
            result.get("rate_limited", False),
            result.get("done", False),
        )
    except Exception as exc:
        logger.warning("_cron_brain_backfill: неожиданная ошибка — %s", exc)


@heartbeat("_cron_brain_miner", 30)
def _cron_brain_miner() -> None:
    """Еженедельный pattern-miner — воскресенье 04:xx по локальному времени.

    Интервал 30 мин, гейт: weekday==6 (вс) и 4 <= hour < 6.
    Дедупликация по ISO-неделе через backfill_state key='miner_run'.
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        # Гейт: только воскресенье (weekday=6) в окне 04:00–05:59
        if now.weekday() != 6 or not (4 <= now.hour < 6):
            return

        week_key = now.strftime("%G-W%V")  # ISO год-неделя

        from services.creative_backfill import _get_state_by_key, _save_state_by_key
        from services.pattern_miner import mine_patterns

        # Проверяем, не запускались ли на этой ISO-неделе
        miner_state = _get_state_by_key("miner_run")
        if miner_state.get("last_iso_week") == week_key:
            return  # уже запускались на этой неделе

        # Помечаем ДО запуска
        _save_state_by_key("miner_run", {"last_iso_week": week_key})

        result = mine_patterns()
        logger.info(
            "_cron_brain_miner: неделя %s — learnings_written=%d slices_evaluated=%d",
            week_key,
            result.get("learnings_written", 0),
            result.get("slices_evaluated", 0),
        )
    except Exception as exc:
        logger.warning("_cron_brain_miner: неожиданная ошибка — %s", exc)


@heartbeat("_cron_metrics_snapshot", 30)
def _cron_metrics_snapshot() -> None:
    """Дневной снапшот метрик объявлений → ad_daily_metrics.

    Интервал 30 мин, гейт 06:00–07:59 по локальному времени (даём FB закрыть вчерашние данные).
    Дедупликация по дню через backfill_state key='metrics_snapshot'.
    При rate_limit сбрасываем пометку — повторим в следующем тике окна.
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        # Гейт: только в окне 06:00–07:59 по локальному времени
        if now.hour not in (6, 7):
            return

        today_key = now.date().isoformat()

        from services.creative_backfill import _get_state_by_key, _save_state_by_key
        from services.fb_token_provider import fb_account, offline_account_context
        from services.launch_routing import accounts_to_scan
        from services.metrics_snapshot import capture_daily_snapshot_complete

        # Снапшот по каждому оффлайн-кабинету карты роутинга (cabinet_a + cabinet_b):
        # upsert по (ad_id, date), ad_id между кабинетами не пересекаются.
        # Ключ состояния дефолтного кабинета — legacy «metrics_snapshot», чтобы
        # после деплоя не переснимать уже снятые окна.
        from config import FB_ACCOUNT_ID

        default_account = str(FB_ACCOUNT_ID or "").replace("act_", "").strip()
        for account_id in accounts_to_scan():
            state_key = (
                "metrics_snapshot"
                if account_id == default_account
                else f"metrics_snapshot:{account_id}"
            )
            # Дедупликация: снапшот кабинета делается один раз в день
            snap_state = _get_state_by_key(state_key)
            if snap_state.get("last_window") == today_key:
                continue  # этот кабинет уже сняли сегодня

            # Pending не считается успехом: incomplete окно обязано повториться.
            _save_state_by_key(
                state_key,
                {"last_window": snap_state.get("last_window"), "pending_window": today_key},
            )

            with fb_account(offline_account_context(account_id)):
                result = capture_daily_snapshot_complete(now=now)
            if not result.complete:
                _save_state_by_key(
                    state_key,
                    {"last_window": snap_state.get("last_window"), "pending_window": today_key},
                )
                logger.warning(
                    "_cron_metrics_snapshot: act_%s incomplete — окно остаётся pending",
                    account_id,
                )
                continue
            logger.info(
                "_cron_metrics_snapshot: act_%s %s — fetched=%d upserted=%d rate_limited=%s",
                account_id,
                result.target_date.isoformat(),
                len(result.fetched_ad_ids),
                len(result.upserted_ad_ids),
                "RATE_LIMITED" in result.incomplete_reason_codes,
            )
            _save_state_by_key(
                state_key,
                {"last_window": today_key, "pending_window": None},
            )

    except Exception as exc:
        logger.warning("_cron_metrics_snapshot: неожиданная ошибка — %s", exc)


@heartbeat("_cron_sync_recent_ads", 30, critical=False)
def _cron_sync_recent_ads() -> None:
    """Ежедневный досинк недавних объявлений (created_time за 7д) → creative_kb с created_at.

    Гейт 01:00–01:59 по локальному времени — ДО окна почасового сборщика (02–03), чтобы у сборщика
    были свежие кандидаты с created_at (если FB-путь сборщика упадёт на фолбэк по
    creative_kb). Дедуп по дню через backfill_state key='sync_recent_ads'. Закрывает
    дыру «новые ad_id не в картотеке»: регулярный синк created_at не пишет, разовый
    бэкфилл давно done. НЕ трогает done-флаг разового бэкфилла (независимый проход).
    При rate_limit сбрасываем пометку — повторим попытку в следующем тике окна.
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        # Гейт: только в окне 01:00–01:59 по локальному времени (перед сборщиком 02–03)
        if now.hour != 1:
            return

        today_key = now.date().isoformat()

        from services.creative_backfill import (
            _get_state_by_key,
            _save_state_by_key,
            sync_recent_ads,
        )

        # Дедупликация: досинк делается один раз в день
        state = _get_state_by_key("sync_recent_ads")
        if state.get("last_window") == today_key:
            return  # уже досинкали сегодня

        # Помечаем ДО запуска — чтобы повторный тик не дублировал
        _save_state_by_key("sync_recent_ads", {"last_window": today_key})

        result = sync_recent_ads(days=7)
        logger.info(
            "_cron_sync_recent_ads: fetched=%d upserted=%d rate_limited=%s error=%s",
            result.get("fetched", 0),
            result.get("upserted", 0),
            result.get("rate_limited", False),
            result.get("error"),
        )

        # При rate_limit сбрасываем пометку — повторим попытку в следующем тике окна
        if result.get("rate_limited"):
            _save_state_by_key("sync_recent_ads", {"last_window": None})
            logger.warning("_cron_sync_recent_ads: rate_limit — сбрасываем пометку для повтора")

    except Exception as exc:
        logger.warning("_cron_sync_recent_ads: неожиданная ошибка — %s", exc)


@heartbeat("_cron_hourly_collector", 30, critical=False)
def _cron_hourly_collector() -> None:
    """Ночной сборщик почасовых метрик первых 48ч жизни объявления (ARCH-hourly-collector).

    Интервал 30 мин, гейт 02:00–03:59 по локальному времени (окно 02–04, тик каждые полчаса даёт
    2 попытки в час — если первая словит rate-limit, вторая догонит). Дедупликация
    по дню через backfill_state key='hourly_collector'. НЕ влияет ни на какие решения —
    чистый сбор данных для будущего research v3 «раннего прогноза».
    При rate_limit сбрасываем пометку — повторим попытку в следующем тике окна.
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        # Гейт: только в окне 02:00–03:59 по локальному времени
        if now.hour not in (2, 3):
            return

        today_key = now.date().isoformat()

        from services.creative_backfill import _get_state_by_key, _save_state_by_key
        from services.hourly_collector import collect_hourly_metrics

        # Дедупликация: сбор делается один раз в день
        state = _get_state_by_key("hourly_collector")
        if state.get("last_window") == today_key:
            return  # уже собрали сегодня

        # Помечаем ДО запуска — чтобы повторный тик не дублировал
        _save_state_by_key("hourly_collector", {"last_window": today_key})

        result = collect_hourly_metrics()
        logger.info(
            "_cron_hourly_collector: candidates=%d processed=%d upserted=%d capped=%s rate_limited=%s",
            result.get("candidates", 0),
            result.get("processed_ads", 0),
            result.get("upserted_rows", 0),
            result.get("capped", False),
            result.get("rate_limited", False),
        )

        # При rate_limit сбрасываем пометку — повторим попытку в следующем тике окна
        if result.get("rate_limited"):
            _save_state_by_key("hourly_collector", {"last_window": None})
            logger.warning("_cron_hourly_collector: rate_limit — сбрасываем пометку для повтора")

    except Exception as exc:
        logger.warning("_cron_hourly_collector: неожиданная ошибка — %s", exc)


@heartbeat("_cron_metrics_backfill", 20)
def _cron_metrics_backfill() -> None:
    """Ночная докрутка исторического бэкфилла day-метрик (один месяц за час).
    Гейт 03:00–05:59 по локальному времени; дедуп по часовому окну через backfill_state['metrics_backfill_cron'].
    Курсор ведётся на КАЖДЫЙ кабинет карты роутинга: cabinet_a — legacy-ключ
    «metrics_backfill», прочие — «metrics_backfill:<account_id>». Это и есть
    штатный дневной писатель ad_daily_metrics (strict-снапшот не завершается
    и строк не пишет). При rate_limit любого кабинета пометка
    окна сбрасывается — повторим в следующем тике, курсор не двигается.
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        if now.hour not in (3, 4, 5):
            return
        window_key = f"{now.date()}-{now.hour}"
        from services.creative_backfill import _get_state_by_key, _save_state_by_key
        from services.launch_routing import accounts_to_scan
        from services.metrics_backfill import run_metrics_backfill_increment
        cron_state = _get_state_by_key("metrics_backfill_cron")
        if cron_state.get("last_window") == window_key:
            return
        _save_state_by_key("metrics_backfill_cron", {"last_window": window_key})
        rate_limited_any = False
        for account_id in accounts_to_scan():
            result = run_metrics_backfill_increment(account_id=account_id)
            logger.info(
                "_cron_metrics_backfill: окно %s act_%s — fetched=%d upserted=%d rate_limited=%s done=%s",
                window_key, account_id, result.get("fetched", 0),
                result.get("upserted", 0), result.get("rate_limited", False), result.get("done", False),
            )
            rate_limited_any = rate_limited_any or bool(result.get("rate_limited"))
        # при rate_limit сбрасываем пометку окна — повторим в следующем тике
        if rate_limited_any:
            _save_state_by_key("metrics_backfill_cron", {"last_window": None})
    except Exception as exc:
        logger.warning("_cron_metrics_backfill: неожиданная ошибка — %s", exc)


@heartbeat("_cron_cohort_builder", 30)
def _cron_cohort_builder() -> None:
    """Недельные когорты объявлений → ad_weekly_cohorts (данные для тренда).

    Гейт 11:00–11:59 по локальному времени: окна 03–05 (FB-бэкфилл) и 06–07 (ночной снапшот)
    уже выбирают лимит FB, а 08–10 заняты отчётами и авто-запуском. Дедуп по
    дню через backfill_state['cohort_builder']. Обновляются последние
    _COHORT_REFRESH_WEEKS недель, включая текущую — квалы в AMO дозревают,
    поэтому свежие недели пересчитываются намеренно.

    При rate limit FB пометка дня сбрасывается: сборщик ничего не записал,
    и следующий тик окна обязан повторить попытку.
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        if now.hour != 11:
            return
        today_key = now.date().isoformat()
        from services.creative_backfill import _get_state_by_key, _save_state_by_key
        from services.cohort_builder import CohortRateLimitError, refresh_recent_weeks

        cron_state = _get_state_by_key("cohort_builder")
        if cron_state.get("last_window") == today_key:
            return
        # Помечаем ДО запуска — чтобы соседний тик не пошёл вторым проходом.
        _save_state_by_key("cohort_builder", {"last_window": today_key})
        try:
            result = refresh_recent_weeks(_COHORT_REFRESH_WEEKS, now=now)
        except CohortRateLimitError as exc:
            _save_state_by_key("cohort_builder", {"last_window": None})
            logger.warning("_cron_cohort_builder: rate limit FB — %s", exc)
            return
        except Exception:
            _save_state_by_key("cohort_builder", {"last_window": None})
            raise
        logger.info(
            "_cron_cohort_builder: %s..%s — недель %d, строк %d, записано %d, "
            "сравнимых %d, причины %s",
            result.get("since"), result.get("until"), result.get("weeks", 0),
            result.get("rows", 0), result.get("written", 0),
            result.get("comparable", 0), result.get("reasons", {}),
        )
    except Exception as exc:
        logger.warning("_cron_cohort_builder: неожиданная ошибка — %s", exc)


@heartbeat("_cron_pattern_engine", 30)
def _cron_pattern_engine() -> None:
    """Еженедельный движок паттернов — воскресенье 05:xx по локальному времени (после miner в 04).
    Гейт weekday==6 и 5<=hour<7; дедуп по ISO-неделе через backfill_state['pattern_engine_run'].
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        if now.weekday() != 6 or not (5 <= now.hour < 7):
            return
        week_key = now.strftime("%G-W%V")
        from services.creative_backfill import _get_state_by_key, _save_state_by_key
        from services.pattern_engine import run_pattern_engine
        st = _get_state_by_key("pattern_engine_run")
        if st.get("last_iso_week") == week_key:
            return
        _save_state_by_key("pattern_engine_run", {"last_iso_week": week_key})
        result = run_pattern_engine()
        logger.info(
            "_cron_pattern_engine: неделя %s — learnings_written=%d sufficiency=%s",
            week_key, result.get("learnings_written", 0), result.get("data_sufficiency"),
        )
    except Exception as exc:
        logger.warning("_cron_pattern_engine: неожиданная ошибка — %s", exc)


# Путь к state-файлу дедупликации ночного крона AMO-исходов
_MATCH_OUTCOMES_STATE = Path(__file__).parent.parent / "data" / "match_outcomes_state.json"


def _load_match_outcomes_state() -> dict:
    """Загружает состояние крона match-outcomes (last_run_date)."""
    try:
        if _MATCH_OUTCOMES_STATE.exists():
            return json.loads(_MATCH_OUTCOMES_STATE.read_text())
    except Exception as exc:
        logger.warning("_cron_match_outcomes: не удалось прочитать state — %s", exc)
    return {}


def _save_match_outcomes_state(state: dict) -> None:
    """Сохраняет состояние крона match-outcomes."""
    try:
        _MATCH_OUTCOMES_STATE.write_text(json.dumps(state, ensure_ascii=False))
    except Exception as exc:
        logger.warning("_cron_match_outcomes: не удалось сохранить state — %s", exc)


# Часы запуска AMO-исходов (4×/день, свежие исходы), локальное время
_MATCH_OUTCOMES_HOURS: frozenset[int] = frozenset({0, 6, 12, 18})


def _should_run_match_outcomes(now: datetime) -> bool:
    """Проверяет, нужно ли запускать крон AMO-исходов.

    Условия:
    - Локальный час в множестве {0, 6, 12, 18} (каждые 6 часов, 4×/день)
    - Этот 6-часовой слот ещё не запускался сегодня

    State хранит: {"slots": {"YYYY-MM-DD-HH": true, ...}}
    Ключ слота: "дата-час" — уникален для каждого допустимого часового окна.
    """
    if now.hour not in _MATCH_OUTCOMES_HOURS:
        return False
    state = _load_match_outcomes_state()
    slot_key = f"{now.date().isoformat()}-{now.hour}"
    # slots — словарь выполненных слотов (дата-час → true)
    slots = state.get("slots", {})
    return not slots.get(slot_key, False)


@heartbeat("_cron_match_outcomes", 15, critical=True)
def _cron_match_outcomes() -> None:
    """Крон сопоставления AMO-исходов к объявлениям creative_kb.

    Интервал 15 мин, гейт — локальный час в {0, 6, 12, 18} И этот слот ещё не запускался.
    Каждые 6 часов: 00:xx, 06:xx, 12:xx, 18:xx — свежие исходы 4× в день.
    Обрабатывает только последние 2 месяца (months_back=2) — не всю историю.
    """
    try:
        now = datetime.now(_TZ_LOCAL)

        if not _should_run_match_outcomes(now):
            return

        # Помечаем слот ДО запуска — повторный тик в том же часу не дублирует
        state = _load_match_outcomes_state()
        slot_key = f"{now.date().isoformat()}-{now.hour}"
        slots = state.get("slots", {})
        slots[slot_key] = True
        # Чистим старые слоты (старше 2 дней, чтобы файл не рос)
        today = now.date().isoformat()
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        slots = {k: v for k, v in slots.items() if k[:10] >= yesterday}
        _save_match_outcomes_state({"slots": slots})

        from services.amo_outcomes import attach_amo_outcomes

        result = attach_amo_outcomes(months_back=2, batch_days=30)

        if "error" in result:
            logger.warning("_cron_match_outcomes: завершился с ошибкой — %s", result["error"])
            # Ошибка возвращена dict'ом (не брошена) — это тоже «тихий» сбой, считаем провалом
            report_cron_failure("_cron_match_outcomes", result["error"])
        else:
            logger.info(
                "_cron_match_outcomes: windows=%d leads=%d matched=%d updated=%d",
                result.get("windows", 0),
                result.get("leads_fetched", 0),
                result.get("ads_matched", 0),
                result.get("ads_updated", 0),
            )
            report_cron_success("_cron_match_outcomes")
    except Exception as exc:
        logger.warning("_cron_match_outcomes: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_match_outcomes", exc)


# Путь к state-файлу дедупликации ежедневного крона боевого автопилота
_AUTOPILOT_LIVE_STATE = Path(__file__).parent.parent / "data" / "autopilot_live_state.json"


def _load_autopilot_live_state() -> dict:
    """Загружает состояние крона _cron_autopilot_live."""
    try:
        if _AUTOPILOT_LIVE_STATE.exists():
            return json.loads(_AUTOPILOT_LIVE_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("_cron_autopilot_live: не удалось прочитать state — %s", exc)
    return {}


def _save_autopilot_live_state(state: dict) -> None:
    """Сохраняет состояние крона _cron_autopilot_live."""
    try:
        _AUTOPILOT_LIVE_STATE.parent.mkdir(parents=True, exist_ok=True)
        _AUTOPILOT_LIVE_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("_cron_autopilot_live: не удалось сохранить state — %s", exc)


# Часы запуска боевого автопилота — Страж (ARCH-phase1-guardian §8.1): учащено
# до каждых 2ч в окне открутки 08-22 по локальному времени (было {9,13,17,21} — 4ч, 4×/день).
# Дневной лимит пауз из конфига (max_pauses_per_day) не даст переборщить.
# Компромисс из спеки: если в боевом режиме всплывёт FB rate-limit — вернуть
# {8,12,16,20} (каждые 4ч) в этой же константе.
_AUTOPILOT_LIVE_HOURS: frozenset[int] = frozenset({8, 10, 12, 14, 16, 18, 20, 22})


def _should_run_autopilot_live(now: datetime) -> bool:
    """Проверяет, нужно ли запускать боевой автопилот в этом тике.

    Условия (оба должны выполняться):
    - Локальный час в _AUTOPILOT_LIVE_HOURS (каждые 2ч, 08-22 по локальному времени, 8×/день)
    - Этот часовой слот ещё не запускался сегодня

    State хранит: {"slots": {"YYYY-MM-DD-HH": <result>, ...}, ...}
    """
    if now.hour not in _AUTOPILOT_LIVE_HOURS:
        return False
    state = _load_autopilot_live_state()
    slot_key = f"{now.date().isoformat()}-{now.hour}"
    slots = state.get("slots", {})
    return slot_key not in slots


@heartbeat("_cron_autopilot_live", 15, critical=True)
def _cron_autopilot_live() -> None:
    """Крон боевого автопилота — паузит аутсайдеров каждые 2ч в 08-22 по локальному времени (Страж).

    Интервал 15 мин, гейт — час в _AUTOPILOT_LIVE_HOURS И этот слот ещё не запускался.
    Дневной лимит пауз (max_pauses_per_day из конфига) защищает от перебора.

    Учащено с 4×/день ({9,13,17,21}) до 8×/день ({8,10,...,22}) по ARCH-phase1-guardian:
    свежий слив должен ловиться в течение дня открутки, а не через сутки. Заодно
    подхватывает PAUSE-кандидатов от новых Guardian-правил (early_waster/wasted_no_crm
    в decision_policy) — единая точка пауз (см. §6.4 спеки), guardian.run_guardian_sweep
    сам не паузит.

    run_autopilot_live сам проверяет enabled/kill_switch — здесь не дублируем.
    max_pauses берётся из конфига (поле max_pauses_per_run, дефолт 5).
    """
    try:
        now = datetime.now(_TZ_LOCAL)

        if not _should_run_autopilot_live(now):
            return

        slot_key = f"{now.date().isoformat()}-{now.hour}"

        # Читаем актуальный state и помечаем слот ДО запуска — защита от дублей
        current_state = _load_autopilot_live_state()
        slots = current_state.get("slots", {})
        slots[slot_key] = "running"
        # Чистим старые слоты (оставляем только за 2 последних дня)
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        slots = {k: v for k, v in slots.items() if k[:10] >= yesterday}
        _save_autopilot_live_state({**current_state, "slots": slots})

        from services.autopilot import run_autopilot_live, get_autopilot_config

        cfg_cap = int(get_autopilot_config().get("max_pauses_per_run", 5))
        result = run_autopilot_live(max_pauses=cfg_cap, trigger="cron")

        # Сохраняем итог слота для диагностики
        current_state2 = _load_autopilot_live_state()
        slots2 = current_state2.get("slots", {})
        slots2[slot_key] = {
            "analyzed": result.get("analyzed", 0),
            "paused": len(result.get("paused", [])),
            "skipped": result.get("skipped"),
            "ran": result.get("ran"),
        }
        _save_autopilot_live_state({**current_state2, "slots": slots2})

        logger.info(
            "_cron_autopilot_live: slot=%s ran=%s analyzed=%d paused=%d skipped=%s",
            slot_key,
            result.get("ran"),
            result.get("analyzed", 0),
            len(result.get("paused", [])),
            result.get("skipped"),
        )
        report_cron_success("_cron_autopilot_live")
    except Exception as exc:
        logger.warning("_cron_autopilot_live: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_autopilot_live", exc)


# Крон _cron_shadow_report (рассылка «🔮 Тень — что бы я сделал сегодня») снят
# по решению владельца — реликт отменённого «режима тени»,
# дублировал и путал с боевым контуром run_autopilot_live. Вместе с ним снята
# и heartbeat-обёртка @heartbeat("_cron_shadow_report", ...) — сторож кронов
# (services/cron_heartbeat.py) больше не ждёт отметок по этому крону.
# Расчётные функции (services.shadow_report._fetch_ads_from_local_db, score_and_decide
# в services.decision_policy) НЕ трогали — их используют autopilot/budget_scaler/guardian/
# coverage_monitor/brief_generator.


# Путь к state-файлу дедупликации ежедневного крона авто-запуска
_AUTO_LAUNCH_CRON_STATE = Path(__file__).parent.parent / "data" / "auto_launch_cron_state.json"


def _load_auto_launch_cron_state() -> dict:
    """Загружает состояние крона _cron_auto_launch (last_run_date)."""
    try:
        if _AUTO_LAUNCH_CRON_STATE.exists():
            return json.loads(_AUTO_LAUNCH_CRON_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("_cron_auto_launch: не удалось прочитать state — %s", exc)
    return {}


def _save_auto_launch_cron_state(state: dict) -> None:
    """Сохраняет состояние крона _cron_auto_launch."""
    try:
        _AUTO_LAUNCH_CRON_STATE.parent.mkdir(parents=True, exist_ok=True)
        _AUTO_LAUNCH_CRON_STATE.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("_cron_auto_launch: не удалось сохранить state — %s", exc)


def _should_run_auto_launch_cron(now: datetime) -> bool:
    """Проверяет, нужно ли запускать крон авто-запуска в этом тике.

    Условия (оба должны выполняться):
    - Локальный час == 10 (после монитора покрытия в 09:xx)
    - Ещё не запускался сегодня (state-файл data/auto_launch_cron_state.json, ключ last_run_date)
    """
    if now.hour != 10:
        return False
    state = _load_auto_launch_cron_state()
    last_run = state.get("last_run_date")
    today_str = now.date().isoformat()
    return last_run != today_str


@heartbeat("_cron_auto_launch", 15)
def _cron_auto_launch() -> None:
    """Ежедневный крон авто-запуска рекламных карточек — 10:xx по локальному времени.

    Интервал 15 мин, гейт — локальный час == 10 И не запускался сегодня
    (state-файл data/auto_launch_cron_state.json, ключ last_run_date).

    Час 10 выбран потому что:
    - монитор покрытия (09:xx) уже выполнился, данные свежие
    - впереди активная часть рабочего дня

    Логика выбора режима:
    - launch_enabled=true (И enabled=true И kill_switch=false) → mode="active"
    - иначе → mode="dry_run" (отправляет ПЛАН в Telegram каждый день,
      даже когда боевые запуски выключены — так владелец видит что рекомендует бот)

    run_auto_launch сам проверяет gates внутри — здесь не дублируем.
    max_launches берётся из конфига (поле max_launches_per_day, дефолт 1).
    """
    try:
        now = datetime.now(_TZ_LOCAL)

        if not _should_run_auto_launch_cron(now):
            return

        # Помечаем дату ДО запуска — защита от дублей при повторных тиках в 10:xx
        _save_auto_launch_cron_state({"last_run_date": now.date().isoformat()})

        from services.auto_launch import run_auto_launch, _get_autopilot_config
        from services.launch_checker import LaunchSource

        cfg = _get_autopilot_config()
        max_launches = int(cfg.get("max_launches_per_day", 1))

        # Определяем режим по состоянию предохранителей
        launch_enabled = (
            cfg.get("launch_enabled", False)
            and cfg.get("enabled", False)
            and not cfg.get("kill_switch", False)
        )
        mode = "active" if launch_enabled else "dry_run"

        result = run_auto_launch(
            mode=mode,
            max_launches=max_launches,
            source=LaunchSource.CRON,
        )

        logger.info(
            "_cron_auto_launch: mode=%s launched=%d recommendations=%d skipped=%s",
            mode,
            len(result.get("launched", [])),
            len(result.get("recommendations", [])),
            result.get("skipped_reason"),
        )
    except Exception as exc:
        logger.warning("_cron_auto_launch: неожиданная ошибка — %s", exc)


# ---------------------------------------------------------------------------
# Крон масштабирования бюджетов (_cron_budget_scaler)
# ---------------------------------------------------------------------------

_BUDGET_SCALER_CRON_STATE = Path(__file__).parent.parent / "data" / "budget_scaler_cron_state.json"

# Таймаут одного прогона скейлера. Был кейс зависания 1ч43м (FB flaky) — прогон
# крутится в воркер-потоке apscheduler (крон синхронный), поэтому signal/SIGALRM
# не годится (работает только в main-thread) → таймаут через ThreadPoolExecutor +
# future.result(timeout). По истечении — прерываем, шлём алерт, день НЕ помечаем
# выполненным (ретрай на следующем тике). Держать в синхроне с текстом алерта ниже.
_SCALER_TIMEOUT_SEC = 20 * 60  # 20 минут
_SCALER_TIMEOUT_ALERT_TITLE = "Прогон скейлера превысил 20 мин — прерван"

# Wave 1B.1: cron-state читаем-модифицируем-пишем под замком (защита от гонки
# двух тиков в одном процессе), запись атомарна (уникальный temp + fsync + rename).
_BUDGET_SCALER_CRON_LOCK = threading.Lock()

def _load_budget_scaler_cron_state() -> dict:
    """Загружает состояние крона _cron_budget_scaler (last_run_date)."""
    try:
        if _BUDGET_SCALER_CRON_STATE.exists():
            return json.loads(_BUDGET_SCALER_CRON_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("_cron_budget_scaler: не удалось прочитать state — %s", exc)
    return {}


def _save_budget_scaler_cron_state(state: dict) -> None:
    """Атомарно сохраняет состояние крона _cron_budget_scaler (уникальный temp+fsync+rename)."""
    try:
        _BUDGET_SCALER_CRON_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _BUDGET_SCALER_CRON_STATE.with_name(
            f"{_BUDGET_SCALER_CRON_STATE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, _BUDGET_SCALER_CRON_STATE)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            raise
    except Exception as exc:
        logger.warning("_cron_budget_scaler: не удалось сохранить state — %s", exc)


def _should_run_budget_scaler_cron(now: datetime) -> bool:
    """Проверяет, нужно ли запускать крон масштабирования бюджетов в этом тике.

    Условия:
    - Локальный час == 13 (после пауз в 12:00 от _cron_autopilot_live)
    - Ещё НЕ помечен выполненным сегодня (last_run_date != today) — метка ставится
      ПОСЛЕ успешного прогона, поэтому таймаут/сбой оставляют день непомеченным → ретрай.
    - Не идёт прогон прямо сейчас: in-flight замок started_at не старше _SCALER_TIMEOUT_SEC
      (защита от параллельного запуска повторным тиком внутри 20-мин окна). Просроченный
      замок (прогон завис/упал) игнорируем — можно ретраить.
    """
    if now.hour != 13:
        return False
    state = _load_budget_scaler_cron_state()
    today_str = now.date().isoformat()
    if state.get("last_run_date") == today_str:
        return False
    started_at = state.get("started_at")
    if started_at:
        try:
            started_dt = datetime.fromisoformat(started_at)
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=_TZ_LOCAL)
            if (now - started_dt).total_seconds() < _SCALER_TIMEOUT_SEC:
                return False  # прогон ещё идёт — не дублируем
        except Exception:
            pass  # битую метку игнорируем — считаем что прогона нет
    return True


@heartbeat("_cron_budget_scaler", 15, critical=True)
def _cron_budget_scaler() -> None:
    """Ежедневный крон масштабирования бюджетов адсетов — 13:xx по локальному времени.

    Интервал 15 мин, гейт — локальный час == 13 И не запускался сегодня
    (state-файл data/budget_scaler_cron_state.json, ключ last_run_date).

    Час 13 выбран потому что:
    - autopilot_live (12:xx) уже выполнился — паузы применены, картина свежая
    - впереди вторая половина активного дня, повышение бюджета даст трафик сегодня

    run_budget_scaling(mode="active") сам проверяет gates внутри:
    - scale_enabled, enabled, kill_switch — предохранители
    - кулдаун 4 часа между реальными повышениями
    Крон не дублирует эти проверки, только передаёт mode="active".
    """
    try:
        now = datetime.now(_TZ_LOCAL)

        # Wave 1B.1: гейт-чек и пометку СТАРТА делаем атомарно под замком —
        # защита от гонки двух тиков одного процесса между _should_run и записью
        # started_at. Дату «выполнено» (last_run_date) ставим ПОСЛЕ успеха, чтобы
        # таймаут/сбой не «съедали» день (ретрай на следующем тике). Сам active-прогон
        # ещё защищён межпроцессным замком внутри run_budget_scaling (Wave 1B.1):
        # даже если прошлый прогон переполз по future.result-таймауту и ещё жив,
        # следующий тик получит skipped_reason, а не второй active-прогон.
        with _BUDGET_SCALER_CRON_LOCK:
            if not _should_run_budget_scaler_cron(now):
                return

            # In-flight замок: помечаем СТАРТ (не «выполнено») — защита от параллельного
            # тика в 20-мин окне. Дату «выполнено» ставим ПОСЛЕ успеха, чтобы таймаут/сбой
            # не «съедали» день (было: пометка ДО запуска → зависание блокировало ретрай).
            start_state = _load_budget_scaler_cron_state()
            start_state["started_at"] = now.isoformat()
            _save_budget_scaler_cron_state(start_state)

        from services.budget_scaler import run_budget_scaling, get_scale_config

        cfg = get_scale_config()
        max_scales = int(cfg.get("max_scales_per_run", 2))

        # Прогон в отдельном потоке с жёстким таймаутом (FB flaky → зависание 1ч43м).
        # future.result(timeout) не убивает зависший поток, но освобождает крон:
        # shutdown(wait=False) не ждёт его, следующий тик ретраит.
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="budget_scaler")
        try:
            future = pool.submit(run_budget_scaling, mode="active", max_scales=max_scales)
            try:
                result = future.result(timeout=_SCALER_TIMEOUT_SEC)
            except FuturesTimeoutError:
                # Прогон завис — прерываем. День НЕ помечаем выполненным (ретрай),
                # started_at оставляем: гейт держит замок до истечения окна, потом ретрай.
                logger.warning("_cron_budget_scaler: прогон превысил %d сек — прерван", _SCALER_TIMEOUT_SEC)
                from services.notifications import send_critical_alert
                send_critical_alert(
                    _SCALER_TIMEOUT_ALERT_TITLE,
                    f"run_budget_scaling не завершился за {_SCALER_TIMEOUT_SEC // 60} мин "
                    f"(вероятно FB подвис). День не помечен выполненным — будет ретрай.",
                    channel="ads",
                )
                report_cron_failure("_cron_budget_scaler", "timeout > %d сек" % _SCALER_TIMEOUT_SEC)
                return
        finally:
            pool.shutdown(wait=False)

        # Успех — ТЕПЕРЬ помечаем день выполненным и снимаем in-flight замок
        done_state = _load_budget_scaler_cron_state()
        done_state["last_run_date"] = now.date().isoformat()
        done_state.pop("started_at", None)
        _save_budget_scaler_cron_state(done_state)

        logger.info(
            "_cron_budget_scaler: ran=%s winners=%d raised=%d skipped=%s",
            result.get("ran"),
            len(result.get("winners", [])),
            len(result.get("scaled", [])),
            result.get("skipped_reason"),
        )
        report_cron_success("_cron_budget_scaler")
    except Exception as exc:
        logger.warning("_cron_budget_scaler: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_budget_scaler", exc)


# ---------------------------------------------------------------------------
# Обновление spend АКТИВНЫХ объявлений (_cron_refresh_active_spend) — Страж
# ---------------------------------------------------------------------------

# Часы рефреша расхода — Страж (ARCH-phase1-guardian §8.1): те же 8 слотов,
# что и у autopilot_live (каждые 2ч, 08-22 по локальному времени). В _REFRESH_FULL_HOUR —
# полный рефреш (spend + статусы, как раньше единственный крон в 12:xx),
# в остальных слотах — лёгкий (только spend, без похода за статусами кабинета).
_REFRESH_HOURS: frozenset[int] = frozenset({8, 10, 12, 14, 16, 18, 20, 22})
_REFRESH_FULL_HOUR = 12


def _should_run_spend_refresh(now: datetime) -> bool:
    """Гейт: локальный час в _REFRESH_HOURS И этот часовой слот сегодня ещё не запускался.

    Дедуп теперь по слоту «дата-час» (а не по дате) — крон бежит несколько раз в день.
    """
    if now.hour not in _REFRESH_HOURS:
        return False
    from services.spend_refresh import _load_state
    state = _load_state()
    slot_key = f"{now.date().isoformat()}-{now.hour}"
    slots = state.get("slots", {})
    return slot_key not in slots


@heartbeat("_cron_refresh_active_spend", 15, critical=True)
def _cron_refresh_active_spend() -> None:
    """Апдейт spend АКТИВНЫХ объявлений — каждые 2ч в 08-22 по локальному времени (Страж).

    Интервал 15 мин, гейт — час в _REFRESH_HOURS И этот слот ещё не запускался.
    В _REFRESH_FULL_HOUR (12:xx) — полный рефреш (spend + effective_status всего
    кабинета, как раньше был единственный ежедневный прогон). В остальных слотах —
    лёгкий (refresh_active_ads_spend_light, только lifetime-spend ACTIVE, БЕЗ статусов) —
    экономит FB-запросы учащённого 2-часового цикла (см. §6.3, §8.1 спеки).
    При успехе пишет guardian.mark_spend_refresh_ok — читается сторожем свежести
    данных (_cron_data_freshness_watchdog).
    """
    try:
        now = datetime.now(_TZ_LOCAL)

        if not _should_run_spend_refresh(now):
            return

        from services.spend_refresh import _load_state, _save_state, refresh_active_ads_spend, refresh_active_ads_spend_light
        from services import guardian

        slot_key = f"{now.date().isoformat()}-{now.hour}"

        # Помечаем слот ДО запуска — защита от дублей при повторных тиках внутри часа
        current_state = _load_state()
        slots = current_state.get("slots", {})
        slots[slot_key] = "running"
        # Чистим старые слоты (оставляем только за 2 последних дня)
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        slots = {k: v for k, v in slots.items() if k[:10] >= yesterday}
        _save_state({**current_state, "slots": slots, "last_run_date": now.date().isoformat()})

        is_full = now.hour == _REFRESH_FULL_HOUR
        result = refresh_active_ads_spend() if is_full else refresh_active_ads_spend_light()

        # Успешный прогон (не бросил исключение) — отмечаем для сторожа свежести данных
        guardian.mark_spend_refresh_ok(now)

        logger.info(
            "_cron_refresh_active_spend: slot=%s mode=%s active=%d fetched=%d updated=%d",
            slot_key,
            "full" if is_full else "light",
            result.get("active_count", 0),
            result.get("fetched", 0),
            result.get("updated", 0),
        )
        report_cron_success("_cron_refresh_active_spend")
    except Exception as exc:
        logger.warning("_cron_refresh_active_spend: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_refresh_active_spend", exc)


# ---------------------------------------------------------------------------
# Захват дневного Google-расхода из снимка Google Sheets
# ---------------------------------------------------------------------------

_GOOGLE_SPEND_CRON_STATE = Path(__file__).parent.parent / "data" / "google_spend_cron_state.json"


def _load_google_spend_cron_state() -> dict:
    """Загружает состояние крона захвата Google-расхода (last_run_date)."""
    try:
        if _GOOGLE_SPEND_CRON_STATE.exists():
            return json.loads(_GOOGLE_SPEND_CRON_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("_cron_capture_google_spend: не удалось прочитать state — %s", exc)
    return {}


def _save_google_spend_cron_state(state: dict) -> None:
    """Сохраняет состояние крона захвата Google-расхода."""
    try:
        _GOOGLE_SPEND_CRON_STATE.parent.mkdir(parents=True, exist_ok=True)
        _GOOGLE_SPEND_CRON_STATE.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("_cron_capture_google_spend: не удалось сохранить state — %s", exc)


def _should_run_google_spend_cron(now: datetime) -> bool:
    """Гейт: локальный час == 8 И дата != last_run_date."""
    if now.hour != 8:
        return False
    state = _load_google_spend_cron_state()
    return state.get("last_run_date") != now.date().isoformat()


@heartbeat("_cron_capture_google_spend", 15)
def _cron_capture_google_spend() -> None:
    """Ежедневный захват дневного Google-расхода из снимка Google Sheets — 08:xx по локальному времени.

    Интервал 15 мин, гейт — локальный час == 8 И не запускался сегодня.
    sheet_id берётся из autopilot.google_spend_sheet_id; если не задан — пропускаем.
    Сохраняет в data/google_daily_spend.json как {date_iso: spend_usd}.
    """
    try:
        now = datetime.now(_TZ_LOCAL)

        if not _should_run_google_spend_cron(now):
            return

        from agent.scheduler import load_settings
        cfg = load_settings().get("autopilot") or {}
        sheet_id = cfg.get("google_spend_sheet_id", "")
        if not sheet_id:
            logger.info("_cron_capture_google_spend: google_spend_sheet_id не задан — пропускаем")
            return

        # Помечаем дату ДО запуска — защита от дублей при повторных тиках в 08:xx
        _save_google_spend_cron_state({"last_run_date": now.date().isoformat()})

        from services.google_spend import (
            capture_daily_google_spend,
            rescan_recent_google_spend,
        )
        capture_daily_google_spend(sheet_id)

        # Рескан последних 7 дней: подрядчик грузит вкладки с задержкой (иногда
        # пачкой за неделю) — дотягиваем недостающие/изменившиеся дни из
        # появившихся вкладок, иначе пропущенный день навсегда остаётся нулём
        # (фикс по претензии владельца). Идемпотентно.
        rescan_stats = rescan_recent_google_spend(sheet_id, days=7)

        logger.info(
            "_cron_capture_google_spend: захват выполнен для %s; рескан 7д — добавлено %d, обновлено %d",
            sheet_id, rescan_stats.get("added", 0), rescan_stats.get("updated", 0),
        )
    except Exception as exc:
        logger.warning("_cron_capture_google_spend: неожиданная ошибка — %s", exc)


@heartbeat("_cron_coverage_report", 15)
def _cron_coverage_report() -> None:
    """Монитор покрытия рекламой — каждые 15 мин; реальная отправка в 09:xx по локальному времени, раз в день.

    Гейт: локальный час == 9 И дата != last_sent_date (data/coverage_state.json).
    Только чтение локальной БД + Telegram. Никаких действий с FB.
    """
    try:
        from services.coverage_monitor import (
            should_send_coverage_report,
            send_coverage_report,
            _load_coverage_state,
            _save_coverage_state,
        )

        now = datetime.now(_TZ_LOCAL)
        state = _load_coverage_state()

        if not should_send_coverage_report(now, state.get("last_sent_date")):
            return

        # Помечаем ДО отправки — повторный тик в 09:xx не дублирует
        state["last_sent_date"] = now.date().isoformat()
        _save_coverage_state(state)

        sent = send_coverage_report()
        if sent:
            logger.info("Монитор покрытия: отчёт отправлен (%s)", now.date())
        else:
            logger.warning("_cron_coverage_report: send_coverage_report вернул False")
    except Exception as exc:
        logger.warning("_cron_coverage_report: неожиданная ошибка — %s", exc)


@heartbeat("_cron_coverage_guard", 30, critical=True)
def _cron_coverage_guard() -> None:
    """Live-страж покрытия адсетов — каждые 30 мин, без гейта по часу.

    В отличие от утреннего _cron_coverage_report (сводка раз в день из локальной
    БД) это частая проверка ЖИВОГО инвентаря FB по N городам × L2/L1:
    ноль effective ACTIVE в скоупе → критический алерт в этом же прогоне,
    ниже MIN_ACTIVE_PER_GROUP → предупреждение.

    Дедуп живёт в coverage_repository (durable инциденты + outbox), своего
    state-файла у стража нет: подтверждённый ZERO-алерт больше не повторяется до
    закрытия инцидента, THIN напоминает не чаще раза в сутки. Поэтому крон можно
    гонять каждые 30 минут без спама.

    FB — только чтение. Неполный инвентарь (ошибка/лимит FB) не выдаёт «всё ок»:
    группы уходят в UNKNOWN, а прогон отмечается провалом через
    report_cron_failure (алерт на 3-м провале подряд).
    """
    try:
        from services.coverage_guard import run_coverage_guard

        result = run_coverage_guard()
        logger.info(
            "_cron_coverage_guard: zero=%s thin=%s unknown=%s ok=%d "
            "открыто=%d напоминаний=%d закрыто=%d отправлено=%d",
            result.get("zero"),
            result.get("thin"),
            result.get("unknown"),
            result.get("ok_count", 0),
            result.get("opened_count", 0),
            result.get("reminder_count", 0),
            result.get("resolved_count", 0),
            result.get("sent_count", 0),
        )
        if result.get("ok"):
            report_cron_success("_cron_coverage_guard")
        else:
            # Неполный live-инвентарь: молчать нельзя, «в норме» рапортовать тоже.
            report_cron_failure(
                "_cron_coverage_guard",
                f"инвентарь неполный, UNKNOWN: {result.get('unknown')}",
            )
    except Exception as exc:
        logger.warning("_cron_coverage_guard: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_coverage_guard", exc)


@heartbeat("_cron_brief_generator", 30)
def _cron_brief_generator() -> None:
    """Крон авто-генератора ТЗ — 2 раза в неделю (понедельник и четверг, 08:xx по локальному времени).

    Каденция x2 в неделю по решению владельца (раз в неделю было слишком
    вяло).

    Гейт: мастер-выключатель settings.brief_generator.enabled (дефолт OFF, генератор
    на редизайне — старой логикой автопостинг в Trello не должен улетать) +
    день недели в (0, 3) (понедельник, четверг) + час == 8 + гейт MIN_INTERVAL_DAYS
    по state-файлу. Помечаем ДО запуска — повторный тик в том же часу не дублирует.
    Только создание карточек Trello — никаких FB-действий.
    """
    try:
        from agent.scheduler import load_settings as _load_settings_for_brief_gate
        if not _load_settings_for_brief_gate().get("brief_generator", {}).get("enabled", False):
            return  # авто-генератор ТЗ выключен (по умолчанию OFF — генератор на редизайне)

        now = datetime.now(_TZ_LOCAL)
        # Понедельник (weekday=0) и четверг (weekday=3), час = 8 по локальному времени
        if now.weekday() not in (0, 3) or now.hour != 8:
            return

        from services.brief_generator import _load_state, _save_state, should_run_generator

        state = _load_state()
        if not should_run_generator(now, state):
            return

        # Помечаем last_scheduled_run_date ДО запуска — повторный тик не дублирует.
        # Фикс: раздельно от last_manual_run_date (ручные /brief и
        # HTTP-эндпоинт generate-briefs-now) — иначе ручной прогон накануне
        # крадёт плановый слот крона (см. services/brief_generator.py).
        state["last_scheduled_run_date"] = now.isoformat()
        _save_state(state)

        from services.brief_generator import generate_and_push_briefs
        result = generate_and_push_briefs(max_briefs=3, trigger="scheduled")
        logger.info(
            "_cron_brief_generator: создано=%d, пропущено=%d, ошибка=%s",
            result.get("created", 0),
            result.get("skipped", 0),
            result.get("error"),
        )
    except Exception as exc:
        logger.warning("_cron_brief_generator: неожиданная ошибка — %s", exc)


# State-файл для гейтинга почасового сторожа (отдельный от watchdog state)
_ADS_WATCHDOG_CRON_STATE: dict = {"last_hour": None}
_ADS_WATCHDOG_LOCK = threading.Lock()


@heartbeat("_cron_ads_watchdog", 15)
def _cron_ads_watchdog() -> None:
    """Почасовой сторож рекламных объявлений — каждые 15 мин, гейт «новый час».

    Проверяет: аномалию открутки и проблемные статусы объявлений.
    Read-only — никаких действий с FB.
    Алерты шлёт в Telegram channel="health" с дедупликацией (6ч на алерт).
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        # Гейт: запускаем один раз в каждом часовом окне
        hour_key = f"{now.date().isoformat()}-{now.hour}"
        with _ADS_WATCHDOG_LOCK:
            if _ADS_WATCHDOG_CRON_STATE["last_hour"] == hour_key:
                return  # этот час уже обработан
            _ADS_WATCHDOG_CRON_STATE["last_hour"] = hour_key

        from services.ads_watchdog import run_watchdog
        result = run_watchdog()
        logger.info(
            "_cron_ads_watchdog: час=%s alerts_sent=%d alerts_skipped=%d ok=%s",
            hour_key,
            result.get("alerts_sent", 0),
            result.get("alerts_skipped", 0),
            result.get("ok"),
        )
    except Exception as exc:
        logger.warning("_cron_ads_watchdog: неожиданная ошибка — %s", exc)


@heartbeat("_cron_scorecard", 15)
def _cron_scorecard() -> None:
    """Крон еженедельного табло автопилота — вс 19:xx по локальному времени, раз в неделю.

    Интервал 15 мин, гейт — weekday==6, hour==19, ISO-неделя ещё не отправлена.
    """
    try:
        from services.scorecard import should_send_scorecard_this_week, send_scorecard, mark_scorecard_sent
        now = datetime.now(_TZ_LOCAL)
        if not should_send_scorecard_this_week(now):
            return
        send_scorecard()
        mark_scorecard_sent(now)
        logger.info("_cron_scorecard: табло отправлено")
    except Exception as exc:
        logger.warning("_cron_scorecard: неожиданная ошибка — %s", exc)


# ---------------------------------------------------------------------------
# Ночной proactive capacity scan (_cron_adset_cleaner)
# ---------------------------------------------------------------------------

_ADSET_CLEANER_HOUR = 5  # локальный час: строго после бэкапа decisions.db (04:xx)
def _should_run_adset_cleaner_cron(now: datetime) -> bool:
    """Гейтит только окно 05:xx; daily lease хранится в SQLite cleaner."""
    return now.hour == _ADSET_CLEANER_HOUR


@heartbeat("_cron_adset_cleaner", 30, critical=True)
def _cron_adset_cleaner() -> None:
    """Запускает только durable read-only capacity scan в 05:xx по локальному времени."""
    try:
        now = datetime.now(_TZ_LOCAL)
        if not _should_run_adset_cleaner_cron(now):
            return

        from services.proactive_adset_cleaner import run_proactive_cleaner

        result = run_proactive_cleaner(scheduled_date=now.date())
        completed = not result.get("errors")
        counters = result.get("counters") or {}
        logger.info(
            "_cron_adset_cleaner: run_id=%s ran=%s phase=%s would=%d errors=%d",
            result.get("run_id"),
            result.get("ran"),
            result.get("phase"),
            counters.get("would_delete", 0),
            counters.get("errors", 0),
        )
        if completed:
            report_cron_success("_cron_adset_cleaner")
        else:
            report_cron_failure("_cron_adset_cleaner", "cleaner run incomplete")
    except Exception as exc:
        logger.warning("_cron_adset_cleaner: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_adset_cleaner", exc)


# ---------------------------------------------------------------------------
# Чистка слотов адсета (_cron_slot_cleaner) — раз в 3 дня, 06:xx по локальному времени
# ---------------------------------------------------------------------------

_SLOT_CLEANER_HOUR = 6  # после ночного capacity scan (05:xx), до авто-запуска (10:xx)
_SLOT_CLEANER_STATE = Path(__file__).parent.parent / "data" / "slot_cleaner_cron_state.json"


def _load_slot_cleaner_cron_state() -> dict:
    """Загружает состояние крона _cron_slot_cleaner (last_run_date)."""
    try:
        if _SLOT_CLEANER_STATE.exists():
            return json.loads(_SLOT_CLEANER_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("_cron_slot_cleaner: не удалось прочитать state — %s", exc)
    return {}


def _save_slot_cleaner_cron_state(state: dict) -> None:
    """Сохраняет состояние крона _cron_slot_cleaner."""
    try:
        _SLOT_CLEANER_STATE.parent.mkdir(parents=True, exist_ok=True)
        _SLOT_CLEANER_STATE.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("_cron_slot_cleaner: не удалось сохранить state — %s", exc)


def _should_run_slot_cleaner_cron(now: datetime, every_days: int) -> bool:
    """Окно 06:xx по локальному времени И с прошлой чистки прошло every_days полных суток.

    Нечитаемая дата прошлого прогона трактуется как «не было» — чистка пойдёт.
    Это безопасная сторона: архивация обратима, а пропуск окна оставляет слоты
    забитыми и молча тормозит запуск новых креативов.
    """
    if now.hour != _SLOT_CLEANER_HOUR:
        return False
    last_run = _load_slot_cleaner_cron_state().get("last_run_date")
    if not isinstance(last_run, str) or not last_run:
        return True
    try:
        last_date = date.fromisoformat(last_run)
    except ValueError:
        return True
    return (now.date() - last_date).days >= every_days


_STAGING_CLEAN_AGE_DAYS = 3


@heartbeat("_cron_staging_cleaner", 360, critical=False)
def _cron_staging_cleaner() -> None:
    """Раз в 6 часов убирает staging-каталоги старше 3 дней.

    Staged медиа — копии вложений Drive; живут они только пока запуск в
    полёте (авторизации — минуты). Без чистки склад дорастал до гигабайт,
    и его убирали руками.
    """
    try:
        import shutil
        import time as _time

        from config import REPORT_CHECKER_STAGING_ROOT

        root = Path(REPORT_CHECKER_STAGING_ROOT)
        if not root.is_dir():
            report_cron_success("_cron_staging_cleaner")
            return
        cutoff = _time.time() - _STAGING_CLEAN_AGE_DAYS * 86400
        removed = freed = 0
        for entry in root.iterdir():
            try:
                if not entry.is_dir() or entry.is_symlink():
                    continue
                if entry.stat().st_mtime >= cutoff:
                    continue
                size = sum(
                    f.stat().st_size for f in entry.rglob("*") if f.is_file()
                )
                shutil.rmtree(entry)
                removed += 1
                freed += size
            except OSError as exc:
                logger.warning(
                    "_cron_staging_cleaner: %s не удалён — %s", entry.name, exc
                )
        if removed:
            logger.info(
                "_cron_staging_cleaner: удалено %d каталогов, освобождено %.1f МБ",
                removed,
                freed / 1e6,
            )
        report_cron_success("_cron_staging_cleaner")
    except Exception as exc:
        logger.warning("_cron_staging_cleaner: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_staging_cleaner", exc)


@heartbeat("_cron_slot_cleaner", 30, critical=False)
def _cron_slot_cleaner() -> None:
    """Раз в 3 дня архивирует давно паузнутые объявления боевых адсетов.

    Мастер-ключ autopilot.slot_cleaner.enabled (дефолт false) решает, будет
    прогон боевым или останется планом. Дату помечаем ДО прогона — повторные
    тики в 06:xx не должны запускать чистку второй раз.
    """
    try:
        now = datetime.now(_TZ_LOCAL)

        from services.slot_cleaner import (
            cleanup_every_days,
            format_report,
            is_slot_cleaner_enabled,
            run_slot_cleanup,
        )

        # Через cleanup_every_days, а не int(): мусор в настройках не должен ни
        # ронять крон, ни превращать период в «каждый тик».
        every_days = cleanup_every_days()
        if not _should_run_slot_cleaner_cron(now, every_days):
            return

        _save_slot_cleaner_cron_state({"last_run_date": now.date().isoformat()})

        result = run_slot_cleanup(dry_run=not is_slot_cleaner_enabled(), now=now)

        logger.info(
            "_cron_slot_cleaner: dry_run=%s кандидатов=%d архивировано=%d",
            result.get("dry_run"),
            result.get("candidates_total", 0),
            result.get("archived_total", 0),
        )

        # Молчим, когда чистить нечего: пустой отчёт раз в 3 дня — это шум.
        if result.get("candidates_total") or result.get("errors"):
            from services.notifications import send_telegram

            send_telegram(format_report(result), channel="ads")
    except Exception as exc:
        logger.warning("_cron_slot_cleaner: неожиданная ошибка — %s", exc)


# ---------------------------------------------------------------------------
# Страж просроченных офферов (_cron_expired_offer_guard) — ночь 03:xx по локальному времени
# ---------------------------------------------------------------------------

_EXPIRED_OFFER_GUARD_HOUR = 3
_EXPIRED_OFFER_GUARD_STATE = Path(__file__).parent.parent / "data" / "expired_offer_guard_cron_state.json"


def _load_expired_offer_guard_cron_state() -> dict:
    """Загружает состояние крона _cron_expired_offer_guard (last_run_date)."""
    try:
        if _EXPIRED_OFFER_GUARD_STATE.exists():
            return json.loads(_EXPIRED_OFFER_GUARD_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("_cron_expired_offer_guard: не удалось прочитать state — %s", exc)
    return {}


def _save_expired_offer_guard_cron_state(state: dict) -> None:
    """Сохраняет состояние крона _cron_expired_offer_guard."""
    try:
        _EXPIRED_OFFER_GUARD_STATE.parent.mkdir(parents=True, exist_ok=True)
        _EXPIRED_OFFER_GUARD_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("_cron_expired_offer_guard: не удалось сохранить state — %s", exc)


def _should_run_expired_offer_guard_cron(now: datetime) -> bool:
    """True, если локальный час == _EXPIRED_OFFER_GUARD_HOUR и сегодня ещё не запускались."""
    if now.hour != _EXPIRED_OFFER_GUARD_HOUR:
        return False
    state = _load_expired_offer_guard_cron_state()
    return state.get("last_run_date") != now.date().isoformat()


@heartbeat("_cron_expired_offer_guard", 30, critical=False)
def _cron_expired_offer_guard() -> None:
    """Ночной страж просроченных офферов — 03:xx по локальному времени, раз в сутки.

    Интервал 30 мин, гейт: локальный час == 3 И не запускались сегодня. Обходит
    эффективно-активные объявления, ищет истёкшие даты оффера в имени/тексте
    креатива, у жёгших вчера бюджет — Telegram-алерт с кнопками «⏸ Остановить»
    (сам крон в dry-run, паузит человек). См. services/expired_offer_guard.py.
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        if not _should_run_expired_offer_guard_cron(now):
            return
        # Помечаем дату ДО запуска — защита от дублей при повторных тиках в 03:xx
        _save_expired_offer_guard_cron_state({"last_run_date": now.date().isoformat()})

        from services.expired_offer_guard import run_expired_offer_guard
        result = run_expired_offer_guard(now)
        logger.info(
            "_cron_expired_offer_guard: enabled=%s checked=%d flagged=%d wasters=%d alerted=%d unverifiable=%d",
            result.get("enabled"), result.get("checked", 0), result.get("flagged", 0),
            result.get("wasters", 0), result.get("alerted", 0), result.get("unverifiable", 0),
        )
        report_cron_success("_cron_expired_offer_guard")
    except Exception as exc:
        logger.warning("_cron_expired_offer_guard: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_expired_offer_guard", exc)


# ---------------------------------------------------------------------------
# Guardian sweep — наблюдатель Стража (_cron_guardian_sweep)
# ---------------------------------------------------------------------------

_GUARDIAN_SWEEP_STATE = Path(__file__).parent.parent / "data" / "guardian_sweep_cron_state.json"


def _load_guardian_sweep_state() -> dict:
    """Загружает состояние крона _cron_guardian_sweep (слоты дата-час)."""
    try:
        if _GUARDIAN_SWEEP_STATE.exists():
            return json.loads(_GUARDIAN_SWEEP_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("_cron_guardian_sweep: не удалось прочитать state — %s", exc)
    return {}


def _save_guardian_sweep_state(state: dict) -> None:
    """Сохраняет состояние крона _cron_guardian_sweep."""
    try:
        _GUARDIAN_SWEEP_STATE.parent.mkdir(parents=True, exist_ok=True)
        _GUARDIAN_SWEEP_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("_cron_guardian_sweep: не удалось сохранить state — %s", exc)


def _should_run_guardian_sweep(now: datetime) -> bool:
    """Гейт: локальный час в _AUTOPILOT_LIVE_HOURS (те же 8 слотов, 08-22) И слот ещё не бежал.

    Страж сверяет кандидатов early_waster/wasted_no_crm синхронно с autopilot_live —
    в дефолтном dry_run-режиме это только сводка в Telegram, боевые паузы (если dry_run
    снят) делает run_autopilot_live отдельным прогоном в том же слоте (§6.4 спеки).
    """
    if now.hour not in _AUTOPILOT_LIVE_HOURS:
        return False
    state = _load_guardian_sweep_state()
    slot_key = f"{now.date().isoformat()}-{now.hour}"
    slots = state.get("slots", {})
    return slot_key not in slots


@heartbeat("_cron_guardian_sweep", 15, critical=True)
def _cron_guardian_sweep() -> None:
    """Крон-наблюдатель Стража — каждые 2ч в 08-22 по локальному времени (те же слоты, что autopilot_live).

    Интервал 15 мин, гейт — час в _AUTOPILOT_LIVE_HOURS И этот слот ещё не запускался.
    Вызывает guardian.run_guardian_sweep, который сам гейтится kill_switch и НИКОГДА
    не бросает исключение. По дефолту (guardian.enabled=False, dry_run=True для обоих
    правил) — только читает локальную БД (0 FB-запросов) и шлёт Telegram-сводку
    «поймал бы X», если есть кандидаты; реальные паузы не делает (см. §6.4, §8.2).
    """
    try:
        now = datetime.now(_TZ_LOCAL)

        if not _should_run_guardian_sweep(now):
            return

        slot_key = f"{now.date().isoformat()}-{now.hour}"

        # Помечаем слот ДО запуска — защита от дублей при повторных тиках внутри часа
        current_state = _load_guardian_sweep_state()
        slots = current_state.get("slots", {})
        slots[slot_key] = "running"
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        slots = {k: v for k, v in slots.items() if k[:10] >= yesterday}
        _save_guardian_sweep_state({**current_state, "slots": slots})

        from services import guardian
        result = guardian.run_guardian_sweep(trigger="cron")

        # Сохраняем итог слота для диагностики
        current_state2 = _load_guardian_sweep_state()
        slots2 = current_state2.get("slots", {})
        slots2[slot_key] = {
            "ran": result.get("ran"),
            "skipped": result.get("skipped"),
            "analyzed": result.get("analyzed", 0),
            "early": len(result.get("early_candidates", [])),
            "wnc": len(result.get("wnc_candidates", [])),
        }
        _save_guardian_sweep_state({**current_state2, "slots": slots2})

        logger.info(
            "_cron_guardian_sweep: slot=%s ran=%s skipped=%s analyzed=%d early=%d wnc=%d",
            slot_key,
            result.get("ran"),
            result.get("skipped"),
            result.get("analyzed", 0),
            len(result.get("early_candidates", [])),
            len(result.get("wnc_candidates", [])),
        )

        # Алерты аномалий (Фаза 5) — ПОСЛЕ sweep, в том же слоте. run_anomaly_alerts
        # never-throw и сам гейтится флагом anomaly_alerts.enabled (см. services/anomaly_alerts.py).
        from services.anomaly_alerts import run_anomaly_alerts
        anomaly_result = run_anomaly_alerts(now=now)
        logger.info(
            "_cron_guardian_sweep: anomaly_alerts sent=%s skipped=%s",
            anomaly_result.get("alerts_sent"), anomaly_result.get("alerts_skipped"),
        )
        report_cron_success("_cron_guardian_sweep")
    except Exception as exc:
        logger.warning("_cron_guardian_sweep: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_guardian_sweep", exc)


# ---------------------------------------------------------------------------
# Ранний стоп сливов (_cron_early_kill) — см. services/early_kill.py
# ---------------------------------------------------------------------------

# Правило проверяется каждый час круглые сутки: cabinet_b живёт по локальному времени и
# тратит и ночью, и днём, а заявки приходят круглосуточно —
# ночь порог не меняет.
_EARLY_KILL_HOURS = frozenset(range(24))
_EARLY_KILL_STATE_KEY = "early_kill_cron"


def _should_run_early_kill(now: datetime) -> bool:
    """Гейт: раз в час, дедуп по слоту дата-час в backfill_state (общий хелпер cron_gate)."""
    from services.creative_backfill import _get_state_by_key
    from web.cron_gate import should_run_hourly_slot

    return should_run_hourly_slot(now, _EARLY_KILL_HOURS, _get_state_by_key(_EARLY_KILL_STATE_KEY))


@heartbeat("_cron_early_kill", 60, critical=False)
def _cron_early_kill() -> None:
    """Крон раннего стопа — каждый час (тик 15 мин, гейт по слоту дата-час).

    Вызывает early_kill.run_early_kill, который сам гейтится mode/kill_switch и
    НИКОГДА не бросает. В shadow (дефолт) — только журнал оценок и Telegram
    «поймал бы»; мутаций рекламы нет. Слот помечается ДО запуска: повторный тик
    внутри часа не дублирует прогон, а ошибка не роняет соседние кроны.
    """
    try:
        from services.creative_backfill import _get_state_by_key, _save_state_by_key

        now = datetime.now(_TZ_LOCAL)
        if not _should_run_early_kill(now):
            return
        slot_key = f"{now.date().isoformat()}-{now.hour}"
        state = _get_state_by_key(_EARLY_KILL_STATE_KEY)
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        slots = {k: v for k, v in (state.get("slots") or {}).items() if k[:10] >= yesterday}
        slots[slot_key] = "running"
        _save_state_by_key(_EARLY_KILL_STATE_KEY, {**state, "slots": slots})

        from services.early_kill import run_early_kill

        result = run_early_kill(trigger="cron")

        state = _get_state_by_key(_EARLY_KILL_STATE_KEY)
        slots = dict(state.get("slots") or {})
        slots[slot_key] = {
            "ran": result.get("ran"),
            "skipped": result.get("skipped"),
            "mode": result.get("mode"),
            "candidates": result.get("candidates", 0),
            "would_pause": len(result.get("would_pause") or []),
        }
        _save_state_by_key(_EARLY_KILL_STATE_KEY, {**state, "slots": slots})
        logger.info(
            "_cron_early_kill: slot=%s ran=%s skipped=%s mode=%s candidates=%d amo=%d would_pause=%d errors=%s",
            slot_key, result.get("ran"), result.get("skipped"), result.get("mode"),
            result.get("candidates", 0), result.get("amo_queries", 0),
            len(result.get("would_pause") or []), result.get("errors"),
        )
        report_cron_success("_cron_early_kill")
    except Exception as exc:
        logger.warning("_cron_early_kill: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_early_kill", exc)


# ---------------------------------------------------------------------------
# Сторож свежести данных (_cron_data_freshness_watchdog)
# ---------------------------------------------------------------------------

# Дедуп алерта о простое рефреша — не чаще 1 раза в 3 часа (§8.5 спеки)
_FRESHNESS_ALERT_COOLDOWN_HOURS = 3
# Порог простоя рефреша расхода, после которого шлём алерт (часов)
_FRESHNESS_MAX_AGE_HOURS = 4.0

# In-memory гейт «новый час» — тот же паттерн, что _cron_ads_watchdog
_DATA_FRESHNESS_CRON_STATE: dict = {"last_hour": None}
_DATA_FRESHNESS_LOCK = threading.Lock()


@heartbeat("_cron_data_freshness_watchdog", 15)
def _cron_data_freshness_watchdog() -> None:
    """Сторож свежести данных Стража — каждые 15 мин, гейт «новый час», 08-22 по локальному времени.

    Если рефреш расхода (refresh_active_ads_spend / _light) не отмечал успешный
    прогон > _FRESHNESS_MAX_AGE_HOURS часов в рабочее время — шлёт Telegram-алерт
    channel="health" с дедупом не чаще раза в _FRESHNESS_ALERT_COOLDOWN_HOURS часов.
    Read-only — никаких действий с FB. Никогда не бросает (см. §8.5 спеки).
    """
    try:
        now = datetime.now(_TZ_LOCAL)

        # Ночью рефреш и не должен бежать — молчим без проверки
        if not (8 <= now.hour <= 22):
            return

        # Гейт: один раз в каждом часовом окне
        hour_key = f"{now.date().isoformat()}-{now.hour}"
        with _DATA_FRESHNESS_LOCK:
            if _DATA_FRESHNESS_CRON_STATE["last_hour"] == hour_key:
                return
            _DATA_FRESHNESS_CRON_STATE["last_hour"] = hour_key

        from services import guardian

        age_hours = guardian.spend_refresh_age_hours(now)
        # Ещё ни одного прогона (например, сразу после деплоя) — нет базовой линии,
        # не алертим первые сутки, чтобы не спамить сразу после старта (fail-quiet)
        if age_hours is None:
            return
        if age_hours <= _FRESHNESS_MAX_AGE_HOURS:
            return

        state = guardian._load_guardian_state()
        last_alert_iso = state.get("freshness_alert_at")
        if last_alert_iso:
            try:
                last_alert = datetime.fromisoformat(last_alert_iso)
                if last_alert.tzinfo is None:
                    last_alert = last_alert.replace(tzinfo=_TZ_LOCAL)
                if (now - last_alert).total_seconds() / 3600.0 < _FRESHNESS_ALERT_COOLDOWN_HOURS:
                    logger.info("_cron_data_freshness_watchdog: алерт продедуплен (age=%.1fч)", age_hours)
                    return
            except Exception as exc:
                logger.warning("_cron_data_freshness_watchdog: не удалось распарсить freshness_alert_at — %s", exc)

        from services.notifications import send_telegram
        send_telegram(
            f"⚠️ Страж: рефреш расхода не запускался {age_hours:.1f}ч (порог {_FRESHNESS_MAX_AGE_HOURS}ч). "
            "Проверь крон _cron_refresh_active_spend — возможен сбой сбора данных.",
            channel="health",
        )
        state["freshness_alert_at"] = now.isoformat()
        guardian._save_guardian_state(state)

        logger.warning("_cron_data_freshness_watchdog: алерт отправлен (age=%.1fч)", age_hours)
    except Exception as exc:
        logger.warning("_cron_data_freshness_watchdog: неожиданная ошибка — %s", exc)


_DB_BACKUP_HOUR = 4  # локальный час для ежедневного бэкапа decisions.db
_DB_BACKUP_KEEP = 7  # сколько последних копий хранить


def _should_run_db_backup(now: datetime, last_backup_date: str | None) -> bool:
    """True, если локальный час == _DB_BACKUP_HOUR и сегодня ещё не бэкапили."""
    if now.hour != _DB_BACKUP_HOUR:
        return False
    return last_backup_date != now.date().isoformat()


def _is_cron_backup_filename(name: str) -> bool:
    """True, если имя файла — крон-бэкап вида decisions-YYYYMMDD.db (ровно 8 цифр даты).
    Отсекает ручные бэкапы вроде decisions-pre-mig010-20260615.db, чтобы ротация
    крона их не трогала."""
    stem = name.removeprefix("decisions-").removesuffix(".db")
    return len(stem) == 8 and stem.isdigit()


@heartbeat("_cron_db_backup", 30, critical=True)
def _cron_db_backup() -> None:
    """Ежедневный бэкап data/decisions.db через sqlite3 Connection.backup() — каждые 30 мин,
    реальный запуск в _DB_BACKUP_HOUR:xx по локальному времени, раз в день. Ротация: _DB_BACKUP_KEEP копий.
    Connection.backup() (не VACUUM INTO) — совместим с любой версией SQLite, включая
    старые (<3.27, где VACUUM INTO ещё не поддерживается).
    Ошибки бэкапа НЕ роняют остальные кроны (весь код в try)."""
    try:
        from services.autopilot import _load_state, _save_state
        now = datetime.now(_TZ_LOCAL)
        state = _load_state()
        if not _should_run_db_backup(now, state.get("last_backup_date")):
            return

        db_path = Path(config.CREATIVE_KB_PATH)  # data/decisions.db
        backup_dir = Path(__file__).parent.parent / "data" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"decisions-{now.strftime('%Y%m%d')}.db"

        if not db_path.exists():
            raise FileNotFoundError(f"decisions.db не найден: {db_path}")

        # Connection.backup() падает, если целевой файл уже существует и в нём
        # уже есть таблицы (конфликт схемы) — удаляем заранее, как и раньше.
        if backup_path.exists():
            backup_path.unlink()

        src_conn = sqlite3.connect(str(db_path))
        dst_conn = sqlite3.connect(str(backup_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        # Ротация: оставляем последние _DB_BACKUP_KEEP крон-бэкапов (сортировка по
        # имени = по дате). Шаблон сужен до decisions-YYYYMMDD.db, чтобы не задеть
        # ручные бэкапы вида decisions-pre-mig010-*.db в той же папке.
        backups = sorted(
            p for p in backup_dir.glob("decisions-*.db") if _is_cron_backup_filename(p.name)
        )
        for old in backups[:-_DB_BACKUP_KEEP]:
            try:
                old.unlink()
            except Exception as _rm:
                logger.warning("_cron_db_backup: не удалось удалить старый бэкап %s: %s", old, _rm)

        state["last_backup_date"] = now.date().isoformat()
        _save_state(state)
        logger.info("_cron_db_backup: создан %s (хранится %d копий)", backup_path.name, min(len(backups), _DB_BACKUP_KEEP))
    except Exception as exc:
        logger.error("_cron_db_backup: ошибка бэкапа — %s", exc)
        try:
            import html
            from services.notifications import send_telegram
            send_telegram(f"🚨 Бэкап decisions.db не удался: {html.escape(str(exc))}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Аналитик-Гипотезник (Фаза 4) — вердикт гипотез + недельный отчёт обучения
# (см. docs/specs/ARCH-phase4-hypothesist.md §10 T9).
# ---------------------------------------------------------------------------

# Локальный час для ежедневного вердикта гипотез — после ночной сверки AMO-исходов
# в 06:xx (_cron_match_outcomes слот 6, см. _MATCH_OUTCOMES_HOURS выше).
_HYPOTHESIS_VERDICT_HOUR = 6

# День недели (0=пн..6=вс) и локальный час для недельного отчёта обучения —
# воскресенье вечером, после того как накопились вердикты за неделю.
_WEEKLY_LEARNING_REPORT_WEEKDAY = 6  # воскресенье
_WEEKLY_LEARNING_REPORT_HOUR = 20


def _hypothesist_enabled() -> bool:
    """Читает мастер-флаг hypothesist.enabled из autopilot-конфига (дефолт True)."""
    from services.autopilot import get_autopilot_config
    return bool(get_autopilot_config().get("hypothesist", {}).get("enabled", True))


@heartbeat("_cron_hypothesis_verdict", 30)
def _cron_hypothesis_verdict() -> None:
    """Ежедневный крон вердикта гипотез — раз в сутки в _HYPOTHESIS_VERDICT_HOUR:xx
    по локальному времени (после ночной сверки AMO-исходов в 06:xx), дедуп по дате через
    creative_backfill state-ключ 'hypothesis_verdict_cron'.

    Под флагом autopilot.hypothesist.enabled (дефолт True). Зовёт never-throw
    hypothesis_verdict.run_verdict_pass — сам крон дополнительно обёрнут в try,
    молчаливая смерть невозможна (logger.warning при любой неожиданной ошибке).
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        if now.hour != _HYPOTHESIS_VERDICT_HOUR:
            return

        if not _hypothesist_enabled():
            return

        from services.creative_backfill import _get_state_by_key, _save_state_by_key
        state = _get_state_by_key("hypothesis_verdict_cron")
        today_key = now.date().isoformat()
        if state.get("last_run_date") == today_key:
            return
        _save_state_by_key("hypothesis_verdict_cron", {"last_run_date": today_key})

        from services.hypothesis_verdict import run_verdict_pass
        result = run_verdict_pass(now=now)
        logger.info(
            "_cron_hypothesis_verdict: evaluated=%s confirmed=%s refuted=%s inconclusive=%s "
            "still_open=%s learnings_written=%s",
            result.get("evaluated"), result.get("confirmed"), result.get("refuted"),
            result.get("inconclusive"), result.get("still_open"), result.get("learnings_written"),
        )
    except Exception as exc:
        logger.warning("_cron_hypothesis_verdict: неожиданная ошибка — %s", exc)


@heartbeat("_cron_weekly_learning_report", 30)
def _cron_weekly_learning_report() -> None:
    """Еженедельный отчёт «чему я научился» — воскресенье
    _WEEKLY_LEARNING_REPORT_HOUR:xx по локальному времени, дедуп по ISO-неделе через
    creative_backfill state-ключ 'weekly_learning_report_cron'.

    Под флагом autopilot.hypothesist.enabled (дефолт True). Зовёт never-throw
    weekly_learning_report.send_weekly_learning_report — крон дополнительно
    обёрнут в try, молчаливая смерть невозможна.
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        if now.weekday() != _WEEKLY_LEARNING_REPORT_WEEKDAY or now.hour != _WEEKLY_LEARNING_REPORT_HOUR:
            return

        if not _hypothesist_enabled():
            return

        week_key = now.strftime("%G-W%V")
        from services.creative_backfill import _get_state_by_key, _save_state_by_key
        state = _get_state_by_key("weekly_learning_report_cron")
        if state.get("last_iso_week") == week_key:
            return
        _save_state_by_key("weekly_learning_report_cron", {"last_iso_week": week_key})

        from services.weekly_learning_report import send_weekly_learning_report
        sent = send_weekly_learning_report(now=now)
        logger.info("_cron_weekly_learning_report: неделя %s — sent=%s", week_key, sent)
    except Exception as exc:
        logger.warning("_cron_weekly_learning_report: неожиданная ошибка — %s", exc)


# ---------------------------------------------------------------------------
# Шаг B (ARCH-cdp-payments) — реальные платежи ERP из CDP как ground truth оплат.
# (см. docs/specs/ARCH-cdp-payments.md §4 "web/app.py").
# ---------------------------------------------------------------------------

# Путь к state-файлу дедупликации крона синка ERP-платежей.
_CDP_PAYMENTS_SYNC_STATE = Path(__file__).parent.parent / "data" / "cdp_payments_sync_cron_state.json"

# Локальный час для ежедневного синка ERP-платежей — ПОСЛЕ ночной сверки AMO-исходов
# (_cron_match_outcomes слот 6:xx) и вердикта гипотез (6:xx), НО до ночного
# пересчёта движка CDP к 09:30 по локальному времени (см. ARCH-cdp-budget-context §"_ENGINE_STALE_HOURS":
# "ночной пересчёт к 09:30") — платежи ERP синкаются в CDP вечером ЗА ПРОШЕДШИЙ день,
# поэтому к 7 утра свежие данные уже точно там; отдельный крон движка (Шаг A) эту
# колонку не трогает, порядок 6→7→09:30 логичен и не создаёт гонки за данные.
_CDP_PAYMENTS_SYNC_HOUR = 7


def _load_cdp_payments_sync_state() -> dict:
    """Загружает состояние крона синка ERP-платежей (last_run_date)."""
    try:
        if _CDP_PAYMENTS_SYNC_STATE.exists():
            return json.loads(_CDP_PAYMENTS_SYNC_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("_cron_cdp_payments_sync: не удалось прочитать state — %s", exc)
    return {}


def _save_cdp_payments_sync_state(state: dict) -> None:
    """Сохраняет состояние крона синка ERP-платежей."""
    try:
        _CDP_PAYMENTS_SYNC_STATE.parent.mkdir(parents=True, exist_ok=True)
        _CDP_PAYMENTS_SYNC_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("_cron_cdp_payments_sync: не удалось сохранить state — %s", exc)


def _should_run_cdp_payments_sync(now: datetime, last_run_date: str | None) -> bool:
    """Проверяет, нужно ли запускать крон синка ERP-платежей в этом тике.

    Условия (оба должны выполняться):
    - Локальный час == _CDP_PAYMENTS_SYNC_HOUR (раз в сутки)
    - Сегодня ещё не запускался (last_run_date != today)
    """
    if now.hour != _CDP_PAYMENTS_SYNC_HOUR:
        return False
    return last_run_date != now.date().isoformat()


@heartbeat("_cron_cdp_payments_sync", 30, critical=False)
def _cron_cdp_payments_sync() -> None:
    """Крон регулярного обновления ERP-колонок creative_kb (Шаг B, ARCH-cdp-payments).

    Интервал 15 мин, реальный запуск раз в сутки в _CDP_PAYMENTS_SYNC_HOUR:xx по локальному времени
    (после ночной сверки AMO-исходов 6:xx, до пересчёта движка CDP к 09:30).
    Зовёт cdp_payments.refresh_creative_kb_payments_erp(months_back=2) — тот же
    горизонт, что у attach_amo_outcomes (months_back=2) в _cron_match_outcomes,
    чтобы ERP- и AMO-агрегаты покрывали одно окно.

    Fail-safe: refresh_creative_kb_payments_erp уже fail-closed внутри (CDP/AMO лёг →
    AMO-поля в creative_kb целы, 0 обновлений, error в результате) — но крон
    дополнительно обёрнут в try/except: сбой синка НЕ должен ронять остальные кроны
    (heartbeat некритичный — critical=False, т.к. отсутствие ERP-данных не блокирует
    решения Стража/пилота, они fallback на AMO §6.5-6.6 спеки).
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        state = _load_cdp_payments_sync_state()

        if not _should_run_cdp_payments_sync(now, state.get("last_run_date")):
            return

        # Помечаем ДО запуска — повторный тик в том же часу не дублирует синк.
        state["last_run_date"] = now.date().isoformat()
        _save_cdp_payments_sync_state(state)

        from services.cdp_payments import refresh_creative_kb_payments_erp

        result = refresh_creative_kb_payments_erp(months_back=2)

        if result.get("error"):
            logger.warning(
                "_cron_cdp_payments_sync: завершился с ошибкой — %s", result["error"],
            )
        else:
            logger.info(
                "_cron_cdp_payments_sync: window=%s..%s ads_updated=%d payments_total=%d revenue_total_lcy=%.2f",
                result.get("window_from"), result.get("window_to"),
                result.get("ads_updated", 0), result.get("payments_total", 0),
                result.get("revenue_total_lcy", 0.0),
            )
    except Exception as exc:
        logger.warning("_cron_cdp_payments_sync: неожиданная ошибка — %s", exc)


# ---------------------------------------------------------------------------
# Страж трат адсетов (services/adset_spend_guard.py) — утренний наблюдатель:
# ловит вчерашний $0-расход и перерасход эффективно-активных leadgen-адсетов.
# 09:xx по локальному времени (после сбора метрик), дедуп по дню. Read-only, ничего не паузит.
# ---------------------------------------------------------------------------

_ADSET_SPEND_GUARD_STATE = Path(__file__).parent.parent / "data" / "adset_spend_guard_cron_state.json"

# Локальный час для утреннего прогона — после ночного сбора метрик (snapshot 06-07,
# backfill 03-05), до первого слота Стража трат-по-факту. 9 утра — метрики уже свежие.
_ADSET_SPEND_GUARD_HOUR = 9


def _load_adset_spend_guard_state() -> dict:
    """Загружает состояние крона стража трат адсетов (last_run_date)."""
    try:
        if _ADSET_SPEND_GUARD_STATE.exists():
            return json.loads(_ADSET_SPEND_GUARD_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("_cron_adset_spend_guard: не удалось прочитать state — %s", exc)
    return {}


def _save_adset_spend_guard_state(state: dict) -> None:
    """Сохраняет состояние крона стража трат адсетов."""
    try:
        _ADSET_SPEND_GUARD_STATE.parent.mkdir(parents=True, exist_ok=True)
        _ADSET_SPEND_GUARD_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("_cron_adset_spend_guard: не удалось сохранить state — %s", exc)


@heartbeat("_cron_adset_spend_guard", 30, critical=False)
def _cron_adset_spend_guard() -> None:
    """Крон стража трат адсетов — каждые 30 мин, реальный прогон в 09:xx по локальному времени (раз в день).

    Гейт: час == _ADSET_SPEND_GUARD_HOUR И дата != last_run_date. Отметку ставим
    ТОЛЬКО после ok-прогона (ok=False при FB-ошибке НЕ помечаем — повтор на след.
    тике в том же часу). FB-ошибки не роняют крон: run_adset_spend_guard never-throw.
    """
    try:
        from services.adset_spend_guard import run_adset_spend_guard

        now = datetime.now(_TZ_LOCAL)
        state = _load_adset_spend_guard_state()
        if now.hour != _ADSET_SPEND_GUARD_HOUR or state.get("last_run_date") == now.date().isoformat():
            return

        result = run_adset_spend_guard(now)
        if result.get("ok"):
            state["last_run_date"] = now.date().isoformat()
            _save_adset_spend_guard_state(state)
        logger.info(
            "_cron_adset_spend_guard: status=%s checked=%d not_spending=%d overspending=%d alerts_sent=%d",
            result.get("status"), result.get("checked", 0), result.get("not_spending", 0),
            result.get("overspending", 0), result.get("alerts_sent", 0),
        )
    except Exception as exc:
        logger.warning("_cron_adset_spend_guard: неожиданная ошибка — %s", exc)


# ---------------------------------------------------------------------------
# Ежедневный онлайн-отчёт из CDP (services/online_report.py) — одно Telegram по
# псевдогороду «Онлайн». 10:3x по локальному времени (после ночного обновления CDP к 09:30).
# Дедуп по дню и антиспам CDP-недоступности живут в самом сервисе — крон только
# гейтит окно 10:30+ (чтобы не дёргать CDP каждый тик весь день).
# ---------------------------------------------------------------------------

_ONLINE_REPORT_HOUR = 10
_ONLINE_REPORT_MIN_MINUTE = 30


@heartbeat("_cron_online_report", 30, critical=False)
def _cron_online_report() -> None:
    """Крон онлайн-отчёта — каждые 15 мин, реальный прогон в 10:3x по локальному времени (раз в день).

    Гейт окна (час==10 И минута>=30) только ограничивает обращения к CDP окном
    10:30–10:59; дедуп по дню и «CDP недоступен — один раз» owns сам сервис
    (data/online_report_state.json). run_online_report never-throw.
    """
    try:
        from services.online_report import run_online_report

        now = datetime.now(_TZ_LOCAL)
        if now.hour != _ONLINE_REPORT_HOUR or now.minute < _ONLINE_REPORT_MIN_MINUTE:
            return

        result = run_online_report(now)
        logger.info(
            "_cron_online_report: status=%s sent=%s target=%s",
            result.get("status"), result.get("sent"), result.get("target_date"),
        )
    except Exception as exc:
        logger.warning("_cron_online_report: неожиданная ошибка — %s", exc)


# ---------------------------------------------------------------------------
# Контроль запуска — проверка, что сегодняшние авто-запуски реально крутятся
# (см. services/launch_verify.py). Три слепые зоны: KB узнаёт о свежих
# объявлениях только ночью, PENDING_REVIEW не проверяется, «ACTIVE но 0
# показов» не проверяется.
# ---------------------------------------------------------------------------

# Путь к state-файлу дедупликации крона Контроля запуска.
_LAUNCH_VERIFY_CRON_STATE = Path(__file__).parent.parent / "data" / "launch_verify_cron_state.json"

# Локальный час для проверки — запуски идут в 10:00, модерации даём 4 часа
# на реакцию FB (13:xx занят слотом масштабирования бюджетов _cron_budget_scaler).
_LAUNCH_VERIFY_HOUR = 14


def _load_launch_verify_cron_state() -> dict:
    """Загружает состояние крона Контроля запуска (last_run_date)."""
    try:
        if _LAUNCH_VERIFY_CRON_STATE.exists():
            return json.loads(_LAUNCH_VERIFY_CRON_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("_cron_launch_verify: не удалось прочитать state — %s", exc)
    return {}


def _save_launch_verify_cron_state(state: dict) -> None:
    """Сохраняет состояние крона Контроля запуска."""
    try:
        _LAUNCH_VERIFY_CRON_STATE.parent.mkdir(parents=True, exist_ok=True)
        _LAUNCH_VERIFY_CRON_STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("_cron_launch_verify: не удалось сохранить state — %s", exc)


def _should_run_launch_verify(now: datetime, last_run_date: str | None) -> bool:
    """Проверяет, нужно ли запускать крон Контроля запуска в этом тике.

    Условия (оба должны выполняться):
    - Локальный час == _LAUNCH_VERIFY_HOUR (раз в сутки)
    - Сегодня ещё не запускался (last_run_date != today)
    """
    if now.hour != _LAUNCH_VERIFY_HOUR:
        return False
    return last_run_date != now.date().isoformat()


@heartbeat("_cron_launch_verify", 30, critical=False)
def _cron_launch_verify() -> None:
    """Крон Контроля запуска — каждые 30 мин, реальная проверка в 14:xx по локальному времени
    (раз в день, запуски были в 10:00, 4 часа на реакцию модерации FB).

    Если сегодня авто-запусков не было — verify_todays_launches сама тихо
    скипает (пустой launched_ever за сегодня), крон это просто логирует.
    Read-only + Telegram только при проблемах — некритичный heartbeat
    (critical=False), сбой не должен ронять остальные кроны.
    """
    try:
        now = datetime.now(_TZ_LOCAL)
        from services.launch_verify import verify_replacement_workflows

        try:
            replacement_result = verify_replacement_workflows()
            logger.info(
                "_cron_launch_verify: replacement ran=%s ready=%d waiting=%d blocked=%d reason=%s",
                replacement_result.get("ran"),
                replacement_result.get("ready", 0),
                replacement_result.get("waiting", 0),
                replacement_result.get("blocked", 0),
                replacement_result.get("skipped_reason"),
            )
        except Exception as exc:
            # Replacement verification fail-closed: старую рекламу не паузим,
            # а дневной read-only контроль запусков всё равно выполняем.
            logger.warning("_cron_launch_verify: replacement verify error — %s", exc)

        # ACTIVE-гейт успеха запуска — на КАЖДОМ тике (не раз в день): пока живой
        # effective_status созданных объявлений не ACTIVE в целевом адсете,
        # «запущено» не рапортуется. Только чтение FB + отчёт.
        try:
            from services.launch_verify import verify_and_report_launch_watchdogs

            watchdog_result = verify_and_report_launch_watchdogs(
                worker_id=f"launch-verify-{os.getpid()}",
            )
            logger.info(
                "_cron_launch_verify: ACTIVE-гейт checked=%d verified=%d pending=%d "
                "failed=%d reconcile=%d errors=%s",
                watchdog_result.get("checked", 0),
                watchdog_result.get("verified", 0),
                watchdog_result.get("pending", 0),
                watchdog_result.get("failed", 0),
                watchdog_result.get("reconcile_required", 0),
                watchdog_result.get("errors"),
            )
        except Exception as exc:
            # Fail-closed: недоступный гейт не превращается в «запущено».
            logger.warning("_cron_launch_verify: ACTIVE-гейт не выполнен — %s", exc)

        state = _load_launch_verify_cron_state()

        if not _should_run_launch_verify(now, state.get("last_run_date")):
            return

        # Помечаем ДО запуска — повторный тик в том же часу не дублирует проверку.
        state["last_run_date"] = now.date().isoformat()
        _save_launch_verify_cron_state(state)

        from services.launch_verify import verify_todays_launches

        result = verify_todays_launches()

        logger.info(
            "_cron_launch_verify: launched=%d running=%d problems=%d",
            result.get("launched", 0), result.get("running", 0),
            len(result.get("problems", [])),
        )
    except Exception as exc:
        logger.warning("_cron_launch_verify: неожиданная ошибка — %s", exc)


@heartbeat("_cron_action_verifier", 15, critical=True)
def _cron_action_verifier() -> None:
    """Независимый верификатор исполненных действий — каждые 15 мин.

    Сверяет живым FB (только GET), что заявленное действие реально случилось:
    PAUSE → PAUSED, UNPAUSE → ACTIVE, SCALE → целевой daily_budget. Для LAUNCH
    второго механизма нет — читается вердикт launch_verify и включается в общий
    след решения.

    Окно — исполнения последних 24 часов; каждое действие проверяется до
    терминального вердикта. Недоступный Facebook даёт честный UNVERIFIABLE и
    повтор на следующем тике, а не «всё ок». Расхождение возвращает задачу через
    существующий execution boundary либо поднимает новое предложение владельцу
    вместе с критическим алертом. Сам верификатор в FB ничего не мутирует.
    """
    try:
        from config import OwnerApprovalConfigError
        from services.action_verifier import verify_executed_actions

        try:
            result = verify_executed_actions(
                worker_id=f"action-verifier-{os.getpid()}",
            )
        except OwnerApprovalConfigError as exc:
            # Контур одобрения ещё не настроен в окружении — это не сбой крона.
            logger.info("_cron_action_verifier: контур не настроен — %s", exc)
            report_cron_success("_cron_action_verifier")
            return
        logger.info(
            "_cron_action_verifier: checked=%d verified=%d mismatch=%d "
            "unverifiable=%d retried=%d escalated=%d errors=%s",
            result.checked,
            result.verified,
            result.mismatch,
            result.unverifiable,
            result.retried,
            result.escalated,
            result.errors,
        )
        # Вторая фаза: сверка неизвестных исходов (RECONCILE_REQUIRED) фактами
        # провайдера — раньше такие задания висели навсегда.
        try:
            from services.action_verifier import reconcile_unknown_outcomes

            reconcile = reconcile_unknown_outcomes(
                worker_id=f"action-verifier-{os.getpid()}",
            )
            if reconcile.checked:
                logger.info(
                    "_cron_action_verifier: reconcile checked=%d confirmed=%d "
                    "no_effect=%d unverifiable=%d errors=%s",
                    reconcile.checked,
                    reconcile.confirmed,
                    reconcile.no_effect,
                    reconcile.unverifiable,
                    reconcile.errors,
                )
        except Exception as exc:  # noqa: BLE001 — сверка не роняет верификатор
            logger.warning("_cron_action_verifier: reconcile упал — %s", exc)

        if result.errors:
            report_cron_failure(
                "_cron_action_verifier",
                f"часть проверок не выполнена: {result.errors}",
            )
        else:
            report_cron_success("_cron_action_verifier")
    except Exception as exc:
        # Fail-closed по смыслу: непроверенное действие не становится «сработало».
        logger.warning("_cron_action_verifier: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_action_verifier", exc)


@heartbeat("_cron_owner_delivery", 15, critical=True)
def _cron_owner_delivery() -> None:
    """Доставка владельцу — каждые 15 мин.

    Внутри: дневной дайджест предложений (копится молча, уходит одним пакетом в
    approval.digest_hour по локальному времени) и сообщения следа «сработало / проверено».

    Приём нажатий здесь НЕ живёт: ответ на callback Telegram принимает считанные
    минуты, поэтому ingress вынесен в _cron_owner_inbox с интервалом 1 минута.
    """
    try:
        from config import OwnerApprovalConfigError
        from services.owner_delivery_outbox import deliver_owner_outbox

        try:
            delivery = deliver_owner_outbox(
                worker_id=f"owner-delivery-{os.getpid()}",
            )
        except OwnerApprovalConfigError as exc:
            # Контур одобрения ещё не настроен в окружении — это не сбой крона.
            logger.info("_cron_owner_delivery: контур не настроен — %s", exc)
            report_cron_success("_cron_owner_delivery")
            return
        logger.info(
            "_cron_owner_delivery: sent=%d trail=%d digest=%s",
            delivery.sent_count,
            delivery.trail_sent_count,
            delivery.digest_id,
        )
        # Отметка «запущено» в Trello едет тем же тиком. Отдельного крона не
        # заводим: это такая же durable очередь, только доставка в Trello, а не
        # в Telegram. Сбой отметки не должен ронять доставку владельцу —
        # поэтому свой try и мягкий лог, очередь вернёт строку следующим тиком.
        try:
            from services.trello_completion import complete_trello_cards_from_config

            trello_run = complete_trello_cards_from_config()
            if trello_run.completed or trello_run.failed or trello_run.skipped:
                logger.info(
                    "_cron_owner_delivery: trello completed=%d skipped=%d failed=%d",
                    len(trello_run.completed),
                    len(trello_run.skipped),
                    len(trello_run.failed),
                )
        except Exception as exc:
            logger.warning(
                "_cron_owner_delivery: отметка карточек не выполнена — %s", exc
            )
        report_cron_success("_cron_owner_delivery")
    except Exception as exc:
        logger.warning("_cron_owner_delivery: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_owner_delivery", exc)


# ---------------------------------------------------------------------------
# Сверка галочек Trello с живой рекламой (services/trello_check_reconciler.py).
# Разовые скрипты долива и клонирования создают объявления мимо контура и не
# трогают Trello — карточка остаётся «незапущенной» при живой рекламе.
# Раз в час: открытые карточки без
# галочки в «Готово»/«Вторая линейка» + ACTIVE-объявления всех кабинетов карты
# роутинга → точное сопоставление по штатному имени → dueComplete. Только
# ставит, никогда не снимает.
# ---------------------------------------------------------------------------


@heartbeat("_cron_trello_check_reconcile", 60)
def _cron_trello_check_reconcile() -> None:
    """Галочки «запущено» по факту живой рекламы — каждый час.

    Отказ Trello на отдельной карточке или непрочитанный кабинет — это провал
    тика для стража подряд-провалов: сверка молча «зелёной» быть не должна,
    иначе карточки снова копятся без галочек.
    """
    try:
        from services.trello_check_reconciler import reconcile_trello_checks_from_config

        run = reconcile_trello_checks_from_config()
        logger.info(
            "_cron_trello_check_reconcile: marked=%d failed=%d ambiguous=%d "
            "needs_review=%d outside=%d unmatched_ads=%d unchecked_without_ads=%d "
            "accounts=%s failed_accounts=%s",
            len(run.marked),
            len(run.failed),
            len(run.ambiguous),
            len(run.needs_review),
            len(run.outside_lists),
            len(run.unmatched_ads),
            run.unchecked_without_ads,
            ",".join(run.accounts_scanned),
            ",".join(run.accounts_failed) or "-",
        )
        if run.failed or run.accounts_failed:
            report_cron_failure(
                "_cron_trello_check_reconcile",
                RuntimeError(
                    f"cards_failed={len(run.failed)} "
                    f"accounts_failed={','.join(run.accounts_failed) or '-'}"
                ),
            )
            return
        report_cron_success("_cron_trello_check_reconcile")
    except Exception as exc:
        logger.warning("_cron_trello_check_reconcile: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_trello_check_reconcile", exc)


# Интервал крона — 1 минута, но порог протухания сознательно мягче (×3 от 5 мин):
# один медленный тик (пачка сообщений следа, ручной /digest) не должен выглядеть
# как «ingress умер». Реально мёртвый приём нажатий сторож всё равно поймает.
@heartbeat("_cron_owner_inbox", 5, critical=True)
def _cron_owner_inbox() -> None:
    """Приём нажатий и реплаев владельца — каждую МИНУТУ.

    Почему отдельным кроном: answerCallbackQuery живёт у Telegram считанные
    минуты. Пока ingress ехал вместе с доставкой раз в 15 минут, «⏳ Принято»
    физически не могло уйти — в логе оставалось «Ack callback не ушёл: HTTPError»
    («query is too old»), а владелец не видел ни подтверждения, ни причины
    отказа. Теперь нажатие подхватывается в пределах минуты, а ack внутри
    process_inbox уходит синхронно до записи решения.
    """
    try:
        from config import OwnerApprovalConfigError
        from services.approval_telegram import process_owner_decisions

        try:
            inbox = process_owner_decisions(
                worker_id=f"owner-inbox-{os.getpid()}",
            )
        except OwnerApprovalConfigError as exc:
            # Контур одобрения ещё не настроен в окружении — это не сбой крона.
            logger.info("_cron_owner_inbox: контур не настроен — %s", exc)
            report_cron_success("_cron_owner_inbox")
            return
        if inbox.claimed_count:
            logger.info(
                "_cron_owner_inbox: decisions=%d batches=%d feedback=%d failed=%d",
                inbox.decision_count,
                len(inbox.batch_reports),
                len(inbox.feedback),
                inbox.failed_count,
            )
        report_cron_success("_cron_owner_inbox")
    except Exception as exc:
        logger.warning("_cron_owner_inbox: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_owner_inbox", exc)


@heartbeat("_cron_owner_execution", 10, critical=True)
def _cron_owner_execution() -> None:
    """Исполнение одобренных владельцем заданий — каждые 3 минуты.

    Кнопка «Одобрить» пишет решение и ставит job в очередь owner_execution_jobs,
    но сама ничего не исполняет. Этот крон — единственный, кто эту очередь
    разбирает: без него одобрение уходит в пустоту. Интервал 3 минуты выбран под
    ожидание владельца: нажал кнопку — реакция в пределах пары минут (доставка
    ответа идёт своим кроном раз в 15 минут, поэтому исполнять реже смысла нет).

    Тем же проходом: свипер протухших недорешённых предложений (PENDING_OWNER /
    DELIVERY_PENDING / POSTPONED → EXPIRED, TTL берётся из самого предложения),
    свипер протухших одобренных (дедлайн исполнения: valid_until либо
    одобрение + 6ч — что позже) и страж «одобрено, но не исполнено 30+ мин».
    """
    try:
        from config import OwnerApprovalConfigError
        from services.owner_action_executor import (
            OwnerExecutionQueueRun,
            ProposalSweepRun,
            run_owner_execution_queue,
            sweep_expired_approved_jobs,
            sweep_expired_proposals,
        )

        queue = OwnerExecutionQueueRun()
        sweep = ProposalSweepRun()
        approved_sweep = ProposalSweepRun()
        failures: list[str] = []
        # Очередь и свипер идут независимо: сбой одного не отменяет другой.
        try:
            # 25, а не дефолтные 10: лестница раннего стопа за день одобряет десятки пауз, а после кэша
            # сделок по отдельным id задание идёт около минуты. Лишнее безопасно отсекает бюджет прогона.
            queue = run_owner_execution_queue(
                worker_id=f"owner-execution-{os.getpid()}",
                limit=25,
            )
        except OwnerApprovalConfigError as exc:
            # Контур одобрения ещё не настроен в окружении — это не сбой крона.
            logger.info("_cron_owner_execution: контур не настроен — %s", exc)
            report_cron_success("_cron_owner_execution")
            return
        except Exception as exc:
            failures.append(f"queue:{type(exc).__name__}")
            logger.warning("_cron_owner_execution: очередь упала — %s", exc)
        try:
            sweep = sweep_expired_proposals()
        except Exception as exc:
            failures.append(f"sweep:{type(exc).__name__}")
            logger.warning("_cron_owner_execution: свипер упал — %s", exc)
        try:
            approved_sweep = sweep_expired_approved_jobs()
        except Exception as exc:
            failures.append(f"approved_sweep:{type(exc).__name__}")
            logger.warning(
                "_cron_owner_execution: свипер одобренных упал — %s", exc
            )
        problems = (
            tuple(failures) + queue.errors + sweep.errors + approved_sweep.errors
        )
        logger.info(
            "_cron_owner_execution: claimed=%d executed=%d retried=%d deferred=%d "
            "exhausted=%d expired=%d stall_alerts=%d swept=%d approved_swept=%d "
            "revoked=%d errors=%s",
            queue.claimed,
            queue.executed,
            queue.retried,
            queue.deferred,
            queue.exhausted,
            queue.expired,
            queue.stall_alerts,
            sweep.expired,
            approved_sweep.expired,
            sweep.tokens_revoked,
            problems,
        )
        if problems:
            report_cron_failure(
                "_cron_owner_execution",
                f"часть заданий не исполнена: {problems}",
            )
        else:
            report_cron_success("_cron_owner_execution")
    except Exception as exc:
        # Fail-closed: задание остаётся в очереди, аренда протухнет сама.
        logger.warning("_cron_owner_execution: неожиданная ошибка — %s", exc)
        report_cron_failure("_cron_owner_execution", exc)


# ---------------------------------------------------------------------------
# Фаза 5 (осведомлённость) — утренний дайджест + сторож кронов
# (см. docs/specs/ARCH-phase5-awareness.md §7.2).
# ---------------------------------------------------------------------------


@heartbeat("_cron_morning_digest", 10)
def _cron_morning_digest() -> None:
    """Крон утреннего дайджеста — каждые 10 мин, реальная отправка в 08:0x по локальному времени
    (раз в день, ДО первого слота Стража 08:xx).

    Гейт: services.morning_digest.should_send_digest + last_morning_digest_date
    в autopilot_state.json (тот же паттерн, что last_report_date вечернего отчёта).
    Под флагом autopilot.morning_digest.enabled — при false ничего не шлёт.
    """
    try:
        from services.autopilot import _load_state, _save_state, get_autopilot_config
        from services.morning_digest import should_send_digest, send_morning_digest
        from datetime import date

        if not get_autopilot_config().get("morning_digest", {}).get("enabled", True):
            return

        now = datetime.now(_TZ_LOCAL)
        state = _load_state()

        last_date_str = state.get("last_morning_digest_date")
        last_digest_date = None
        if last_date_str:
            try:
                last_digest_date = date.fromisoformat(last_date_str)
            except ValueError:
                pass

        if not should_send_digest(now, last_digest_date):
            return

        sent = send_morning_digest()
        if sent:
            state["last_morning_digest_date"] = now.date().isoformat()
            _save_state(state)
            logger.info("_cron_morning_digest: дайджест отправлен (%s)", now.date())
        else:
            logger.warning("_cron_morning_digest: send_morning_digest вернул False")
    except Exception as exc:
        logger.warning("_cron_morning_digest: неожиданная ошибка — %s", exc)


def _cron_cron_watchdog() -> None:
    """Крон-сторож кронов — каждые 30 мин, сверяет heartbeat-отметки всех кронов
    и шлёт Telegram-алерт (health) по молчащим (дедуп 6ч внутри run_cron_watchdog).

    НЕ оборачивается собственным @heartbeat — не сторожим сторожа (иначе
    рекурсивный шум); его смерть косвенно ловится тем, что дайджест/аномалии
    тоже замолчат (§7.2 спеки). Под флагом autopilot.cron_watchdog.enabled.
    """
    try:
        from services.autopilot import get_autopilot_config
        from services.cron_heartbeat import run_cron_watchdog

        if not get_autopilot_config().get("cron_watchdog", {}).get("enabled", True):
            return

        result = run_cron_watchdog()
        logger.info(
            "_cron_cron_watchdog: stale=%s alerts_sent=%s alerts_skipped=%s",
            result.get("stale"), result.get("alerts_sent"), result.get("alerts_skipped"),
        )
    except Exception as exc:
        logger.warning("_cron_cron_watchdog: неожиданная ошибка — %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ML-модель (sentence-transformers + torch ~600MB RAM) НЕ грузим при старте —
    # она нужна только для скоринга карточек. Грузится лениво при первом запросе
    # /api/cards (см. agent/scorer._load_model). Так idle-RSS дашборда ~100-150MB
    # вместо ~780MB — важно на сервере с ограниченной памятью.

    # Запускаем крон при старте сервера
    scheduler = AsyncIOScheduler()
    # Каждые 30 минут
    scheduler.add_job(_cron_sync_mql, "interval", minutes=30)
    # Прогрев кеша обзора + аналитики автопилота — каждые 120 минут.
    # 120 мин < 180 мин (_CACHE_MAX_AGE_HOURS=3ч / _ANALYTICS_CACHE_MAX_AGE_MIN=180) →
    # fallback автопилота и сторожа всегда найдут свежий кеш (в худшем случае 120 мин).
    # Снизили с 60 мин: прогрев теперь делает 2 FB-запроса (обзор + автопилот-аналитика),
    # поэтому 120 мин бережёт CPU-бюджет FB DEV-кабинета вдвое.
    # Первый прогон через 1 минуту после старта, не сразу (даём приложению подняться).
    scheduler.add_job(
        _cron_prewarm_overview, "interval", minutes=120,
        next_run_time=datetime.now() + timedelta(minutes=1),
    )
    # Сторож FB-лидов — каждые 15 минут проверяем, что лиды падают в AMO
    scheduler.add_job(_cron_fb_lead_watchdog, "interval", minutes=15)
    # Пульс FB-лидов — каждые 20 мин; интервал-гейтинг отправит раз в час (день) или раз в 3ч (ночь)
    scheduler.add_job(_cron_fb_lead_heartbeat, "interval", minutes=20)
    # Автопилот — каждые 15 мин, реальный запуск в окнах 10/15/20 ч по локальному времени
    scheduler.add_job(_cron_autopilot, "interval", minutes=15)
    # Вечерний отчёт — каждые 10 мин, реальная отправка в 21:00–21:59 по локальному времени
    scheduler.add_job(_cron_evening_report, "interval", minutes=10)
    scheduler.add_job(_cron_autonomous_summary, "interval", minutes=10)

    scheduler.add_job(_cron_launch_summary, "interval", minutes=10)
    # Polling Telegram-кнопок — каждые 60 секунд
    scheduler.add_job(_cron_telegram_poll, "interval", seconds=60)
    # Ночной бэкфилл кабинета FB → creative_kb (02–05 по локальному времени, гейт по часу)
    scheduler.add_job(_cron_brain_backfill, "interval", minutes=20)
    # Еженедельный pattern-miner (вс 04:xx по локальному времени, гейт по ISO-неделе)
    scheduler.add_job(_cron_brain_miner, "interval", minutes=30)
    # Дневной снапшот метрик объявлений (06–07 по локальному времени, раз в день)
    scheduler.add_job(_cron_metrics_snapshot, "interval", minutes=30)
    # Ежедневный досинк недавних объявлений (created_time за 7д) → creative_kb с
    # created_at (01:xx по локальному времени, раз в день). ДО окна почасового сборщика — питает
    # его свежими кандидатами, закрывает дыру «новые ad_id не в картотеке».
    scheduler.add_job(_cron_sync_recent_ads, "interval", minutes=30)
    # Почасовой сборщик метрик первых 48ч объявления (02–04 по локальному времени, раз в день,
    # ARCH-hourly-collector — датасет для «Раннего прогноза» v3, не влияет на решения)
    scheduler.add_job(_cron_hourly_collector, "interval", minutes=30)
    # Историческая докрутка day-метрик (03–05 по локальному времени, месяц за час)
    scheduler.add_job(_cron_metrics_backfill, "interval", minutes=20)
    # Недельные когорты объявлений — каждые 30 мин, реальный прогон в 11:xx
    # по локальному времени (раз в день). Час выбран вне окон 03–05 (FB-бэкфилл) и 06–07
    # (ночной снапшот), которые уже выбирают лимит FB.
    scheduler.add_job(_cron_cohort_builder, "interval", minutes=30)
    # Еженедельный движок паттернов (вс 05:xx по локальному времени)
    scheduler.add_job(_cron_pattern_engine, "interval", minutes=30)
    # Ночное сопоставление AMO-исходов → creative_kb (04:xx по локальному времени, раз в день, последние 60 дней)
    scheduler.add_job(_cron_match_outcomes, "interval", minutes=15)
    # Теневой отчёт (крон рассылки «🔮 Тень») отключён по решению владельца:
    # дублировал и противоречил боевому контуру run_autopilot_live. Расчёты (score_and_decide,
    # _fetch_ads_from_local_db) остались — их используют decision_policy/autopilot/budget_scaler/guardian.
    # Боевой автопилот Live — каждые 15 мин, реальный запуск в 12:xx по локальному времени (раз в день)
    scheduler.add_job(_cron_autopilot_live, "interval", minutes=15)
    # Монитор покрытия рекламой — каждые 15 мин, отправка в 09:xx по локальному времени (раз в день)
    scheduler.add_job(_cron_coverage_report, "interval", minutes=15)
    # Live-страж покрытия адсетов — каждые 30 мин, без гейта по часу.
    # Ноль effective ACTIVE в скоупе города×L2/L1 → критический алерт сразу,
    # ниже минимума → предупреждение. Дедуп в durable инцидентах, FB только чтение.
    scheduler.add_job(_cron_coverage_guard, "interval", minutes=30)
    # Авто-запуск рекламных карточек — каждые 15 мин, реальный/dry-run в 10:xx по локальному времени (раз в день)
    scheduler.add_job(_cron_auto_launch, "interval", minutes=15)
    # Авто-генератор ТЗ — каждые 30 мин, реальный запуск: пн 08:xx по локальному времени (раз в неделю)
    scheduler.add_job(_cron_brief_generator, "interval", minutes=30)
    # Масштабирование бюджетов — каждые 15 мин, реальный запуск в 13:xx по локальному времени (раз в день)
    scheduler.add_job(_cron_budget_scaler, "interval", minutes=15)
    # Захват дневного Google-расхода из снимка Google Sheets — 15 мин, 08:xx по локальному времени
    scheduler.add_job(_cron_capture_google_spend, "interval", minutes=15)
    # Обновление spend активных объявлений — каждые 15 мин, реальный запуск в 12:xx по локальному времени (раз в день)
    scheduler.add_job(_cron_refresh_active_spend, "interval", minutes=15)
    # Почасовой сторож рекламных объявлений — каждые 15 мин, гейт «новый час»
    scheduler.add_job(_cron_ads_watchdog, "interval", minutes=15)
    # Еженедельное табло точности автопилота — каждые 15 мин, реальная отправка вс 19:xx по локальному времени
    scheduler.add_job(_cron_scorecard, "interval", minutes=15)
    # Ежедневный бэкап decisions.db — каждые 30 мин, реальный запуск в 04:xx по локальному времени (раз в день)
    scheduler.add_job(_cron_db_backup, "interval", minutes=30)
    # Ночной read-only capacity scan — каждые 30 мин, окно 05:xx по локальному времени.
    # Durable SQLite lease внутри proactive cleaner обеспечивает один run в день.
    scheduler.add_job(_cron_adset_cleaner, "interval", minutes=30)
    # Чистка слотов: тик каждые 30 мин, внутри гейт «06:xx и прошло 3 суток».
    scheduler.add_job(_cron_slot_cleaner, "interval", minutes=30)
    scheduler.add_job(_cron_staging_cleaner, "interval", hours=6)
    scheduler.add_job(_cron_autonomy_invariant, "interval", minutes=15)

    # Страж просроченных офферов — ночь 03:xx по локальному времени (гейт внутри крона)
    scheduler.add_job(_cron_expired_offer_guard, "interval", minutes=30)
    # Guardian sweep — каждые 15 мин, реальный запуск в {8,10,...,22}:xx по локальному времени (8×/день).
    # Наблюдатель Стража: по дефолту только Telegram-сводка, паузы не делает.
    scheduler.add_job(_cron_guardian_sweep, "interval", minutes=15)
    # Ранний стоп сливов: тик 15 мин, реально раз в час (services/early_kill.py)
    scheduler.add_job(_cron_early_kill, "interval", minutes=15)
    # Сторож свежести данных Стража — каждые 15 мин, гейт «новый час», 08-22 по локальному времени.
    scheduler.add_job(_cron_data_freshness_watchdog, "interval", minutes=15)
    # Вердикт гипотез Аналитика-Гипотезника (Фаза 4) — каждые 30 мин, реальный
    # запуск в 06:xx по локальному времени (раз в день, после ночной сверки AMO-исходов).
    scheduler.add_job(_cron_hypothesis_verdict, "interval", minutes=30)
    # Недельный отчёт «чему я научился» (Фаза 4) — каждые 30 мин, реальная
    # отправка вс 20:xx по локальному времени (раз в неделю, дедуп по ISO-неделе).
    scheduler.add_job(_cron_weekly_learning_report, "interval", minutes=30)
    # Синк ERP-платежей (Шаг B, ARCH-cdp-payments) — каждые 15 мин, реальный
    # запуск в 07:xx по локальному времени (раз в день, после AMO-исходов 6:xx, до пересчёта
    # движка CDP к 09:30). Пишет payments_erp/revenue_erp_lcy в creative_kb.
    scheduler.add_job(_cron_cdp_payments_sync, "interval", minutes=15)
    # Контроль запуска — каждые 30 мин, реальная проверка в 14:xx по локальному времени (раз в день,
    # запуски в 10:00 + 4ч на модерацию FB). Проверяет DISAPPROVED/PENDING_REVIEW/0 показов.
    scheduler.add_job(_cron_launch_verify, "interval", minutes=30)
    # Независимый верификатор исполненных действий (волна E) — каждые 15 мин,
    # окно 24ч, только чтение FB. Расхождение → возврат задачи или новое
    # предложение владельцу + критический алерт.
    scheduler.add_job(_cron_action_verifier, "interval", minutes=15)
    # Доставка владельцу (волна E) — каждые 15 мин. Дневной дайджест предложений
    # уходит в approval.digest_hour по локальному времени.
    scheduler.add_job(_cron_owner_delivery, "interval", minutes=15)
    # Сверка галочек Trello с живой рекламой — каждый час. Страховка от
    # запусков мимо контура (разовые скрипты долива/клонирования): карточка с
    # живыми объявлениями получает dueComplete по точному имени объявления.
    scheduler.add_job(_cron_trello_check_reconcile, "interval", hours=1)
    # Приём нажатий владельца — каждую минуту. Отдельно от доставки: ответ на
    # callback Telegram принимает считанные минуты, на 15-минутном тике «⏳
    # Принято» гарантированно опаздывало.
    scheduler.add_job(_cron_owner_inbox, "interval", minutes=1)
    # Исполнение одобренных заданий — каждые 3 мин. Владелец нажал «Одобрить» →
    # очередь owner_execution_jobs разбирается здесь; без этого крона одобрение
    # никуда не уходит. Тем же проходом — свипер протухших предложений и страж
    # «одобрено, но не исполнено 30+ мин».
    # 30, а не 3: прогон с заливкой видео живёт ~20 минут, и трёхминутные тики
    # только копили skipped-логи, а главное — каждый systemctl restart в это
    # окно попадал в stop-sigterm timeout и убивал заливку SIGKILL-ом,
    # оставляя вечный UPLOAD_STARTED (bug9).
    scheduler.add_job(_cron_owner_execution, "interval", minutes=30)
    # Утренний дайджест (Фаза 5) — каждые 10 мин, реальная отправка в 08:0x по локальному времени
    # (раз в день, ДО первого слота Стража). Гейт: autopilot.morning_digest.enabled.
    scheduler.add_job(_cron_morning_digest, "interval", minutes=10)
    # Сторож кронов (Фаза 5) — каждые 30 мин, health-алерт по молчащим кронам,
    # дедуп 6ч. Гейт: autopilot.cron_watchdog.enabled.
    scheduler.add_job(_cron_cron_watchdog, "interval", minutes=30)
    # Страж трат адсетов — каждые 30 мин, реальный прогон в 09:xx по локальному времени (раз в день).
    # $0-расход / перерасход leadgen-адсетов. Read-only, только Telegram-сводка.
    scheduler.add_job(_cron_adset_spend_guard, "interval", minutes=30)
    # Ежедневный онлайн-отчёт из CDP — каждые 15 мин, реальный прогон в 10:3x по локальному времени.
    # Псевдогород «Онлайн»: расход/лиды/квалы/ДРР за вчера и месяц. Read-only.
    scheduler.add_job(_cron_online_report, "interval", minutes=15)
    scheduler.start()
    logger.info(
        "Крон запущен: CAPI 30 мин + прогрев обзора 120 мин + сторож FB-лидов 15 мин"
        " + пульс FB час/3ч + автопилот 10/15/20 + вечерний отчёт 21:00 + telegram-кнопки"
        " + brain backfill 20 мин (ночь) + brain miner 30 мин (вс)"
        " + metrics snapshot 30 мин (06-07 по локальному времени)"
        " + hourly collector 30 мин (02-04 по локальному времени, раз в день, датасет раннего прогноза v3)"
        " + metrics backfill 20 мин (03-05) + pattern engine (вс 05:xx)"
        " + недельные когорты 30 мин (11:xx по локальному времени, раз в день, данные для тренда)"
        " + match-outcomes 15 мин ({0,6,12,18}:xx по локальному времени, 4×/день)"
        " + теневой отчёт 15 мин (20:xx по локальному времени, раз в день)"
        " + autopilot_live 15 мин ({8,10,12,14,16,18,20,22}:xx по локальному времени, 8×/день, Страж)"
        " + монитор покрытия 15 мин (09:xx по локальному времени, раз в день)"
        " + авто-запуск 15 мин (10:xx по локальному времени, раз в день)"
        " + авто-ТЗ 30 мин (пн 08:xx по локальному времени, раз в неделю)"
        " + масштабирование бюджетов 15 мин (13:xx по локальному времени, раз в день)"
        " + захват Google-расхода 15 мин (08:xx по локальному времени, раз в день)"
        " + обновление spend активных 15 мин ({8,10,...,22}:xx по локальному времени, 12:xx full/остальные light, Страж)"
        " + сторож рекламных объявлений 15 мин (раз в час)"
        " + scorecard 15 мин (вс 19:xx по локальному времени, раз в неделю)"
        " + чистильщик адсетов 30 мин (05:xx по локальному времени, раз в день, после бэкапа)"
        " + guardian sweep 15 мин ({8,10,...,22}:xx по локальному времени, 8×/день, наблюдатель)"
        " + сторож свежести данных 15 мин (гейт час, 08-22 по локальному времени)"
        " + вердикт гипотез 30 мин (06:xx по локальному времени, раз в день, Гипотезник)"
        " + недельный отчёт обучения 30 мин (вс 20:xx по локальному времени, раз в неделю)"
        " + синк ERP-платежей 15 мин (07:xx по локальному времени, раз в день, Шаг B CDP)"
        " + утренний дайджест 10 мин (08:0x по локальному времени, раз в день)"
        " + сторож кронов 30 мин (health-алерт по молчащим кронам, дедуп 6ч)"
        " + страж трат адсетов 30 мин (09:xx по локальному времени, раз в день)"
        " + онлайн-отчёт CDP 15 мин (10:3x по локальному времени, раз в день)"
        " + приём нажатий владельца 1 мин (ack не должен опаздывать)"
    )
    yield
    scheduler.shutdown()


app = FastAPI(title="Yuko", lifespan=lifespan)

# --- Политика доступа ---
#
# По умолчанию ЗАКРЫТО: любой путь (в т.ч. GET) требует валидный X-API-Key.
# Публичны только точечно перечисленные пути — «ничего больше» (ревью Wave 2).
#
# ВАЖНО: вебхук AMO исключаем ТОЧНЫМ путём, а не widely-prefix'ом — у него своя
# проверка секрета (web/amo_webhook_routes.py). Никаких «/api/v1/amo/webhooks*».

# Точные публичные пути (без ключа).
_PUBLIC_EXACT_PATHS = frozenset({
    "/",                                  # дашборд (index.html) — сам ключ не содержит
    "/health",                            # мониторинг
    "/favicon.ico",                       # иконка вкладки
    "/api/v1/amo/webhooks/lead-created",  # вебхук AMO — своя constant-time проверка секрета
})
# Публичные префиксы — только статика дашборда.
_PUBLIC_PREFIXES = ("/static/",)


def _is_public_path(path: str) -> bool:
    """True — путь доступен без X-API-Key. Всё остальное закрыто (fail-closed)."""
    if path in _PUBLIC_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


# --- Rate limiting: provider-backed чтения и тяжёлые ручные refresh ---
from web.rate_limit import limiter as _rate_limiter  # noqa: E402

# Семейства GET-ручек, которые ходят во внешние провайдеры (FB/Trello/AMO/CDP/
# Google/LLM) или считают тяжёлую аналитику — лимит 10 req/min на principal.
_PROVIDER_GET_PREFIXES = (
    "/api/analytics", "/api/cards", "/api/adsets", "/api/learning",
    "/api/hypotheses", "/api/historical", "/api/funnel", "/api/comparison",
    "/api/lang-split", "/api/metrics", "/api/overview", "/api/amo",
    "/api/intelligence", "/api/brain", "/api/creative", "/api/vision",
    "/api/winners",
)
# Тяжёлые ручные POST-рефреши/синки — лимит 2 req/min на principal.
_HEAVY_POST_PREFIXES = (
    "/api/overview/refresh", "/api/scheduler/run", "/api/autopilot/",
    "/api/amo/sync", "/api/intelligence/sync", "/api/brain/",
    "/api/winners/sync", "/api/prediction/train", "/api/learning/v2/to-trello",
    "/api/hypotheses/publish", "/api/texts/to-trello", "/api/creative",
    "/api/ad-generator/generate",
)


def _match_family(path: str, prefixes: tuple[str, ...]) -> str | None:
    """Возвращает самый длинный подходящий префикс-семейство или None."""
    best = None
    for p in prefixes:
        if path.startswith(p) and (best is None or len(p) > len(best)):
            best = p
    return best


def _rate_limit_rule(request: Request) -> tuple[str, int] | None:
    """(bucket, limit/min) для запроса или None, если лимит не применяется.

    force_refresh и тяжёлые ручные refresh → 2/min; provider-backed GET → 10/min.
    """
    path = request.url.path
    method = request.method
    force = request.query_params.get("force_refresh", "").lower() in ("1", "true", "yes")
    if force:
        fam = _match_family(path, _PROVIDER_GET_PREFIXES + _HEAVY_POST_PREFIXES) or path
        return (f"heavy:{fam}", 2)
    if method == "POST":
        fam = _match_family(path, _HEAVY_POST_PREFIXES)
        if fam:
            return (f"heavy:{fam}", 2)
    if method == "GET":
        fam = _match_family(path, _PROVIDER_GET_PREFIXES)
        if fam:
            return (f"provider:{fam}", 10)
    return None


# Включаем лимитер в боевом режиме; под pytest — выключен (иначе окно копится между
# тестами → ложные 429). Профильные rate-limit тесты включают его точечно.
# Kill-switch без правки config.py: RATE_LIMIT_ENABLED=0 в .env выключит лимитер.
_UNDER_PYTEST = "pytest" in sys.modules
_rate_limit_env = os.getenv("RATE_LIMIT_ENABLED", "1").strip().lower()
_rate_limiter.enabled = (_rate_limit_env not in ("0", "false", "no", "off")) and not _UNDER_PYTEST


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    """Единый гейт доступа для ВСЕХ методов (включая GET).

    1. Публичные пути (health/root/static/точный вебхук) — пропускаем.
    2. Пустой server API_KEY → fail-closed 503 (сервис стартует: кроны крутятся
       внутри процесса, не через HTTP; падать целиком вреднее).
    3. Неверный/отсутствующий X-API-Key → 403 (constant-time сравнение).
    4. Provider-backed / тяжёлые ручки — rate limit; 429 возвращается ДО роутинга,
       поэтому внешний провайдер при 429 не вызывается.
    Ключ нигде не логируем и не кладём в тело ответа.
    """
    from fastapi.responses import JSONResponse

    path = request.url.path
    if _is_public_path(path):
        return await call_next(request)

    if not config.API_KEY:
        return JSONResponse(
            status_code=503,
            content={"detail": "API_KEY не настроен — запросы к API отключены"},
        )

    provided = request.headers.get("X-API-Key") or ""
    if not hmac.compare_digest(provided, config.API_KEY):
        return JSONResponse(
            status_code=403,
            content={"detail": "Неверный или отсутствующий X-API-Key"},
        )

    # Principal = хеш валидного ключа (не храним сырой ключ в лимитере).
    rule = _rate_limit_rule(request)
    if rule is not None:
        bucket, limit = rule
        principal = hashlib.sha256(provided.encode()).hexdigest()[:16]
        if not _rate_limiter.allow(f"{principal}|{bucket}", limit):
            return JSONResponse(
                status_code=429,
                content={"detail": "Слишком много запросов — попробуйте через минуту"},
            )

    return await call_next(request)


init_db()

# Инициализируем Knowledge Base (идемпотентно — CREATE IF NOT EXISTS).
# KB не критичен для дашборда: если миграция упадёт — логируем и продолжаем,
# чтобы одна необязательная таблица не клала весь сервис.
from services.creative_intelligence import init_kb as init_creative_kb
try:
    init_creative_kb()
except Exception:
    logging.getLogger(__name__).exception("init_creative_kb failed — продолжаем без KB")

app.include_router(overview_router)
app.include_router(intelligence_router)
app.include_router(ad_generator_router)
app.include_router(amo_webhook_router)
app.include_router(brain_router)
app.include_router(sources_router)
app.include_router(seasons_router)

_START_TIME = time.time()
_PROC = psutil.Process(os.getpid())

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "rss_mb": _PROC.memory_info().rss // 1024 // 1024,
        "uptime_s": int(time.time() - _START_TIME),
    }

# Статус текущего запуска (для SSE)
# step — живой под-статус (напр. "Facebook обрабатывает видео… 30с"), step_pct — 0-100 или None
def _launch_status_idle() -> dict:
    """Идл-форма launch_status — единственный источник формы. Отсюда глобал
    стартует при импорте, и к этому же состоянию тесты сбрасывают его между
    прогонами (tests/conftest.py::_isolate_launch_state). Фабрика, а не
    константа: значения-списки должны быть свежими объектами на каждый сброс."""
    return {
        "running": False,
        "outcome": "failed",
        "check_id": None,
        "operation_id": None,
        "idempotency_key": None,
        "action_result": None,
        "reconciliation_required": False,
        "reason_codes": [],
        "reasons": [],
        "current": "",
        "progress": 0,
        "total": 0,
        "step": "",
        "step_pct": None,
        "log": [],
    }


launch_status = _launch_status_idle()
_launch_lock = threading.Lock()  # защита launch_status от race condition
# Отдельный короткоживущий claim на время full preflight. В этот момент
# running ещё False: UI не показывает CREATE до выдачи authorization.
_launch_preflight_running = False

# Кеш аналитики (stale-while-revalidate + диск-кеш) вынесен в services/analytics_cache.py
# (T-CACHE-1). Реэкспортируем имена 1-в-1 — их патчат по имени web.app.* существующие тесты
# (test_autopilot, test_api, test_disk_cache_fallback, test_pause_during_refresh,
# test_watchdog_cache_and_prewarm). noqa: F401 — используются тестами через monkeypatch/patch
# по имени атрибута web.app.*, а не прямым вызовом в этом файле.
from services.analytics_cache import (  # noqa: F401
    _analytics_cache,
    _cache_lock,
    _DISK_CACHE_MAX_AGE_SEC,
    _DISK_CACHE_PATH,
    _load_disk_cache,
    _refresh_cache,
    _refresh_locks,
    _save_disk_cache,
    get_cached_analytics,
    preload_disk_cache_into_memory,
)

# При старте — загружаем диск-кеш в память ДАЖЕ если он старый (защита от 502 после рестарта).
preload_disk_cache_into_memory()

# Отдельный пул для тяжёлой аналитики (analyze_all на минуты).
# Изолирован от дефолтного пула asyncio.to_thread, на котором сидят pause/unpause —
# чтобы мутирующие эндпоинты всегда имели свободный воркер и отвечали быстро.
_analytics_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analytics")


async def _cached_analytics_async(date_from: str = None, date_to: str = None) -> list[dict]:
    """get_cached_analytics в выделенном пуле — не блокирует event loop и
    не занимает дефолтный пул, на котором работают pause/unpause."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_analytics_pool, get_cached_analytics, date_from, date_to)


# --- API: Карточки ---

# Продуктовые метки Trello
_PRODUCT_LABELS = {"PRODA", "PRODB", "Старт"}
# Форматные метки Trello
_FORMAT_LABELS = {"СТАТИКА", "ВИДЕО", "АНИМАЦИЯ"}


def _detect_product(labels: list) -> str | None:
    """
    Определяет продукт по меткам Trello.
    PRODA, PRODB, Старт → один или объединение через "+".
    Остальные метки игнорируются. Если продуктовых меток нет — None.
    """
    found = [lbl for lbl in labels if lbl in _PRODUCT_LABELS]
    if not found:
        return None
    # Стабильный порядок: PRODA, PRODB, Старт
    ordered = [p for p in ("PRODA", "PRODB", "Старт") if p in found]
    return "+".join(ordered)


def _detect_format_labels(labels: list) -> list:
    """Возвращает форматные метки из списка (СТАТИКА/ВИДЕО/АНИМАЦИЯ)."""
    return [lbl for lbl in labels if lbl in _FORMAT_LABELS]


def _load_web_launch_state() -> dict:
    """Читает единый durable ledger автозапуска для checker-а."""
    from services.auto_launch import _load_auto_launch_state

    return _load_auto_launch_state()


def _get_web_launch_checker(mode: str):
    """Обёртка production checker-а для явного offline-mock в API-тестах."""
    from services.launch_checker_runtime import build_production_launch_checker

    return build_production_launch_checker(mode)


def _get_web_checker_mode() -> str:
    """Mode берётся только из server settings, не из HTTP."""
    from services.autopilot import get_autopilot_config

    checker = get_autopilot_config().get("launch_checker") or {}
    return str(checker.get("mode", "observe"))


def _card_launch_request(*, source, campaign_type: str = "leadgen", actor: str = "system"):
    from services.launch_checker import LaunchCheckRequest

    return LaunchCheckRequest(
        source=source,
        campaign_type=campaign_type,
        cities=None,
        as_carousel=False,
        actor=actor,
    )


def _blocked_launch_detail(exc) -> dict:
    """Типизированный и безопасный 409 contract."""
    code = str(getattr(exc, "code", "LAUNCH_CHECK_BLOCKED"))
    raw_reasons = getattr(exc, "reasons", (str(exc),))
    reasons = [_redact_secrets(str(reason)) for reason in raw_reasons]
    return {
        "status": "blocked",
        "check_id": getattr(exc, "check_id", None),
        "reason_codes": [code],
        "reasons": reasons,
    }


def _set_terminal_launch_status(
    outcome: str,
    *,
    check_id: str | None = None,
    reason_codes: list[str] | None = None,
    reasons: list[str] | None = None,
) -> None:
    """Терминальный SSE-снимок всегда running=False."""
    with _launch_lock:
        launch_status.update(
            {
                "running": False,
                "outcome": outcome,
                "check_id": check_id,
                "operation_id": None,
                "action_result": None,
                "reconciliation_required": False,
                "reason_codes": list(reason_codes or []),
                "reasons": list(reasons or []),
                "step": "",
                "step_pct": None,
            }
        )


def _exact_live_launch_card(card_id: str) -> dict:
    """Повторно читает exact карточку и её текущую позицию в «Готово»."""
    list_id = get_done_list_id()
    exact = get_card(card_id)
    if (
        exact.get("idList") != list_id
        or exact.get("dueComplete") is not False
        or exact.get("closed") is not False
    ):
        raise HTTPException(status_code=404, detail="Карточка больше не готова к запуску")

    cards = get_unlaunched_cards(list_id)
    card = next((item for item in cards if item.get("id") == card_id), None)
    if card is None:
        raise HTTPException(status_code=404, detail="Карточка не найдена")
    if (
        exact.get("name") != card.get("name")
        or exact.get("desc") != card.get("desc", "")
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "status": "blocked",
                "check_id": None,
                "reason_codes": ["CARD_SNAPSHOT_DRIFT"],
                "reasons": ["Карточка изменилась во время проверки"],
            },
        )
    return card


def _finish_web_launch_authorization(plan, requested_outcome: str):
    """Возвращает public durable lifecycle snapshot без private auto API."""
    from services.launch_checker_runtime import finalize_launch_authorization

    return finalize_launch_authorization(
        plan.authorization,
        requested_outcome=requested_outcome,
    )


def _apply_web_launch_finalization(
    finalization,
    *,
    check_id: str | None,
    fallback_detail: dict | None = None,
) -> None:
    """SSE outcome определяется только durable provider snapshot-ом."""
    from services.launch_checker_runtime import LaunchLifecycleOutcome

    durable_outcome = finalization.outcome
    reason_codes: list[str] = []
    reasons: list[str] = []
    if durable_outcome is LaunchLifecycleOutcome.COMPLETED:
        web_outcome = "succeeded"
    elif durable_outcome is LaunchLifecycleOutcome.PARTIAL:
        web_outcome = "partial"
        reason_codes = ["PARTIAL_CREATE"]
        reasons = ["Facebook подтвердил только часть объявлений"]
    elif durable_outcome is LaunchLifecycleOutcome.BLOCKED_RECONCILE:
        web_outcome = "partial" if finalization.created_ads > 0 else "blocked"
        reason_codes = ["CREATE_RECONCILE_REQUIRED"]
        reasons = ["Результат CREATE неоднозначен; повторный запуск заблокирован"]
    elif durable_outcome is LaunchLifecycleOutcome.BLOCKED:
        web_outcome = "blocked"
    else:
        web_outcome = "failed"

    if fallback_detail and not reason_codes:
        reason_codes = list(fallback_detail.get("reason_codes") or [])
        reasons = list(fallback_detail.get("reasons") or [])
    _set_terminal_launch_status(
        web_outcome,
        check_id=(fallback_detail or {}).get("check_id") or check_id,
        reason_codes=reason_codes,
        reasons=reasons,
    )


def _run_checked_web_launch(card: dict, plan, campaign_type: str, cities: list[str] | None,
                            as_carousel: bool, idempotency_key: str) -> None:
    """Фоновый CREATE по fresh server-owned staging и sealed gateway."""
    from services.approval_checker_models import ActionOrigin
    from services.launch_checker import LaunchCheckBlocked
    from services.launch_checker_runtime import cleanup_prepared_media

    try:
        # Legacy full-preflight больше не выдаёт execution capability:
        # освобождаем его reserve до нового gateway review.
        _finish_web_launch_authorization(plan, "RELEASED")
        cleanup_prepared_media(plan.media)
        launch_single(
            card_id=card["id"],
            card_name=card["name"],
            card_desc=card.get("desc", ""),
            status=launch_status,
            tenant_id="default",
            campaign_type=campaign_type,
            cities=cities,
            as_carousel=as_carousel,
            trello_labels=card.get("labels", []),
            idempotency_key=idempotency_key,
            origin=ActionOrigin.WEB,
        )
    except LaunchCheckBlocked as exc:
        detail = _blocked_launch_detail(exc)
        _set_terminal_launch_status(
            "blocked",
            check_id=detail["check_id"],
            reason_codes=detail["reason_codes"],
            reasons=detail["reasons"],
        )
    except Exception as exc:
        safe_reason = _redact_secrets(str(exc))
        logger.error("Web launch background failed: %s", safe_reason)
        _set_terminal_launch_status(
            "failed",
            check_id=getattr(plan, "check_id", None),
            reason_codes=["LAUNCH_FAILED"],
            reasons=[safe_reason],
        )
    finally:
        # launch_single тоже снимает running; это fail-safe для сбоя до входа в него.
        with _launch_lock:
            launch_status["running"] = False


@app.get("/api/cards")
async def get_cards():
    """Сырой top-to-bottom список с cheap checker-классификацией."""
    try:
        from services.launch_checker import LaunchSource, LaunchCheckStatus, check_candidate

        list_id = get_done_list_id()
        cards = sorted(
            get_unlaunched_cards(list_id),
            key=lambda card: (float(card["pos"]), str(card["id"])),
        )
        state = _load_web_launch_state()

        # Параллельно получаем drive-ссылки для всех карточек
        drive_urls = await asyncio.gather(
            *[asyncio.to_thread(get_card_drive_link, card["id"]) for card in cards]
        )

        result = []
        for card, drive_url in zip(cards, drive_urls):
            adset_type = detect_language(card["name"], card.get("desc", ""))
            media_type = detect_media_hint(drive_url)
            card_labels = card.get("labels", [])
            quick_check = check_candidate(
                card,
                _card_launch_request(source=LaunchSource.MANUAL),
                state,
            )
            launch_status_value = quick_check.status.value
            reason_codes = list(quick_check.reason_codes)
            reasons = list(quick_check.reasons)
            if quick_check.status is not LaunchCheckStatus.BLOCKED and not drive_url:
                launch_status_value = LaunchCheckStatus.BLOCKED.value
                reason_codes = ["MEDIA_LINK_MISSING"]
                reasons = ["В карточке нет Google Drive ссылки"]
            result.append({
                "id": card["id"],
                "name": card["name"],
                "desc": card.get("desc", ""),
                "pos": float(card["pos"]),
                "language": adset_type,
                "has_video": media_type is not None,  # обратная совместимость
                "media_type": media_type,
                "drive_url": drive_url,
                "url": f"https://trello.com/c/{card.get('shortLink', card['id'])}",
                "labels": card_labels,
                "product": _detect_product(card_labels),
                "format_labels": _detect_format_labels(card_labels),
                "launch_status": launch_status_value,
                "reason_codes": reason_codes,
                "reasons": reasons,
                "topic_override_available": quick_check.topic_override_available,
            })

        # Параллельный скоринг всех карточек
        async def _score(card_data):
            try:
                card_data["score"] = await asyncio.to_thread(score_creative, card_data["name"])
            except Exception as exc:
                logger.warning("Ошибка скоринга карточки %s: %s", card_data["name"], exc)
                card_data["score"] = {"level": "LOW", "value": 0, "reason": "Ошибка скоринга"}

        await asyncio.gather(*[_score(card_data) for card_data in result])
        raw_count = len(result)
        blocked_count = sum(card["launch_status"] == "blocked" for card in result)
        eligible_count = sum(card["launch_status"] == "eligible" for card in result)
        preflight_pending_count = sum(
            card["launch_status"] == "needs_full_preflight" for card in result
        )
        return {
            "cards": result,
            "count": raw_count,
            "raw_count": raw_count,
            # Cheap check не может объявить карточку fully eligible: это делает
            # только full live preflight непосредственно перед reservation.
            "eligible_count": eligible_count,
            "preflight_pending_count": preflight_pending_count,
            "blocked_count": blocked_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_trello_detail(e))


@app.get("/api/cards/{card_id}")
async def get_card_detail(card_id: str):
    """Детали одной карточки."""
    try:
        list_id = get_done_list_id()
        cards = get_unlaunched_cards(list_id)
        card = next((c for c in cards if c["id"] == card_id), None)
        if not card:
            raise HTTPException(status_code=404, detail="Карточка не найдена")

        adset_type = detect_language(card["name"], card.get("desc", ""))
        drive_url = get_card_drive_link(card_id)
        body = AD_BODY[adset_type]

        # Показываем в какие адсеты пойдёт
        target_adsets = {city: adsets[adset_type] for city, adsets in ADSETS.items()}

        return {
            "id": card["id"],
            "name": card["name"],
            "desc": card.get("desc", ""),
            "body": body,
            "language": adset_type,
            "drive_url": drive_url,
            "target_adsets": target_adsets,
            "cities": list(ADSETS.keys()),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_trello_detail(e))


# --- API: Запуск ---

@app.post("/api/launch/{card_id}")
async def launch_card(request: Request, card_id: str, campaign_type: str = "leadgen",
                      cities: str = "", as_carousel: bool = False,
                      source: str = "manual", override_topic_veto: bool = False,
                      override_reason: str | None = None):
    """Запустить креатив из карточки.

    Query:
      campaign_type — "leadgen" (FB Lead форма) / "website" (N городов на сайт)
                     / "mql_online" (псевдогород "Онлайн", вся страна на сайт)
                     / "leadgen_prodb" (PRODB — отдельный IG-аккаунт + текст PRODB).
      cities — список городов через запятую (например "CityA" или
               "CityA,CityB"). Пусто = во все доступные.
      as_carousel — если true и в карточке несколько картинок (2-10) —
                    объединить их в одну FB Carousel Ad вместо отдельных
                    объявлений на каждую картинку.
    """
    from services.launch_checker import (
        CheckerMode,
        LaunchCheckBlocked,
        LaunchCheckRequest,
        LaunchSource,
        hashed_api_key_actor,
    )
    from services.launch_checker_runtime import cleanup_prepared_media
    idempotency_key = _require_idempotency_key(request)
    from services.action_producer_gateway import (
        ProducerIdempotencyConflict,
        reserve_idempotency,
    )
    try:
        reserve_idempotency(
            f"web:{idempotency_key}",
            {
                "kind": "LAUNCH",
                "card_id": card_id,
                "campaign_type": campaign_type,
                "cities": cities,
                "as_carousel": as_carousel,
                "source": source,
                "override_topic_veto": override_topic_veto,
                "override_reason": override_reason,
            },
            idempotency_key,
        )
    except ProducerIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Lost-response retry сначала ищет durable gateway operation и никогда не
    # запускает второй preflight/CREATE.
    from services.action_gateway import get_operation
    from services.approval_audit import find_operation
    existing = find_operation(idempotency_key)
    if existing is not None:
        run = get_operation(existing.operation_id)
        if run is None:
            return JSONResponse(
                status_code=202,
                content={
                    "operation_id": existing.operation_id,
                    "state": "UNKNOWN",
                    "result": None,
                    "reconciliation_required": True,
                },
            )
        return JSONResponse(
            status_code=202 if run.reconciliation_required else 200,
            content=_approval_action_payload(run),
        )

    if campaign_type not in ("leadgen", "website", "mql_online", "leadgen_prodb", "prodb_online"):
        raise HTTPException(status_code=400,
            detail="campaign_type: leadgen | website | mql_online | leadgen_prodb | prodb_online")

    source_key = source.strip().lower()
    source_map = {"manual": LaunchSource.MANUAL, "batch": LaunchSource.BATCH}
    if source_key not in source_map:
        raise HTTPException(status_code=400, detail="source: manual | batch")
    normalized_reason = override_reason.strip() if override_reason is not None else None
    if override_topic_veto and (normalized_reason is None or not 10 <= len(normalized_reason) <= 300):
        raise HTTPException(
            status_code=400,
            detail="override_reason обязателен и должен содержать 10–300 символов",
        )
    if not override_topic_veto and normalized_reason:
        raise HTTPException(
            status_code=400,
            detail="override_reason допустим только с override_topic_veto=true",
        )

    # Парсим cities из query (пусто → None, чтобы запустить во все)
    cities_list = [c.strip() for c in cities.split(",") if c.strip()] if cities else None

    # Claim под одним lock закрывает двойной клик ещё до дорогого preflight.
    global _launch_preflight_running
    with _launch_lock:
        if launch_status["running"] or _launch_preflight_running:
            if launch_status.get("idempotency_key") == idempotency_key:
                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "started",
                        "operation_id": launch_status.get("operation_id"),
                    },
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "blocked",
                    "check_id": launch_status.get("check_id"),
                    "reason_codes": ["LAUNCH_IN_PROGRESS"],
                    "reasons": ["Уже идёт запуск. Дождитесь завершения."],
                },
            )
        _launch_preflight_running = True

    plan = None
    try:
        card = await asyncio.to_thread(_exact_live_launch_card, card_id)
        actor = hashed_api_key_actor(request.headers.get("X-API-Key") or "")
        checker_mode = _get_web_checker_mode()
        checker = _get_web_launch_checker(checker_mode)
        check_request = LaunchCheckRequest(
            source=source_map[source_key],
            campaign_type=campaign_type,
            cities=tuple(cities_list) if cities_list is not None else None,
            as_carousel=as_carousel,
            override_topic_veto=override_topic_veto,
            override_reason=normalized_reason,
            actor=actor,
        )
        plan = await asyncio.to_thread(
            checker.prepare_and_reserve,
            card,
            check_request,
            _load_web_launch_state(),
        )
        if checker_mode != CheckerMode.ENFORCE.value or plan.authorization is None:
            detail = {
                "status": "blocked",
                "check_id": plan.check_id,
                "reason_codes": ["CHECKER_OBSERVE"],
                "reasons": ["launch_checker.mode=observe — CREATE запрещён"],
            }
            _set_terminal_launch_status(
                "blocked",
                check_id=plan.check_id,
                reason_codes=detail["reason_codes"],
                reasons=detail["reasons"],
            )
            cleanup_prepared_media(plan.media)
            plan = None
            raise HTTPException(status_code=409, detail=detail)

        if cities_list:
            total = len(cities_list)
        else:
            # «Все города» = карта роутинга, а не константа: число городов
            # меняется через settings без деплоя.
            from services.launch_routing import all_cities

            total = len(all_cities())
        with _launch_lock:
            _launch_preflight_running = False
            launch_status.update({
                "running": True,
                "outcome": "running",
                "check_id": plan.check_id,
                "operation_id": None,
                "idempotency_key": idempotency_key,
                "action_result": None,
                "reconciliation_required": False,
                "reason_codes": [],
                "reasons": [],
                "current": card["name"],
                "progress": 0,
                "total": total,
                "step": "",
                "step_pct": None,
                "log": [],
            })

        asyncio.create_task(asyncio.to_thread(
            _run_checked_web_launch, card, plan, campaign_type, cities_list, as_carousel,
            idempotency_key,
        ))

        return JSONResponse(
            status_code=202,
            content={
                "status": "started",
                "check_id": plan.check_id,
                "auth_id": plan.authorization.auth_id,
                "card": card["name"],
                "campaign_type": campaign_type,
                "cities": cities_list or "all",
            },
        )
    except LaunchCheckBlocked as exc:
        detail = _blocked_launch_detail(exc)
        _set_terminal_launch_status(
            "blocked",
            check_id=detail["check_id"],
            reason_codes=detail["reason_codes"],
            reasons=detail["reasons"],
        )
        raise HTTPException(status_code=409, detail=detail)
    except HTTPException:
        raise
    except Exception as e:
        # get_done_list_id()/get_unlaunched_cards() при сбое Trello поднимают
        # requests.HTTPError с URL, где в query лежат key/token. str(e) слил бы
        # живые креды в HTTP-ответ — маскируем тем же _safe_trello_detail, что и
        # остальные Trello-эндпоинты (наружу только ref, секреты redacted в лог).
        _set_terminal_launch_status(
            "failed",
            reason_codes=["LAUNCH_PREFLIGHT_FAILED"],
            reasons=["Не удалось проверить карточку"],
        )
        raise HTTPException(status_code=500, detail=_safe_trello_detail(e))
    finally:
        with _launch_lock:
            _launch_preflight_running = False


@app.get("/api/launch-status")
async def get_launch_status():
    """Текущий статус запуска (polling)."""
    with _launch_lock:
        return dict(launch_status)


@app.get("/api/launch-stream")
async def launch_stream():
    """SSE поток для прогресса запуска."""
    async def event_generator():
        last_log_len = 0
        while True:
            # Снимок под lock — не держим lock долго
            with _launch_lock:
                log_snapshot = list(launch_status["log"])
                status_snapshot = {
                    "running": launch_status["running"],
                    "outcome": launch_status.get("outcome", "failed"),
                    "check_id": launch_status.get("check_id"),
                    "reason_codes": list(launch_status.get("reason_codes", [])),
                    "reasons": list(launch_status.get("reasons", [])),
                    "current": launch_status["current"],
                    "progress": launch_status["progress"],
                    "total": launch_status["total"],
                    "step": launch_status["step"],
                    "step_pct": launch_status["step_pct"],
                }

            if len(log_snapshot) > last_log_len:
                for msg in log_snapshot[last_log_len:]:
                    yield {"event": "log", "data": msg}
                last_log_len = len(log_snapshot)

            yield {
                "event": "status",
                "data": json.dumps(status_snapshot),
            }

            if not status_snapshot["running"] and last_log_len == len(log_snapshot):
                # Terminal outcome обязателен: partial/blocked нельзя
                # интерпретировать как безусловный success.
                yield {"event": "done", "data": json.dumps(status_snapshot)}
                break

            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


# --- API: История ---

@app.get("/api/history")
async def get_history():
    """История запусков."""
    history = history_repo.get_history("default")
    return {"history": history}


# --- API: Аналитика ---

@app.get("/api/analytics")
async def get_analytics(date_from: str = None, date_to: str = None, days: int = None,
                        ):
    """Анализ объявлений. Приоритет: date_from/date_to > days > default 7."""
    try:
        # Обратная совместимость: days → даты
        if not date_from and days:
            date_to = datetime.now().strftime("%Y-%m-%d")
            date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        results = await _cached_analytics_async(date_from, date_to)

        # Обновляем effective_status СВЕЖИМИ данными FB (лёгкий запрос, кеш 120с).
        # Метрики могут быть из старого кеша, но статус active/paused/deleted —
        # всегда актуальный. Иначе приостановленные рекламы висят как ACTIVE.
        from agent.analyzer import refresh_statuses_in_place
        await asyncio.to_thread(refresh_statuses_in_place, results)
        # Удалённые объявления (DELETED) — убираем из выдачи совсем
        results = [a for a in results if a.get("effective_status") != "DELETED"]

        # Любое НЕ-активное объявление (PAUSED/ADSET_PAUSED/CAMPAIGN_PAUSED/
        # DISAPPROVED) — это кандидат на ВЕРНУТЬ, а не на ОТКЛЮЧИТЬ.
        # Применяем ко ВСЕМ (не только к тем у кого есть AMO данные).
        for ad in results:
            if ad.get("effective_status") != "ACTIVE":
                ad["recommendation"] = "ВЕРНУТЬ"
                ad["reason"] = f"Не активно ({ad.get('effective_status')})"

        # Загружаем пороги из settings один раз — используются при пересчёте Decision Tree
        from agent.scheduler import load_settings as _load_settings_for_analytics
        _analytics_thresholds = _load_settings_for_analytics().get("thresholds") or {}

        # Подставляем AMO данные (поиск по ad_id, затем по имени)
        amo = amo_repo.get_amo_data("default")
        for ad in results:
            amo_entry = amo.get(ad["id"], {}) or amo.get(ad["name"].lower(), {})
            if amo_entry:
                ad["qual_pct"] = amo_entry.get("qual_pct")
                ad["romi"] = amo_entry.get("romi")
                ad["payments"] = amo_entry.get("payments")
                ad["cpql"] = amo_entry.get("cpql")
                ad["revenue"] = amo_entry.get("revenue")
                ad["qual_leads"] = amo_entry.get("qual_leads")
                # Если расход = 0 в выбранном периоде — ROMI/CPQL невалидны
                if float(ad.get("spend", 0)) == 0:
                    ad["romi"] = None
                    ad["cpql"] = None
                # Если FB-лидов за период 0 — квал/ROMI/оплаты из AMO относятся
                # к другому периоду (AMO хранит данные за всё время).
                # Показываем их только если в выбранном периоде реально были лиды.
                if int(ad.get("leads", 0) or 0) == 0:
                    ad["qual_pct"] = None
                    ad["qual_leads"] = None
                    ad["romi"] = None
                    ad["cpql"] = None
                    ad["payments"] = None
                    ad["revenue"] = None

            # Пересчитываем рекомендацию для ВСЕХ ACTIVE объявлений с учётом
            # пользовательских порогов из settings.json.
            # Не-активные уже помечены как ВЕРНУТЬ выше — не трогаем их.
            if ad.get("effective_status") == "ACTIVE":
                decision = apply_decision_tree(ad, _analytics_thresholds)
                ad["recommendation"] = decision["action"]
                ad["reason"] = decision["reason"]

        # Портфельный слой поверх абсолютного Decision Tree
        apply_portfolio_decisions(results, _analytics_thresholds)

        # Добавляем decision_count и has_repeat_problem (S15)
        d_counts = decisions_repo.get_decision_counts("default")
        for ad in results:
            ad["decision_count"] = d_counts.get(ad["id"], 0)
            ad["has_repeat_problem"] = decisions_repo.has_repeat_problem(
                "default", ad["id"], ad.get("recommendation", ""))

        # Оптимистичное обновление: если пауза подтверждена недавно,
        # но FB API ещё не обновил статус — считаем рекламу на паузе
        recent_decisions = decisions_repo.get_decisions("default", limit=200)
        recently_paused_ids = set()
        now = datetime.now()
        for d in recent_decisions:
            if d.get("action") == "PAUSED":
                try:
                    dt = datetime.strptime(d["timestamp"], "%Y-%m-%d %H:%M:%S")
                except (KeyError, ValueError):
                    continue
                if (now - dt).total_seconds() < 600:  # 10 минут
                    recently_paused_ids.add(d["ad_id"])

        for ad in results:
            if ad["id"] in recently_paused_ids and ad.get("recommendation") == "ОТКЛЮЧИТЬ":
                ad["recommendation"] = "ВЕРНУТЬ"
                ad["reason"] = "Поставлено на паузу (ожидание подтверждения FB)"

        # Подмешиваем скоринг-поля из creative_kb (total_score, score_grade, score_breakdown)
        try:
            kb_rows = get_kb_creatives(limit=500)
            kb_by_id = {row["ad_id"]: row for row in kb_rows}
            for ad in results:
                kb = kb_by_id.get(ad["id"]) or kb_by_id.get(ad.get("ad_id", ""))
                if kb:
                    ad["total_score"] = kb.get("total_score")
                    ad["score_grade"] = kb.get("score_grade")
                    ad["score_breakdown"] = kb.get("score_breakdown")  # JSON-строка
        except Exception as exc:
            # Некритично — скоринг не ломает аналитику, просто логируем
            logger.warning("Не удалось подмешать скоринг-поля в аналитику: %s", exc)

        stats = {
            "total": len(results),
            "disable": len([r for r in results if r["recommendation"] == "ОТКЛЮЧИТЬ"]),
            "wait": len([r for r in results if r["recommendation"] == "ЖДАТЬ"]),
            "keep": len([r for r in results if r["recommendation"] == "ОСТАВИТЬ"]),
            "paused": len([r for r in results if r["recommendation"] == "ВЕРНУТЬ"]),
        }
        return {"ads": results, "stats": stats, "statuses_age_sec": get_statuses_age_seconds()}
    except FBApiError as e:
        raise HTTPException(status_code=502, detail=f"FB API недоступен: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/analytics/{ad_id}/pause")
async def pause_ad_endpoint(ad_id: str, request: Request):
    """Поставить PAUSE либо создать replacement для последней ACTIVE рекламы."""
    from services.action_producer_gateway import execute_pause
    from services.approval_checker_models import ActionOrigin

    idempotency_key = _require_idempotency_key(request)
    metrics = await _optional_json_object(request)
    try:
        pause_outcome = await asyncio.to_thread(
            execute_pause,
            ad_id,
            origin=ActionOrigin.WEB,
            scope=f"web:{idempotency_key}",
            reason_code="WEB_OWNER_PAUSE",
            idempotency_key=idempotency_key,
        )
    except Exception as e:
        logger.error("pause endpoint safety guard error: %s", type(e).__name__)
        raise _approval_gateway_http_error(e) from e
    # Боевой контракт execute_pause — proposal-only: он возвращает receipt, а не
    # run. Без этой ветки успешно созданное предложение доходило до финального
    # 409 APPROVAL_DENIED. Ветки ниже описывают исполнение после одобрения.
    proposal_response = _owner_proposal_response(pause_outcome)
    if proposal_response is not None:
        return proposal_response
    if pause_outcome.confirmed and pause_outcome.run is not None:
        effect_id = f"{pause_outcome.run.operation_id}:decision:PAUSED"
        notification_payload = {
            "event_type": EVENT_AD_PAUSED,
            "title": f"Объявление отключено: {metrics.get('ad_name', ad_id)}",
            "detail": f"ID: {ad_id}",
            "level": "critical",
            "meta": {"ad_id": ad_id, "ad_name": metrics.get("ad_name", "")},
        }
        effect_applied = decisions_repo.save_decision(
            "default", ad_id, metrics.get("ad_name", ""), "PAUSED",
            "Подтверждено пользователем",
            spend=metrics.get("spend"), leads=metrics.get("leads"),
            cpl=metrics.get("cpl"), ctr=metrics.get("ctr"),
            cpm=metrics.get("cpm"), romi=metrics.get("romi"),
            qual_pct=metrics.get("qual_pct"),
            effect_id=effect_id,
            outbox_channel="IN_APP",
            outbox_payload=notification_payload)
        delivered = _deliver_in_app_action_outbox(effect_id)
        if effect_applied is not False and not delivered:
            try:
                notify(**notification_payload)
            except Exception as exc:
                logger.warning("pause notification не отправлена: %s", type(exc).__name__)
        return _approval_action_payload(pause_outcome.run)
    if pause_outcome.action == "REPLACEMENT_ENQUEUED":
        return {
            "status": "replacement_enqueued",
            "workflow_id": pause_outcome.workflow_id,
        }
    if pause_outcome.reason in {
        "last_active_no_replacement",
        "last_active_without_replacement",
    }:
        raise HTTPException(
            status_code=409,
            detail="PAUSE заблокирована: в adset нет другой ACTIVE-рекламы",
        )
    if pause_outcome.run is not None and pause_outcome.run.reconciliation_required:
        return JSONResponse(
            status_code=202,
            content=_approval_action_payload(pause_outcome.run),
        )
    if pause_outcome.run is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": pause_outcome.reason or pause_outcome.action or "APPROVAL_DENIED",
                "operation_id": pause_outcome.run.operation_id,
            },
        )
    raise HTTPException(
        status_code=409,
        detail={"code": "APPROVAL_DENIED", "operation_id": None},
    )




@app.post("/api/analytics/{ad_id}/unpause")
async def unpause_ad_endpoint(ad_id: str, request: Request):
    """Вернуть объявление из паузы (ACTIVE)."""
    from services.action_producer_gateway import execute_unpause
    from services.approval_checker_models import ActionOrigin

    idempotency_key = _require_idempotency_key(request)
    metrics = await _optional_json_object(request)
    try:
        outcome = await asyncio.to_thread(
            execute_unpause,
            ad_id,
            origin=ActionOrigin.WEB,
            scope=f"web:{idempotency_key}",
            idempotency_key=idempotency_key,
        )
    except Exception as e:
        logger.error("unpause endpoint gateway error: %s", type(e).__name__)
        raise _approval_gateway_http_error(e) from e
    # Как и в pause: боевой execute_unpause отдаёт receipt предложения, а не run.
    proposal_response = _owner_proposal_response(outcome)
    if proposal_response is not None:
        return proposal_response
    if outcome.confirmed and outcome.run is not None:
        effect_id = f"{outcome.run.operation_id}:decision:REACTIVATED"
        notification_payload = {
            "event_type": EVENT_AD_PAUSED,
            "title": f"Объявление возвращено: {metrics.get('ad_name', ad_id)}",
            "detail": f"ID: {ad_id}",
            "level": "info",
            "meta": {"ad_id": ad_id, "ad_name": metrics.get("ad_name", "")},
        }
        effect_applied = decisions_repo.save_decision(
            "default", ad_id, metrics.get("ad_name", ""), "REACTIVATED",
            "Возвращено пользователем",
            spend=metrics.get("spend"), leads=metrics.get("leads"),
            cpl=metrics.get("cpl"), ctr=metrics.get("ctr"),
            cpm=metrics.get("cpm"), romi=metrics.get("romi"),
            qual_pct=metrics.get("qual_pct"),
            effect_id=effect_id,
            outbox_channel="IN_APP",
            outbox_payload=notification_payload)
        delivered = _deliver_in_app_action_outbox(effect_id)
        if effect_applied is not False:
            with _cache_lock:
                _analytics_cache.clear()
                _refresh_locks.clear()
            if not delivered:
                try:
                    notify(**notification_payload)
                except Exception as exc:
                    logger.warning("unpause notification не отправлена: %s", type(exc).__name__)
        return _approval_action_payload(outcome.run)
    if outcome.run is not None and outcome.run.reconciliation_required:
        return JSONResponse(
            status_code=202,
            content=_approval_action_payload(outcome.run),
        )
    if outcome.run is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": outcome.reason or outcome.action or "APPROVAL_DENIED",
                "operation_id": outcome.run.operation_id,
            },
        )
    raise HTTPException(
        status_code=409,
        detail={"code": "APPROVAL_DENIED", "operation_id": None},
    )

@app.post("/api/analytics/{ad_id}/dismiss")
async def dismiss_recommendation(ad_id: str, request: Request):
    """Отклонить рекомендацию (оставить объявление)."""
    metrics = {}
    try:
        body = await request.json()
        metrics = body if isinstance(body, dict) else {}
    except Exception:
        pass
    decisions_repo.save_decision(
        "default", ad_id, metrics.get("ad_name", ""), "DISMISSED",
        "Отклонено пользователем",
        spend=metrics.get("spend"), leads=metrics.get("leads"),
        cpl=metrics.get("cpl"), ctr=metrics.get("ctr"),
        cpm=metrics.get("cpm"), romi=metrics.get("romi"),
        qual_pct=metrics.get("qual_pct"))
    notify(
        EVENT_AD_DISMISSED,
        f"Рекомендация отклонена: {metrics.get('ad_name', ad_id)}",
        detail=f"ID: {ad_id}",
        level="info",
        meta={"ad_id": ad_id, "ad_name": metrics.get("ad_name", "")},
    )
    return {"status": "dismissed"}


@app.get("/api/approval/actions/{operation_id}")
async def get_approval_action(operation_id: str):
    """Только GET-reconciliation: никогда не повторяет provider mutation."""
    from services.action_gateway import get_operation, reconcile_operation
    from services.approval_audit import find_operation
    from services.action_producer_gateway import require_uuid4

    try:
        canonical_id = require_uuid4(operation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    run = get_operation(canonical_id)
    if run is None:
        reserved = find_operation(canonical_id)
        if reserved is not None:
            canonical_id = reserved.operation_id
            run = get_operation(canonical_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Operation не найдена")
    if run.reconciliation_required:
        run = await asyncio.to_thread(reconcile_operation, canonical_id)
    return _approval_action_payload(run)


@app.get("/api/decisions")
async def get_decisions():
    """История решений."""
    return {"decisions": decisions_repo.get_decisions("default")}


@app.get("/api/decisions/history")
async def get_decisions_history_endpoint(
    action: str = None,
    ad_name: str = None,
    date_from: str = None,
    date_to: str = None,
    limit: int = 50,
    offset: int = 0,
):
    """История решений с фильтрами и пагинацией."""
    return decisions_repo.get_decisions_history(
        "default",
        action=action, ad_name=ad_name,
        date_from=date_from, date_to=date_to,
        limit=limit, offset=offset,
    )


@app.get("/api/decisions/{ad_id}")
async def get_decisions_for_ad_endpoint(ad_id: str):
    """История решений для конкретного объявления."""
    decisions = decisions_repo.get_decisions_for_ad("default", ad_id)
    return {"ad_id": ad_id, "decisions": decisions, "count": len(decisions)}


# --- S16: Воронка оплат ---

@app.get("/api/creative-thumbnail/{creative_id}")
async def get_creative_thumbnail(creative_id: str):
    """Получить превью креатива по ID.

    Сначала проверяем локальный _thumb_cache из analyzer — экономит FB-запрос.
    Кеш не протухает (URL превью постоянный).
    """
    from agent.analyzer import _thumb_cache
    # Быстрый путь: превью уже есть в памяти
    if creative_id in _thumb_cache:
        return {"thumbnail_url": _thumb_cache[creative_id]}

    from config import FB_TOKEN
    from agent.fb_common import _throttled_get
    try:
        resp = _throttled_get(
            f"https://graph.facebook.com/v21.0/{creative_id}",
            params={"access_token": FB_TOKEN, "fields": "thumbnail_url,image_url"}
        )
        if resp.ok:
            data = resp.json()
            url = data.get("thumbnail_url") or data.get("image_url") or ""
            return {"thumbnail_url": url}
        return {"thumbnail_url": ""}
    except Exception:
        return {"thumbnail_url": ""}


@app.post("/api/creative-thumbnails")
async def get_creative_thumbnails_batch(body: dict):
    """Batch получение превью по списку creative_id (макс 50).

    Сначала проверяем _thumb_cache — FB-запрос только для незакешированных.
    """
    from agent.analyzer import _thumb_cache
    from config import FB_TOKEN
    from agent.fb_common import _throttled_get

    ids = body.get("ids", [])[:50]
    if not ids:
        return {"thumbnails": {}}

    # Разделяем: что есть в кеше, что нужно запросить у FB
    result: dict = {}
    miss_ids: list[str] = []
    for cid in ids:
        if cid in _thumb_cache:
            result[cid] = _thumb_cache[cid]
        else:
            miss_ids.append(cid)

    # Если всё нашлось в кеше — возвращаем без FB-запроса
    if not miss_ids:
        return {"thumbnails": result}

    # Один batch-запрос к FB только для отсутствующих в кеше
    try:
        resp = _throttled_get(
            "https://graph.facebook.com/v21.0/",
            params={
                "access_token": FB_TOKEN,
                "ids": ",".join(miss_ids),
                "fields": "thumbnail_url,image_url"
            }
        )
        if resp.ok:
            data = resp.json()
            for cid, info in data.items():
                result[cid] = info.get("thumbnail_url") or info.get("image_url") or ""
    except Exception:
        pass
    return {"thumbnails": result}


@app.get("/api/funnel")
async def get_funnel(date_from: str = None, date_to: str = None, lang: str = None):
    """Воронка оплат — leads → quals → payments по городам. lang=L2/L1/all."""
    from services.funnel import aggregate_funnel
    from services.language_split import filter_by_lang
    try:
        if not date_from:
            date_to = date_to or datetime.now().strftime("%Y-%m-%d")
            date_from = (datetime.strptime(date_to, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        ads = await _cached_analytics_async(date_from, date_to)
        ads = filter_by_lang(ads, lang)
        return aggregate_funnel(ads)
    except Exception as e:
        logger.error(f"Funnel error: {e}")
        return {"cities": {}, "total": {"leads": 0, "quals": 0, "payments": 0, "revenue": 0, "spend": 0.0,
                "lead_to_qual": 0, "qual_to_payment": 0, "cpl": 0}}


# --- S17: Сравнение периодов ---
@app.get("/api/comparison")
async def get_comparison(period: str = "week", lang: str = None):
    """Сравнение текущего и предыдущего периода. lang=L2/L1/all."""
    from services.comparison import compare_periods, get_period_ranges
    from services.language_split import filter_by_lang
    try:
        current_range, previous_range = get_period_ranges(period)
        current_ads = await _cached_analytics_async(current_range[0], current_range[1])
        previous_ads = await _cached_analytics_async(previous_range[0], previous_range[1])
        current_ads = filter_by_lang(current_ads, lang)
        previous_ads = filter_by_lang(previous_ads, lang)
        return compare_periods(current_ads, previous_ads, current_range, previous_range)
    except Exception as e:
        logger.error(f"Comparison error: {e}")
        return {"cities": {}, "total": {"current": {}, "previous": {}, "delta": {}},
                "current_period": {}, "previous_period": {}}


# --- S22: Разбивка L1/L2 ---
@app.get("/api/lang-split")
async def get_lang_split(date_from: str = None, date_to: str = None):
    """Метрики в разбивке L2/L1."""
    from services.language_split import aggregate_by_lang
    try:
        if not date_from:
            date_to = date_to or datetime.now().strftime("%Y-%m-%d")
            date_from = (datetime.strptime(date_to, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        ads = await _cached_analytics_async(date_from, date_to)
        return aggregate_by_lang(ads)
    except Exception as e:
        logger.error(f"Lang split error: {e}")
        return {"L2": {}, "L1": {}, "total": {}}


@app.get("/api/metrics/timeseries")
async def get_timeseries(ad_ids: str, date_from: str = None, date_to: str = None, days: int = 14,
                         ):
    """Daily breakdown CPL/spend/leads для графиков.
    ad_ids: comma-separated list of ad IDs."""
    try:
        ids = [x.strip() for x in ad_ids.split(",") if x.strip()]
        if not ids:
            return {"timeseries": {}}

        # Дефолтный период
        if not date_from:
            date_to = date_to or datetime.now().strftime("%Y-%m-%d")
            date_from = (datetime.strptime(date_to, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
        if not date_to:
            date_to = datetime.now().strftime("%Y-%m-%d")

        result = await asyncio.to_thread(get_daily_insights, ids, date_from, date_to)
        return {"timeseries": result}
    except FBApiError as e:
        raise HTTPException(status_code=502, detail=f"FB API недоступен: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- API: AMO данные ---

@app.get("/api/amo")
async def get_amo():
    """Все AMO данные."""
    return amo_repo.get_amo_data("default")


@app.post("/api/amo/sync")
async def sync_amo(date_from: str = None, date_to: str = None, days: int = None,
                   ):
    """Синхронизация данных из AMO CRM.
    Выгружает лиды, матчит по fb_ad_name/fb_adset_name/fb_campaign_name, считает метрики."""
    try:
        # Обратная совместимость: days → date_from/date_to
        if not date_from and days:
            date_to = datetime.now().strftime("%Y-%m-%d")
            date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        # Получаем объявления — сначала из кеша, потом из FB API
        ads = None
        try:
            ads = await _cached_analytics_async(date_from, date_to)
        except Exception:
            pass
        if not ads:
            ads = await asyncio.to_thread(get_ads_with_metrics, date_from, date_to)

        # Синхронизируем из AMO (матчинг по fb_ad_name → ad_name из FB)
        amo_days = (datetime.strptime(date_to, "%Y-%m-%d") - datetime.strptime(date_from, "%Y-%m-%d")).days if date_from and date_to else 30
        metrics = await asyncio.to_thread(sync_amo_data, ads, amo_days)

        # Сохраняем в Supabase
        amo_repo.save_amo_bulk("default", metrics)

        return {
            "status": "ok",
            "synced": len(metrics),
            "metrics": metrics,
        }
    except FBApiError as e:
        raise HTTPException(status_code=502, detail=f"FB API недоступен: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/amo/sync-mql-capi")
async def sync_mql_to_capi(days: int = 30, include_ga: bool = True):
    """Отправляет MQL-события в Facebook CAPI и (параллельно) в GA4 MP.

    Загружает квалифицированные лиды из AMO CRM и шлёт событие MQL в:
      - Facebook CAPI (для Meta Ads "Конвертированный лид")
      - Google Analytics 4 Measurement Protocol (для key event `qualified_lead`)

    Параметры:
      days — за сколько последних дней искать квал-лиды (по умолчанию 30).
      include_ga — если False, шлёт только в FB CAPI (по умолчанию True).

    Возвращает статистику по обоим каналам.
    """
    try:
        leads = await asyncio.to_thread(get_qualified_leads_with_fb_id, days)

        # Facebook CAPI (как было)
        fb_result = await asyncio.to_thread(send_mql_batch, leads)

        # GA4 Measurement Protocol (новое)
        ga_result = {"sent": 0, "skipped": 0, "errors": 0, "details": []}
        if include_ga:
            from integrations.ga_mp import send_mql_batch_to_ga4
            ga_result = await asyncio.to_thread(send_mql_batch_to_ga4, leads)

        return {
            "ok": True,
            "total_qualified": len(leads),
            "facebook_capi": {
                "sent": fb_result["sent"],
                "skipped": fb_result["skipped"],
                "errors": fb_result["errors"],
            },
            "ga4_mp": {
                "sent": ga_result["sent"],
                "skipped": ga_result["skipped"],
                "errors": ga_result["errors"],
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/amo/{ad_id}")
async def save_amo(ad_id: str, data: dict):
    """Сохранить AMO данные для объявления.
    Body: {qual_pct, romi, payments, days_running?, leads?, cpl?, spend?}
    Если переданы метрики объявления — возвращает пересчитанную рекомендацию."""
    amo_repo.save_amo_entry("default", ad_id, {
        "qual_pct": data.get("qual_pct"),
        "romi": data.get("romi"),
        "payments": data.get("payments"),
    })

    # Если переданы метрики объявления — пересчитываем рекомендацию
    if "days_running" in data:
        ad = {
            "days_running": data["days_running"],
            "leads": data.get("leads", 0),
            "cpl": data.get("cpl", 0),
            "spend": data.get("spend", 0),
            "qual_pct": data.get("qual_pct"),
            "romi": data.get("romi"),
            "payments": data.get("payments"),
        }
        decision = apply_decision_tree(ad)
        return {
            "status": "saved", "ad_id": ad_id,
            "recommendation": decision["action"],
            "reason": decision["reason"],
        }

    return {"status": "saved", "ad_id": ad_id}


# --- API: Адсеты ---

@app.get("/api/adsets")
async def get_adsets():
    """Список всех адсетов из конфига."""
    result = []
    for city, adsets in ADSETS.items():
        for adset_type, adset_id in adsets.items():
            result.append({"city": city, "type": adset_type, "id": adset_id})
    return {"adsets": result}


@app.get("/api/adsets/capacity")
async def get_adsets_capacity():
    """Ёмкость всех адсетов: сколько объявлений, лимит, рекомендация."""
    try:
        # Собираем все адсеты: [(city, adset_type, adset_id), ...]
        tasks = []
        for city, adsets in ADSETS.items():
            for adset_type, adset_id in adsets.items():
                tasks.append((city, adset_type, adset_id))

        # Параллельные запросы (10 адсетов за ~2с вместо ~10с)
        def _fetch_all_capacity():
            with ThreadPoolExecutor(max_workers=5) as pool:
                return list(pool.map(lambda t: get_adset_capacity(t[2]), tasks))

        caps = await asyncio.to_thread(_fetch_all_capacity)

        result = {}
        for (city, adset_type, _), cap in zip(tasks, caps):
            if city not in result:
                result[city] = {}
            result[city][adset_type] = cap
        return {"capacity": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _status_datetime(value: object) -> datetime:
    """Парсит durable timestamp для расчёта возраста workflow."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("status timestamp должен содержать timezone")
    return parsed


def _replacement_status_severity(
    workflow: dict,
    generated_at: datetime,
    max_pending_hours: int,
) -> str:
    """Возвращает эксплуатационную severity без изменения workflow."""
    phase = workflow.get("phase")
    age_hours = max(
        (generated_at - _status_datetime(workflow["created_at"])).total_seconds() / 3600,
        0.0,
    )
    if phase == "BLOCKED":
        return "critical"
    if phase == "WAITING_SLOT":
        if age_hours > 24:
            return "critical"
        if age_hours > 6:
            return "warning"
    if phase == "WAITING_ACTIVE":
        if age_hours > max_pending_hours:
            return "critical"
        if age_hours > 2:
            return "warning"
    return "ok"


def _build_cleaner_status() -> CleanerStatusResponse:
    """Нормализует только durable SQLite snapshot; провайдеры не вызываются."""
    from services.autopilot import get_autopilot_config
    from services.cleanup_repository import get_cleanup_status, sanitize_text

    raw = get_cleanup_status("default")
    settings = get_autopilot_config()
    cleaner = settings.get("cleaner")
    replacement = settings.get("replacement")
    if not isinstance(cleaner, dict) or not isinstance(replacement, dict):
        raise ValueError("cleaner settings unavailable")

    generated_at = _status_datetime(raw["generated_at"])
    target_free = cleaner.get("target_free")
    if type(target_free) is not int or not 0 <= target_free <= 50:
        raise ValueError("cleaner target_free invalid")
    max_pending_hours = replacement.get("max_pending_hours")
    if type(max_pending_hours) is not int or max_pending_hours < 1:
        raise ValueError("replacement max_pending_hours invalid")

    last_run = raw.get("last_run")
    pressure_by_adset: dict[str, dict] = {}
    if isinstance(last_run, dict):
        evidence = last_run.get("evidence")
        pressure = evidence.get("pressure") if isinstance(evidence, dict) else None
        if isinstance(pressure, list):
            pressure_by_adset = {
                str(item.get("adset_id")): item
                for item in pressure
                if isinstance(item, dict) and item.get("adset_id")
            }

    adsets = []
    for item in raw.get("adsets", []):
        if not isinstance(item, dict):
            raise ValueError("invalid adset status row")
        pressure = pressure_by_adset.get(str(item.get("adset_id")), {})
        available = item.get("available")
        source = item.get("source")
        if item.get("live_status") in {
            "ok", "unavailable", "incomplete", "unknown_status"
        }:
            live_status = item["live_status"]
        elif source != "fb_api":
            live_status = "unavailable"
        elif item.get("inventory_complete") is not True:
            live_status = "incomplete"
        elif item.get("effective_status") != "ACTIVE":
            live_status = "unknown_status"
        else:
            live_status = "ok"
        if available is None:
            deficit_to_target = None
        else:
            deficit_to_target = max(target_free - int(available), 0)
        adsets.append(
            {
                "account_kind": item.get("account_kind"),
                "adset_id": item.get("adset_id"),
                "adset_name": sanitize_text(item.get("adset_name")),
                "live_status": live_status,
                "used": item.get("used"),
                "available": available,
                "active_count": item.get("active_count"),
                "safe_candidate_count": pressure.get(
                    "safe_candidate_count", item.get("safe_candidate_count", 0)
                ),
                "deficit_to_target": deficit_to_target,
                "severity": pressure.get("severity", item.get("severity", "critical")),
                "reason": sanitize_text(
                    pressure.get("reason", item.get("reason"))
                ) or None,
            }
        )

    workflows = []
    for item in raw.get("replacement_workflows", []):
        if not isinstance(item, dict):
            raise ValueError("invalid replacement workflow row")
        created_at = _status_datetime(item["created_at"])
        workflows.append(
            {
                "workflow_id": item.get("workflow_id"),
                "old_ad_id": item.get("old_ad_id"),
                "adset_id": item.get("adset_id"),
                "phase": item.get("phase"),
                "replacement_ad_id": item.get("replacement_ad_id"),
                "age_hours": max(
                    (generated_at - created_at).total_seconds() / 3600,
                    0.0,
                ),
                "severity": _replacement_status_severity(
                    item, generated_at, max_pending_hours
                ),
                "last_error": sanitize_text(item.get("last_error")) or None,
            }
        )

    recovery_cases = []
    for case in raw.get("recovery_cases", []):
        if not isinstance(case, dict):
            raise ValueError("invalid recovery case row")
        city_plans = []
        for plan in case.get("city_plans", []):
            if not isinstance(plan, dict):
                raise ValueError("invalid recovery city plan row")
            city_plans.append(
                {
                    "plan_id": plan.get("plan_id"),
                    "city": sanitize_text(plan.get("city")),
                    "account_kind": plan.get("account_kind"),
                    "account_id": plan.get("account_id"),
                    "adset_id": plan.get("adset_id"),
                    "expected_ad_names": [
                        sanitize_text(name) for name in plan.get("expected_ad_names", [])
                    ],
                    "expected_ad_count": plan.get("expected_ad_count"),
                    "reconcile_from": plan.get("reconcile_from"),
                    "reconcile_until": plan.get("reconcile_until"),
                    "phase": plan.get("phase"),
                    "found_ad_ids": plan.get("found_ad_ids", []),
                    "media_manifest_sha256": plan.get("media_manifest_sha256"),
                    "launch_attempt_key": plan.get("launch_attempt_key"),
                    "capacity_available": plan.get("capacity_available"),
                    "last_rechecked_at": plan.get("last_rechecked_at"),
                    "last_error": sanitize_text(plan.get("last_error")) or None,
                }
            )
        recovery_cases.append(
            {
                "case_id": case.get("case_id"),
                "card_id": case.get("card_id"),
                "card_name": sanitize_text(case.get("card_name")),
                "source_completed_at": case.get("source_completed_at"),
                "phase": case.get("phase"),
                "missing_cities": [
                    sanitize_text(city) for city in case.get("missing_cities", [])
                ],
                "media_manifest_sha256": case.get("media_manifest_sha256"),
                "last_error": sanitize_text(case.get("last_error")) or None,
                "city_plans": city_plans,
            }
        )

    cleaned_last_run = None
    if isinstance(last_run, dict):
        cleaned_last_run = {
            key: last_run.get(key)
            for key in (
                "run_id", "run_kind", "workflow_id", "scheduled_date",
                "requested_mode", "effective_mode", "phase", "discovered_count",
                "eligible_count", "would_delete_count", "deleted_count",
                "skipped_count", "warning_count", "error_count", "started_at",
                "completed_at",
            )
        }
        cleaned_last_run["error"] = sanitize_text(last_run.get("error")) or None

    claims = []
    for claim in raw.get("unresolved_claims", []):
        if not isinstance(claim, dict):
            raise ValueError("invalid cleanup claim row")
        claims.append(
            {
                key: claim.get(key)
                for key in (
                    "claim_id", "run_id", "workflow_id", "ad_id", "adset_id",
                    "purpose", "state", "claimed_at",
                )
            }
        )
        claims[-1]["error"] = sanitize_text(claim.get("error")) or None

    proactive_enabled = cleaner.get("proactive_enabled") is True
    cleaner_enabled = cleaner.get("enabled") is True
    replacement_delete_enabled = all(
        (
            settings.get("kill_switch") is not True,
            cleaner_enabled,
            cleaner.get("dry_run") is False,
            cleaner.get("allow_irreversible_delete") is True,
            replacement.get("enabled") is True,
        )
    )
    return CleanerStatusResponse.model_validate(
        {
            "generated_at": generated_at,
            "cleaner_enabled": cleaner_enabled,
            "proactive_enabled": proactive_enabled,
            "proactive_mode": "dry_run" if proactive_enabled else "disabled",
            "replacement_delete_enabled": replacement_delete_enabled,
            "last_run": cleaned_last_run,
            "adsets": adsets,
            "unresolved_claims": claims,
            "replacement_workflows": workflows,
            "recovery_cases": recovery_cases,
        }
    )


@app.get("/api/adsets/cleaner/status", response_model=CleanerStatusResponse)
async def get_cleaner_status():
    """Возвращает последний durable cleaner/recovery snapshot без live reads."""
    try:
        return await asyncio.to_thread(_build_cleaner_status)
    except Exception as exc:
        logger.warning("cleaner status unavailable: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Cleaner state unavailable")


# Секреты в тексте ошибок/логов маскируем: access_token FB и api_key дашборда
# не должны утекать в SSE-поток или логи сервера (требование ревью).
_SECRET_QS_RE = re.compile(r"(access_token|api_key)=[^&\s\"']+", re.IGNORECASE)


def _redact_secrets(text: object) -> str:
    """Убирает access_token/api_key из строки перед отправкой в SSE или лог."""
    return _SECRET_QS_RE.sub(r"\1=***", redact_trello_secrets(text))


def _safe_trello_detail(exc: Exception) -> str:
    """Безопасный detail для клиента при ошибке Trello.

    Исходный requests-exception содержит URL с ?key=..&token=.. (session.params).
    Наружу отдаём только стабильный текст + correlation id; в лог пишем
    redacted-детали (key/token замаскированы) под тем же ref. Так секреты Trello
    не утекают ни в HTTP-ответ, ни в логи (требование ревью Wave 2).
    """
    cid = uuid.uuid4().hex[:12]
    logger.warning("Trello error [ref=%s]: %s", cid, redact_trello_secrets(exc))
    return f"Ошибка Trello (ref: {cid})"


@app.post(
    "/api/adsets/cleanup-stream",
    response_model=DisabledCleanupResponse,
    status_code=409,
)
@app.get(
    "/api/adsets/cleanup-stream",
    response_model=DisabledCleanupResponse,
    status_code=409,
)
async def cleanup_stream():
    """Совместимый маршрут: direct cleanup необратимо отключён до любых reads."""
    raise HTTPException(
        status_code=409,
        detail="Прямое удаление отключено; используйте безопасный cleaner workflow",
    )


# --- API: Обучение ---

@app.get("/api/learning/v2")
async def get_learning_v2(date_from: str = None, date_to: str = None, days: int = 14):
    """Обучение v2: анализ контента -> инсайты -> брифы -> сценарии -> Trello."""
    from services.creative_briefs import analyze_winners, build_content_insights, build_creative_briefs, build_llm_prompt
    from services.brief_generator import _generate_scenario_for_brief
    from services.llm_credit_guard import CreditBalanceError
    try:
        if not date_from:
            date_to = date_to or datetime.now().strftime("%Y-%m-%d")
            date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        if not date_to:
            date_to = datetime.now().strftime("%Y-%m-%d")

        # Получаем данные рекламы (с fallback на кеш)
        try:
            ads = await _cached_analytics_async(date_from, date_to)
        except Exception:
            with _cache_lock:
                for key, val in _analytics_cache.items():
                    if val.get("data"):
                        ads = val["data"]
                        break
                else:
                    disk = _load_disk_cache()
                    if disk and disk.get("data"):
                        ads = disk["data"]
                    else:
                        raise

        # Только реклама с данными
        active_ads = [a for a in ads if a.get("spend", 0) > 0]

        # 1. Анализ победителей и проигравших
        analysis = analyze_winners(active_ads)

        # 2. Инсайты с анализом контента
        insights = build_content_insights(analysis)

        # 3. Креативные брифы (10 полей)
        briefs = build_creative_briefs(analysis, insights)

        # 4. Генерация ОДНОГО цельного сценария через LLM (для первых 5 брифов)
        scenarios_by_brief = {}
        for brief in briefs[:5]:
            try:
                scenario = await asyncio.to_thread(_generate_scenario_for_brief, brief)
                # Пустой сценарий — не подсовываем шаблон, просто пустой список
                scenarios_by_brief[brief["hypothesis"]] = [scenario] if scenario else []
            except CreditBalanceError:
                # Кончились кредиты Anthropic — алерт уже отправлен стражем, прерываем цикл
                logger.error("get_learning_v2: кредиты Anthropic кончились — прерываю генерацию сценариев")
                break
            except Exception as exc:
                logger.warning("LLM сценарий не удался для %s: %s", brief["hypothesis"], exc)
                scenarios_by_brief[brief["hypothesis"]] = []

        return {
            "insights": insights,
            "briefs": briefs,
            "scenarios": scenarios_by_brief,
            "analysis": {
                "total_ads": len(active_ads),
                "winners_count": len(analysis.get("winners", [])),
                "median_cpl": analysis.get("median_cpl", 0),
            },
            "period": {"date_from": date_from, "date_to": date_to},
        }
    except Exception as e:
        logger.error(f"Learning v2 error: {e}")
        raise HTTPException(status_code=500, detail=str(e))




@app.post("/api/learning/v2/to-trello")
async def send_brief_to_trello(body: dict):
    """Отправляет бриф с сценариями в Trello колонку ТЗ реклам."""
    from services.creative_briefs import format_brief_for_trello
    from integrations.trello import get_or_create_list
    try:
        brief = body.get("brief", {})
        scenarios = body.get("scenarios", [])
        # Тело карточки — только сценарий текстом, без бюрократии полей.
        # Фронт может прислать несколько вариантов — берём первый непустой.
        scenario = next((s for s in scenarios if s and str(s).strip()), None)
        if not scenario:
            raise HTTPException(status_code=400, detail="Пустой сценарий — карточку создать нельзя")
        list_name = body.get("list_name", "ТЗ реклам")

        card_data = format_brief_for_trello(brief, scenario)
        list_id = get_or_create_list(list_name)

        # key/token уходят через session.params (safe_request), НЕ явным params —
        # иначе секреты плодятся в коде и в тексте возможной ошибки.
        from integrations.trello import safe_request, BASE
        resp = safe_request(
            "POST",
            f"{BASE}/cards",
            params={"idList": list_id, "name": card_data["name"], "desc": card_data["desc"]},
        )
        return {"ok": True, "card_id": resp.json()["id"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_trello_detail(e))

@app.get("/api/learning")
async def get_learning(days: int = 30):
    """Рейтинги, паттерны и гипотезы."""
    try:
        result = await asyncio.to_thread(run_learner, days)
        return result
    except FBApiError as e:
        raise HTTPException(status_code=502, detail=f"FB API недоступен: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/learning/cached")
async def get_learning_cached():
    """Последний сохранённый результат обучения (быстро, без запросов к FB)."""
    cached = learner_repo.get_learner_cache("default")
    if cached:
        return cached
    return {
        "rankings": {}, "hypotheses": [], "fatigued": [],
        "creative_table": [], "class_distribution": {},
        "business_distribution": {},
        "total_ads": 0, "total_spend": 0, "total_leads": 0,
        "total_payments": 0,
    }


@app.get("/api/hypotheses")
async def get_hypotheses(
    days: int = 30,
    type: str = None,
    priority: str = None,
):
    """Гипотезы с фильтрацией по типу и приоритету."""
    try:
        result = await asyncio.to_thread(run_learner, days)
        hypotheses = result.get("hypotheses", [])
        if type:
            hypotheses = [h for h in hypotheses if h.get("type") == type]
        if priority:
            hypotheses = [h for h in hypotheses if h.get("priority") == priority]
        return {"hypotheses": hypotheses, "total": len(hypotheses)}
    except FBApiError as e:
        raise HTTPException(status_code=502, detail=f"FB API недоступен: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



# --- API: Исторические данные (Google Sheets) ---

@app.get("/api/historical")
async def get_historical_data(city: str = None):
    """Исторические данные по лидам из Google Sheets."""
    try:
        from integrations.gsheets import fetch_all_historical_data, get_seasonal_insights
        data = await asyncio.to_thread(fetch_all_historical_data)
        if city:
            data = [d for d in data if d["city"] == city]
        insights = await asyncio.to_thread(get_seasonal_insights, city)
        return {"data": data, "insights": insights, "total": len(data)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- API: Гипотезы → Trello ---

@app.post("/api/hypotheses/publish")
async def publish_hypotheses_to_trello(data: dict):
    """Публикует гипотезы в Trello колонку 'Гипотезы'.
    Body: {hypotheses: [{title, description, type, priority, ads}]}"""
    hypotheses = data.get("hypotheses", [])
    if not hypotheses:
        raise HTTPException(status_code=400, detail="Нет гипотез для публикации")

    try:
        created = publish_hypotheses(hypotheses)
        return {"status": "ok", "created": len(created), "cards": created}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_trello_detail(e))


# --- API: Генерация текстов ---


@app.post("/api/texts/to-trello")
async def text_to_trello(request: Request):
    """Отправляет текст рекламы в Trello колонку 'ТЗ реклам'."""
    from integrations.trello import safe_request, BASE
    data = await request.json()
    text = data.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Текст пустой")

    # Колонка «ТЗ реклам» задаётся окружением: без неё фича выключена,
    # карточку не создаём (не шлём POST в Trello с пустым idList).
    tz_list_id = os.getenv("TRELLO_TZ_LIST_ID", "").strip()
    if not tz_list_id:
        raise HTTPException(
            status_code=503,
            detail="Колонка «ТЗ реклам» не настроена (TRELLO_TZ_LIST_ID)",
        )
    title = text[:60] + ("..." if len(text) > 60 else "")

    try:
        resp = safe_request("POST", f"{BASE}/cards", params={
            "idList": tz_list_id,
            "name": title,
            "desc": text,
            "pos": "top",
        })
        card = resp.json()
        return {"ok": True, "card_id": card["id"], "url": card.get("url", "")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_trello_detail(e))


@app.post("/api/generate-texts")
async def generate_texts_endpoint(data: dict):
    """Генерация вариантов рекламного текста через Claude API.
    Body: {prompt: str, count: int (3-10), language: str ("l1" по умолчанию / "l2")}"""
    prompt = (data.get("prompt") or "").strip()
    count = data.get("count", 5)
    language = data.get("language", "l1")

    try:
        variants = await asyncio.to_thread(generate_ad_texts, prompt, count, language)
        model = getattr(config, "CLAUDE_HAIKU_MODEL", "claude-haiku-4-5")
        return {"variants": variants, "model": model, "prompt_used": prompt}
    except ValueError as e:
        msg = str(e)
        if "API_KEY" in msg:
            raise HTTPException(status_code=503, detail=f"Claude API недоступен: {msg}")
        raise HTTPException(status_code=400, detail=msg)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"Claude API ошибка: {e}")


# --- API: Победители ---

@app.get("/api/winners")
async def get_winners():
    """Список победителей из архива."""
    items = winners_repo.get_winners("default")
    return {"winners": items, "count": len(items)}


@app.post("/api/winners")
async def add_winner(data: dict):
    """Добавить победителя. Body: {ad_name: str}"""
    ad_name = (data.get("ad_name") or "").strip()
    if not ad_name:
        raise HTTPException(status_code=400, detail="ad_name обязателен")
    result = winners_repo.add_winner("default", ad_name)
    return {"status": "added", "id": result.get("id")}


@app.post("/api/winners/sync")
async def sync_winners():
    """Синхронизировать победителей из learner_cache."""
    result = winners_repo.sync_from_learner("default")
    return {"status": "synced", "imported": result["imported"], "skipped": result["skipped"]}


@app.delete("/api/winners/{winner_id}")
async def delete_winner(winner_id: int):
    """Удалить победителя из архива."""
    deleted = winners_repo.delete_winner("default", winner_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Победитель не найден")
    return {"status": "deleted"}



# --- API: Настройки ---

from agent.scheduler import load_settings, save_settings, load_auto_actions, daily_analysis
from web.settings_validation import validate_settings_update


@app.get("/api/settings")
async def get_settings():
    """Настройки автопилота."""
    settings = load_settings()
    return settings


@app.post("/api/settings")
async def update_settings(data: dict):
    """Обновить настройки (auto_apply toggle и/или блок autopilot).
    Валидация вынесена в web/settings_validation.py::validate_settings_update
    (T-SETTINGS-1) — ручка только принимает запрос, валидирует и сохраняет."""
    settings = validate_settings_update(data, load_settings())
    save_settings(settings)
    return settings


@app.get("/api/settings/thresholds")
async def get_thresholds():
    """Пороги Decision Tree."""
    settings = load_settings()
    return {"thresholds": settings.get("thresholds", {})}


@app.post("/api/settings/thresholds")
async def update_thresholds(data: dict):
    """Обновить пороги Decision Tree."""
    settings = load_settings()
    thresholds = settings.get("thresholds", {})

    from agent.analyzer import DEFAULT_THRESHOLDS
    allowed = set(DEFAULT_THRESHOLDS.keys())

    for key, value in data.items():
        if key in allowed:
            try:
                thresholds[key] = float(value)
            except (ValueError, TypeError):
                pass

    settings["thresholds"] = thresholds
    save_settings(settings)
    return {"thresholds": thresholds}


@app.get("/api/auto-actions")
async def get_auto_actions():
    """Журнал автодействий."""
    actions = load_auto_actions()
    return {"actions": actions}


@app.post("/api/scheduler/run")
async def run_scheduler_now():
    """Ручной запуск автопилота (manual trigger, без гейта по времени)."""
    from services.autopilot import run_autopilot
    result = await asyncio.to_thread(run_autopilot, "manual")
    return result


@app.post("/api/autopilot/report-now")
async def send_report_now():
    """Отладочная отправка вечернего отчёта прямо сейчас (без гейта времени)."""
    from services.evening_report import send_evening_report
    sent = await asyncio.to_thread(send_evening_report)
    return {"sent": sent}


# POST /api/autopilot/shadow-now (ручная отправка теневого отчёта) удалён вместе
# с кроном по решению владельца — см. комментарий у _cron_shadow_report выше.


@app.post("/api/autopilot/coverage-now")
async def coverage_now():
    """Проверить покрытие рекламой прямо сейчас (без гейта времени) и отправить в Telegram.

    Защищён X-API-Key (POST-middleware). Только чтение локальной БД + Telegram.
    Возвращает {thin_count, empty_count, ok_count}.
    """
    from services.coverage_monitor import analyze_coverage, send_coverage_report

    data = await asyncio.to_thread(analyze_coverage)
    await asyncio.to_thread(send_coverage_report)
    return {
        "thin_count": len(data.get("thin", [])),
        "empty_count": len(data.get("empty", [])),
        "ok_count": data.get("ok_count", 0),
    }


@app.post("/api/autopilot/run-live")
async def run_autopilot_live_endpoint(data: dict = None):
    """Ручной запуск боевого автопилота (паузит аутсайдеров по decision_policy).

    ТОЛЬКО ПАУЗА — никакого DELETE или archive.
    Ежедневный крон запускается автоматически в 12:xx по локальному времени (_cron_autopilot_live).
    Требует autopilot.enabled=true и kill_switch=false в settings.json.

    Параметр max_pauses (опционально, 1-20) переопределяет max_pauses_per_run из конфига.
    Если не передан — берёт max_pauses_per_run из конфига (через run_autopilot_live).
    """
    from services.autopilot import run_autopilot_live, get_autopilot_config

    # Без параметра — берём cap из конфига (большое число: run_autopilot_live сам срежет до cfg_cap)
    cfg_cap = int(get_autopilot_config().get("max_pauses_per_run", 3))
    max_pauses = cfg_cap

    if data and "max_pauses" in data:
        try:
            v = int(data["max_pauses"])
            if 1 <= v <= 20:
                max_pauses = v
        except (TypeError, ValueError):
            pass

    result = await asyncio.to_thread(run_autopilot_live, max_pauses, "manual")
    return result


@app.post("/api/autopilot/scale-now")
async def scale_budget_now(data: dict = None):
    """Масштабирование дневного бюджета адсетов-победителей.

    ⚠️ ОПАСНО: в режиме active напрямую увеличивает расходы.
    По умолчанию mode=dry_run — только рекомендации без изменений.
    Реальное изменение требует scale_enabled=true в autopilot конфиге.

    Параметры тела (опционально):
      mode: "dry_run" (дефолт) | "active"
      max_scales: 1–10 (дефолт из конфига, max_scales_per_run)

    Защищён X-API-Key (POST-middleware). КРОН НЕ ВЕШАТЬ — только ручной запуск.
    """
    from services.budget_scaler import run_budget_scaling, get_scale_config

    mode = "dry_run"
    cfg_cap = int(get_scale_config().get("max_scales_per_run", 2))
    max_scales = cfg_cap

    if data:
        if "mode" in data and data["mode"] in ("dry_run", "active"):
            mode = data["mode"]
        if "max_scales" in data:
            try:
                v = int(data["max_scales"])
                if 1 <= v <= 10:
                    max_scales = v
            except (TypeError, ValueError):
                pass

    result = await asyncio.to_thread(run_budget_scaling, mode, max_scales)
    return result


@app.post("/api/autopilot/auto-launch-now")
async def auto_launch_now(data: dict = None):
    """Авто-запуск незапущенных карточек для закрытия пробелов покрытия.

    Защищён X-API-Key (POST-middleware).

    Тело запроса (опционально):
      mode: "dry_run" (дефолт) | "active"
      max_launches: int (1-5, дефолт 1)

    dry_run — только рекомендации + Telegram. Ничего не запускает.
    active — реальный запуск, только при autopilot.launch_enabled=true
             И autopilot.enabled=true И kill_switch=false в settings.json.

    ВАЖНО про бюджет: launch_single добавляет объявления в существующий адсет.
    Бюджет адсета НЕ меняется — новые объявления конкурируют в том же бюджете.
    """
    from services.auto_launch import run_auto_launch
    from services.launch_checker import LaunchSource

    mode = "dry_run"
    max_launches = 1

    if data:
        raw_mode = data.get("mode", "dry_run")
        if raw_mode not in ("dry_run", "active"):
            raise HTTPException(
                status_code=400,
                detail="mode должен быть 'dry_run' или 'active'",
            )
        mode = raw_mode

        if "max_launches" in data:
            try:
                v = int(data["max_launches"])
                # Потолок поднят с 3 до 5 по решению владельца (автозапуск карточек)
                if not 1 <= v <= 5:
                    raise HTTPException(
                        status_code=400,
                        detail="max_launches должен быть от 1 до 5",
                    )
                max_launches = v
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail="max_launches должен быть целым числом",
                )

    result = await asyncio.to_thread(
        run_auto_launch,
        mode,
        max_launches,
        source=LaunchSource.AUTO_LAUNCH_NOW,
    )
    return result


@app.post("/api/autopilot/generate-briefs-now")
async def generate_briefs_now(data: dict = None):
    """Запустить авто-генератор ТЗ прямо сейчас (без гейта времени).

    Защищён X-API-Key (POST-middleware).
    Только создание карточек Trello — никаких FB-действий.

    Тело запроса (опционально):
      max_briefs: int (1-10, дефолт 3) — сколько карточек создавать за раз
    """
    from services.brief_generator import generate_and_push_briefs

    max_briefs = 3
    if data and "max_briefs" in data:
        try:
            v = int(data["max_briefs"])
            if 1 <= v <= 10:
                max_briefs = v
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="max_briefs должен быть целым числом от 1 до 10",
            )

    result = await asyncio.to_thread(generate_and_push_briefs, max_briefs)
    return result


@app.post("/api/autopilot/scorecard-now")
async def scorecard_now():
    """Отправить еженедельное табло точности автопилота прямо сейчас.

    Защищён X-API-Key (POST-middleware). Никаких действий с FB — только чтение + Telegram.
    """
    from services.scorecard import build_scorecard, format_scorecard
    from services.notifications import send_telegram

    data = await asyncio.to_thread(build_scorecard, 7)
    text = format_scorecard(data)
    await asyncio.to_thread(send_telegram, text)
    return {"sent": True, "scorecard": data}


# --- Статика ---

STATIC_DIR = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")




@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))








# --- API: Уведомления (S21) ---

from services.notifications import (
    add_event as notify,
    get_events as get_notification_events,
    mark_read as mark_notification_read,
    mark_all_read as mark_all_notifications_read,
    EVENT_AD_PAUSED,
    EVENT_AD_DISMISSED,
)


@app.get("/api/notifications")
async def get_notifications(
    limit: int = 50,
    offset: int = 0,
    type: str = None,
    unread_only: bool = False,
):
    """Лента уведомлений с фильтрацией."""
    return get_notification_events(
        limit=limit, offset=offset, event_type=type, unread_only=unread_only
    )


@app.post("/api/notifications/read-all")
async def mark_all_read_endpoint():
    """Отметить все уведомления прочитанными."""
    count = mark_all_notifications_read()
    return {"marked": count}


@app.post("/api/notifications/{event_id}/read")
async def mark_single_read_endpoint(event_id: int):
    """Отметить одно уведомление прочитанным."""
    success = mark_notification_read(event_id)
    if not success:
        raise HTTPException(status_code=404, detail="Уведомление не найдено")
    return {"status": "read"}


# --- API: Предсказание победителей (S25) ---

from services.prediction import (
    train_model as prediction_train,
    predict_winner as prediction_predict,
    get_model_status as prediction_status,
)


@app.get("/api/prediction/status")
async def get_prediction_status():
    """Статус модели предсказания."""
    return prediction_status()


@app.post("/api/prediction/train")
async def train_prediction_model():
    """Обучить модель на исторических данных."""
    result = await asyncio.to_thread(prediction_train)
    return result


@app.post("/api/prediction/predict")
async def predict_winner_endpoint(data: dict):
    """Предсказать вероятность победителя.
    Body: {ctr, hook_rate, hold_rate, cpl}"""
    result = prediction_predict(data)
    return result

# --- API: Визуальный анализ креативов (S27) ---

from services.vision_analysis import (
    analyze_creative,
    get_analysis as get_vision_analysis,
    get_all_analyses as get_all_vision_analyses,
    get_vision_patterns,
)


@app.get("/api/creative/{ad_id}/analysis")
async def get_creative_analysis(ad_id: str):
    """Получить результат визуального анализа креатива."""
    result = get_vision_analysis(ad_id)
    if not result:
        raise HTTPException(status_code=404, detail="Анализ не найден. Запустите POST для анализа.")
    return result


@app.post("/api/creative/{ad_id}/analysis")
async def run_creative_analysis(ad_id: str, data: dict = None):
    """Запустить визуальный анализ креатива через Gemini Vision."""
    video_url = data.get("video_url") if data else None
    ad_name = data.get("ad_name", "") if data else ""
    result = await asyncio.to_thread(analyze_creative, ad_id, ad_name, video_url)
    return result


@app.get("/api/vision/patterns")
async def get_patterns():
    """Получить паттерны из проанализированных креативов."""
    return get_vision_patterns()


@app.get("/api/vision/analyses")
async def list_all_analyses():
    """Список всех проведённых анализов."""
    analyses = get_all_vision_analyses()
    return {"total": len(analyses), "analyses": list(analyses.values())}
