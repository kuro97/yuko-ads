"""
Google Ads API — получение расходов YouTube по городам.

Использует resource `location_view` для YouTube-кампании владельца
(ID кампании — GOOGLE_ADS_YOUTUBE_CAMPAIGN_ID, аккаунт — GOOGLE_ADS_CUSTOMER_ID).

ENV переменные:
    GOOGLE_ADS_DEVELOPER_TOKEN — токен разработчика из Google Ads API-центра
    GOOGLE_ADS_CLIENT_ID       — OAuth2 Client ID из GCP
    GOOGLE_ADS_CLIENT_SECRET   — OAuth2 Client Secret из GCP
    GOOGLE_ADS_REFRESH_TOKEN   — получается через scripts/setup_google_ads_oauth.py
    GOOGLE_ADS_CUSTOMER_ID     — ID рекламного аккаунта (цифры, дефисы допустимы)
    GOOGLE_ADS_YOUTUBE_CAMPAIGN_ID — ID YouTube-кампании (без него YouTube = 0,
                                 а Search считает все кампании аккаунта)
    GOOGLE_ADS_LOGIN_CUSTOMER_ID — опционально, если нужен MCC-аккаунт
"""

import logging
import os
import re
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def _location_view_to_gtc(resource_name: str) -> str | None:
    """Извлекает geo_target_constant из resource_name location_view.

    `customers/X/locationViews/{campaign_id}~{criterion_id}` → `geoTargetConstants/{criterion_id}`.
    """
    m = re.search(r"~(\d+)$", resource_name)
    return f"geoTargetConstants/{m.group(1)}" if m else None

# ID рекламного аккаунта и YouTube-кампании — из env, в коде значений нет.
# Дефис в customer ID (формат интерфейса «123-456-7890») API не принимает.
CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "").strip()
# «0» — ни одна кампания такого ID не имеет: без настройки YouTube-запрос пуст,
# а фильтр Search «campaign.id != 0» пропускает все кампании аккаунта.
# ID подставляется в GAQL, поэтому принимаем только цифры.
_YOUTUBE_CAMPAIGN_ID_RAW = os.getenv("GOOGLE_ADS_YOUTUBE_CAMPAIGN_ID", "").strip()
CAMPAIGN_ID = _YOUTUBE_CAMPAIGN_ID_RAW if _YOUTUBE_CAMPAIGN_ID_RAW.isdigit() else "0"

# Маппинг названий гео из API (geo_target_constant.name, в нижнем регистре) →
# каноническое имя города в отчётах. Альтернативные написания одного города
# (латиница, старые названия, местный язык) добавляются сюда же отдельными ключами.
_CITY_NAME_MAP = {
    "citya": "CityA",
    "cityb": "CityB",
    "cityc": "CityC",
    "cityd": "CityD",
    "citye": "CityE",
}

# Целевые города — именно эти нужны в отчёте
_TARGET_CITIES = {"CityA", "CityB", "CityC", "CityD", "CityE"}


