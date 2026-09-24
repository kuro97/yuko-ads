"""
Creative Briefs v2 — конвейер креативных брифов.
Данные → анализ контента → инсайты → полные брифы → Trello.

Каждый бриф = готовое ТЗ для съёмки из 10 полей.
"""
import re
import json
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# Таксономия хуков для рекламы сервиса
HOOK_TYPES = {
    "problem_callout": "Вопрос-проблема",
    "shocking_stat": "Шокирующая статистика",
    "transformation": "История трансформации",
    "fear": "Страх последствий",
    "insider": "Инсайдерское знание",
    "testimonial": "Отзыв клиента",
    "demo": "Разбор кейса",
    "urgency": "Срочность/дедлайн",
}

ANGLES = {
    "transformation": "Трансформация (до/после)",
    "fear": "Страх / последствия",
    "social_proof": "Социальное доказательство",
    "insider": "Инсайдерское знание",
    "urgency": "Срочность",
    "authority": "Авторитет / методика",
    "value_first": "Ценность сразу (демо)",
}

# Паттерны для определения темы/угла из названия креатива.
# ПРИМЕР таксономии — замените на темы своего аккаунта. Ключ — подстрока
# названия креатива в нижнем регистре, значение — название угла. Порядок
# важен: побеждает первое совпадение ("рассроч" стоит раньше "срочн", иначе
# «рассрочной» ловилось бы как срочность).
TOPIC_PATTERNS = {
    "результат": "Результат / Показатели",
    "до-после": "До-После трансформация",
    "страх": "Страх ошибки",
    "prodb": "Продукт B",
    "proda": "Продукт A",
    "ugc": "UGC от клиентов",
    "отзыв": "Отзыв клиента",
    "сторител": "Сторителлинг",
    "консультац": "Бесплатная консультация",
    "карусель": "Карусель",
    "рассроч": "Рассрочка",
    "срочн": "Срочность / Дедлайн",
}


def extract_city(ad_name: str) -> str:
    """Извлекает город из имени рекламы."""
    parts = ad_name.split("|")
    if len(parts) >= 2:
        city = parts[0].strip()
        # Убираем лишнее (CR001, итд)
        if "/" in city:
            city = city.split("/")[0].strip()
        return city
    return "?"


def extract_topic(ad_name: str) -> str:
    """Извлекает тему из имени рекламы. Паттерн: 'Город | Тема / Подтема'."""
    parts = ad_name.split("|")
    if len(parts) >= 2:
        topic = parts[1].strip()
        return topic
    return ad_name.strip()


def detect_angle(ad_name: str) -> str:
    """Определяет угол/тему рекламы по названию."""
    name_lower = ad_name.lower()
    for pattern, angle in TOPIC_PATTERNS.items():
        if pattern in name_lower:
            return angle
    return "Другое"


# Порог качества — та же сигнатура confirmed_waster что и в decision_policy.py
# (min_qual_pct=10.0): payments==0 и qual_pct<10% = подтверждённый слив,
# такую рекламу/угол нельзя предлагать как "победителя" для масштабирования.
_MIN_QUAL_PCT_THRESHOLD = 10.0
# Если квал недоступен (None) — не блокируем автоматически (нет данных ≠
# плохо), но требуем минимальный сигнал: leads>=3, иначе тоже не победитель.
_MIN_LEADS_WITHOUT_QUAL_DATA = 3


def _is_confirmed_waster(payments, qual_pct) -> bool:
    """Проверяет сигнатуру подтверждённого слива: 0 оплат + низкий квал.

    Симметрично confirmed_waster из services/decision_policy.py (tier B):
    payments == 0 AND qual_pct < 10%. Если qual_pct is None — считается
    "нет данных", а не "плохо", поэтому waster'ом это НЕ помечаем здесь
    (для этого случая на верхнем уровне используется отдельная проверка
    минимального количества лидов — см. _passes_quality_gate).
    """
    if payments != 0:
        return False
    if qual_pct is None:
        return False
    return float(qual_pct) < _MIN_QUAL_PCT_THRESHOLD


