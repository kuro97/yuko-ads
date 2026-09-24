"""
Инсайты и гипотезы v2 — конвейер: данные -> инсайты -> гипотезы -> тексты.
"""
import re
import json
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def extract_topic(ad_name: str) -> str:
    """Извлекает тему из имени рекламы. Паттерн: 'Город | Тема / Подтема'."""
    parts = ad_name.split("|")
    if len(parts) >= 2:
        topic = parts[1].strip()
        # Убираем подтему после /
        topic = topic.split("/")[0].strip()
        return topic
    return ad_name.strip()


def build_insights(ads: list[dict]) -> list[dict]:
    """Строит инсайты из списка объявлений.
    Группирует по городам и темам, находит паттерны."""
    insights = []

    # --- По городам ---
    by_city = defaultdict(lambda: {"spend": 0, "leads": 0, "ads": 0, "topics": defaultdict(lambda: {"spend": 0, "leads": 0, "cpl": 0})})
    for ad in ads:
        city = ad.get("city", "?")
        topic = extract_topic(ad.get("name", ad.get("ad_name", "")))
        spend = ad.get("spend", 0)
        leads = ad.get("leads", 0)
        by_city[city]["spend"] += spend
        by_city[city]["leads"] += leads
        by_city[city]["ads"] += 1
        by_city[city]["topics"][topic]["spend"] += spend
        by_city[city]["topics"][topic]["leads"] += leads

    # Лучшая тема в каждом городе
    for city, data in by_city.items():
        if data["leads"] < 3:
            continue
        cpl = round(data["spend"] / data["leads"], 2) if data["leads"] > 0 else 0

        best_topic = None
        best_cpl = 999
        for topic, tdata in data["topics"].items():
            if tdata["leads"] >= 2:
                t_cpl = round(tdata["spend"] / tdata["leads"], 2)
                tdata["cpl"] = t_cpl
                if t_cpl < best_cpl:
                    best_cpl = t_cpl
                    best_topic = topic

        if best_topic:
            insights.append({
                "type": "city_best",
                "icon": "📍",
                "text": f"{city}: лучшая тема \"{best_topic}\" (CPL ${best_cpl}), средний CPL по городу ${cpl}",
                "city": city,
                "topic": best_topic,
                "cpl": best_cpl,
                "avg_cpl": cpl,
            })

    # --- По темам (кросс-город) ---
    by_topic = defaultdict(lambda: {"spend": 0, "leads": 0, "cities": set(), "ads": []})
    for ad in ads:
        topic = extract_topic(ad.get("name", ad.get("ad_name", "")))
        by_topic[topic]["spend"] += ad.get("spend", 0)
        by_topic[topic]["leads"] += ad.get("leads", 0)
        by_topic[topic]["cities"].add(ad.get("city", "?"))
        by_topic[topic]["ads"].append(ad)

    # Темы с хорошим CPL
    topic_stats = []
    for topic, data in by_topic.items():
        if data["leads"] >= 3:
            cpl = round(data["spend"] / data["leads"], 2)
            topic_stats.append({"topic": topic, "cpl": cpl, "leads": data["leads"],
                                "spend": round(data["spend"], 2),
                                "cities": list(data["cities"]), "ads": data["ads"]})

    topic_stats.sort(key=lambda x: x["cpl"])

    if len(topic_stats) >= 2:
        best = topic_stats[0]
        worst = topic_stats[-1]
        insights.append({
            "type": "topic_compare",
            "icon": "🏆",
            "text": f"Лучшая тема: \"{best['topic']}\" (CPL ${best['cpl']}, {best['leads']} лидов). "
                    f"Худшая: \"{worst['topic']}\" (CPL ${worst['cpl']}, {worst['leads']} лидов)",
            "best_topic": best["topic"],
            "best_cpl": best["cpl"],
            "worst_topic": worst["topic"],
            "worst_cpl": worst["cpl"],
        })

    # Темы которые работают не во всех городах
    all_cities = set(ad.get("city", "?") for ad in ads)
    for ts in topic_stats[:3]:  # Топ-3 темы
        missing = all_cities - set(ts["cities"])
        if missing and ts["cpl"] < 5:  # Хорошая тема не запущена везде
            insights.append({
                "type": "topic_expansion",
                "icon": "🚀",
                "text": f"Тема \"{ts['topic']}\" (CPL ${ts['cpl']}) не запущена в: {', '.join(sorted(missing))}",
                "topic": ts["topic"],
                "cpl": ts["cpl"],
                "missing_cities": sorted(list(missing)),
            })

    # --- Усталость ---
    fatigued = [ad for ad in ads if ad.get("frequency", 0) > 2.5 and ad.get("spend", 0) > 10]
    if fatigued:
        insights.append({
            "type": "fatigue",
            "icon": "😴",
            "text": f"{len(fatigued)} рекламы устали (частота >2.5) - нужны свежие версии",
            "count": len(fatigued),
            "ads": [{"name": a.get("name", a.get("ad_name", "")), "city": a.get("city", "?"),
                     "frequency": a.get("frequency", 0)} for a in fatigued[:5]],
        })

    return insights


