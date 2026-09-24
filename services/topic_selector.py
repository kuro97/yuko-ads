"""
Выбор и приоритизация тем для Сценариста v2.

Тема (Topic) — единица работы для генератора сценариев: объединяет источник
(пробел покрытия ИЛИ teardown победителя по продажам) со всеми полями,
нужными для промпта (город/сегмент/голос/формат/референс/переменная-вариации).

Источники тем (см. ARCH-phase3-scenarist.md §T4):
- _coverage_topics: пробелы покрытия (coverage_monitor.analyze_coverage) —
  город×adset_type без активных реклам (empty) или мало (thin).
- _teardown_topics: разбор победителей по факту продаж
  (creative_briefs.select_winner_teardowns) — что реально сработало.

Приоритизация: coverage.empty > coverage.thin > teardown. Это гарантирует
«генерация идёт ПО ПРОБЕЛАМ» (роадмап §7) — пустые группы (0 реклам)
критичнее тонких, teardown победителей добивает остаток до max_topics.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Путь к settings.json — читаем гейт hypothesist.enabled напрямую отсюда, а
# не через agent.scheduler.load_settings(). Так сделано намеренно: ручной
# путь генерации ТЗ (services.brief_generator.generate_and_push_briefs, тест
# tests/test_brief_generator_master_switch.py::test_manual_endpoint_not_gated_by_flag)
# гарантирует, что load_settings НИКОГДА не вызывается — это защищённый
# контракт другой фичи (мастер-выключатель brief_generator.enabled). Прямое
# чтение файла не задевает этот мок и не дублирует бизнес-логику мержа
# thresholds (нужен только один булев флаг).
#
# services.hypothesis_influence.load_verdict_weights (модуль T5) читает свои
# пороги (ttl_days/dead_min_refuted) тем же способом — напрямую из
# settings.json, без get_autopilot_config()/load_settings() — поэтому
# транзитивный вызов из этого модуля тоже не задевает защищённый контракт.
_SETTINGS_FILE = Path(__file__).parent.parent / "data" / "settings.json"

# Дефолтный формат для coverage-тем — нет референса, значит нет данных
# о формате победителя; video_speaker — самый дешёвый в производстве формат.
_DEFAULT_COVERAGE_FORMAT = "video_speaker"

# Дефолтный голос — testimonial заблокирован, пока fact_sheet.testimonial_mode_enabled=False.
_DEFAULT_VOICE = "brand"

# Порядок приоритетов для сортировки (меньше — выше приоритет)
_PRIORITY_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2}

# Порядок источников для сортировки: coverage.empty > coverage.thin > teardown
_SOURCE_RANK = {
    ("coverage", "empty"): 0,
    ("coverage", "thin"): 1,
    ("teardown", None): 2,
}


def _coverage_topics(fact_sheet: dict) -> list[dict]:
    """Строит темы из пробелов покрытия (coverage_monitor.analyze_coverage).

    Каждая пустая/тонкая группа (город×adset_type) → одна тема. Угол берём
    round-robin из fact_sheet["promo_claims"], чтобы разные пробелы получали
    разные офферы, а не один и тот же промо-акцент на все карточки.

    Args:
        fact_sheet: dict из load_fact_sheet() — источник cities/promo_claims

    Returns:
        список тем с source="coverage", gap_level в rationale, priority HIGH (empty) / MED (thin)
    """
    # Ленивый импорт — coverage_monitor тянет services.shadow_report, не нужен
    # на этапе простого построения тем без реальной БД (тестируемость)
    from services.coverage_monitor import analyze_coverage

    coverage = analyze_coverage()
    allowed_cities = set(fact_sheet.get("cities", []))
    promo_claims = fact_sheet.get("promo_claims", []) or ["Бесплатная консультация"]

    topics: list[dict] = []
    claim_idx = 0

    def _build(entry: dict, gap_level: str, priority: str) -> dict | None:
        nonlocal claim_idx
        city = entry.get("city", "")
        if city not in allowed_cities:
            # Город вне списка присутствия ACME — защита от мусорных coverage-групп
            logger.info("topic_selector: coverage-город '%s' не в fact_sheet.cities — пропущен", city)
            return None
        angle = promo_claims[claim_idx % len(promo_claims)]
        claim_idx += 1
        adset_type = entry.get("adset_type", "")
        topic = {
            "source": "coverage",
            "city": city,
            "segment": "общий",  # adset_type (L2/L1) не различает продукт PRODA/PRODB — берём общий
            "adset_type": adset_type,
            "voice": _DEFAULT_VOICE,
            "ad_format": _DEFAULT_COVERAGE_FORMAT,
            "reference": None,
            "variable_to_vary": "город",
            "angle": angle,
            "priority": priority,
            "rationale": f"{city}/{adset_type} — {entry.get('count', 0)} активных реклам ({gap_level})",
        }
        return topic

    for entry in coverage.get("empty", []):
        topic = _build(entry, "пусто", "HIGH")
        if topic:
            topics.append(topic)

    for entry in coverage.get("thin", []):
        topic = _build(entry, "мало", "MED")
        if topic:
            topics.append(topic)

    return topics


def _teardown_topics(fact_sheet: dict, max_n: int = 10) -> list[dict]:
    """Строит темы из teardown победителей по факту продаж.

    Источник данных: brief_generator._get_fresh_ads (свежие ады из creative_kb)
    → creative_briefs.analyze_winners → creative_briefs.select_winner_teardowns.
    Ленивый импорт brief_generator (избегаем циклического импорта:
    brief_generator в будущем импортирует topic_selector.select_topics).

    Args:
        fact_sheet: dict из load_fact_sheet() — источник cities
        max_n: максимум teardown-тем на выходе

    Returns:
        список тем с source="teardown", priority LOW (масштабирование уже
        работающего — менее критично, чем закрыть пробел покрытия)
    """
    from services.brief_generator import _get_fresh_ads
    from services.creative_briefs import analyze_winners, select_winner_teardowns

    ads = _get_fresh_ads()
    if not ads:
        return []

    analysis = analyze_winners(ads)
    teardowns = select_winner_teardowns(analysis, max_n=max_n)

    allowed_cities = set(fact_sheet.get("cities", []))
    topics: list[dict] = []
    for td in teardowns:
        city = td.get("city", "")
        if city not in allowed_cities:
            logger.info("topic_selector: teardown-город '%s' не в fact_sheet.cities — пропущен", city)
            continue
        topics.append({
            "source": "teardown",
            "city": city,
            "segment": "общий",
            "voice": _DEFAULT_VOICE,
            "ad_format": td.get("ad_format", _DEFAULT_COVERAGE_FORMAT),
            "reference": td.get("reference"),
            "variable_to_vary": td.get("variable_to_vary", "хук"),
            "angle": td.get("angle", ""),
            "priority": "LOW",
            "rationale": (
                f"Победитель «{td.get('reference', {}).get('name', '?')}» "
                f"({td.get('payments', 0)} оплат) — варьируем {td.get('variable_to_vary', 'хук')}"
            ),
        })

    return topics


def _prioritize(topics: list[dict]) -> list[dict]:
    """Сортирует темы: coverage.empty > coverage.thin > teardown, внутри — по priority.

    Args:
        topics: сырой список тем (уже с полем source/priority/rationale)

    Returns:
        отсортированный список (не мутирует вход)
    """
    def _sort_key(topic: dict) -> tuple:
        source = topic.get("source", "teardown")
        if source == "coverage":
            gap_level = "empty" if "пусто" in topic.get("rationale", "") else "thin"
            source_rank = _SOURCE_RANK.get((source, gap_level), 9)
        else:
            source_rank = _SOURCE_RANK.get((source, None), 9)
        priority_rank = _PRIORITY_ORDER.get(topic.get("priority", "LOW"), 9)
        return (source_rank, priority_rank)

    return sorted(topics, key=_sort_key)


def select_topics(max_topics: int = 3) -> list[dict]:
    """Выбирает и приоритизирует темы для генерации сценариев.

    Приоритет: пробелы покрытия (empty > thin) выше teardown победителей.
    testimonial-темы отфильтровываются, пока fact_sheet.testimonial_mode_enabled=False.
    Города вне fact_sheet.cities отфильтрованы на уровне _coverage_topics/_teardown_topics.

    Fail-closed по Fact Sheet: если Fact Sheet недоступен/повреждён —
    fact_sheet.FactSheetError пробрасывается наверх (generate_and_push_briefs
    обязан поймать и не создавать карточек, см. §8 спеки).

    Args:
        max_topics: максимум тем в результате (топ по приоритету)

    Returns:
        список тем (см. §6.1 API Contract ARCH-phase3-scenarist.md), длина <= max_topics.
        Пустой список — если покрытие в норме И нет победителей с оплатами.
    """
    from services.fact_sheet import load_fact_sheet

    fact_sheet = load_fact_sheet()

    topics = _coverage_topics(fact_sheet)
    topics += _teardown_topics(fact_sheet)

    if not fact_sheet.get("testimonial_mode_enabled", False):
        topics = [t for t in topics if t.get("voice") != "testimonial"]

    topics = _prioritize(topics)
    topics = _apply_hypothesis_influence(topics)

    return topics[:max_topics]


def _hypothesist_enabled() -> bool:
    """Читает флаг hypothesist.enabled напрямую из settings.json (см. комментарий
    у _SETTINGS_FILE — почему не через agent.scheduler.load_settings/get_autopilot_config).

    Дефолт True — совпадает с AUTOPILOT_DEFAULTS['hypothesist']['enabled']
    в services/autopilot.py (безопасный контур влияния включён по умолчанию).
    Верхнеуровневый мердж, как в get_autopilot_config: если в settings.json
    задан блок autopilot.hypothesist — он используется целиком.
    """
    if not _SETTINGS_FILE.exists():
        return True
    data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    hypothesist_cfg = (data.get("autopilot") or {}).get("hypothesist")
    if hypothesist_cfg is None:
        return True
    return bool(hypothesist_cfg.get("enabled", True))


def _apply_hypothesis_influence(topics: list[dict]) -> list[dict]:
    """Применяет мягкое влияние вердиктов гипотез поверх приоритизации.

    Под флагом hypothesist.enabled (см. docs/specs/ARCH-phase4-hypothesist.md
    §T7): при enabled=False возвращает темы как есть (поведение как раньше).
    Сбой влияния (БД недоступна, конфиг битый и т.п.) НЕ должен ломать выбор
    тем — темы важнее весов, поэтому вся операция обёрнута в try/except с
    warning, а на выходе — исходный (уже приоритизированный) список.
    """
    try:
        if not _hypothesist_enabled():
            return topics

        from services.hypothesis_influence import apply_soft_influence, load_verdict_weights

        weights = load_verdict_weights()
        return apply_soft_influence(topics, weights)
    except Exception:
        logger.warning("topic_selector: сбой применения влияния гипотез — темы без влияния", exc_info=True)
        return topics
