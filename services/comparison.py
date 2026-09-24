"""Сравнение периодов — дельты метрик между двумя временными диапазонами."""

from datetime import date, timedelta


def compare_periods(current_ads: list[dict], previous_ads: list[dict],
                    current_range: tuple, previous_range: tuple) -> dict:
    """Сравнивает метрики двух периодов, агрегируя по городам."""
    current_by_city = _aggregate_by_city(current_ads)
    previous_by_city = _aggregate_by_city(previous_ads)

    all_cities = set(current_by_city.keys()) | set(previous_by_city.keys())

    cities = {}
    for city in sorted(all_cities):
        cur = current_by_city.get(city, _empty_metrics())
        prev = previous_by_city.get(city, _empty_metrics())
        cities[city] = {
            "current": cur,
            "previous": prev,
            "delta": _calc_delta(cur, prev),
        }

    # Тотал
    cur_total = _sum_metrics(current_by_city.values())
    prev_total = _sum_metrics(previous_by_city.values())
    total = {
        "current": cur_total,
        "previous": prev_total,
        "delta": _calc_delta(cur_total, prev_total),
    }

    return {
        "current_period": {"from": current_range[0], "to": current_range[1]},
        "previous_period": {"from": previous_range[0], "to": previous_range[1]},
        "cities": cities,
        "total": total,
    }


def get_period_ranges(period: str = "week") -> tuple:
    """Возвращает (current_range, previous_range) для заданного периода."""
    today = date.today()
    days = 30 if period == "month" else 7

    current_to = today.strftime("%Y-%m-%d")
    current_from = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    previous_to = current_from
    previous_from = (today - timedelta(days=days * 2)).strftime("%Y-%m-%d")

    return (current_from, current_to), (previous_from, previous_to)


def _aggregate_by_city(ads: list[dict]) -> dict:
    """Агрегирует метрики по городам."""
    by_city = {}
    for ad in ads:
        city = ad.get("city", "Другое")
        if city not in by_city:
            by_city[city] = _empty_metrics()
        m = by_city[city]
        m["spend"] += float(ad.get("spend", 0) or 0)
        m["leads"] += int(ad.get("leads", 0) or 0)
        m["payments"] += int(ad.get("payments", 0) or 0)
        m["revenue"] += int(ad.get("revenue", 0) or 0)

    # Пересчитываем CPL после агрегации
    for m in by_city.values():
        m["cpl"] = round(m["spend"] / m["leads"], 2) if m["leads"] > 0 else 0

    return by_city


def _empty_metrics() -> dict:
    return {"spend": 0.0, "leads": 0, "cpl": 0, "payments": 0, "revenue": 0}


def _sum_metrics(metrics_list) -> dict:
    """Суммирует список метрик."""
    total = _empty_metrics()
    for m in metrics_list:
        for k in ("spend", "leads", "payments", "revenue"):
            total[k] += m[k]
    total["cpl"] = round(total["spend"] / total["leads"], 2) if total["leads"] > 0 else 0
    return total


def _calc_delta(current: dict, previous: dict) -> dict:
    """Рассчитывает дельту в процентах."""
    delta = {}
    for key in ("spend", "leads", "cpl", "payments", "revenue"):
        cur_val = current.get(key, 0) or 0
        prev_val = previous.get(key, 0) or 0
        if prev_val == 0:
            delta[key] = None
        else:
            delta[key] = round((cur_val - prev_val) / prev_val * 100, 1)
    return delta
