"""FastAPI роутер для вебхуков AMO CRM — автоматическое копирование источника лида."""

import hmac
import re
import logging

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, Query

from config import AMO_WEBHOOK_SECRET
from services.amo_auto_source import process_lead_created

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/amo/webhooks", tags=["amo-webhooks"])

# Регэксп для парсинга только leads[add][N][id]
ADD_ID_RE = re.compile(r'^leads\[add\]\[(\d+)\]\[id\]$')

# Заголовок с webhook-секретом — предпочтительный способ (секрет не попадает в
# URL и в nginx access-логи). Query-параметр ?secret= оставлен как fallback для
# уже настроенного в проде интегратора amoconnect, который шлёт секрет в query.
_SECRET_HEADER = "X-Webhook-Secret"


def _secret_ok(provided: str) -> bool:
    """Constant-time сравнение webhook-секрета.

    AMO_WEBHOOK_SECRET — ОТДЕЛЬНЫЙ секрет вебхука, не равный общему дашбордному
    API_KEY (см. config). Пустой серверный секрет → всегда отказ (fail-closed).
    hmac.compare_digest вместо != — чтобы не сливать длину/совпадение по времени.
    """
    if not AMO_WEBHOOK_SECRET:
        return False
    return hmac.compare_digest(provided or "", AMO_WEBHOOK_SECRET)


@router.post("/lead-created")
async def lead_created(
    request: Request,
    background_tasks: BackgroundTasks,
    secret: str = Query(default=""),
):
    """Принимает webhook от AMO при создании нового лида.

    AMO шлёт application/x-www-form-urlencoded. Секрет принимаем из заголовка
    X-Webhook-Secret (предпочтительно) либо из query ?secret= (fallback под
    текущий прод-интегратор). Сравнение — constant-time. Обработка запускается
    в фоне — ответ возвращается мгновенно.

    Замечание по безопасности: у CRM-вебхуков AMO (leads[add]) нет официальной
    HMAC-подписи (X-Signature HMAC-SHA1 доступна только для Chats-каналов),
    поэтому подлинность держится на общем секрете. Секрет в query виден в
    access-логах nginx — это residual risk (nginx в этой задаче не трогаем),
    поэтому предпочтителен заголовок. Секрет НИКОГДА не логируем.

    Replay-защиты нет: в запросе нет timestamp/nonce, поэтому захваченный
    валидный вебхук (или query-secret из nginx-логов) можно повторить —
    сервер обработает его снова. Дедупликация/окно свежести закрываются
    только на стороне интегратора (amoconnect), здесь их нет.
    """
    # 1. Валидация секрета: заголовок приоритетнее query. Пустой серверный
    #    секрет или несовпадение → 401. Значение секрета в лог не пишем.
    provided = request.headers.get(_SECRET_HEADER) or secret
    if not _secret_ok(provided):
        raise HTTPException(status_code=401, detail="Invalid or missing secret")

    # 2. Парсинг form-urlencoded — берём только leads[add][N][id]
    form = await request.form()
    lead_ids = []
    for key, value in form.items():
        match = ADD_ID_RE.match(key)
        if match:
            try:
                lead_ids.append(int(value))
            except (ValueError, TypeError):
                continue

    if not lead_ids:
        return {"status": "ignored", "reason": "no leads[add]", "count": 0}

    # 3. Запуск обработки в background — не блокирует ответ AMO
    for lid in lead_ids:
        background_tasks.add_task(_safe_process, lid)

    log.info(f"Webhook принят: {len(lead_ids)} новых лидов в очередь")
    return {"status": "accepted", "count": len(lead_ids), "lead_ids": lead_ids}


def _safe_process(lead_id: int):
    """Обёртка с логированием — чтобы исключение в background не похоронить."""
    try:
        result = process_lead_created(lead_id)
        log.info(f"Lead {lead_id}: {result.get('status')} — {result.get('reason')}")
    except Exception as e:
        log.exception(f"Lead {lead_id}: процессинг упал: {e}")
