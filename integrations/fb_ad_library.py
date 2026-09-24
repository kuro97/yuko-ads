"""Мониторинг конкурентов через Facebook Ad Library API."""

import os

import requests

from agent.fb_common import FBApiError
import config

# Connection pooling
session = requests.Session()

AD_LIBRARY_API = "https://graph.facebook.com/v20.0/ads_archive"

# Страна показа для поиска (ISO 3166-1 alpha-2). Настраивается через
# config.AD_LIBRARY_COUNTRY или env AD_LIBRARY_COUNTRY; это нейтральный дефолт.
_DEFAULT_AD_LIBRARY_COUNTRY = "US"

FIELDS = ",".join([
    "id",
    "ad_creative_bodies",
    "ad_creative_link_titles",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_snapshot_url",
    "page_name",
    "spend",
    "impressions",
    "publisher_platforms",
])


def _parse_int(val):
    """Парсит строковое число в int, None если пусто."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _normalize_ad(ad: dict) -> dict:
    """Нормализует объявление из FB Ad Library в плоский dict."""
    return {
        "ad_id": ad.get("id"),
        "page_name": ad.get("page_name"),
        "body": (ad.get("ad_creative_bodies") or [None])[0],
        "title": (ad.get("ad_creative_link_titles") or [None])[0],
        "started": ad.get("ad_delivery_start_time"),
        "stopped": ad.get("ad_delivery_stop_time"),
        "snapshot_url": ad.get("ad_snapshot_url"),
        "spend_lower": _parse_int((ad.get("spend") or {}).get("lower_bound")),
        "spend_upper": _parse_int((ad.get("spend") or {}).get("upper_bound")),
        "impressions_lower": _parse_int((ad.get("impressions") or {}).get("lower_bound")),
        "impressions_upper": _parse_int((ad.get("impressions") or {}).get("upper_bound")),
    }


def _default_country() -> str:
    """Страна поиска по умолчанию: config.AD_LIBRARY_COUNTRY → env AD_LIBRARY_COUNTRY → "US"."""
    value = getattr(config, "AD_LIBRARY_COUNTRY", None) or os.getenv("AD_LIBRARY_COUNTRY", "")
    return (value or _DEFAULT_AD_LIBRARY_COUNTRY).strip().upper()


def get_competitor_ads(page_id: str, country: str | None = None) -> list[dict]:
    """Получает активные объявления конкурента из FB Ad Library.

    Args:
        page_id: Facebook Page ID конкурента
        country: код страны ISO 3166-1 alpha-2; None → настройка
            AD_LIBRARY_COUNTRY (config/env), иначе "US"

    Returns:
        Список нормализованных объявлений

    Raises:
        ValueError: невалидные параметры
        FBApiError: ошибка FB API
    """
    if not config.FB_TOKEN:
        raise ValueError("FB_TOKEN не задан")

    if not page_id:
        raise ValueError("page_id обязателен")

    country = country or _default_country()

    all_ads = []
    url = AD_LIBRARY_API
    params = {
        "access_token": config.FB_TOKEN,
        "search_page_ids": page_id,
        "ad_reached_countries": f'["{country}"]',
        "ad_type": "ALL",
        "ad_active_status": "ACTIVE",
        "fields": FIELDS,
        "limit": 100,
    }

    # Пагинация (максимум 5 страниц)
    for _ in range(5):
        resp = session.get(url, params=params)
        if resp.status_code != 200:
            raise FBApiError(
                f"FB Ad Library ошибка: {resp.text[:300]}",
                resp.status_code,
            )

        data = resp.json()

        # Проверяем на ошибку в теле ответа
        if "error" in data:
            err = data["error"]
            raise FBApiError(
                f"FB Ad Library: {err.get('message', 'Unknown error')}",
                err.get("code", 0),
            )

        for ad in data.get("data", []):
            all_ads.append(_normalize_ad(ad))

        # Следующая страница
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break

        url = next_url
        params = {}  # параметры уже в URL

    return all_ads
