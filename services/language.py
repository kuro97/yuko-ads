"""Языковой сервис — определение языка, конфиг L2/L1, аналитика по языкам.

L1 — основной язык объявлений (по умолчанию), L2 — второй язык аудитории.
Язык карточки определяется только по явным маркерам, без разбора алфавита:
алфавит и словарь второго языка у каждой компании свои.
"""
import re
from collections.abc import Iterable

from config import AD_BODY, L2_MARKERS, LEAD_FORMS

# Слова-маркеры второго языка (L2) в имени карточки. Набор настраивается через
# env L2_MARKERS (через запятую, см. config.py). Сравнение строгое — по токенам
# после split по /|,()[] и пробелам, без учёта регистра.
L2_WORD_MARKERS: frozenset[str] = frozenset(L2_MARKERS)

# Явный тег «[L2]» — ищется и в имени, и в описании карточки.
_L2_TAG_RE = re.compile(r"\[\s*l2\s*\]", re.IGNORECASE)

# Разделители токенов имени: /|()[],-—. и пробелы
_TOKEN_SPLIT_RE = re.compile(r"[\s/|()\[\],\-—\.]+")

LANGUAGES: dict = {
    "L2": {"id": "L2", "display_name": "Language 2", "display_name_ru": "Второй язык"},
    "L1": {"id": "L1", "display_name": "Language 1", "display_name_ru": "Основной язык"},
}


def _normalize_markers(markers: Iterable[str] | None) -> frozenset[str]:
    """Маркеры в нижнем регистре без пустых строк; None → набор из конфига."""
    if markers is None:
        return L2_WORD_MARKERS
    return frozenset(m.strip().lower() for m in markers if m and m.strip())


def detect_language(
    card_name: str,
    card_desc: str,
    markers: Iterable[str] | None = None,
) -> str:
    """L2 или L1 — по явным маркерам второго языка.

    1. Если в имени или описании есть тег «[L2]» (регистр не важен) → L2.
    2. Если в имени есть слово-маркер (по умолчанию «l2»; набор берётся из
       config.L2_MARKERS или из аргумента markers) → L2.
       Слово выделяется как токен по разделителям /|()[],-—. и пробелам:
       «Тема А / Подтема / l2» → L2, а «l2» внутри слова («model2l2x») — нет.
       В описании голое слово-маркер не считается — только тег «[L2]».
    3. Иначе → L1 (язык по умолчанию).
    """
    name = card_name or ""
    desc = card_desc or ""
    if _L2_TAG_RE.search(name) or _L2_TAG_RE.search(desc):
        return "L2"

    marker_set = _normalize_markers(markers)
    tokens = {t for t in _TOKEN_SPLIT_RE.split(name.lower()) if t}
    if tokens & marker_set:
        return "L2"

    return "L1"


def get_language_config() -> dict:
    """Конфиг языков с текстами объявлений и формами.

    Возвращает: {L2: {id, display_name, display_name_ru, ad_body, form_id},
                 L1: {...}}
    """
    result = {}
    for lang_id, lang_info in LANGUAGES.items():
        result[lang_id] = {
            **lang_info,
            "ad_body": AD_BODY.get(lang_id, ""),
            "form_id": LEAD_FORMS.get(lang_id, {}).get("form_id", ""),
        }
    return result


def get_analytics_by_language(ads_with_metrics: list[dict]) -> dict:
    """Группировка метрик по языку (adset_type: L2/L1).

    Каждый элемент: {adset_type, spend, leads, cpl}.
    Возвращает: {L2: {count, spend, leads, avg_cpl}, L1: {...}}.
    При leads=0 avg_cpl=0.0. Пустой список → нули для обоих.
    """
    result: dict = {
        "L2": {"count": 0, "spend": 0.0, "leads": 0, "avg_cpl": 0.0},
        "L1": {"count": 0, "spend": 0.0, "leads": 0, "avg_cpl": 0.0},
    }

    for ad in ads_with_metrics:
        lang = ad.get("adset_type")
        if lang not in result:
            continue
        result[lang]["count"] += 1
        result[lang]["spend"] += float(ad.get("spend", 0) or 0)
        result[lang]["leads"] += int(ad.get("leads", 0) or 0)

    # avg_cpl после агрегации
    for lang_data in result.values():
        leads = lang_data["leads"]
        spend = lang_data["spend"]
        lang_data["avg_cpl"] = round(spend / leads, 2) if leads > 0 else 0.0

    return result
