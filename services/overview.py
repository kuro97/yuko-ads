"""
Обзорный дашборд: агрегация метрик по городам + лента алертов.

Собирает FB Ads метрики по городам (L2/L1 × N городов из конфига),
сравнивает с предыдущим периодом, генерирует алерты по правилам.
"""

from datetime import datetime, timedelta

from agent.analyzer import get_ads_with_metrics
from integrations.amo import sync_amo_data
from config import ADSETS
from services.zscore import calc_zscore_alerts

# Пороги для алертов
CPL_THRESHOLD = 30      # USD — выше = warning
ROMI_THRESHOLD = 100    # % — ниже = warning
NO_LEADS_DAYS = 7       # дней без лидов = critical
BUDGET_USAGE_PCT = 90   # % использованного бюджета = warning


def get_overview(days: int = 14) -> dict:
    """Получить обзор по городам + алерты за период.

    Args:
        days: количество дней для анализа (7/14/30/90).

    Returns:
        {"cities": [...], "alerts": [...], "period": {"from": ..., "to": ...}}
    """
    # Даты текущего и предыдущего периода
    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    prev_date_to = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    prev_date_from = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")

    # Получить метрики из FB API за оба периода.
    # Предыдущий период — light: нужны только spend/leads для дельт,
    # без креативов и HD-превью (иначе зря жжём rate limit).
    current_ads = get_ads_with_metrics(date_from, date_to)
    previous_ads = get_ads_with_metrics(prev_date_from, prev_date_to, light=True)

    # Обогатить AMO данными (qual_pct, romi, revenue)
    try:
        sync_amo_data(current_ads, days)
    except Exception:
        pass  # AMO может быть недоступен — не блокируем обзор

    # Агрегация по городам
    cities = _aggregate_by_city(current_ads, previous_ads)

    # Генерация алертов (фиксированные пороги + Z-score аномалии)
    alerts = _generate_alerts(cities)
    zscore_alerts = calc_zscore_alerts(cities)

    return {
        "cities": cities,
        "alerts": alerts,
        "zscore_alerts": zscore_alerts,
        "period": {"from": date_from, "to": date_to},
    }


def _aggregate_by_city(current_ads: list[dict], previous_ads: list[dict]) -> list[dict]:
    """Агрегировать метрики по ключу 'город тип' (напр. 'CityA L2')."""
    current_groups = _group_ads(current_ads)
    prev_groups = _group_ads(previous_ads)

    cities = []
    # Итерация по всем городам из конфига (даже если нет объявлений)
    for city_name, types in ADSETS.items():
        for lang in types:
            key = f"{city_name} {lang}"
            ads = current_groups.get(key, [])
            prev_ads = prev_groups.get(key, [])

            # Текущий период
            spend = sum(a.get("spend", 0) for a in ads)
            leads = sum(a.get("leads", 0) for a in ads)
            cpl = round(spend / leads, 1) if leads > 0 else 0
            ctr = round(
                sum(a.get("ctr", 0) for a in ads) / len(ads), 2
            ) if ads else 0

            # AMO метрики — агрегаты, не средние!
            # ROMI = сумма выручки (LCY) / сумма расхода (USD * курс) * 100
            # qual_pct = сумма квал-лидов / сумма всех лидов * 100
            from services.exchange_rate import get_usd_to_lcy
            rev_vals = [a.get("revenue") for a in ads if a.get("revenue") is not None]
            revenue = sum(rev_vals) if rev_vals else None

            # ROMI: считаем по агрегатам (revenue/spend), а не как среднее ROMI.
            # Курс — из services.exchange_rate (настраиваемый источник, с fallback на константу USD_TO_LCY).
            if revenue and spend > 0:
                romi = round(revenue / (spend * get_usd_to_lcy()) * 100, 1)
            else:
                romi = None

            # qual_pct = сумма qual_leads / сумма leads * 100 (тоже агрегатно)
            qual_leads_total = sum(a.get("qual_leads", 0) or 0 for a in ads)
            qual_pct = round(qual_leads_total / leads * 100, 1) if leads > 0 and qual_leads_total > 0 else None

            # Предыдущий период — для дельт
            prev_spend = sum(a.get("spend", 0) for a in prev_ads)
            prev_leads = sum(a.get("leads", 0) for a in prev_ads)
            prev_cpl = round(prev_spend / prev_leads, 1) if prev_leads > 0 else 0

            # Дельты (в %)
            delta_cpl = round(
                (cpl - prev_cpl) / prev_cpl * 100, 1
            ) if prev_cpl > 0 else 0
            delta_leads = round(
                (leads - prev_leads) / prev_leads * 100, 1
            ) if prev_leads > 0 else 0

            cities.append({
                "name": key,
                "spend": round(spend, 2),
                "leads": leads,
                "cpl": cpl,
                "ctr": ctr,
                "romi": romi,
                "qual_pct": qual_pct,
                "revenue": revenue,
                "delta_cpl": delta_cpl,
                "delta_leads": delta_leads,
            })

    return cities


def _group_ads(ads: list[dict]) -> dict[str, list[dict]]:
    """Группировать объявления по ключу 'город тип'."""
    groups: dict[str, list[dict]] = {}
    for ad in ads:
        key = f"{ad.get('city', 'Unknown')} {ad.get('adset_type', '')}"
        groups.setdefault(key, []).append(ad)
    return groups


def _generate_alerts(cities: list[dict]) -> list[dict]:
    """Сгенерировать алерты по правилам для каждого города."""
    alerts = []
    now = datetime.now().isoformat(timespec="seconds")

    for city in cities:
        name = city["name"]

        # 0 лидов — critical
        if city["leads"] == 0 and city["spend"] > 0:
            alerts.append({
                "level": "critical",
                "city": name,
                "message": "0 лидов за период",
                "timestamp": now,
            })

        # CPL выше порога — warning
        if city["cpl"] > CPL_THRESHOLD and city["leads"] > 0:
            alerts.append({
                "level": "warning",
                "city": name,
                "message": f"CPL = {city['cpl']}$ (порог {CPL_THRESHOLD}$)",
                "timestamp": now,
            })

        # ROMI ниже 100% — warning
        if city["romi"] is not None and city["romi"] < ROMI_THRESHOLD:
            alerts.append({
                "level": "warning",
                "city": name,
                "message": f"ROMI = {city['romi']}% (ниже {ROMI_THRESHOLD}%)",
                "timestamp": now,
            })

    # Сортировка: critical → warning → info
    priority = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: priority.get(a["level"], 99))

    return alerts


def build_overview_from_ads(ads: list[dict], days: int = 14) -> dict:
    """Построить overview из кешированных analytics данных (без FB API запроса)."""
    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    cities = _aggregate_by_city(ads, [])  # без предыдущего периода
    alerts = _generate_alerts(cities)

    try:
        zscore_alerts = calc_zscore_alerts(cities)
    except Exception:
        zscore_alerts = []

    return {
        "cities": cities,
        "alerts": alerts,
        "zscore_alerts": zscore_alerts,
        "period": {"from": date_from, "to": date_to},
        "_cached": True,
    }
