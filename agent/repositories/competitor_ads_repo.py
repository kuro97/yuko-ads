"""Репозиторий снимков объявлений конкурентов — JSON файл."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
ADS_FILE = DATA_DIR / "competitor_ads.json"

NEW_AD_DAYS = 7
NEW_CAMPAIGN_DAYS = 3


def _read() -> list[dict]:
    if not ADS_FILE.exists():
        return []
    try:
        return json.loads(ADS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ошибка чтения competitor_ads.json: %s", e)
        return []


def _write(data: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ADS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_ads(tenant_id: str, page_id: str, ads: list[dict]) -> dict:
    """Upsert объявления. Возвращает {new: int, total: int}."""
    if not ads:
        return {"new": 0, "total": 0}

    all_ads = _read()
    now = datetime.now(timezone.utc).isoformat()

    existing_ad_ids = {a["ad_id"] for a in all_ads if a.get("tenant_id") == tenant_id}

    new_count = 0
    for ad in ads:
        ad_id = str(ad.get("ad_id") or ad.get("id", ""))
        if not ad_id:
            continue

        is_new = ad_id not in existing_ad_ids

        record = {
            "tenant_id": tenant_id,
            "page_id": page_id,
            "ad_id": ad_id,
            "page_name": ad.get("page_name"),
            "title": ad.get("title"),
            "body": ad.get("body"),
            "started": ad.get("started"),
            "stopped": ad.get("stopped"),
            "snapshot_url": ad.get("snapshot_url"),
            "spend_lower": ad.get("spend_lower"),
            "spend_upper": ad.get("spend_upper"),
            "impressions_lower": ad.get("impressions_lower"),
            "impressions_upper": ad.get("impressions_upper"),
            "last_seen_at": now,
            "is_active": True,
        }

        if is_new:
            record["first_seen_at"] = now
            new_count += 1
            all_ads.append(record)
            existing_ad_ids.add(ad_id)
        else:
            # Обновляем существующую запись
            for i, existing in enumerate(all_ads):
                if existing.get("ad_id") == ad_id and existing.get("tenant_id") == tenant_id:
                    record["first_seen_at"] = existing.get("first_seen_at", now)
                    all_ads[i] = record
                    break

    _write(all_ads)

    total = sum(1 for a in all_ads
                if a.get("tenant_id") == tenant_id and a.get("page_id") == page_id)
    return {"new": new_count, "total": total}


def get_ads_by_page(tenant_id: str, page_id: str) -> list[dict]:
    """Получить все объявления конкурента."""
    all_ads = _read()
    return [a for a in all_ads
            if a.get("tenant_id") == tenant_id and a.get("page_id") == page_id]


def get_activity_summary(tenant_id: str) -> list[dict]:
    """Сводка активности по всем конкурентам."""
    all_ads = _read()
    tenant_ads = [a for a in all_ads if a.get("tenant_id") == tenant_id]
    if not tenant_ads:
        return []

    now = datetime.now(timezone.utc)

    pages: dict[str, list[dict]] = {}
    for ad in tenant_ads:
        pid = ad.get("page_id", "")
        pages.setdefault(pid, []).append(ad)

    summary = []
    for pid, page_ads in pages.items():
        total_ads = len(page_ads)
        active_ads = sum(1 for a in page_ads if a.get("is_active", True))

        new_ads_7d = 0
        has_new_campaign = False
        for ad in page_ads:
            first_seen_raw = ad.get("first_seen_at")
            if first_seen_raw:
                try:
                    first_seen = datetime.fromisoformat(
                        first_seen_raw.replace("Z", "+00:00"))
                    diff_days = (now - first_seen).days
                    if diff_days < NEW_AD_DAYS:
                        new_ads_7d += 1
                    if diff_days < NEW_CAMPAIGN_DAYS:
                        has_new_campaign = True
                except (ValueError, AttributeError):
                    pass

        spend_lower_sum = sum(a.get("spend_lower") or 0 for a in page_ads)
        spend_upper_sum = sum(a.get("spend_upper") or 0 for a in page_ads)
        name = page_ads[0].get("page_name") if page_ads else None

        summary.append({
            "page_id": pid,
            "name": name,
            "total_ads": total_ads,
            "active_ads": active_ads,
            "new_ads_7d": new_ads_7d,
            "spend_lower_sum": spend_lower_sum,
            "spend_upper_sum": spend_upper_sum,
            "has_new_campaign": has_new_campaign,
        })
    return summary


def mark_inactive(tenant_id: str, page_id: str, active_ad_ids: list[str]) -> int:
    """Пометить объявления как неактивные."""
    all_ads = _read()
    active_set = {str(aid) for aid in active_ad_ids}
    inactive_count = 0

    for ad in all_ads:
        if (ad.get("tenant_id") == tenant_id
                and ad.get("page_id") == page_id
                and ad.get("ad_id") not in active_set):
            ad["is_active"] = False
            inactive_count += 1

    _write(all_ads)
    return inactive_count
