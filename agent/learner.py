"""
Learner — глубинный анализ креативов.

Иерархия анализа:
1. Бизнес-метрики: ROMI, квал%, оплаты (из AMO) — ГЛАВНОЕ
2. Видео-метрики: Hook Rate, Hold Rate, Retention (из FB API)
3. Усталость креатива: частота, CTR
4. 2×2 матрица: Winner / Clickbait / Hidden Gem / Dead
5. Генерация гипотез на основе паттернов
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.fb_token_provider import get_fb_token, get_fb_account_id
from services.meta_lead_actions import parse_meta_lead_actions
from agent.fb_common import build_adset_map, get_all_ads, session, API, FBApiError

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
LEARNER_FILE = DATA_DIR / "learner_results.json"
AMO_FILE = DATA_DIR / "amo_data.json"

# Пороги для классификации
HOOK_RATE_GOOD = 30   # >30% — хороший хук
HOLD_RATE_GOOD = 15   # >15% — контент удерживает
FATIGUE_FREQUENCY = 2.5  # Частота >2.5 — признак усталости


def load_amo_data() -> dict:
    """Загружает AMO данные. Формат: {ad_id: {qual_pct, romi, payments}}"""
    if AMO_FILE.exists():
        return json.loads(AMO_FILE.read_text(encoding="utf-8"))
    return {}


def classify_business(ad: dict) -> str:
    """
    Бизнес-классификация на основе ROMI и квал%.
    Это ГЛАВНЫЙ уровень анализа — деньги важнее метрик.

    Прибыльный — ROMI >= 200%
    Окупается — ROMI 100-200%
    Убыточный — ROMI < 100%
    Перспективный — квал >= 20%, ждём оплат
    Низкая квал — квал < 20%
    Нет данных — AMO не заполнен
    """
    romi = ad.get("romi")
    qual = ad.get("qual_pct")
    payments = ad.get("payments")

    if romi is not None:
        if romi >= 200:
            return "Прибыльный"
        if romi >= 100:
            return "Окупается"
        return "Убыточный"

    if qual is not None:
        if qual >= 20 and (payments is None or payments == 0):
            return "Перспективный"
        if qual < 20:
            return "Низкая квал"

    return "Нет данных"


def _offline_accounts() -> tuple[str, ...]:
    """Кабинеты для сбора метрик: все оффлайн-кабинеты карты роутинга.

    Раньше learner собирал ТОЛЬКО дефолтный кабинет (cabinet_a) — весь
    cabinet_b был слепой зоной creative_kb, и автономные паузы не видели его
    сливы (неделями в эфире при нуле квалов). Фолбэк при
    недоступной карте — дефолтный кабинет, как раньше.
    """
    try:
        from services.launch_routing import accounts_to_scan

        accounts = accounts_to_scan()
        if accounts:
            return accounts
    except Exception as exc:  # noqa: BLE001 — деградация до старого поведения
        import logging
        logging.getLogger(__name__).warning(
            "learner: карта роутинга недоступна (%s) — только дефолтный кабинет", exc
        )
    return (str(get_fb_account_id()).replace("act_", ""),)


def get_creative_metrics(days: int = 30) -> list[dict]:
    """
    Собирает расширенные метрики всех объявлений за N дней.
    Оптимизировано: 2 запроса на КАЖДЫЙ кабинет вместо 110+ (account-level
    insights). Кабинеты — все оффлайн из карты роутинга (cabinet_a + cabinet_b).
    """
    from services.fb_token_provider import fb_account, offline_account_context

    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")

    # Карта адсетов строится ВНЕ account-контекста: discovery сам мультикабинетный
    adset_map = build_adset_map()

    ads_by_id: dict = {}
    insights: dict = {}
    for account_id in _offline_accounts():
        ctx = offline_account_context(account_id)
        try:
            with fb_account(ctx):
                # 1. Все объявления кабинета (1 запрос + пагинация)
                account_ads = get_all_ads(adset_map)
                # 2. Видео-метрики всех объявлений кабинета (1 запрос + пагинация)
                account_insights = _get_account_video_insights(date_from, date_to)
        except Exception:
            if ctx is None:
                # Дефолтный кабинет обязателен — без него результат бессмысленен
                raise
            import logging
            logging.getLogger(__name__).warning(
                "learner: кабинет %s недоступен — пропускаю (частичный сбор)",
                account_id,
                exc_info=True,
            )
            continue
        ads_by_id.update(account_ads)
        insights.update(account_insights)

    # 3. Объединяем и рассчитываем
    all_ads = []
    for ad_id, ad in ads_by_id.items():
        metrics = insights.get(ad_id, {})
        created = datetime.fromisoformat(ad["created_time"].replace("+0000", "+00:00"))
        days_running = (datetime.now(created.tzinfo) - created).days

        impressions = metrics.get("impressions", 0)
        video_views_3s = metrics.get("video_views_3s", 0)
        thruplay = metrics.get("thruplay", 0)

        hook_rate = round(video_views_3s / impressions * 100, 1) if impressions > 0 else 0
        hold_rate = round(thruplay / video_views_3s * 100, 1) if video_views_3s > 0 else 0
        creative_class = classify_creative(hook_rate, hold_rate)

        all_ads.append({
            "ad_id": ad_id,
            "ad_name": ad["name"],
            "status": ad["status"],
            # Реальный статус с учётом родителей — без него новая строка KB
            # невидима автопилоту (_fetch_ads_from_local_db фильтрует по нему)
            "effective_status": ad.get("effective_status", ad["status"]),
            "city": ad["city"],
            "adset_type": ad["adset_type"],
            "days_running": days_running,
            # Базовые метрики
            "spend": metrics.get("spend", 0),
            "leads": metrics.get("leads", 0),
            "cpl": metrics.get("cpl", 0),
            "ctr": metrics.get("ctr", 0),
            "cpm": metrics.get("cpm", 0),
            "impressions": impressions,
            "clicks": metrics.get("clicks", 0),
            "frequency": metrics.get("frequency", 0),
            # Видео-метрики
            "video_views_3s": video_views_3s,
            "thruplay": thruplay,
            "video_p25": metrics.get("video_p25", 0),
            "video_p50": metrics.get("video_p50", 0),
            "video_p75": metrics.get("video_p75", 0),
            "video_p100": metrics.get("video_p100", 0),
            # Рассчитанные метрики
            "hook_rate": hook_rate,
            "hold_rate": hold_rate,
            "creative_class": creative_class,
        })

    return all_ads


def _get_account_video_insights(date_from: str, date_to: str) -> dict:
    """Видео-метрики всех объявлений одним запросом (account-level, level=ad)."""
    insights = {}
    resp = session.get(
        f"{API}/act_{get_fb_account_id()}/insights",
        params={
            "access_token": get_fb_token(),
            "level": "ad",
            "fields": ",".join([
                "ad_id", "spend", "impressions", "clicks", "ctr", "cpm", "frequency",
                "actions",
                "video_thruplay_watched_actions",
                "video_p25_watched_actions",
                "video_p50_watched_actions",
                "video_p75_watched_actions",
                "video_p100_watched_actions",
            ]),
            "time_range": json.dumps({"since": date_from, "until": date_to}),
            "limit": 500,
        },
    )
    if resp.status_code != 200:
        raise FBApiError(f"FB API video insights ошибка: {resp.status_code} {resp.text[:200]}", resp.status_code)

    data = resp.json()
    for row in data.get("data", []):
        insights[row["ad_id"]] = parse_video_insight_row(row)

    # Пагинация
    while "paging" in data and "next" in data.get("paging", {}):
        resp = session.get(data["paging"]["next"])
        if resp.status_code != 200:
            raise FBApiError(f"FB API video insights ошибка при пагинации: {resp.status_code}", resp.status_code)
        data = resp.json()
        for row in data.get("data", []):
            insights[row["ad_id"]] = parse_video_insight_row(row)

    return insights


def parse_video_insight_row(row: dict) -> dict:
    """Парсит строку инсайтов с видео-метриками.

    Публичная функция — используется как внутри learner, так и внешними модулями
    (например, services/creative_backfill.py в рамках задачи T4).
    Поведение не изменено относительно прежней приватной _parse_video_insight_row.
    """
    spend = float(row.get("spend", 0))
    impressions = int(row.get("impressions", 0))
    clicks = int(row.get("clicks", 0))
    ctr = float(row.get("ctr", 0))
    cpm = float(row.get("cpm", 0))
    frequency = float(row.get("frequency", 0))

    lead_result = parse_meta_lead_actions(row.get("actions"))
    if lead_result.canonical_total is None:
        raise ValueError(f"invalid Meta lead actions: {','.join(lead_result.problems)}")
    if lead_result.problems:
        logger.warning("Meta lead components расходятся: %s", ",".join(lead_result.problems))
    leads = lead_result.canonical_total

    # 3-секундные просмотры из actions
    video_views_3s = 0
    for action in row.get("actions") or []:
        atype = action["action_type"]
        val = int(action["value"])
        if atype == "video_view":
            video_views_3s += val

    # ThruPlay
    thruplay = 0
    for action in row.get("video_thruplay_watched_actions", []):
        thruplay += int(action.get("value", 0))

    # Retention
    def extract_video_metric(field_name):
        total = 0
        for action in row.get(field_name, []):
            total += int(action.get("value", 0))
        return total

    cpl = round(spend / leads, 2) if leads > 0 else 0

    return {
        "spend": round(spend, 2),
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(ctr, 2),
        "cpm": round(cpm, 2),
        "frequency": round(frequency, 2),
        "leads": leads,
        "cpl": cpl,
        "video_views_3s": video_views_3s,
        "thruplay": thruplay,
        "video_p25": extract_video_metric("video_p25_watched_actions"),
        "video_p50": extract_video_metric("video_p50_watched_actions"),
        "video_p75": extract_video_metric("video_p75_watched_actions"),
        "video_p100": extract_video_metric("video_p100_watched_actions"),
    }


def classify_creative(hook_rate: float, hold_rate: float) -> str:
    """
    2×2 матрица классификации креатива.

    Hook высокий + Hold высокий = Winner (масштабировать)
    Hook высокий + Hold низкий  = Clickbait (переделать контент)
    Hook низкий  + Hold высокий = Hidden Gem (сменить хук)
    Hook низкий  + Hold низкий  = Dead (отключить)
    """
    hook_good = hook_rate >= HOOK_RATE_GOOD
    hold_good = hold_rate >= HOLD_RATE_GOOD

    if hook_good and hold_good:
        return "Winner"
    if hook_good and not hold_good:
        return "Clickbait"
    if not hook_good and hold_good:
        return "Hidden Gem"
    return "Dead"


def detect_fatigue(ads: list[dict]) -> list[dict]:
    """
    Определяет креативы с признаками усталости.
    Критерии: высокая частота, низкий CTR при большом числе показов.
    """
    fatigued = []
    for ad in ads:
        if ad["impressions"] < 500:
            continue

        reasons = []
        if ad["frequency"] >= FATIGUE_FREQUENCY:
            reasons.append(f"Частота {ad['frequency']} (норма < {FATIGUE_FREQUENCY})")

        if ad["ctr"] < 0.5 and ad["impressions"] > 1000:
            reasons.append(f"CTR {ad['ctr']}% — очень низкий")

        if reasons:
            fatigued.append({
                "ad_id": ad["ad_id"],
                "ad_name": ad["ad_name"],
                "city": ad["city"],
                "reasons": reasons,
                "frequency": ad["frequency"],
                "ctr": ad["ctr"],
                "cpl": ad["cpl"],
                "spend": ad["spend"],
            })

    return sorted(fatigued, key=lambda x: -x["spend"])


def build_rankings(ads: list[dict]) -> dict:
    """Строит рейтинги по городам, адсетам, классам креативов."""

    def aggregate(group_key: str) -> list[dict]:
        groups = defaultdict(lambda: {
            "spend": 0, "leads": 0, "ads_count": 0,
            "video_views_3s": 0, "thruplay": 0, "impressions": 0,
        })
        for ad in ads:
            key = ad.get(group_key, "Неизвестно")
            if not key:
                continue
            g = groups[key]
            g["spend"] += ad.get("spend", 0)
            g["leads"] += ad.get("leads", 0)
            g["ads_count"] += 1
            g["video_views_3s"] += ad.get("video_views_3s", 0)
            g["thruplay"] += ad.get("thruplay", 0)
            g["impressions"] += ad.get("impressions", 0)

        result = []
        for name, g in groups.items():
            cpl = round(g["spend"] / g["leads"], 2) if g["leads"] > 0 else 0
            hook_rate = round(g["video_views_3s"] / g["impressions"] * 100, 1) if g["impressions"] > 0 else 0
            hold_rate = round(g["thruplay"] / g["video_views_3s"] * 100, 1) if g["video_views_3s"] > 0 else 0

            result.append({
                "name": name,
                "spend": round(g["spend"], 2),
                "leads": g["leads"],
                "cpl": cpl,
                "ads_count": g["ads_count"],
                "hook_rate": hook_rate,
                "hold_rate": hold_rate,
            })

        with_leads = sorted([r for r in result if r["leads"] > 0], key=lambda x: x["cpl"])
        no_leads = sorted([r for r in result if r["leads"] == 0], key=lambda x: -x["spend"])
        return with_leads + no_leads

    return {
        "by_city": aggregate("city"),
        "by_adset_type": aggregate("adset_type"),
        "by_creative_class": aggregate("creative_class"),
    }


def enrich_with_amo(ads: list[dict]) -> list[dict]:
    """Подставляет AMO данные (ROMI, квал%, оплаты) и бизнес-класс."""
    amo = load_amo_data()
    for ad in ads:
        # Поиск по ad_id, затем по имени объявления
        amo_entry = amo.get(ad["ad_id"], {}) or amo.get(ad.get("ad_name", "").lower(), {})
        ad["qual_pct"] = amo_entry.get("qual_pct")
        ad["romi"] = amo_entry.get("romi")
        ad["payments"] = amo_entry.get("payments")
        ad["cpql"] = amo_entry.get("cpql")
        ad["revenue"] = amo_entry.get("revenue")
        ad["qual_leads"] = amo_entry.get("qual_leads")
        ad["business_class"] = classify_business(ad)
    return ads


def build_creative_table(ads: list[dict]) -> list[dict]:
    """Таблица всех креативов с полными метриками для UI."""
    return sorted(
        [
            {
                "ad_id": ad["ad_id"],
                "ad_name": ad["ad_name"],
                "city": ad["city"],
                "adset_type": ad["adset_type"],
                "status": ad["status"],
                "effective_status": ad.get("effective_status", ad["status"]),
                "days_running": ad["days_running"],
                "spend": ad["spend"],
                "leads": ad["leads"],
                "cpl": ad["cpl"],
                "ctr": ad["ctr"],
                "cpm": ad["cpm"],
                "frequency": ad["frequency"],
                # Бизнес (AMO)
                "qual_pct": ad.get("qual_pct"),
                "romi": ad.get("romi"),
                "payments": ad.get("payments"),
                "cpql": ad.get("cpql"),
                "revenue": ad.get("revenue"),
                "qual_leads": ad.get("qual_leads"),
                "business_class": ad.get("business_class", "Нет данных"),
                # Видео
                "hook_rate": ad["hook_rate"],
                "hold_rate": ad["hold_rate"],
                "creative_class": ad["creative_class"],
                "video_views_3s": ad["video_views_3s"],
                "thruplay": ad["thruplay"],
                "video_p25": ad["video_p25"],
                "video_p50": ad["video_p50"],
                "video_p75": ad["video_p75"],
                "video_p100": ad["video_p100"],
            }
            for ad in ads
        ],
        key=lambda x: x["spend"],
        reverse=True,
    )


def generate_hypotheses(rankings: dict, ads: list[dict], fatigued: list[dict]) -> list[dict]:
    """
    Генерирует гипотезы. Порядок приоритета:
    1. Бизнес-метрики (ROMI, квал) — главные
    2. Видео-метрики (Hook/Hold) — объясняют причины
    3. Усталость — операционные улучшения
    """
    hypotheses = []

    # === УМНЫЕ ГИПОТЕЗЫ: Winner vs Loser (делегируем в сервис) ===
    from services.hypotheses import build_smart_hypotheses
    hypotheses.extend(build_smart_hypotheses(ads))

    # === БИЗНЕС-ГИПОТЕЗЫ (ROMI + квал) — первые ===

    # Прибыльные — масштабировать
    profitable = [a for a in ads if a.get("business_class") == "Прибыльный"]
    if profitable:
        best = max(profitable, key=lambda x: x.get("romi", 0))
        hypotheses.append({
            "type": "scale",
            "title": f"ROMI {best.get('romi')}% — масштабировать",
            "description": (
                f"{best['ad_name'][:60]}\n"
                f"ROMI {best.get('romi')}%, квал {best.get('qual_pct', '?')}%, "
                f"оплат: {best.get('payments', 0)}. CPL ${best['cpl']}, {best['leads']} лидов.\n"
                f"Создать похожие креативы и увеличить бюджет."
            ),
            "confidence": "высокая",
            "priority": "HIGH",
            "ad_id": best["ad_id"],
        })

    # Убыточные с большим расходом — сократить
    unprofitable = [a for a in ads if a.get("business_class") == "Убыточный" and a["spend"] > 20]
    for u in sorted(unprofitable, key=lambda x: -x["spend"])[:2]:
        hypotheses.append({
            "type": "reduce",
            "title": f"ROMI {u.get('romi')}% — убыточный",
            "description": (
                f"{u['ad_name'][:60]}\n"
                f"ROMI {u.get('romi')}%, квал {u.get('qual_pct', '?')}%, "
                f"расход ${u['spend']}. Рассмотреть паузу."
            ),
            "confidence": "высокая",
            "priority": "HIGH",
            "ad_id": u["ad_id"],
        })

    # Низкая квалификация — проблема не в рекламе, а в аудитории
    low_qual = [a for a in ads if a.get("business_class") == "Низкая квал" and a["leads"] >= 3]
    for lq in low_qual[:2]:
        hypotheses.append({
            "type": "investigate",
            "title": f"Квал {lq.get('qual_pct')}% — проверить аудиторию",
            "description": (
                f"{lq['ad_name'][:60]}\n"
                f"Квал {lq.get('qual_pct')}%, {lq['leads']} лидов, CPL ${lq['cpl']}.\n"
                f"Лиды идут, но качество низкое. Проверить таргетинг и лид-форму."
            ),
            "confidence": "средняя",
            "priority": "MED",
            "ad_id": lq["ad_id"],
        })

    # Нет AMO данных — напоминание
    no_amo = [a for a in ads if a.get("business_class") == "Нет данных" and a["leads"] >= 3]
    if no_amo:
        count = len(no_amo)
        hypotheses.append({
            "type": "investigate",
            "title": f"{count} объявлений без AMO данных",
            "description": (
                f"У {count} объявлений с лидами нет данных по ROMI/квал.\n"
                f"Заполните AMO данные в разделе Аналитика для точного анализа."
            ),
            "confidence": "высокая",
            "priority": "LOW",
        })

    # === ВИДЕО-ГИПОТЕЗЫ (Hook/Hold) — вторые ===

    # Winners — масштабировать
    winners = [a for a in ads if a["creative_class"] == "Winner" and a["leads"] >= 3]
    if winners:
        best = min(winners, key=lambda x: x["cpl"])
        # Не дублируем если уже есть в прибыльных
        if not any(h.get("ad_id") == best["ad_id"] for h in hypotheses):
            hypotheses.append({
                "type": "scale",
                "title": "Winner по видео-метрикам",
                "description": (
                    f"{best['ad_name'][:60]}\n"
                    f"Hook {best['hook_rate']}%, Hold {best['hold_rate']}%, "
                    f"CPL ${best['cpl']}, {best['leads']} лидов."
                ),
                "confidence": "высокая" if best["leads"] >= 10 else "средняя",
                "priority": "MED",
                "ad_id": best["ad_id"],
            })

    # Hidden Gems — сменить хук
    hidden_gems = [a for a in ads if a["creative_class"] == "Hidden Gem" and a["spend"] > 10]
    for gem in hidden_gems[:2]:
        hypotheses.append({
            "type": "experiment",
            "title": "Новый хук нужен",
            "description": (
                f"{gem['ad_name'][:60]}\n"
                f"Hold {gem['hold_rate']}% — контент удерживает. "
                f"Hook {gem['hook_rate']}% — хук не цепляет."
            ),
            "confidence": "средняя",
            "priority": "MED",
            "ad_id": gem["ad_id"],
        })

    # Clickbait — контент не работает
    clickbaits = [a for a in ads if a["creative_class"] == "Clickbait" and a["spend"] > 10]
    for cb in clickbaits[:2]:
        hypotheses.append({
            "type": "experiment",
            "title": "Переделать контент",
            "description": (
                f"{cb['ad_name'][:60]}\n"
                f"Hook {cb['hook_rate']}% — хук работает. "
                f"Hold {cb['hold_rate']}% — зрители уходят."
            ),
            "confidence": "средняя",
            "priority": "MED",
            "ad_id": cb["ad_id"],
        })

    # Города — разрыв в CPL
    cities = [c for c in rankings["by_city"] if c["leads"] >= 3]
    if len(cities) >= 2:
        best_city = cities[0]
        worst_city = cities[-1]
        if worst_city["cpl"] > best_city["cpl"] * 1.5:
            hypotheses.append({
                "type": "investigate",
                "title": f"{best_city['name']} vs {worst_city['name']}",
                "description": (
                    f"{best_city['name']}: CPL ${best_city['cpl']}, "
                    f"Hook {best_city['hook_rate']}%.\n"
                    f"{worst_city['name']}: CPL ${worst_city['cpl']}, "
                    f"Hook {worst_city['hook_rate']}%."
                ),
                "confidence": "высокая",
                "priority": "LOW",
            })

    # === ОПЕРАЦИОННЫЕ ГИПОТЕЗЫ — третьи ===

    # Усталые креативы
    for f in fatigued[:3]:
        hypotheses.append({
            "type": "reduce",
            "title": "Усталость креатива",
            "description": (
                f"{f['ad_name'][:60]}\n"
                f"{', '.join(f['reasons'])}. Расход ${f['spend']}."
            ),
            "confidence": "средняя",
            "priority": "MED",
            "ad_id": f["ad_id"],
        })

    # Dead с большим расходом
    dead_costly = [a for a in ads if a["creative_class"] == "Dead" and a["spend"] > 30 and a["leads"] == 0]
    for dead in sorted(dead_costly, key=lambda x: -x["spend"])[:3]:
        hypotheses.append({
            "type": "reduce",
            "title": "Отключить Dead",
            "description": (
                f"{dead['ad_name'][:60]}\n"
                f"Расход ${dead['spend']}, 0 лидов. "
                f"Hook {dead['hook_rate']}%, Hold {dead['hold_rate']}%."
            ),
            "confidence": "высокая",
            "priority": "MED",
            "ad_id": dead["ad_id"],
        })


    # === СЕЗОННЫЕ ГИПОТЕЗЫ (исторические данные) — четвёртые ===
    try:
        from integrations.gsheets import get_seasonal_insights
        seasonal = get_seasonal_insights()
        for ins in seasonal:
            if ins['type'] == 'seasonal':
                hypotheses.append({
                    'type': 'investigate',
                    'title': ins['title'],
                    'description': ins['description'],
                    'confidence': 'средняя',
                    'priority': 'LOW',
                })
            elif ins['type'] == 'seasonal_comparison':
                hypotheses.append({
                    'type': 'investigate',
                    'title': ins['title'],
                    'description': ins['description'],
                    'confidence': 'средняя',
                    'priority': 'LOW',
                })
            elif ins['type'] == 'trend':
                priority = 'MED' if abs(ins.get('change_pct', 0)) > 20 else 'LOW'
                hypotheses.append({
                    'type': 'investigate',
                    'title': ins['title'],
                    'description': ins['description'],
                    'confidence': 'средняя',
                    'priority': priority,
                })
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('Сезонные данные недоступны: %s', e)

    return hypotheses


def run_learner(days: int = 30) -> dict:
    """Полный цикл обучения."""
    ads = get_creative_metrics(days)

    # Подставляем AMO данные (ROMI, квал, оплаты) — главный уровень анализа
    ads = enrich_with_amo(ads)

    rankings = build_rankings(ads)
    fatigued = detect_fatigue(ads)
    hypotheses = generate_hypotheses(rankings, ads, fatigued)
    creative_table = build_creative_table(ads)

    # Статистика по видео-классам (2×2 матрица)
    class_counts = defaultdict(int)
    for ad in ads:
        class_counts[ad["creative_class"]] += 1

    # Статистика по бизнес-классам (ROMI + квал)
    business_counts = defaultdict(int)
    for ad in ads:
        business_counts[ad.get("business_class", "Нет данных")] += 1

    result = {
        # Бизнес-анализ (первый)
        "business_distribution": dict(business_counts),
        # Видео-анализ (второй)
        "class_distribution": dict(class_counts),
        "rankings": rankings,
        "hypotheses": hypotheses,
        "fatigued": fatigued,
        "creative_table": creative_table,
        # Общие
        "total_ads": len(ads),
        "total_spend": round(sum(a.get("spend", 0) for a in ads), 2),
        "total_leads": sum(a.get("leads", 0) for a in ads),
        "total_payments": sum(a.get("payments", 0) or 0 for a in ads),
        "period_days": days,
        "generated_at": datetime.now().isoformat(),
    }

    # Сохраняем результат
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LEARNER_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return result