def _check_credentials() -> bool:
    """Проверяет наличие обязательных переменных среды для Google Ads API."""
    required = [
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
        "GOOGLE_ADS_CUSTOMER_ID",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        logger.warning(
            "Google Ads: отсутствуют переменные среды: %s. "
            "YouTube costs = 0. Запустите scripts/setup_google_ads_oauth.py",
            ", ".join(missing),
        )
        return False
    return True


def _resolve_geo_names(client, resource_names: list[str]) -> dict[str, str]:
    """Разрешает geo_target_constant resource names в названия городов одним запросом.

    Args:
        client: GoogleAdsClient
        resource_names: список вида ['geoTargetConstants/1000000', ...]

    Returns:
        {resource_name: city_name} — только города из _TARGET_CITIES
    """
    if not resource_names:
        return {}

    ga_service = client.get_service("GoogleAdsService")

    # Формируем IN-список для GAQL
    quoted = ", ".join(f"'{rn}'" for rn in resource_names)
    query = f"""
        SELECT
            geo_target_constant.resource_name,
            geo_target_constant.name,
            geo_target_constant.canonical_name
        FROM geo_target_constant
        WHERE geo_target_constant.resource_name IN ({quoted})
    """

    result = {}
    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        for row in response:
            rn = row.geo_target_constant.resource_name
            name = row.geo_target_constant.name or ""
            canonical = row.geo_target_constant.canonical_name or ""

            # Ищем совпадение по имени или первой части canonical_name
            city_name = None
            for candidate in [name, canonical.split(",")[0].strip()]:
                city_name = _CITY_NAME_MAP.get(candidate.lower())
                if city_name:
                    break

            if city_name and city_name in _TARGET_CITIES:
                result[rn] = city_name
                logger.debug("Гео: %s → %s (%s)", rn, city_name, name)

    except Exception as e:
        logger.error("Google Ads: ошибка разрешения geo_target_constant: %s", e)

    return result


def get_youtube_costs_by_city(target_date: date) -> dict[str, float]:
    """Получает расходы YouTube Ads по городам за указанную дату.

    Запрашивает location_view YouTube-кампании (CAMPAIGN_ID) через Google Ads API.
    Разрешает geo_target_constant resource names в канонические названия городов.

    Args:
        target_date: дата за которую нужны данные

    Returns:
        {город: cost_usd} для городов из _TARGET_CITIES.
        Возвращает нули если нет данных или нет credentials.
    """
    # Нулевой результат — используется при graceful degradation
    zero_result = {city: 0.0 for city in _TARGET_CITIES}

    if not _check_credentials():
        return zero_result

    # Импортируем Google Ads только если credentials есть — избегаем лишних ошибок
    try:
        from google.ads.googleads.client import GoogleAdsClient
        from google.ads.googleads.errors import GoogleAdsException
    except ImportError:
        logger.error("Google Ads: пакет google-ads не установлен. pip install google-ads")
        return zero_result

    try:
        # load_from_env читает GOOGLE_ADS_* переменные окружения
        client = GoogleAdsClient.load_from_env(version="v23")
    except Exception as e:
        logger.error("Google Ads: не удалось создать клиент: %s", e)
        return zero_result

    date_str = target_date.strftime("%Y-%m-%d")
    ga_service = client.get_service("GoogleAdsService")

    # Запрашиваем расходы по локациям кампании
    query = f"""
        SELECT
            location_view.resource_name,
            metrics.cost_micros,
            metrics.impressions,
            metrics.clicks
        FROM location_view
        WHERE
            campaign.id = {CAMPAIGN_ID}
            AND segments.date = '{date_str}'
    """

    # Агрегируем cost_micros по geo_target_constant (извлечён из resource_name)
    raw_costs: dict[str, int] = {}       # {geoTargetConstants/{id}: cost_micros}
    raw_impressions: dict[str, int] = {}
    raw_clicks: dict[str, int] = {}

    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        for row in response:
            gtc = _location_view_to_gtc(row.location_view.resource_name)
            if not gtc:
                continue
            raw_costs[gtc] = raw_costs.get(gtc, 0) + row.metrics.cost_micros
            raw_impressions[gtc] = raw_impressions.get(gtc, 0) + row.metrics.impressions
            raw_clicks[gtc] = raw_clicks.get(gtc, 0) + row.metrics.clicks

    except GoogleAdsException as gae:
        # Разворачиваем детали ошибки Google Ads API
        for error in gae.failure.errors:
            logger.error(
                "Google Ads API error: %s — %s",
                error.error_code,
                error.message,
            )
        return zero_result

    except Exception as e:
        logger.error("Google Ads: ошибка запроса location_view: %s", e)
        return zero_result

    if not raw_costs:
        logger.info("Google Ads: нет данных location_view за %s (кампания %s)", date_str, CAMPAIGN_ID)
        return zero_result

    # Разрешаем все resource names за один запрос
    all_resource_names = list(raw_costs.keys())
    geo_map = _resolve_geo_names(client, all_resource_names)

    # Агрегируем по каноническим городам
    city_costs: dict[str, float] = {city: 0.0 for city in _TARGET_CITIES}

    for rn, cost_micros in raw_costs.items():
        city_name = geo_map.get(rn)
        if not city_name:
            continue
        cost_usd = cost_micros / 1_000_000
        city_costs[city_name] += cost_usd

    # Логируем результат
    logger.info("Google Ads YouTube costs за %s:", date_str)
    for city in _TARGET_CITIES:
        cost = city_costs[city]
        imp = sum(v for rn, v in raw_impressions.items() if geo_map.get(rn) == city)
        clicks = sum(v for rn, v in raw_clicks.items() if geo_map.get(rn) == city)
        if cost > 0 or imp > 0:
            logger.info("  %s: cost=%.4f USD imp=%d clicks=%d", city, cost, imp, clicks)

    return city_costs


def _query_location_view(
    client, date_str: str, campaign_filter: str
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Вспомогательная функция: запрашивает location_view с заданным фильтром.

    Args:
        client: GoogleAdsClient
        date_str: дата в формате YYYY-MM-DD
        campaign_filter: строка WHERE-условия для фильтрации кампаний

    Returns:
        (raw_costs, raw_impressions, raw_clicks) — {resource_name: value}
    """
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
            location_view.resource_name,
            metrics.cost_micros,
            metrics.impressions,
            metrics.clicks
        FROM location_view
        WHERE
            {campaign_filter}
            AND segments.date = '{date_str}'
    """

    from google.ads.googleads.errors import GoogleAdsException

    raw_costs: dict[str, int] = {}
    raw_impressions: dict[str, int] = {}
    raw_clicks: dict[str, int] = {}

    try:
        response = ga_service.search(customer_id=CUSTOMER_ID, query=query)
        for row in response:
            gtc = _location_view_to_gtc(row.location_view.resource_name)
            if not gtc:
                continue
            raw_costs[gtc] = raw_costs.get(gtc, 0) + row.metrics.cost_micros
            raw_impressions[gtc] = raw_impressions.get(gtc, 0) + row.metrics.impressions
            raw_clicks[gtc] = raw_clicks.get(gtc, 0) + row.metrics.clicks

    except GoogleAdsException as gae:
        for error in gae.failure.errors:
            logger.error("Google Ads API error: %s — %s", error.error_code, error.message)
    except Exception as e:
        logger.error("Google Ads: ошибка location_view (filter=%s): %s", campaign_filter, e)

    return raw_costs, raw_impressions, raw_clicks


def get_google_search_costs_by_city(target_date: date) -> dict[str, float]:
    """Получает расходы Google Search (все кампании кроме YouTube) по городам.

    Аккаунт тот же (CUSTOMER_ID).
    Исключает YouTube-кампанию (CAMPAIGN_ID).
    Данные берутся из location_view.

    Args:
        target_date: дата за которую нужны данные

    Returns:
        {город: cost_usd} для городов из _TARGET_CITIES.
        Возвращает нули если нет данных или нет credentials.
    """
    zero_result = {city: 0.0 for city in _TARGET_CITIES}

    if not _check_credentials():
        return zero_result

    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError:
        logger.error("Google Ads: пакет google-ads не установлен")
        return zero_result

    try:
        client = GoogleAdsClient.load_from_env(version="v23")
    except Exception as e:
        logger.error("Google Ads: не удалось создать клиент: %s", e)
        return zero_result

    date_str = target_date.strftime("%Y-%m-%d")

    # Все кампании в аккаунте, кроме YouTube
    raw_costs, raw_impressions, raw_clicks = _query_location_view(
        client, date_str, f"campaign.id != {CAMPAIGN_ID}"
    )

    if not raw_costs:
        logger.info("Google Search: нет данных location_view за %s", date_str)
        return zero_result

    # Разрешаем geo resource names в канонические города
    geo_map = _resolve_geo_names(client, list(raw_costs.keys()))

    city_costs: dict[str, float] = {city: 0.0 for city in _TARGET_CITIES}
    for rn, cost_micros in raw_costs.items():
        city_name = geo_map.get(rn)
        if city_name:
            city_costs[city_name] += cost_micros / 1_000_000

    logger.info("Google Search costs за %s:", date_str)
    for city in _TARGET_CITIES:
        cost = city_costs[city]
        if cost > 0:
            logger.info("  %s: cost=%.4f USD", city, cost)

    return city_costs


def get_google_week_spend(date_from: date, date_to: date) -> float:
    """Суммарный расход Google Ads (Search + YouTube) за диапазон дат.

    Суммирует расходы по всем городам за каждый день в окне [date_from, date_to].
    Используется в budget_scaler для подсчёта общего недельного расхода (FB + Google).

    Args:
        date_from: первый день периода (включительно).
        date_to: последний день периода (включительно).

    Returns:
        Суммарный расход в USD (float). При ошибке — 0.0 (некритично, логируем).
    """
    total = 0.0
    current = date_from
    while current <= date_to:
        try:
            # Google Search (все кампании кроме YouTube)
            search_costs = get_google_search_costs_by_city(current)
            total += sum(search_costs.values())
        except Exception as exc:
            logger.warning("get_google_week_spend: ошибка Search за %s — %s", current, exc)

        try:
            # YouTube кампания
            yt_costs = get_youtube_costs_by_city(current)
            total += sum(yt_costs.values())
        except Exception as exc:
            logger.warning("get_google_week_spend: ошибка YouTube за %s — %s", current, exc)

        current += timedelta(days=1)

    logger.info(
        "get_google_week_spend: %s – %s → суммарно $%.2f",
        date_from, date_to, total,
    )
    return total
