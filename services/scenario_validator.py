"""
Блок-проверки сценариста v2 — последний рубеж перед публикацией.

Два независимых гейта, оба fail-closed (сомнение = блок):
1. validate_scenario — детерминированные regex/правила (без LLM, 100% воспроизводимо):
   пакет↔продукт, числа без источника, смешение голосов, мисматч формата, выдуманный дедлайн.
2. self_critique — второй дешёвый LLM-вызов (sonnet, thinking disabled) на качество
   (дуга/хук/живость/формат), PASS|BLOCK. Ошибка/нет ключа/невалидный вердикт → BLOCK.

Любое нарушение из validate_scenario ИЛИ verdict="BLOCK" из self_critique → карточка
не создаётся (решение принимает services/brief_generator.py, этот модуль только
возвращает результат проверки).
"""

import logging
import re
import time
from dataclasses import dataclass, field

from services.scenario_formats import validate_format_structure

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Результат детерминированных блок-проверок."""
    passed: bool
    violations: list[str] = field(default_factory=list)


@dataclass
class CritiqueResult:
    """Результат LLM-самокритики."""
    verdict: str  # "PASS" | "BLOCK"
    reason: str


# Маркеры голоса brand (говорит компания) — из §8 спеки: «мы/наш сервис/ACME предлагает»
_BRAND_VOICE_MARKERS = [
    "мы в acme",
    "мы —",
    "мы -",
    "наш сервис",
    "наша команда",
    "acme предлагает",
    "в acme мы",
]

# Маркеры голоса testimonial (говорит клиент от первого лица) —
# «я записалась/я обратился/я подключила»
_TESTIMONIAL_VOICE_MARKERS = [
    "я записал",
    "я записала",
    "я обратился",
    "я обратилась",
    "я подключил",
    "я подключила",
]

# Регекс для чисел, которые "выглядят как бизнес-статистика"
# (§8: \d+\s*(из|%|клиентов|покупателей|пунктов))
_NUMBER_STAT_RE = re.compile(
    r"\d+\s*(?:из\s*\d+|%|клиент(?:ов|ок|а|ы)?|покупател(?:ей|я|и|ь)|пункт(?:ов|а)?)",
    re.IGNORECASE,
)

# Все числа внутри найденного статистического выражения (чтобы сверить с allowed_numbers)
_NUMBER_RE = re.compile(r"\d+")

# 4-значный год — не блокируем как "число без источника" (2026, 2025, ...)
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

# Даты вида "до 15 августа" / "до 9 февраля"
_DATE_DEADLINE_RE = re.compile(
    r"до\s+\d{1,2}\s+"
    r"(январ[яь]|феврал[яь]|март[а]?|апрел[яь]|ма[яй]|июн[яь]|июл[яь]|"
    r"август[а]?|сентябр[яь]|октябр[яь]|ноябр[яь]|декабр[яь])",
    re.IGNORECASE,
)

# "осталось N дней/дня/дню" — конкретный отсчёт времени, а не абстрактная срочность
_COUNTDOWN_RE = re.compile(r"осталось\s+\d+\s*дн", re.IGNORECASE)

# ISO-дата (2026-02-09) в тексте сценария
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Месяцы для сопоставления ISO-дедлайна с текстовой датой в сценарии
_MONTH_NAMES = {
    1: "январ", 2: "феврал", 3: "март", 4: "апрел", 5: "ма",
    6: "июн", 7: "июл", 8: "август", 9: "сентябр", 10: "октябр",
    11: "ноябр", 12: "декабр",
}


def _check_testimonial_gate(topic: dict, fact_sheet: dict) -> list[str]:
    """§7/§8: testimonial-голос заблокирован пока в Fact Sheet нет реальных отзывов."""
    violations: list[str] = []
    if topic.get("voice") == "testimonial" and not fact_sheet.get("testimonial_mode_enabled"):
        violations.append(
            "Голос 'testimonial' запрещён: testimonial_mode_enabled=False (нет отзывов в Fact Sheet)"
        )
    return violations


def _marker_pattern(marker: str) -> re.Pattern:
    """Регекс forbidden-маркера правила несовместимости с учётом словоформ.

    Слово маркера матчится как основа (+ любое окончание): «пакет 2» ловит и
    «пакет 2», и «пакете 2», и «пакета 2». Число — целиком, без хвоста
    из цифр («пакет 2» не ловит «пакет 20»). Между токенами — любые пробелы.
    """
    parts = []
    for token in marker.split():
        if token.isdigit():
            parts.append(rf"(?<!\d){re.escape(token)}(?!\d)")
        else:
            parts.append(rf"{re.escape(token)}\w*")
    return re.compile(r"\s+".join(parts), re.IGNORECASE)


def _check_package_product(scenario: str, fact_sheet: dict) -> list[str]:
    """Несовместимость пакет↔продукт из fact_sheet['incompatibility_rules'].

    Правило нарушено, если ВСЕ его forbidden-маркеры одновременно встречаются в тексте
    (напр. «пакет 2» и «PRODB» в одном сценарии — пакет 2 есть только у PRODA).
    """
    violations: list[str] = []
    for rule in fact_sheet.get("incompatibility_rules", []):
        forbidden = rule.get("forbidden", [])
        if forbidden and all(_marker_pattern(marker).search(scenario) for marker in forbidden):
            violations.append(
                f"Несовместимость пакет↔продукт ({' + '.join(forbidden)}): {rule.get('reason', '')}"
            )
    return violations


def _check_numbers(scenario: str, fact_sheet: dict) -> list[str]:
    """Число-статистика (\\d+ из|%|клиентов|покупателей|пунктов), которого нет в allowed_numbers.

    Год (4 цифры) не блокируем — это не бизнес-статистика, а календарная привязка.
    Номера пакетов и прочие allowed_numbers из Fact Sheet — тоже не блокируем.
    """
    from services.fact_sheet import list_allowed_numbers

    violations: list[str] = []
    allowed = set(list_allowed_numbers(fact_sheet))

    for match in _NUMBER_STAT_RE.finditer(scenario):
        stat_phrase = match.group(0)
        for num_match in _NUMBER_RE.finditer(stat_phrase):
            number = num_match.group(0)
            if _YEAR_RE.match(number):
                continue
            if number in allowed:
                continue
            violations.append(
                f"Число без источника в Fact Sheet: '{stat_phrase.strip()}' "
                f"(число '{number}' не входит в allowed_numbers)"
            )
    return violations


def _check_voice_mixing(scenario: str) -> list[str]:
    """Смешение голосов: маркеры brand И testimonial одновременно в одном сценарии."""
    violations: list[str] = []
    scenario_lower = scenario.lower()

    has_brand = any(marker in scenario_lower for marker in _BRAND_VOICE_MARKERS)
    has_testimonial = any(marker in scenario_lower for marker in _TESTIMONIAL_VOICE_MARKERS)

    if has_brand and has_testimonial:
        found_brand = [m for m in _BRAND_VOICE_MARKERS if m in scenario_lower]
        found_testimonial = [m for m in _TESTIMONIAL_VOICE_MARKERS if m in scenario_lower]
        violations.append(
            f"Смешение голосов: brand-маркеры {found_brand} и testimonial-маркеры "
            f"{found_testimonial} встречаются одновременно"
        )
    return violations


def _check_format(scenario: str, topic: dict) -> list[str]:
    """Мисматч структуры формата — делегирует scenario_formats.validate_format_structure."""
    ad_format = topic.get("ad_format", "")
    return validate_format_structure(scenario, ad_format)


def _deadline_in_fact_sheet(day: int, month_word: str, fact_sheet: dict) -> bool:
    """Проверяет, есть ли дата (день + название месяца из текста) среди fact_sheet['deadlines']."""
    month_word_lower = month_word.lower()
    for iso_date in fact_sheet.get("deadlines", []):
        iso_match = _ISO_DATE_RE.match(iso_date)
        if not iso_match:
            continue
        try:
            _, month_num, day_num = iso_date.split("-")
            month_num = int(month_num)
            day_num = int(day_num)
        except ValueError:
            continue
        month_prefix = _MONTH_NAMES.get(month_num, "")
        if day_num == day and month_word_lower.startswith(month_prefix) and month_prefix:
            return True
    return False


def _check_fake_deadline(scenario: str, fact_sheet: dict) -> list[str]:
    """Выдуманный дедлайн: дата/отсчёт дней, которых нет в fact_sheet['deadlines'].

    Абстрактную срочность («времени мало», «сезон короткий — решать надо сейчас») НЕ блокируем —
    ловим только конкретные даты (`до N месяца`) и отсчёты (`осталось N дней`).
    """
    violations: list[str] = []

    for match in _DATE_DEADLINE_RE.finditer(scenario):
        day_str = re.search(r"\d{1,2}", match.group(0))
        month_match = re.search(
            r"(январ[яь]|феврал[яь]|март[а]?|апрел[яь]|ма[яй]|июн[яь]|июл[яь]|"
            r"август[а]?|сентябр[яь]|октябр[яь]|ноябр[яь]|декабр[яь])",
            match.group(0),
            re.IGNORECASE,
        )
        if not day_str or not month_match:
            continue
        day = int(day_str.group(0))
        month_word = month_match.group(0)
        if not _deadline_in_fact_sheet(day, month_word, fact_sheet):
            violations.append(
                f"Выдуманный дедлайн: '{match.group(0).strip()}' не найден в fact_sheet['deadlines']"
            )

    if _COUNTDOWN_RE.search(scenario):
        # Конкретный отсчёт дней ("осталось 12 дней") — источника с таким отсчётом
        # в Fact Sheet нет по определению (дедлайны там — фиксированные даты, не отсчёты).
        countdown_match = _COUNTDOWN_RE.search(scenario)
        violations.append(
            f"Выдуманный дедлайн: '{countdown_match.group(0).strip()}' — конкретный отсчёт "
            f"дней не подтверждён Fact Sheet"
        )

    return violations


def validate_scenario(scenario: str, topic: dict, fact_sheet: dict) -> ValidationResult:
    """Прогоняет сценарий через все детерминированные блок-проверки.

    Args:
        scenario: текст сгенерированного сценария
        topic: тема из topic_selector.select_topics (voice, ad_format и т.д.)
        fact_sheet: dict из fact_sheet.load_fact_sheet()

    Returns:
        ValidationResult(passed=True, violations=[]) — сценарий прошёл все проверки.
        ValidationResult(passed=False, violations=[...]) — хотя бы одна проверка нашла нарушение.
    """
    if not scenario or not scenario.strip():
        return ValidationResult(passed=False, violations=["Пустой сценарий"])

    violations: list[str] = []
    violations.extend(_check_testimonial_gate(topic, fact_sheet))
    violations.extend(_check_package_product(scenario, fact_sheet))
    violations.extend(_check_numbers(scenario, fact_sheet))
    violations.extend(_check_voice_mixing(scenario))
    violations.extend(_check_format(scenario, topic))
    violations.extend(_check_fake_deadline(scenario, fact_sheet))

    return ValidationResult(passed=len(violations) == 0, violations=violations)


_CRITIQUE_SYSTEM_PROMPT = (
    "Ты — редактор рекламных сценариев ACME. Проверяешь сценарий по чек-листу качества:\n"
    "1. Дуга: есть развитие мысли (хук → тело → CTA), а не набор лозунгов.\n"
    "2. Хук: первые секунды/строки реально цепляют, не банальны.\n"
    "3. Живость: текст звучит как речь человека, а не как канцелярит/бюрократия.\n"
    "4. Формат: структура соответствует заявленному формату.\n"
    "5. Логическая консистентность истории и персонажа — сценарий НЕ должен "
    "противоречить сам себе. Проверь связки:\n"
    "   - проблема vs результат: нельзя одновременно «у клиента всё шло отлично с самого начала» и "
    "«решаем проблему, которая мешала клиенту получить результат» про одного и того же клиента;\n"
    "   - сегмент клиента vs продукт: пакет и сегмент клиента должны биться с продуктом "
    "(пакет 2 есть только у PRODA, не у PRODB);\n"
    "   - город vs продукт: город и продукт не должны конфликтовать;\n"
    "   - дедлайны vs текущая дата: указанные сроки/даты не должны быть уже прошедшими "
    "или невозможными относительно момента показа.\n"
    "Любое такое внутреннее противоречие — это BLOCK.\n\n"
    "Ответь СТРОГО в формате первой строки:\n"
    "PASS: <краткая причина>\n"
    "или\n"
    "BLOCK: <краткая причина>\n"
    "Никакого другого текста до этой строки."
)


def _parse_critique_verdict(raw_text: str) -> CritiqueResult:
    """Парсит первую строку ответа LLM в CritiqueResult. Невалидный формат → BLOCK (fail-closed)."""
    first_line = raw_text.strip().splitlines()[0] if raw_text.strip() else ""

    match = re.match(r"^(PASS|BLOCK)\s*:\s*(.*)$", first_line.strip(), re.IGNORECASE)
    if not match:
        return CritiqueResult(verdict="BLOCK", reason=f"LLM вернул невалидный вердикт: '{first_line}'")

    verdict = match.group(1).upper()
    reason = match.group(2).strip() or "(без причины)"
    return CritiqueResult(verdict=verdict, reason=reason)


def self_critique(scenario: str, topic: dict) -> CritiqueResult:
    """Второй LLM-вызов (sonnet, thinking disabled) — качественная самокритика сценария.

    Fail-closed: любая ошибка/недоступность LLM или невалидный ответ → verdict="BLOCK".
    Лучше не выпустить нормальный сценарий, чем выпустить плохой.

    Args:
        scenario: текст сгенерированного сценария
        topic: тема (для контекста промпта — формат/голос)

    Returns:
        CritiqueResult(verdict="PASS"|"BLOCK", reason=str)
    """
    import config

    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        logger.warning("scenario_validator: ANTHROPIC_API_KEY не задан — self_critique BLOCK (fail-closed)")
        return CritiqueResult(verdict="BLOCK", reason="LLM недоступен (нет ключа)")

    prompt = (
        f"Формат: {topic.get('ad_format', '?')}. Голос: {topic.get('voice', '?')}.\n\n"
        f"Сценарий:\n{scenario}"
    )

    try:
        import anthropic
        from services.llm_logger import log_llm_call

        model = getattr(config, "CLAUDE_SONNET_MODEL", "claude-sonnet-5")
        client = anthropic.Anthropic(api_key=api_key)

        start = time.monotonic()
        response = client.messages.create(
            model=model,
            max_tokens=256,
            # Самокритика — задача классификации по чек-листу, adaptive thinking
            # только тратит токены и путает извлечение текстового блока.
            thinking={"type": "disabled"},
            system=_CRITIQUE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = int((time.monotonic() - start) * 1000)
    except Exception as exc:
        # Кончились кредиты Anthropic → алерт (дедуп) + прерываем батч-цикл самокритики
        from services.llm_credit_guard import raise_if_credit_error
        raise_if_credit_error("scenario_validator.self_critique", exc)
        logger.warning("scenario_validator: self_critique LLM вызов упал — %s", exc)
        return CritiqueResult(verdict="BLOCK", reason=f"Ошибка LLM: {exc}")

    usage = getattr(response, "usage", None)
    try:
        log_llm_call(
            model=model,
            purpose="scenario_critique",
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            latency_ms=latency_ms,
        )
    except Exception as exc:
        # Логирование в БД некритично — не должно ронять самокритику
        logger.warning("scenario_validator: не удалось залогировать LLM-вызов — %s", exc)

    if not response.content:
        return CritiqueResult(verdict="BLOCK", reason="LLM вернул пустой ответ")

    text_block = next((block for block in response.content if getattr(block, "type", None) == "text"), None)
    if text_block is None:
        return CritiqueResult(verdict="BLOCK", reason="LLM ответ не содержит текстового блока")

    return _parse_critique_verdict(text_block.text)
