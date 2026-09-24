"""Воронка оплат — агрегация по городам."""


def aggregate_funnel(ads: list[dict]) -> dict:
    """Агрегирует воронку из списка объявлений (уже обогащённых AMO).

    ads: список объявлений с полями city, leads, qual_leads, payments, revenue, spend.

    Возвращает:
    {
        "cities": {"CityB": {leads, quals, payments, revenue, spend, lead_to_qual, qual_to_payment, cpl}, ...},
        "total": { same }
    }
    """
    cities = {}

    for ad in ads:
        city = ad.get("city", "Другое")
        if city not in cities:
            cities[city] = {"leads": 0, "quals": 0, "payments": 0, "revenue": 0, "spend": 0.0}

        c = cities[city]
        c["leads"] += int(ad.get("leads", 0) or 0)
        c["quals"] += int(ad.get("qual_leads", 0) or 0)
        c["payments"] += int(ad.get("payments", 0) or 0)
        c["revenue"] += int(ad.get("revenue", 0) or 0)
        c["spend"] += float(ad.get("spend", 0) or 0)

    # Конверсии
    for city_data in cities.values():
        _calc_conversions(city_data)

    # Тотал
    total = {"leads": 0, "quals": 0, "payments": 0, "revenue": 0, "spend": 0.0}
    for c in cities.values():
        for k in total:
            total[k] += c[k]
    _calc_conversions(total)

    return {"cities": cities, "total": total}


def _calc_conversions(d: dict):
    """Добавляет конверсии к словарю с leads/quals/payments/spend."""
    d["lead_to_qual"] = round(d["quals"] / d["leads"] * 100, 1) if d["leads"] > 0 else 0
    d["qual_to_payment"] = round(d["payments"] / d["quals"] * 100, 1) if d["quals"] > 0 else 0
    d["cpl"] = round(d["spend"] / d["leads"], 2) if d["leads"] > 0 else 0
