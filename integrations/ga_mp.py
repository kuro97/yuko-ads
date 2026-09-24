"""
Google Analytics 4 Measurement Protocol — серверная отправка MQL-событий.

Параллель к fb_capi.py: когда AMO-лид квалифицируется, шлём событие
одновременно в Facebook CAPI и в GA4 MP. В GA4 это создаёт Key Event
`qualified_lead`, которое видно в отчётах как конверсия.

Использование:
    from integrations.ga_mp import send_qualified_lead, send_mql_batch_to_ga4
    send_qualified_lead(email="a@b.com", phone="+10000000001", amo_lead_id=12345)

ENV:
    GA_MEASUREMENT_ID — например G-XXXXXXXXXX (основное property сайта, напр. example.com)
    GA_API_SECRET    — создать в GA4 admin → Data Streams → Web → Measurement Protocol API secrets
"""

import hashlib
import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_MP_URL = "https://www.google-analytics.com/mp/collect"

# Где брать из env (оба через .env)
GA_MEASUREMENT_ID = os.getenv("GA_MEASUREMENT_ID", "")
GA_API_SECRET = os.getenv("GA_API_SECRET", "")


def _hash_for_id(*parts: str) -> str:
    """SHA256(email+phone+lead_id) — детерминированный client_id."""
    raw = ":".join(str(p or "") for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _normalize_phone(phone: str) -> str:
    """Телефон в E.164-подобный вид: «+» и только цифры (+XXXXXXXXXXX).

    Номер ожидается в международном формате (с кодом страны). Никаких
    страно-специфичных переписываний (замена префикса, дописывание кода страны)
    здесь нет: пробелы, скобки, дефисы и ведущий «+» просто отбрасываются.
    """
    import re
    digits = re.sub(r"\D", "", phone or "")
    return "+" + digits if digits else ""


def send_qualified_lead(
    amo_lead_id: int,
    email: str = "",
    phone: str = "",
    event_name: str = "qualified_lead",
    measurement_id: Optional[str] = None,
    api_secret: Optional[str] = None,
    extra_params: Optional[dict] = None,
) -> dict:
    """Шлёт GA4 MP event для квалифицированного лида.

    Args:
        amo_lead_id: Lead ID из AMO CRM.
        email: email (не хешированный).
        phone: телефон (нормализуется до E.164).
        event_name: qualified_lead / generate_lead / Lead.
        measurement_id: по умолчанию из env GA_MEASUREMENT_ID.
        api_secret: по умолчанию из env GA_API_SECRET.
        extra_params: доп. event params (utm_source, form_name и т.д.).

    Returns:
        {"success": bool, "error": str|None, "client_id": str}
    """
    measurement_id = measurement_id or GA_MEASUREMENT_ID
    api_secret = api_secret or GA_API_SECRET

    if not measurement_id:
        return {"success": False, "error": "GA_MEASUREMENT_ID не задан"}
    if not api_secret:
        return {"success": False, "error": "GA_API_SECRET не задан (создать в GA4 admin → Data Streams → MP)"}

    email_norm = _normalize_email(email)
    phone_norm = _normalize_phone(phone)

    # client_id — детерминированный ID. Если есть реальный GA _ga cookie — лучше использовать его,
    # но в серверном сценарии его нет, так что генерим устойчивый хеш.
    client_id = _hash_for_id(phone_norm, email_norm, amo_lead_id)[:32]

    # Deduplication на стороне GA — через event_id в params.
    event_id = _hash_for_id(amo_lead_id, event_name, phone_norm)[:24]

    event_params = {
        "engagement_time_msec": 100,  # обязательно для GA4 MP
        "session_id": str(int(time.time())),
        "amo_lead_id": str(amo_lead_id),
        "event_id": event_id,  # custom param для дедупа
    }
    if extra_params:
        event_params.update(extra_params)

    payload = {
        "client_id": client_id,
        "user_id": phone_norm or email_norm or f"amo_{amo_lead_id}",  # для user-level атрибуции
        "events": [
            {
                "name": event_name,
                "params": event_params,
            }
        ],
    }

    # Enhanced Conversions: user_data (SHA256 хешированные email/phone)
    if email_norm or phone_norm:
        user_data = {}
        if email_norm:
            user_data["sha256_email_address"] = hashlib.sha256(email_norm.encode()).hexdigest()
        if phone_norm:
            user_data["sha256_phone_number"] = hashlib.sha256(phone_norm.encode()).hexdigest()
        payload["user_data"] = user_data

    url = f"{_MP_URL}?measurement_id={measurement_id}&api_secret={api_secret}"
    try:
        resp = requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error("GA4 MP: сетевая ошибка для lead %s: %s", amo_lead_id, e)
        return {"success": False, "error": str(e), "client_id": client_id}

    # GA4 MP возвращает 204 No Content при успехе
    if resp.status_code == 204:
        logger.info("GA4 MP: отправлен %s для amo_lead=%s (client_id=%s)",
                    event_name, amo_lead_id, client_id[:12])
        return {"success": True, "error": None, "client_id": client_id}

    logger.error("GA4 MP: HTTP %s для lead %s: %s",
                 resp.status_code, amo_lead_id, resp.text[:200])
    return {
        "success": False,
        "error": f"HTTP {resp.status_code}: {resp.text[:100]}",
        "client_id": client_id,
    }


def send_mql_batch_to_ga4(leads: list[dict], already_sent: Optional[set] = None) -> dict:
    """Пакетная отправка квалифицированных лидов в GA4 MP.

    Args:
        leads: список лидов. Каждый — dict с ключами `id`, `email`, `phone`.
               Нужен хотя бы email ИЛИ phone ИЛИ amo_lead_id для идентификации.
        already_sent: множество amo_lead_id уже отправленных.

    Returns:
        {sent: int, skipped: int, errors: int, details: list}
    """
    if already_sent is None:
        already_sent = set()
    results = {"sent": 0, "skipped": 0, "errors": 0, "details": []}

    for lead in leads:
        amo_id = lead.get("id")
        if not amo_id:
            results["skipped"] += 1
            continue
        if int(amo_id) in already_sent:
            results["skipped"] += 1
            continue

        result = send_qualified_lead(
            amo_lead_id=int(amo_id),
            email=lead.get("email", ""),
            phone=lead.get("phone", ""),
            event_name="qualified_lead",
        )
        if result["success"]:
            results["sent"] += 1
            already_sent.add(int(amo_id))
            results["details"].append({"amo_id": amo_id, "status": "sent"})
        else:
            results["errors"] += 1
            results["details"].append({
                "amo_id": amo_id,
                "status": "error",
                "error": result.get("error"),
            })

    logger.info(
        "GA4 MP batch: отправлено=%d, пропущено=%d, ошибок=%d",
        results["sent"], results["skipped"], results["errors"]
    )
    return results
