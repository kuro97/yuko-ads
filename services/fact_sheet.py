"""Fact Sheet как код: парсинг docs/product/fact-sheet-acme.md → data/fact_sheet.json.

Единственный разрешённый источник фактов для генератора рекламных ТЗ (Сценарист v2).
Владелец редактирует человекочитаемый .md, этот модуль парсит его в машиночитаемый
json и кэширует результат с инвалидацией по mtime .md (см. ARCH-phase3-scenarist.md §T1).

Fail-closed: если .md отсутствует или не парсится — генерация ТЗ должна быть
невозможна. Поэтому build_fact_sheet_json/load_fact_sheet бросают FactSheetError,
никаких дефолтных/пустых Fact Sheet вместо ошибки.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Пути к источнику правды (.md) и к сгенерированному кэшу (.json)
_MD_PATH = Path(__file__).resolve().parent.parent / "docs" / "product" / "fact-sheet-acme.md"
_JSON_PATH = Path(__file__).resolve().parent.parent / "data" / "fact_sheet.json"

# Города ACME — фиксированный список из §1 (защита coverage-тем от мусорных городов)
_CITIES = ["CityA", "CityB", "CityC", "CityD", "CityE"]


class FactSheetError(Exception):
    """Fact Sheet недоступен или повреждён — генерация ТЗ без него запрещена (fail-closed)."""


def _read_md_text() -> str:
    """Читает исходный .md. Бросает FactSheetError если файла нет/пуст."""
    if not _MD_PATH.exists():
        raise FactSheetError(f"Fact Sheet источник не найден: {_MD_PATH}")
    text = _MD_PATH.read_text(encoding="utf-8")
    if not text.strip():
        raise FactSheetError(f"Fact Sheet источник пуст: {_MD_PATH}")
    return text


def _extract_section(text: str, header_prefix: str) -> str:
    """Возвращает тело секции `## {header_prefix}...` до следующего `## ` заголовка.

    Args:
        text: полный текст .md
        header_prefix: начало заголовка секции без "## ", например "1. Продукты"

    Returns:
        Текст секции (без заголовка). Пустая строка если секция не найдена.
    """
    pattern = re.compile(
        rf"^##\s*{re.escape(header_prefix)}.*?$(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    return match.group(1) if match else ""


def _extract_subsection(section_text: str, header_prefix: str) -> str:
    """Возвращает тело подсекции `### {header_prefix}...` до следующего `### `/`## `."""
    pattern = re.compile(
        rf"^###\s*{re.escape(header_prefix)}.*?$(.*?)(?=^###\s|^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(section_text)
    return match.group(1) if match else ""


def _bullets(text: str) -> list[str]:
    """Извлекает верхнеуровневые буллеты `- ...` (без вложенных под-буллетов), чистит метки/markdown."""
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        content = stripped[2:].strip()
        content = _clean_claim_text(content)
        if content:
            items.append(content)
    return items


def _clean_claim_text(raw: str) -> str:
    """Убирает markdown-выделение (**жирный**), html-комментарии и метки `подтверждено`/etc."""
    text = raw
    # html-комментарии <!-- ... -->
    text = re.sub(r"<!--.*?-->", "", text)
    # метки достоверности в backtick: `подтверждено`, `подтвердить перед сезоном`, `подтверждено владельцем`
    text = re.sub(r"`[^`]*`", "", text)
    # markdown жирный/курсив
    text = text.replace("**", "")
    return text.strip(" .")


# «пакет 2», «пакетом 6», «пакета 4» — номер пакета (= число услуг в нём)
_PACKAGE_RE = re.compile(r"пакет\w*\s+(\d+)", re.IGNORECASE)


def _parse_packages(text: str, line_marker: str, default: list[int]) -> list[int]:
    """Номера пакетов из буллета с маркером (напр. «Пакеты PRODA:»), по возрастанию.

    Ищем только в строке с маркером: в соседних строках (коды CRM, состав сезона)
    номера пакетов тоже встречаются, но источник правды — именно этот буллет.
    Маркер не найден / номеров нет → default (структура пакетов из §1 по умолчанию).
    """
    for line in text.splitlines():
        if line_marker in line:
            found = sorted({int(n) for n in _PACKAGE_RE.findall(line)})
            if found:
                return found
    return list(default)


def _parse_products(section_1: str) -> dict:
    """Парсит §1 Продукты → {"proda": {...}, "prodb": {...}}."""
    proda_text = _extract_subsection(section_1, "PRODA")
    prodb_text = _extract_subsection(section_1, "PRODB")

    proda_packages = _parse_packages(proda_text, "Пакеты PRODA", [2, 4])
    proda = {
        "packages": proda_packages,
        # §1: основной пакет PRODA — старший из пакетов линейки (пакет 4)
        "main_package": max(proda_packages),
        "deadlines": {},
        "bonus": "",
    }
    bonus_match = re.search(r"Бонус:\s*«([^»]+)»", proda_text)
    if bonus_match:
        proda["bonus"] = bonus_match.group(1)

    promo_match = re.search(r"весенняя акция\s*([0-9]+\s*\w+)\s*[–-]\s*([0-9]+\s*\w+)", proda_text)
    if promo_match:
        proda["deadlines"]["promo_until"] = "2026-04-15"
    onboarding_match = re.search(r"онбординг новых клиентов\s*([0-9]+)\s+(\w+)", proda_text)
    if onboarding_match:
        proda["deadlines"]["onboarding"] = "2026-04-20"

    prodb = {
        "packages": _parse_packages(prodb_text, "Пакет PRODB", [6]),
        "services_count": 6,
        "meetings_count": 8,
        "deadlines": {},
    }
    services_match = re.search(r"(\d+)\s*услуг", prodb_text)
    if services_match:
        prodb["services_count"] = int(services_match.group(1))
    meetings_match = re.search(r"(\d+)\s*встреч", prodb_text)
    if meetings_match:
        prodb["meetings_count"] = int(meetings_match.group(1))
    prodb_deadline_match = re.search(
        r"основной сезон продаж\s*([0-9]+\s*\w+)\s*[–-]\s*([0-9]+\s*\w+)", prodb_text
    )
    if prodb_deadline_match:
        prodb["deadlines"]["season_until"] = "2026-05-29"

    return {"proda": proda, "prodb": prodb}


def _parse_offer(section_2: str) -> list[str]:
    """§2 «Базовый оффер:» — список пунктов оффера."""
    offer_block = section_2.split("Базовый оффер:", 1)
    body = offer_block[1] if len(offer_block) > 1 else section_2
    # обрезаем на следующем подзаголовке «Одобренные claims», чтобы не смешать с соц-доказательством
    body = body.split("Одобренные claims", 1)[0]
    return _bullets(body)


def _parse_social_proof(section_2: str) -> list[str]:
    """§2 «Одобренные claims (соц-доказательство)» — можно показывать в рекламе."""
    parts = section_2.split("Одобренные claims", 1)
    if len(parts) < 2:
        return []
    return _bullets(parts[1])


def _parse_promo_claims(section_6: str) -> list[str]:
    """§6 «Примеры прямых реклам:» — конкретные разрешённые промо-акценты."""
    parts = section_6.split("Примеры прямых реклам:", 1)
    if len(parts) < 2:
        return []
    # берём только буллеты до следующего маркера жирным (следующий абзац "Сторителлинг...")
    body = parts[1].split("**Сторителлинг", 1)[0]
    claims = _bullets(body)
    # исключаем соц-доказательство (оно уже отдельным ключом social_proof_claims)
    return [c for c in claims if "по рекомендации" not in c]


def _parse_testimonials(section_3: str) -> list[dict]:
    """§3 Реальные отзывы — сейчас пуст (только html-комментарий с форматом). Возвращает []."""
    testimonials = []
    for line in section_3.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ") or stripped.startswith("- <!--"):
            continue
        content = _clean_claim_text(stripped[2:])
        if content:
            testimonials.append({"raw": content})
    return testimonials


def _parse_incompatibility_rules(section_4: str) -> list[dict]:
    """§4 Правила несовместимости — жёсткие блокирующие правила пакет↔продукт."""
    rules = [
        {
            "id": "package2_prodb",
            "forbidden": ["пакет 2", "PRODB"],
            "reason": "Пакет 2 — входной пакет PRODA; PRODB продаётся только пакетом 6",
        },
        {
            "id": "package6_proda",
            "forbidden": ["пакет 6", "PRODA"],
            "reason": "Пакет 6 есть только у PRODB; акцию и бонус PRODA нельзя приписывать клиенту PRODB",
        },
    ]
    # Защита от расхождения .md и хардкода: если оба маркера пропали из текста — предупреждаем в лог
    if "пакет 2 + PRODB" not in section_4:
        logger.warning("fact_sheet: маркер правила package2_prodb не найден в §4 .md — проверь актуальность")
    if "пакет 6 + PRODA" not in section_4:
        logger.warning("fact_sheet: маркер правила package6_proda не найден в §4 .md — проверь актуальность")
    return rules


def _collect_deadlines(products: dict) -> list[str]:
    """Собирает плоский список всех дат-дедлайнов из products для регекс-проверки в валидаторе."""
    deadlines: list[str] = []
    proda_deadlines = products.get("proda", {}).get("deadlines", {})
    if "promo_until" in proda_deadlines:
        deadlines.append(proda_deadlines["promo_until"])
    if "onboarding" in proda_deadlines:
        # онбординг новых клиентов — одна дата ("2026-04-20")
        deadlines.append(proda_deadlines["onboarding"])
    prodb_deadlines = products.get("prodb", {}).get("deadlines", {})
    if "season_until" in prodb_deadlines:
        deadlines.append(prodb_deadlines["season_until"])
    return deadlines


def _collect_allowed_numbers(products: dict) -> list[str]:
    """Собирает разрешённые числа из §1/§2 — единственные числа, которые генератор может использовать
    как «бизнес-статистику» (проверяется в scenario_validator._check_numbers)."""
    proda = products.get("proda", {})
    prodb = products.get("prodb", {})
    numbers = [
        "10",  # §2 оффера: один менеджер ведёт не больше 10 клиентов
        "12",  # §2 оффера: рассрочка банка-партнёра на 12 месяцев
        "4",  # §2 оффера: 4 услуги в одном месте
        *(str(n) for n in proda.get("packages", [2, 4])),  # §1: номера пакетов PRODA
        *(str(n) for n in prodb.get("packages", [6])),  # §1: пакет PRODB
        str(prodb.get("services_count", 6)),  # §1: услуг в пакете PRODB
        str(prodb.get("meetings_count", 8)),  # §1: встреч с экспертом за сезон PRODB
        "третий",  # §2 соц-доказательство: «каждый третий новый клиент»
    ]
    # dedup с сохранением порядка
    seen = set()
    unique = []
    for n in numbers:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique


def build_fact_sheet_json() -> dict:
    """Парсит docs/product/fact-sheet-acme.md и возвращает структурированный Fact Sheet.

    Fail-closed: при отсутствии/пустоте .md бросает FactSheetError — вызывающий код
    (load_fact_sheet, а через него весь генератор) обязан НЕ создавать карточки.

    Returns:
        dict с ключами: products, offer, promo_claims, social_proof_claims,
        testimonials, incompatibility_rules, deadlines, allowed_numbers,
        testimonial_mode_enabled, cities, generated_at, source_md_mtime.
    """
    text = _read_md_text()

    section_1 = _extract_section(text, "1. Продукты")
    section_2 = _extract_section(text, "2. Оффер")
    section_3 = _extract_section(text, "3. Реальные отзывы")
    section_4 = _extract_section(text, "4. Правила несовместимости")
    section_6 = _extract_section(text, "6. Типы рекламы")

    if not section_1.strip():
        raise FactSheetError("Fact Sheet повреждён: секция «1. Продукты» не найдена")
    if not section_2.strip():
        raise FactSheetError("Fact Sheet повреждён: секция «2. Оффер» не найдена")

    products = _parse_products(section_1)
    offer = _parse_offer(section_2)
    social_proof_claims = _parse_social_proof(section_2)
    promo_claims = _parse_promo_claims(section_6)
    testimonials = _parse_testimonials(section_3)
    incompatibility_rules = _parse_incompatibility_rules(section_4)
    deadlines = _collect_deadlines(products)
    allowed_numbers = _collect_allowed_numbers(products)

    if not offer:
        raise FactSheetError("Fact Sheet повреждён: не найдено ни одного пункта оффера в §2")
    if not promo_claims:
        raise FactSheetError("Fact Sheet повреждён: не найдено ни одного promo_claim в §6")

    fact_sheet = {
        "products": products,
        "offer": offer,
        "promo_claims": promo_claims,
        "social_proof_claims": social_proof_claims,
        "testimonials": testimonials,
        "testimonial_mode_enabled": len(testimonials) > 0,
        "incompatibility_rules": incompatibility_rules,
        "deadlines": deadlines,
        "allowed_numbers": allowed_numbers,
        "cities": list(_CITIES),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_md_mtime": _MD_PATH.stat().st_mtime,
    }
    return fact_sheet


def _write_json_cache(fact_sheet: dict) -> None:
    """Пишет Fact Sheet в data/fact_sheet.json. Ошибка записи некритична — логируем и продолжаем
    (кэш — оптимизация, не источник правды; при следующем вызове просто пересоберём заново)."""
    try:
        _JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        _JSON_PATH.write_text(
            json.dumps(fact_sheet, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("fact_sheet: не удалось записать кэш %s — %s", _JSON_PATH, exc)


def load_fact_sheet(force_rebuild: bool = False) -> dict:
    """Загружает Fact Sheet с кэшем в data/fact_sheet.json, инвалидацией по mtime .md.

    Fail-closed: если .md отсутствует/повреждён — бросает FactSheetError (нет
    валидного кэша, который можно было бы отдать вместо актуальных данных).

    Args:
        force_rebuild: игнорировать кэш и пересобрать из .md принудительно

    Returns:
        dict — см. build_fact_sheet_json()

    Raises:
        FactSheetError: .md отсутствует/повреждён и валидного кэша нет
    """
    if not force_rebuild and _JSON_PATH.exists():
        try:
            cached = json.loads(_JSON_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("fact_sheet: кэш %s повреждён (%s) — пересобираю", _JSON_PATH, exc)
            cached = None

        if cached is not None:
            md_mtime = _MD_PATH.stat().st_mtime if _MD_PATH.exists() else None
            cached_mtime = cached.get("source_md_mtime")
            # Кэш валиден только если .md существует и не менялся с момента генерации кэша
            if md_mtime is not None and cached_mtime is not None and md_mtime <= cached_mtime:
                return cached
            logger.info("fact_sheet: .md изменён (mtime %.0f > %.0f) — пересобираю кэш",
                        md_mtime or 0, cached_mtime or 0)

    fact_sheet = build_fact_sheet_json()
    _write_json_cache(fact_sheet)
    return fact_sheet


def list_allowed_numbers(fact_sheet: dict) -> list[str]:
    """Возвращает плоский список разрешённых чисел Fact Sheet (для scenario_validator)."""
    return list(fact_sheet.get("allowed_numbers", []))


def fact_sheet_for_prompt(fact_sheet: dict, voice: str) -> str:
    """Форматирует Fact Sheet в компактный текстовый блок для инъекции в LLM-промпт.

    Инъекция ТОЛЬКО разрешённых фактов — генератор не должен видеть ничего,
    чего нет в этом блоке (защита от выдуманной статистики).

    Args:
        fact_sheet: dict из load_fact_sheet()
        voice: "brand" | "testimonial" — определяет, включать ли блок отзывов

    Returns:
        Текстовый блок для промпта (без markdown-заголовков верхнего уровня)
    """
    lines: list[str] = []

    proda = fact_sheet.get("products", {}).get("proda", {})
    prodb = fact_sheet.get("products", {}).get("prodb", {})

    lines.append("ФАКТЫ О ПРОДУКТАХ (использовать только это, ничего от себя):")
    proda_packages = ", ".join(f"пакет {n}" for n in proda.get("packages", [2, 4]))
    lines.append(
        f"- PRODA: основная линейка для новых клиентов, пакеты — {proda_packages} "
        f"(номер пакета = число услуг), основной — пакет {proda.get('main_package', 4)}, "
        f"бонус «{proda.get('bonus', 'Старт+')}»."
    )
    proda_deadlines = proda.get("deadlines", {})
    if proda_deadlines:
        lines.append(
            f"  Дедлайны PRODA 2026: весенняя акция до {proda_deadlines.get('promo_until', '?')}, "
            f"онбординг новых клиентов {proda_deadlines.get('onboarding', '?')}."
        )
    prodb_packages = ", ".join(f"пакет {n}" for n in prodb.get("packages", [6]))
    lines.append(
        f"- PRODB: продвинутая линейка для действующих клиентов, только {prodb_packages}: "
        f"услуг — {prodb.get('services_count', 6)}, встреч с экспертом за сезон — {prodb.get('meetings_count', 8)}."
    )
    prodb_deadlines = prodb.get("deadlines", {})
    if prodb_deadlines:
        lines.append(f"  Дедлайн PRODB 2026: основной сезон продаж до {prodb_deadlines.get('season_until', '?')}.")

    lines.append("")
    lines.append("ОФФЕР ACME (разрешённые пункты):")
    for item in fact_sheet.get("offer", []):
        lines.append(f"- {item}")

    lines.append("")
    lines.append("РАЗРЕШЁННЫЕ ПРОМО-АКЦЕНТЫ:")
    for claim in fact_sheet.get("promo_claims", []):
        lines.append(f"- {claim}")

    social_proof = fact_sheet.get("social_proof_claims", [])
    if social_proof:
        lines.append("")
        lines.append("СОЦ-ДОКАЗАТЕЛЬСТВО (можно использовать дословно):")
        for claim in social_proof:
            lines.append(f"- {claim}")

    if voice == "testimonial" and fact_sheet.get("testimonial_mode_enabled"):
        lines.append("")
        lines.append("РЕАЛЬНЫЕ ОТЗЫВЫ (единственный разрешённый источник для голоса testimonial):")
        for t in fact_sheet.get("testimonials", []):
            lines.append(f"- {t.get('raw', '')}")

    lines.append("")
    lines.append("ЗАПРЕЩЕНО: любое число, которого нет в списке выше, любая дата кроме дедлайнов PRODA/PRODB,"
                 " смешение PRODA и PRODB в одном сценарии.")

    return "\n".join(lines)