def build_hypotheses_v2(ads: list[dict], insights: list[dict]) -> list[dict]:
    """Генерирует конкретные гипотезы на основе инсайтов."""
    hypotheses = []

    # По темам
    by_topic = defaultdict(lambda: {"spend": 0, "leads": 0, "cities": set(), "best_ad": None})
    for ad in ads:
        topic = extract_topic(ad.get("name", ad.get("ad_name", "")))
        by_topic[topic]["spend"] += ad.get("spend", 0)
        by_topic[topic]["leads"] += ad.get("leads", 0)
        by_topic[topic]["cities"].add(ad.get("city", "?"))
        cpl = ad.get("cpl", 999)
        if by_topic[topic]["best_ad"] is None or cpl < by_topic[topic]["best_ad"].get("cpl", 999):
            by_topic[topic]["best_ad"] = ad

    topic_stats = []
    for topic, data in by_topic.items():
        if data["leads"] >= 2:
            cpl = round(data["spend"] / data["leads"], 2)
            topic_stats.append({"topic": topic, "cpl": cpl, "leads": data["leads"],
                                "spend": round(data["spend"], 2), "cities": data["cities"],
                                "best_ad": data["best_ad"]})
    topic_stats.sort(key=lambda x: x["cpl"])

    all_cities = set(ad.get("city", "?") for ad in ads)

    # 1. Расширение лучших тем на другие города
    for ts in topic_stats[:5]:
        missing = all_cities - ts["cities"]
        if missing and ts["cpl"] < 8:
            for city in sorted(missing):
                hypotheses.append({
                    "type": "expand",
                    "priority": "HIGH" if ts["cpl"] < 4 else "MED",
                    "title": f"Запустить \"{ts['topic']}\" в {city}",
                    "description": f"Тема даёт CPL ${ts['cpl']} в {', '.join(sorted(ts['cities']))}. "
                                   f"В {city} ещё не запущена. {ts['leads']} лидов за период.",
                    "topic": ts["topic"],
                    "city": city,
                    "reference_cpl": ts["cpl"],
                })

    # 2. Масштабирование топ тем (больше вариантов)
    for ts in topic_stats[:3]:
        if ts["leads"] >= 5 and ts["cpl"] < 5:
            hypotheses.append({
                "type": "scale",
                "priority": "HIGH",
                "title": f"Сделать ещё 3 варианта \"{ts['topic']}\"",
                "description": f"CPL ${ts['cpl']}, {ts['leads']} лидов, расход ${ts['spend']}. "
                               f"Тема работает - нужно больше вариантов для ротации.",
                "topic": ts["topic"],
                "reference_cpl": ts["cpl"],
            })

    # 3. Обновление уставших
    fatigued = [ad for ad in ads if ad.get("frequency", 0) > 2.5 and ad.get("spend", 0) > 10]
    for ad in fatigued[:5]:
        topic = extract_topic(ad.get("name", ad.get("ad_name", "")))
        city = ad.get("city", "?")
        hypotheses.append({
            "type": "refresh",
            "priority": "MED",
            "title": f"Обновить \"{topic}\" для {city}",
            "description": f"Частота {ad.get('frequency', 0):.1f}, CPL ${ad.get('cpl', 0)}. "
                           f"Аудитория видит рекламу слишком часто - нужна свежая версия.",
            "topic": topic,
            "city": city,
        })

    # 4. Остановить убыточные темы
    for ts in topic_stats:
        if ts["cpl"] > 15 and ts["leads"] >= 3:
            hypotheses.append({
                "type": "stop",
                "priority": "HIGH",
                "title": f"Остановить \"{ts['topic']}\" - CPL ${ts['cpl']}",
                "description": f"CPL ${ts['cpl']} при {ts['leads']} лидах, расход ${ts['spend']}. "
                               f"Тема не работает.",
                "topic": ts["topic"],
                "reference_cpl": ts["cpl"],
            })

    # Сортировка: HIGH первые
    priority_order = {"HIGH": 0, "MED": 1, "LOW": 2}
    hypotheses.sort(key=lambda h: priority_order.get(h["priority"], 9))

    return hypotheses


def build_text_prompts(hypotheses: list[dict]) -> list[dict]:
    """Для каждой гипотезы строит промпт для генерации текстов."""
    prompts = []
    for h in hypotheses:
        if h["type"] in ("expand", "scale", "refresh"):
            city = h.get("city", "все города")
            topic = h.get("topic", "")
            prompts.append({
                "hypothesis_title": h["title"],
                "prompt": f"Напиши 3 варианта рекламного текста для сервиса ACME.\n"
                          f"Тема: {topic}\n"
                          f"Город: {city}\n"
                          f"Контекст: продуктовые линии «Продукт A» и «Продукт B», аудитория — клиенты сервиса.\n"
                          f"Формат: короткий текст для Facebook (2-3 предложения), цепляющий, с призывом к действию.",
                "count": 3,
            })
    return prompts
