"""S22: Разбивка L1/L2 — фильтрация и агрегация по типу адсета (L2/L1)."""


def filter_by_lang(ads: list[dict], lang: str | None = None) -> list[dict]:
    """Фильтрует объявления по типу адсета (L2/L1).

    lang=None или 'all' — без фильтра.
    lang='L2' — только второй язык.
    lang='L1' — только основной язык.
    Регистр не важен: 'l2' == 'L2'.
    """
    if not lang or lang.upper() == "ALL":
        return ads
    target = lang.upper()
    return [ad for ad in ads if ad.get("adset_type", "").upper() == target]


def split_by_lang(ads: list[dict]) -> dict[str, list[dict]]:
    """Разделяет объявления на группы по adset_type.
    
    Возвращает {"L2": [...], "L1": [...], "other": [...]}.
    """
    result: dict[str, list[dict]] = {"L2": [], "L1": [], "other": []}
    for ad in ads:
        atype = ad.get("adset_type", "").upper()
        if atype == "L2":
            result["L2"].append(ad)
        elif atype == "L1":
            result["L1"].append(ad)
        else:
            result["other"].append(ad)
    return result


def aggregate_by_lang(ads: list[dict]) -> dict[str, dict]:
    """Агрегирует метрики по L2/L1.
    
    Возвращает {"L2": {spend, leads, cpl, ...}, "L1": {...}, "total": {...}}.
    """
    groups = split_by_lang(ads)
    result = {}
    for lang_key in ("L2", "L1"):
        group = groups[lang_key]
        metrics = _aggregate_metrics(group)
        result[lang_key] = metrics
    result["total"] = _aggregate_metrics(ads)
    return result


def _aggregate_metrics(ads: list[dict]) -> dict:
    """Агрегирует базовые метрики из списка объявлений."""
    m = {"spend": 0.0, "leads": 0, "quals": 0, "payments": 0, "revenue": 0, "count": len(ads)}
    for ad in ads:
        m["spend"] += float(ad.get("spend", 0) or 0)
        m["leads"] += int(ad.get("leads", 0) or 0)
        m["quals"] += int(ad.get("qual_leads", 0) or 0)
        m["payments"] += int(ad.get("payments", 0) or 0)
        m["revenue"] += int(ad.get("revenue", 0) or 0)
    m["cpl"] = round(m["spend"] / m["leads"], 2) if m["leads"] > 0 else 0
    return m