def _passes_quality_gate(ad: dict) -> bool:
    """Победитель не может быть выбран только по дешёвому CPL — нужен
    хоть какой-то сигнал качества (оплаты или квал), либо достаточно
    лидов если квал недоступен.

    Возвращает False (не победитель) если:
    - подтверждённый слив (payments==0 и qual_pct<10%)
    - квал недоступен (None) и лидов меньше минимального порога
    """
    payments = ad.get("payments")
    qual_pct = ad.get("qual_pct")

    if _is_confirmed_waster(payments, qual_pct):
        return False

    if qual_pct is None and ad.get("leads", 0) < _MIN_LEADS_WITHOUT_QUAL_DATA:
        return False

    return True


def analyze_winners(ads: list[dict]) -> dict:
    """Анализирует победителей и проигравших.
    Возвращает структурированный анализ по темам, углам, городам."""

    # Фильтруем рекламы с данными
    with_data = [a for a in ads if a.get("spend", 0) > 0]
    if not with_data:
        return {"winners": [], "losers": [], "by_angle": {}, "by_city": {}}

    # Определяем победителей (CPL < медианы, есть лиды)
    with_leads = [a for a in with_data if a.get("leads", 0) > 0]
    if not with_leads:
        return {"winners": [], "losers": [], "by_angle": {}, "by_city": {}}

    cpls = sorted([a.get("cpl", 999) for a in with_leads])
    median_cpl = cpls[len(cpls) // 2] if cpls else 10

    winners = []
    losers = []
    for ad in with_leads:
        ad_info = {
            "name": ad.get("name", ad.get("ad_name", "")),
            "city": extract_city(ad.get("name", ad.get("ad_name", ""))),
            "topic": extract_topic(ad.get("name", ad.get("ad_name", ""))),
            "angle": detect_angle(ad.get("name", ad.get("ad_name", ""))),
            "cpl": ad.get("cpl", 0),
            "leads": ad.get("leads", 0),
            "spend": ad.get("spend", 0),
            "hook_rate": ad.get("hook_rate", 0),
            "hold_rate": ad.get("hold_rate", 0),
            "frequency": ad.get("frequency", 0),
            "days_running": ad.get("days_running", 0),
            "qual_pct": ad.get("qual_pct"),
            "payments": ad.get("payments"),
            # target_product из creative_kb (канон продукта после бэкфилла T2,
            # ARCH-product-tags.md) — прокидывается дальше в teardown-тему,
            # чтобы Сценарист v2 знал продукт победителя без keyword-гадания.
            # Ключа может не быть во входных ads (старые вызовы/тесты) — .get()
            # безопасно даёт None, дальше по цепочке решает classify_product.
            "target_product": ad.get("target_product"),
        }
        # Победитель = дешёвый CPL И (оплаты/квал подтверждают ИЛИ нет данных
        # но есть минимум лидов). Дешевизна одна не даёт права на "победитель" —
        # именно так генератор ТЗ раньше выбирал мёртвые темы
        # (дешёвый CPL, но квал около нуля и ни одной оплаты).
        if ad_info["cpl"] <= median_cpl and _passes_quality_gate(ad_info):
            winners.append(ad_info)
        elif ad_info["cpl"] > median_cpl * 2:
            losers.append(ad_info)

    winners.sort(key=lambda x: x["cpl"])
    losers.sort(key=lambda x: x["cpl"], reverse=True)

    # Анализ по углам
    by_angle = defaultdict(lambda: {
        "ads": 0, "leads": 0, "spend": 0, "winners": 0, "cpls": [],
        "qual_pcts": [], "total_payments": 0,
    })
    for ad in with_leads:
        angle = detect_angle(ad.get("name", ad.get("ad_name", "")))
        by_angle[angle]["ads"] += 1
        by_angle[angle]["leads"] += ad.get("leads", 0)
        by_angle[angle]["spend"] += ad.get("spend", 0)
        by_angle[angle]["cpls"].append(ad.get("cpl", 0))
        qual_pct = ad.get("qual_pct")
        if qual_pct is not None:
            by_angle[angle]["qual_pcts"].append(float(qual_pct))
        payments = ad.get("payments")
        if payments is not None:
            by_angle[angle]["total_payments"] += payments
        if ad.get("cpl", 999) <= median_cpl:
            by_angle[angle]["winners"] += 1

    for angle, data in by_angle.items():
        data["avg_cpl"] = round(sum(data["cpls"]) / len(data["cpls"]), 2) if data["cpls"] else 0
        data["win_rate"] = round(data["winners"] / data["ads"] * 100) if data["ads"] else 0
        # avg_qual_pct — среднее по объявлениям угла, где квал известен;
        # None если ни у одного объявления угла нет данных по кваку
        data["avg_qual_pct"] = (
            round(sum(data["qual_pcts"]) / len(data["qual_pcts"]), 1)
            if data["qual_pcts"] else None
        )
        # cpls/qual_pcts — служебные списки для расчёта, наружу не нужны
        del data["qual_pcts"]

    # Анализ по городам
    by_city = defaultdict(lambda: {"ads": 0, "leads": 0, "spend": 0, "best_angle": "", "best_cpl": 999, "angles": defaultdict(list)})
    for ad in with_leads:
        city = extract_city(ad.get("name", ad.get("ad_name", "")))
        angle = detect_angle(ad.get("name", ad.get("ad_name", "")))
        cpl = ad.get("cpl", 0)
        by_city[city]["ads"] += 1
        by_city[city]["leads"] += ad.get("leads", 0)
        by_city[city]["spend"] += ad.get("spend", 0)
        by_city[city]["angles"][angle].append(cpl)
        if cpl < by_city[city]["best_cpl"]:
            by_city[city]["best_cpl"] = cpl
            by_city[city]["best_angle"] = angle

    return {
        "winners": winners[:10],
        "losers": losers[:5],
        "by_angle": dict(by_angle),
        "by_city": dict(by_city),
        "median_cpl": median_cpl,
    }


def _angle_is_dead(data: dict) -> bool:
    """Угол "не конвертит" — дёшево по CPL, но 0 оплат и низкий квал.

    Ровно случай "Карусель": total_payments==0 и avg_qual_pct<10%
    (когда avg_qual_pct известен). Если avg_qual_pct is None (нет данных
    ни у одного объявления угла) — не считаем угол мёртвым, недостаточно
    информации для такого вывода.
    """
    if data.get("total_payments", 0) != 0:
        return False
    avg_qual_pct = data.get("avg_qual_pct")
    if avg_qual_pct is None:
        return False
    return avg_qual_pct < _MIN_QUAL_PCT_THRESHOLD


def build_content_insights(analysis: dict) -> list[dict]:
    """Строит инсайты с анализом СОДЕРЖАНИЯ, не только цифр."""
    insights = []

    # 1. Лучшие углы
    by_angle = analysis.get("by_angle", {})
    angle_list = sorted(by_angle.items(), key=lambda x: x[1].get("avg_cpl", 999))
    for angle, data in angle_list[:3]:
        if data["leads"] >= 2:
            insights.append({
                "type": "angle",
                "icon": "🎯",
                "title": f"Угол \"{angle}\" работает лучше всех",
                "description": f"CPL ${data['avg_cpl']}, {data['leads']} лидов, "
                               f"{data['win_rate']}% победителей. "
                               f"{data['ads']} объявлений используют этот подход.",
                "data": {"angle": angle, "avg_cpl": data["avg_cpl"], "leads": data["leads"]},
            })

    # 2. Лучшие города + что в них работает
    by_city = analysis.get("by_city", {})
    for city, data in sorted(by_city.items(), key=lambda x: x[1].get("best_cpl", 999)):
        if data["leads"] >= 3:
            insights.append({
                "type": "city",
                "icon": "📍",
                "title": f"{city}: лучше всего \"{data['best_angle']}\"",
                "description": f"Лучший CPL ${data['best_cpl']:.2f} через угол \"{data['best_angle']}\". "
                               f"Всего {data['leads']} лидов, расход ${data['spend']:.0f}.",
                "data": {"city": city, "best_angle": data["best_angle"], "best_cpl": data["best_cpl"]},
            })

    # 3. Что у победителей общего
    winners = analysis.get("winners", [])
    if len(winners) >= 3:
        winner_angles = defaultdict(int)
        for w in winners:
            winner_angles[w["angle"]] += 1
        top_angle = max(winner_angles, key=winner_angles.get) if winner_angles else None
        if top_angle:
            count = winner_angles[top_angle]
            insights.append({
                "type": "pattern",
                "icon": "💡",
                "title": f"{count} из {len(winners)} победителей используют \"{top_angle}\"",
                "description": f"Это главный паттерн успеха. Новые креативы должны "
                               f"использовать этот угол в первую очередь.",
            })

    # 4. Усталость
    fatigued_count = len([w for w in analysis.get("winners", []) + analysis.get("losers", [])
                          if w.get("frequency", 0) > 2.5])
    if fatigued_count:
        insights.append({
            "type": "fatigue",
            "icon": "😴",
            "title": f"{fatigued_count} креативов устали (частота >2.5)",
            "description": "Аудитория видит их слишком часто. Нужны свежие версии тех же тем.",
        })

    return insights


def build_creative_briefs(analysis: dict, insights: list[dict]) -> list[dict]:
    """Генерирует 10-польные креативные брифы на основе анализа.
    Возвращает данные для LLM-генерации полных сценариев."""
    briefs = []
    winners = analysis.get("winners", [])
    by_angle = analysis.get("by_angle", {})
    by_city = analysis.get("by_city", {})
    all_cities = list(by_city.keys())

    # 1. Масштабирование лучших углов в новые города
    for angle, data in sorted(by_angle.items(), key=lambda x: x[1].get("avg_cpl", 999)):
        if data["avg_cpl"] > 10 or data["leads"] < 2:
            continue
        # Угол дёшев по CPL, но не конвертит (0 оплат, квал<10%) — не
        # предлагаем его на масштабирование в новые города
        if _angle_is_dead(data):
            continue
        # Города где этот угол ещё не запущен
        angle_cities = set()
        for city, cdata in by_city.items():
            if angle in cdata.get("angles", {}):
                angle_cities.add(city)
        missing = set(all_cities) - angle_cities
        for city in sorted(missing)[:2]:
            ref_winners = [w for w in winners if w["angle"] == angle][:2]
            briefs.append({
                "type": "expand",
                "priority": "HIGH" if data["avg_cpl"] < 5 else "MED",
                "hypothesis": f"Запустить \"{angle}\" в {city}",
                "rationale": f"Угол даёт CPL ${data['avg_cpl']} в других городах ({data['leads']} лидов). В {city} ещё нет.",
                "audience": f"Потенциальные клиенты в {city}. Выбирают между PRODA и PRODB.",
                "message": _generate_message(angle),
                "hook_direction": _generate_hook(angle),
                "angle": angle,
                "emotion": _detect_emotion(angle),
                "tone": "UGC, тёплый, от лица клиента" if "ugc" in angle.lower() or "сторител" in angle.lower() else "Авторитетный но не холодный",
                "format": "Reels 9:16, 20-30 сек, субтитры обязательны",
                "references": [{"name": w["name"], "cpl": w["cpl"], "leads": w["leads"]} for w in ref_winners],
                "city": city,
                "topic": angle,
            })

    # 2. Масштабирование топ тем (больше вариантов)
    top_angles = sorted(by_angle.items(), key=lambda x: x[1].get("avg_cpl", 999))[:3]
    for angle, data in top_angles:
        # Тот же гейт качества: не предлагать масштабирование мёртвой темы
        if _angle_is_dead(data):
            continue
        if data["leads"] >= 5 and data["avg_cpl"] < 6:
            ref_winners = [w for w in winners if w["angle"] == angle][:3]
            briefs.append({
                "type": "scale",
                "priority": "HIGH",
                "hypothesis": f"Сделать 3 новых варианта \"{angle}\"",
                "rationale": f"CPL ${data['avg_cpl']}, {data['leads']} лидов. Тема работает — нужно больше вариантов для ротации.",
                "audience": "Потенциальные клиенты, все города",
                "message": _generate_message(angle),
                "hook_direction": _generate_hook(angle),
                "angle": angle,
                "emotion": _detect_emotion(angle),
                "tone": "Как в референсах",
                "format": "Reels 9:16, 20-30 сек, субтитры обязательны",
                "references": [{"name": w["name"], "cpl": w["cpl"], "leads": w["leads"]} for w in ref_winners],
                "city": "все города",
                "topic": angle,
            })

    # 3. Обновление уставших
    all_ads = winners + analysis.get("losers", [])
    fatigued = [a for a in all_ads if a.get("frequency", 0) > 2.5 and a.get("spend", 0) > 5]
    for ad in fatigued[:3]:
        briefs.append({
            "type": "refresh",
            "priority": "MED",
            "hypothesis": f"Обновить \"{ad['topic']}\" для {ad['city']}",
            "rationale": f"Частота {ad['frequency']:.1f} — аудитория устала. CPL ${ad['cpl']}. Нужна свежая версия.",
            "audience": f"Потенциальные клиенты в {ad['city']}",
            "message": _generate_message(ad["angle"]),
            "hook_direction": "Новый хук на ту же тему — другой формат или персонаж",
            "angle": ad["angle"],
            "emotion": _detect_emotion(ad["angle"]),
            "tone": "Свежий, отличающийся от текущего",
            "format": "Reels 9:16, 20-30 сек, субтитры",
            "references": [{"name": ad["name"], "cpl": ad["cpl"], "leads": ad["leads"]}],
            "city": ad["city"],
            "topic": ad["angle"],
        })

    # Сортировка: HIGH первые
    priority_order = {"HIGH": 0, "MED": 1, "LOW": 2}
    briefs.sort(key=lambda b: priority_order.get(b["priority"], 9))

    return briefs[:10]  # Макс 10 брифов


# Синтетические примеры формата ТЗ — эталон стиля и структуры (few-shot).
# Персонажи, сюжет и детали вымышлены; LLM копирует уровень живости
# и конкретики, а не сам текст.
_EXAMPLE_SPEAKER_MONOLOGUE = """Название карточки: "Менеджер Антон / Непонятная цена / Всё на одной странице"
Текст:
**Спикер / Непонятная цена / Всё на одной странице**

**(Хук):** Вы откладываете не решение — вы откладываете разговор о деньгах.

Многие клиенты месяцами держат вкладку с сервисом открытой и так и не звонят. Не потому, что сервис не нужен, а потому что непонятно, сколько в итоге придётся заплатить и что всплывёт уже после договора.

В ACME за каждым клиентом закреплён личный менеджер. На первой встрече он раскладывает стоимость по строкам: что входит в пакет, что нет и сколько стоит каждый шаг. Никаких «уточним позже».

Когда цена понятна, решение занимает один вечер, а не полгода. Начните с бесплатной консультации — посчитаем вместе."""

_EXAMPLE_STORYTELLING = """Название карточки: "Дмитрий / Полгода «подумаю» / Одна смета вместо десяти звонков"
Текст:
Сценарий:

Полгода я говорил себе «подумаю на выходных». У меня своя мастерская, двое сотрудников и вечная нехватка времени. Задача висела давно: снять с себя дела, которые я тащил в одиночку. Но каждый раз, открыв сайты похожих сервисов, я закрывал их через пять минут.

Везде было одно и то же: «от» и «цена по запросу».

Сначала я попробовал сравнить сам. Завёл таблицу, выписал несколько компаний. К вечеру в таблице были названия и ни одной понятной цифры.

Потом я обзвонил пару из них.

В первой мне полчаса рассказывали про «индивидуальный подход», а про деньги сказали: «Это обсудим на встрече». Во второй назвали сумму, а через неделю прислали счёт, где она заметно выросла — «дополнительные работы».

После этого я решил, что проще уж самому. «Так дешевле выйдет», — сказал я жене. Она молча посмотрела на часы: была глубокая ночь, а я всё ещё сидел над бумагами.

Про ACME мне рассказал поставщик. Сказал коротко: «Там сразу показывают, за что платишь».

Я пришёл на бесплатную консультацию с готовой фразой: «Сразу скажите, сколько это будет стоить в итоге».

Менеджер не стал уходить от ответа. Он открыл смету и прошёлся со мной по каждой строке: что входит в пакет, что можно не брать и где я сэкономлю, если часть сделаю сам.

И тут я поймал себя на странной мысли.

Всё это время я откладывал не из-за денег. Сумма оказалась вполне подъёмной. Я откладывал, потому что не понимал, во что ввязываюсь. Неизвестность пугала сильнее любой цифры.

Мы договорились начать с малого. Менеджер каждую неделю присылал короткий отчёт, а в личном кабинете я видел, какой этап закрыт и сколько он стоил. Ни одной строки, о которой меня не предупредили заранее.

Прошло три месяца.

Вечерами я больше не сижу над бумагами. Сотрудники знают, к кому идти с вопросами, а я знаю, сколько трачу и за что.

Жена как-то спросила: «Ну что, дорого вышло?»

Я ответил: «Дорого было полгода откладывать».

Если вы тоже месяцами держите вкладку открытой и не звоните, потому что непонятно, сколько это будет стоить, — запишитесь на бесплатную консультацию по кнопке ниже. Запись на этот месяц скоро закроется."""


def build_llm_prompt(brief: dict) -> str:
    """Строит промпт для LLM из брифа — генерация ОДНОГО цельного сценария.

    В отличие от старой версии, не просит бюрократическую таблицу полей
    (Хук/Угол/Эмоция/Тон/Формат) и не просит 3 варианта — только один
    живой сценарий в стиле примеров ТЗ (см. few-shot примеры).
    """
    refs = brief.get("references", [])
    refs_text = ""
    if refs:
        refs_text = "\n\nРеференсы (наши лучшие ролики на эту тему — что уже сработало):\n"
        for r in refs:
            refs_text += f"- {r['name']} — CPL ${r['cpl']}, {r['leads']} лидов\n"

    return f"""Ты — креативный директор сервиса ACME (продуктовые линейки PRODA и PRODB). Ты пишешь ТЗ для видеорекламы (Reels) так, как их пишет владелец бизнеса — не маркетолог с шаблонами, а человек, который сам придумывает живые сценарии.

Вот два примера ТЗ — это эталон стиля, тона и формата. Копируй именно такой уровень живости, конкретики и структуры, НЕ копируй сам текст и детали дословно (это должен быть новый сценарий):

=== ПРИМЕР 1: спикер-монолог (короткий, ~600-800 символов) ===
{_EXAMPLE_SPEAKER_MONOLOGUE}

=== ПРИМЕР 2: сторителлинг-сценарий (длинный, ~2000-3500 символов) ===
{_EXAMPLE_STORYTELLING}

=== ЧТО ОБЩЕГО У ОБОИХ ПРИМЕРОВ (обязательно повтори) ===
- Это ЦЕЛЬНЫЙ ЖИВОЙ ТЕКСТ от первого лица, а НЕ таблица полей. Никаких заголовков вида "Аудитория:", "Сообщение:", "Эмоция:", "Тон:", "Формат:", "Тип:", "Приоритет:" — это бюрократия, она запрещена.
- Разговорный русский, без маркетингового буллшита ("уникальная методика", "инновационный подход" и т.п. — запрещены).
- Конкретные детали: бытовые подробности (сколько времени клиент откладывает задачу, что видно в личном кабинете), термины (PRODA, PRODB), диалоги в кавычках-ёлочках «...».
- В сторителлинге — трансформация по дуге: проблема → неудачные попытки решить → ACME → инсайт/трансформация → мягкий CTA с триггером срочности в конце (например "запись на этот месяц скоро закроется").
- В спикер-монологе — короткий и прямой: одна пометка "(Хук):" в начале, дальше 3-4 абзаца сплошного текста от лица менеджера/спикера/эксперта, без диалогов.

ЗАДАЧА: Напиши ОДИН цельный сценарий (только один — не 3 варианта) для следующего брифа.

ДАННЫЕ БРИФА (используй как основу, не вставляй как есть):
- Гипотеза: {brief['hypothesis']}
- Аудитория: {brief['audience']}
- Главное сообщение: {brief['message']}
- Направление хука: {brief['hook_direction']}
- Угол: {brief['angle']}
- Эмоция: {brief['emotion']}
- Город: {brief.get('city', 'все города')}
{refs_text}
ВЫБЕРИ ЖАНР САМ (в зависимости от угла и эмоции брифа):
- Сторителлинг-сценарий — если тема эмоциональная: страх ошибки, трансформация "было/стало", тревога клиента, история клиента.
- Спикер-монолог — если тема более прямая: оффер, авторитет методики, делегирование контроля, конкретное преимущество.

Можешь выдумывать правдоподобные детали в духе ACME: конкретный эксперт/менеджер, пакет услуг, бесплатная консультация, PRODA/PRODB, личный кабинет, конкретный город из брифа.

ФОРМАТ ОТВЕТА (строго):
- Верни ТОЛЬКО текст сценария — без вступлений вроде "Вот сценарий:", без markdown-заголовков, без нумерации полей, без пояснений от себя.
- Начни сразу с "(Хук):" (для спикер-монолога) или "Сценарий:" (для сторителлинга) — как в примерах.
- Пиши на русском."""


def format_brief_for_trello(brief: dict, scenario: str) -> dict:
    """Форматирует бриф в Trello-карточку.

    Тело карточки — ТОЛЬКО сгенерированный сценарий (текст целиком, как
    его написала LLM). Никакой бюрократической структуры полей.
    Внизу — одна короткая служебная строка курсивом для трекинга
    происхождения карточки (референс-креатив, CPL).

    Args:
        brief: словарь брифа (из build_creative_briefs)
        scenario: непустой текст сценария, сгенерированный LLM

    Returns:
        {"name": ..., "desc": ...} для create_card

    Raises:
        ValueError: если scenario пустой — карточку с пустым сценарием
            создавать нельзя (лучше 0 карточек, чем бюрократический мусор)
    """
    if not scenario or not scenario.strip():
        raise ValueError("format_brief_for_trello: scenario пустой — карточку создавать нельзя")

    # Служебная строка для трекинга — референс с лучшим CPL, если есть
    refs = brief.get("references", [])
    tracking_line = f"*Авто-ТЗ · угол «{brief.get('angle', '?')}»*"
    if refs:
        best_ref = min(refs, key=lambda r: r.get("cpl", 999))
        tracking_line = (
            f"*Авто-ТЗ · CPL ${best_ref.get('cpl', '?')} · "
            f"референс: {best_ref.get('name', '?')} ({best_ref.get('leads', '?')} лидов)*"
        )

    body = f"{scenario.strip()}\n\n---\n{tracking_line}"

    # Название карточки — коротко и по-человечески, без техпрефикса.
    # Формат "Тема / Угол" — как у карточек, написанных вручную.
    topic = brief.get("topic") or brief.get("angle", "ACME")
    city = brief.get("city", "")
    name = f"{topic} / {brief.get('angle', '')}" if topic != brief.get("angle") else topic
    if city and city != "все города":
        name = f"{city} / {name}"

    return {
        "name": name,
        "desc": body,
    }


def _generate_message(angle: str) -> str:
    """Генерирует главное сообщение по углу."""
    messages = {
        "Результат / Показатели": "Ваш результат может быть лучше — мы знаем как",
        "До-После трансформация": "За 3 месяца от хаоса к системе — реальные результаты клиентов",
        "Страх ошибки": "Не дайте страху ошибки остановить вас на старте",
        "Продукт B": "Сезон PRODB короткий — каждая неделя на счету",
        "Продукт A": "PRODA — пакет услуг с персональным менеджером",
        "UGC от клиентов": "Реальные клиенты рассказывают о результатах",
        "Отзыв клиента": "Клиенты своими словами — что изменилось после ACME",
        "Сторителлинг": "История одного клиента, который не верил в результат",
        "Бесплатная консультация": "Бесплатная консультация покажет, с чего начать именно вам",
        "Рассрочка": "Рассрочка банка-партнёра на 12 месяцев — без большого платежа сразу",
        "Срочность / Дедлайн": "У менеджера не больше 10 клиентов — свободных мест в этом месяце немного, запишитесь сейчас",
    }
    return messages.get(angle, "ACME — PRODA и PRODB с понятным результатом")


def _generate_hook(angle: str) -> str:
    """Генерирует направление хука по углу."""
    hooks = {
        "Результат / Показатели": "Клиент смотрит на график в личном кабинете. Голос: 'Результат снова не сдвинулся?'",
        "До-После трансформация": "Скриншот слабых показателей → через 3 секунды скриншот сильных",
        "Страх ошибки": "Крупный план встревоженного клиента. 'Что будет, если снова не получится?'",
        "Продукт B": "Календарь с обведённой датой конца сезона. 'До конца сезона PRODB осталось...'",
        "Продукт A": "Офис ACME. 'Места на PRODA в этом сезоне ограничены'",
        "UGC от клиентов": "Клиентка в камеру: 'Я не верила, что это поможет, но...'",
        "Отзыв клиента": "Клиент в камеру: 'Раньше я боялся начинать, а теперь...'",
        "Сторителлинг": "Клиентка за столом, уставшая. Голос: 'Когда Анна пришла к нам...'",
        "Бесплатная консультация": "Короткий чек-лист на экране. 'Знаете, с чего начать именно вам?'",
        "Рассрочка": "Клиент с калькулятором. 'Без большого платежа сразу — так можно?'",
        "Срочность / Дедлайн": "Чат с сообщениями записи. 'Места заканчиваются — 3 из 10 осталось'",
    }
    return hooks.get(angle, "Прямой вопрос клиенту о его цели")


def _detect_ad_format(ref_name: str) -> str:
    """Определяет формат референса по имени объявления.

    Обёртка над scenario_formats.detect_format_from_reference (T2, wave 1,
    тот же файл создаётся параллельно). Модуль creative_briefs.py не должен
    падать, если scenario_formats ещё не создан/недоступен — импорт делаем
    лениво внутри функции с fallback на "video_speaker" (дефолт форматной
    осознанности, см. §6.2 спеки ARCH-phase3-scenarist).
    """
    try:
        from services.scenario_formats import detect_format_from_reference
        return detect_format_from_reference(ref_name)
    except ImportError:
        return "video_speaker"


def _variable_to_vary(winner: dict, by_angle: dict, by_city: dict) -> str:
    """Определяет, какую ОДНУ переменную варьировать в teardown-теме.

    Правило по спеке (T3): город — если угол победителя уже запущен во
    всех известных городах (некуда расширять географию, варьируем сам
    креатив); хук — иначе (угол ещё не везде, логичнее сначала докрутить
    подачу темы, прежде чем плодить города).
    """
    angle = winner["angle"]
    angle_data = by_angle.get(angle, {})
    angle_cities = {
        city for city, cdata in by_city.items()
        if angle in cdata.get("angles", {})
    }
    all_cities = set(by_city.keys())
    if all_cities and angle_cities >= all_cities:
        return "город"
    return "хук"


def select_winner_teardowns(analysis: dict, max_n: int = 5) -> list[dict]:
    """Teardown победителей по факту продаж — темы для сценариста v2.

    В отличие от build_creative_briefs (масштабирование углов по CPL),
    здесь источник — ТОЛЬКО победители с реальными оплатами (payments>0):
    разбираем, что именно сработало в конкретной рекламе, и предлагаем
    вариацию ОДНОЙ переменной (город/хук), а не абстрактный угол.

    Переиспользует analyze_winners (анализ уже посчитан заранее вызывающим
    кодом) и _passes_quality_gate (тот же гейт качества, что и winners).

    Args:
        analysis: результат analyze_winners(ads) — {"winners": [...], "by_angle": ..., "by_city": ...}
        max_n: максимум teardown-тем в результате

    Returns:
        список тем: [{"reference": {..., "target_product": str | None}, "ad_format": str,
                       "variable_to_vary": str, "angle": str, "city": str,
                       "cpl": float, "leads": int, "payments": int}, ...]
        отсортирован по payments убыв. (сильнейшие продажи — первыми).
        reference["target_product"] — продукт победителя из creative_kb (см.
        ARCH-product-tags.md) — Сценарист v2 использует его как источник
        Trello-метки продукта новой карточки ТЗ, не только keyword-эвристику.
    """
    winners = analysis.get("winners", [])
    by_angle = analysis.get("by_angle", {})
    by_city = analysis.get("by_city", {})

    # Только реальные продажи — teardown не работает с "возможно хорошими"
    # рекламами без подтверждённых оплат (payments must be > 0, не None/0)
    sellers = [
        w for w in winners
        if w.get("payments") and w["payments"] > 0 and _passes_quality_gate(w)
    ]
    sellers.sort(key=lambda w: w["payments"], reverse=True)

    teardowns = []
    for winner in sellers[:max_n]:
        teardowns.append({
            "reference": {
                "name": winner["name"],
                "cpl": winner["cpl"],
                "leads": winner["leads"],
                "payments": winner["payments"],
                # Продукт победителя (ARCH-product-tags.md) — источник для
                # Trello-метки новой карточки ТЗ (brief_generator).
                "target_product": winner.get("target_product"),
            },
            "ad_format": _detect_ad_format(winner["name"]),
            "variable_to_vary": _variable_to_vary(winner, by_angle, by_city),
            "angle": winner["angle"],
            "city": winner["city"],
            "cpl": winner["cpl"],
            "leads": winner["leads"],
            "payments": winner["payments"],
        })

    return teardowns


def _detect_emotion(angle: str) -> str:
    """Определяет эмоцию по углу."""
    emotions = {
        "Результат / Показатели": "Беспокойство → надежда",
        "До-После трансформация": "Удивление → вдохновение",
        "Страх ошибки": "Страх → облегчение",
        "Продукт B": "Тревога → решимость",
        "Продукт A": "Амбиция → уверенность",
        "UGC от клиентов": "Скептицизм → доверие",
        "Отзыв клиента": "Скептицизм → доверие",
        "Сторителлинг": "Сопереживание → надежда",
        "Бесплатная консультация": "Любопытство → мотивация",
        "Рассрочка": "Сомнение («дорого») → облегчение",
        "Срочность / Дедлайн": "FOMO → действие",
    }
    return emotions.get(angle, "Интерес → доверие")
