"""Сервисный слой мониторинга конкурентов.

Бизнес-логика: fetch из FB Ad Library → сохранение в Supabase → аналитика.
Не знает про HTTP — только бизнес-операции.
"""

import logging
from datetime import datetime, timezone, timedelta

from agent.repositories import competitor_ads_repo, competitors_repo
from integrations.fb_ad_library import get_competitor_ads

logger = logging.getLogger(__name__)

# Количество дней для пометки объявления как "новое"
NEW_AD_DAYS = 7


def fetch_and_store_ads(tenant_id: str, page_id: str) -> dict:
    """Получить объявления из FB Ad Library и сохранить в Supabase.

    1. Запрашивает объявления из FB Ad Library по page_id
    2. Сохраняет/обновляет через upsert (нет дублей)
    3. Помечает как неактивные те, которых нет в текущем снимке

    Returns:
        {new: int, total: int}
    """
    # Получаем объявления из FB Ad Library
    ads = get_competitor_ads(page_id)

    # Сохраняем через upsert
    result = competitor_ads_repo.upsert_ads(tenant_id, page_id, ads)

    # Помечаем как неактивные объявления, которых нет в текущем снимке
    active_ad_ids = [
        str(ad.get("ad_id") or ad.get("id"))
        for ad in ads
        if ad.get("ad_id") or ad.get("id")
    ]
    competitor_ads_repo.mark_inactive(tenant_id, page_id, active_ad_ids)

    return result


def get_stored_ads(tenant_id: str, page_id: str) -> list[dict]:
    """Получить сохранённые объявления конкурента с пометкой is_new.

    Добавляет поле is_new = True если объявление появилось менее 7 дней назад.
    """
    ads = competitor_ads_repo.get_ads_by_page(tenant_id, page_id)

    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=NEW_AD_DAYS)

    result = []
    for ad in ads:
        ad_copy = dict(ad)

        # Вычисляем is_new на основе first_seen_at
        is_new = False
        first_seen_raw = ad.get("first_seen_at")
        if first_seen_raw:
            try:
                first_seen = datetime.fromisoformat(
                    first_seen_raw.replace("Z", "+00:00")
                )
                is_new = first_seen >= threshold
            except (ValueError, AttributeError):
                pass

        ad_copy["is_new"] = is_new
        result.append(ad_copy)

    return result


def get_activity_summary(tenant_id: str) -> list[dict]:
    """Получить сводку активности по всем конкурентам."""
    return competitor_ads_repo.get_activity_summary(tenant_id)


def fetch_all_competitors(tenant_id: str) -> dict:
    """Обновить объявления всех конкурентов tenant'а.

    Проходит по всем конкурентам, для каждого вызывает fetch_and_store_ads.
    Ошибки для отдельного конкурента не прерывают обработку остальных.

    Returns:
        {fetched: int, errors: int, details: list}
    """
    competitors = competitors_repo.get_competitors(tenant_id)

    fetched = 0
    errors = 0
    details = []

    for competitor in competitors:
        page_id = competitor.get("page_id")
        name = competitor.get("name", page_id)

        if not page_id:
            logger.warning("Конкурент без page_id: %s", competitor)
            errors += 1
            details.append({
                "page_id": None,
                "name": name,
                "status": "error",
                "error": "page_id отсутствует",
            })
            continue

        try:
            result = fetch_and_store_ads(tenant_id, page_id)
            fetched += 1
            details.append({
                "page_id": page_id,
                "name": name,
                "status": "ok",
                "new": result["new"],
                "total": result["total"],
            })
            logger.info(
                "Конкурент %s (%s): new=%d, total=%d",
                name, page_id, result["new"], result["total"],
            )
        except Exception as exc:
            errors += 1
            details.append({
                "page_id": page_id,
                "name": name,
                "status": "error",
                "error": str(exc),
            })
            logger.error(
                "Ошибка при обновлении конкурента %s (%s): %s",
                name, page_id, exc,
            )

    return {"fetched": fetched, "errors": errors, "details": details}
