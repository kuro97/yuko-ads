"""
Facebook Conversions API (CAPI) — прямая отправка CRM-событий.

Обходит баг Zapier "Send Funnel Event", который не включает обязательные поля:
  - custom_data.event_source: "crm"
  - custom_data.lead_event_source

Без этих полей Facebook принимает событие (200 OK), но НЕ маршрутизирует его
в pipeline Conversion Leads → колонка "Конвертированный лид" остаётся пустой.

Использование:
    from integrations.fb_capi import send_crm_event, send_mql_batch

    # Одно событие
    result = send_crm_event(fb_lead_id=973523146039534, event_name="MQL")

    # Пакет лидов из AMO CRM
    results = send_mql_batch(qualified_leads)
"""

import hashlib
import logging
import time
from typing import Optional

import requests

from config import FB_CAPI_TOKEN, FB_DATASET_ID

logger = logging.getLogger(__name__)

# Graph API endpoint
_GRAPH_URL = "https://graph.facebook.com/v21.0/{dataset_id}/events"

# Храним уже отправленные fb_lead_id в памяти (между перезапусками — в файле)
_sent_event_ids: set[str] = set()


def _make_event_id(fb_lead_id: int, event_name: str) -> str:
    """Уникальный event_id для дедупликации на стороне Facebook."""
    raw = f"{fb_lead_id}:{event_name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def send_crm_event(
    fb_lead_id: int,
    event_name: str = "MQL",
    dataset_id: str = None,
    access_token: str = None,
) -> dict:
    """Отправляет одно CRM-событие в Facebook CAPI напрямую (минуя Zapier).

    Args:
        fb_lead_id: Facebook Lead ID (leadgen_id) — число 15-17 цифр, без хеширования.
        event_name: Название события ("MQL", "SQL", "Won").
        dataset_id: ID датасета. Явный параметр — боевой FB_DATASET_ID
            по умолчанию НЕ подставляется (см. A3), передавайте его явно.
        access_token: System User Token. Явный параметр — боевой FB_CAPI_TOKEN
            по умолчанию НЕ подставляется (см. A3), передавайте его явно.

    Returns:
        dict с ключами: success (bool), events_received (int), error (str|None).
    """
    # A3: НЕ подставляем боевые dataset_id/access_token по умолчанию — иначе тесты
    # без явных значений уходят в реальный Facebook API. Вызывающий код
    # (send_mql_batch) передаёт FB_DATASET_ID/FB_CAPI_TOKEN явно.

    if not dataset_id:
        return {"success": False, "error": "FB_DATASET_ID не задан в .env"}
    if not access_token:
        return {"success": False, "error": "FB_CAPI_TOKEN не задан в .env"}
    if not fb_lead_id:
        return {"success": False, "error": "fb_lead_id пустой или None"}

    event_id = _make_event_id(int(fb_lead_id), event_name)

    payload = {
        "data": [
            {
                "event_name": event_name,
                "event_time": int(time.time()),
                "event_id": event_id,
                # Обязательно для CRM pipeline
                "action_source": "system_generated",
                "user_data": {
                    # lead_id — целое число, НЕ строка, НЕ хешировать
                    "lead_id": int(fb_lead_id),
                },
                "custom_data": {
                    # ЭТИ ДВА ПОЛЯ ОТСУТСТВУЮТ В ZAPIER — ROOT CAUSE проблемы
                    "event_source": "crm",
                    "lead_event_source": "AMO CRM",
                },
            }
        ],
        "access_token": access_token,
    }

    url = _GRAPH_URL.format(dataset_id=dataset_id)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
    except Exception as e:
        logger.error("CAPI: сетевая ошибка при отправке %s для lead %s: %s", event_name, fb_lead_id, e)
        return {"success": False, "error": str(e)}

    if resp.status_code != 200:
        logger.error(
            "CAPI: ошибка %s для lead %s: %s",
            resp.status_code, fb_lead_id, data.get("error", {}).get("message", str(data))
        )
        return {
            "success": False,
            "error": data.get("error", {}).get("message", f"HTTP {resp.status_code}"),
        }

    events_received = data.get("events_received", 0)
    logger.info(
        "CAPI: отправлен %s для lead_id=%s, events_received=%s, event_id=%s",
        event_name, fb_lead_id, events_received, event_id
    )
    return {"success": True, "events_received": events_received, "event_id": event_id}


def send_mql_batch(leads: list[dict], already_sent: Optional[set] = None) -> dict:
    """Отправляет CRM-события для пакета квалифицированных лидов.

    Args:
        leads: список лидов из get_qualified_leads_with_fb_id().
               Каждый лид должен содержать 'fb_lead_id' (int).
        already_sent: множество fb_lead_id уже отправленных ранее (дедупликация).

    Returns:
        dict: {sent: int, skipped: int, errors: int, details: list}
    """
    if already_sent is None:
        already_sent = set()
    results = {"sent": 0, "skipped": 0, "errors": 0, "details": []}

    for lead in leads:
        fb_lead_id = lead.get("fb_lead_id")
        amo_id = lead.get("id", "?")

        if not fb_lead_id:
            logger.debug("Лид #%s: нет fb_lead_id, пропускаем", amo_id)
            results["skipped"] += 1
            results["details"].append({"amo_id": amo_id, "status": "skip", "reason": "no fb_lead_id"})
            continue

        if int(fb_lead_id) in already_sent:
            logger.debug("Лид #%s (fb=%s): уже отправлен, пропускаем", amo_id, fb_lead_id)
            results["skipped"] += 1
            results["details"].append({"amo_id": amo_id, "fb_lead_id": fb_lead_id, "status": "skip", "reason": "already_sent"})
            continue

        # A3: боевые dataset_id/токен передаём явно — send_crm_event больше не подставляет их сам
        result = send_crm_event(
            fb_lead_id=int(fb_lead_id), event_name="MQL",
            dataset_id=FB_DATASET_ID, access_token=FB_CAPI_TOKEN,
        )
        if result["success"]:
            results["sent"] += 1
            already_sent.add(int(fb_lead_id))
            results["details"].append({"amo_id": amo_id, "fb_lead_id": fb_lead_id, "status": "sent"})
        else:
            results["errors"] += 1
            results["details"].append({
                "amo_id": amo_id,
                "fb_lead_id": fb_lead_id,
                "status": "error",
                "error": result.get("error"),
            })

    logger.info(
        "CAPI batch: отправлено=%d, пропущено=%d, ошибок=%d",
        results["sent"], results["skipped"], results["errors"]
    )
    return results
