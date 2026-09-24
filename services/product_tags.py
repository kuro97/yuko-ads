"""
Канонический реестр продуктов ACME (PRODA/PRODB/СТАРТ/ОБЩАЯ) + классификатор +
форматтер имени объявления. См. docs/specs/ARCH-product-tags.md.

Единая точка правды: если появляется новый продукт — дописываем ключ в
PRODUCTS (tag/keywords/product_aliases), классификатор/форматтер/отчёт не меняются.
"""

import logging
import re

logger = logging.getLogger(__name__)


# Реестр продуктов. Ключ — канон, значение — метаданные.
# tag — суффикс в имени; keywords — для эвристики; product_aliases — значения target_product,
# которые считаются этим продуктом (нормализация старых кодов из миграции 007).
# Keywords ниже — ПРИМЕР: замените на названия и пакеты своих продуктовых линеек.
PRODUCTS: dict[str, dict] = {
    "PRODA": {
        "tag": "[PRODA]",
        # PRODC/PRODD/PRODE — CRM-коды пакетов линейки «Продукт A»
        # (пакет 2 / пакет 4 / пакет 4 «Премиум», см. docs/product/fact-sheet-acme.md §1)
        "keywords": ["proda", "prodc", "prodd", "prode", "продукт a", "продукта a"],
        "product_aliases": ["PRODA", "PRODC", "PRODD", "PRODE"],
    },
    "PRODB": {
        "tag": "[PRODB]",
        # «пакет 6» (и словоформы) — единственный пакет PRODB (fact sheet §1).
        "keywords": ["prodb", "продукт b", "продукта b", "пакет 6", "пакета 6",
                     "пакете 6", "пакетом 6", "продвинут"],
        "product_aliases": ["PRODB"],
    },
    "СТАРТ": {
        "tag": "[СТАРТ]",
        "keywords": ["стартов", "новичк", "начинающ"],
        "product_aliases": ["START", "СТАРТ", "BASIC"],
    },
    "ОБЩАЯ": {
        "tag": "[ОБЩАЯ]",
        "keywords": [],  # дефолт, эвристикой не ловим
        "product_aliases": ["GENERAL", "ОБЩАЯ", "COMMON"],
    },
}

VALID_PRODUCTS: set[str] = set(PRODUCTS.keys())  # {"PRODA","PRODB","СТАРТ","ОБЩАЯ"}
DEFAULT_PRODUCT: str = "ОБЩАЯ"

# Порядок keyword/label-эвристики: ОБЩАЯ пропускаем — у неё нет keywords,
# она остаётся дефолтом, если ничего не совпало.
_HEURISTIC_ORDER: tuple[str, ...] = ("PRODA", "PRODB", "СТАРТ")

def _build_product_alias_map() -> dict[str, str]:
    """Обратная карта: старое значение target_product (в верхнем регистре) ->
    канон продукта. Строится один раз из PRODUCTS."""
    alias_map: dict[str, str] = {}
    for product, meta in PRODUCTS.items():
        alias_map[product.upper()] = product
        for alias in meta["product_aliases"]:
            alias_map[alias.upper()] = product
    return alias_map


_PRODUCT_ALIAS_MAP: dict[str, str] = _build_product_alias_map()


def normalize_product(raw: str | None) -> str | None:
    """Старое значение target_product (PRODA/PRODC/PRODD/PRODE/PRODB/...) → канон продукта.
    Неизвестное/пустое → None (не ОБЩАЯ — пусть решает следующий источник)."""
    if not raw:
        return None
    return _PRODUCT_ALIAS_MAP.get(str(raw).strip().upper())


def _has_keyword(haystack_lower: str, keyword: str) -> bool:
    """Ищет keyword в haystack_lower с границей слова СЛЕВА: предотвращает
    ложные срабатывания вида «продвинут» внутри «непродвинутых» (keywords
    не должны ловиться как часть более длинного слова). Границу СПРАВА
    намеренно не требуем — часть keywords в реестре это словоформы-основы
    («продвинут», «стартов», «новичк»), которые обязаны матчить более
    длинные словоформы («продвинутый», «стартовый», «новичков»)."""
    pattern = r"(?<!\w)" + re.escape(keyword)
    return re.search(pattern, haystack_lower) is not None


def _product_tokens(product: str) -> list[str]:
    """Токены для распознавания продукта: канон + product_aliases + keywords,
    в нижнем регистре. Один и тот же набор используется и для Trello-меток,
    и для keyword-эвристики по названию/ТЗ."""
    meta = PRODUCTS[product]
    tokens = [product, *meta["product_aliases"], *meta["keywords"]]
    return [token.lower() for token in tokens]


def _match_by_text(text_lower: str) -> str | None:
    """Ищет первый продукт (в порядке _HEURISTIC_ORDER), чьи токены
    встречаются в тексте. None, если ничего не совпало."""
    for product in _HEURISTIC_ORDER:
        for token in _product_tokens(product):
            if token and _has_keyword(text_lower, token):
                return product
    return None


def classify_product(
    name: str | None = None,
    desc: str = "",
    trello_labels: list[str] | None = None,
    target_product: str | None = None,
) -> str:
    """Определяет продукт по приоритету источников. Всегда возвращает канон из
    VALID_PRODUCTS. Порядок:
      1) Trello-label (точное вхождение канона/алиаса/keyword-токена в любую
         метку, case-insensitive). Метки проверяются в порядке списка
         trello_labels — берём первую совпавшую.
      2) target_product из KB через normalize_product (если дал канон).
      3) keyword-эвристика по (name + ' ' + desc), первый совпавший продукт
         в порядке [PRODA, PRODB, СТАРТ] (ОБЩАЯ пропускаем — у неё нет keywords).
      4) ОБЩАЯ.
    Никогда не бросает исключений. LLM здесь НЕ зовётся — это чистая быстрая
    функция; LLM-добор делает отдельный _llm_classify_product в бэкфилле (§5)."""
    try:
        name = name or ""
        desc = desc or ""
        labels = trello_labels or []

        # 1) Trello-label — по порядку списка меток.
        for label in labels:
            if not label:
                continue
            label_lower = str(label).lower()
            matched = _match_by_text(label_lower)
            if matched is not None:
                return matched

        # 2) target_product из KB.
        canon = normalize_product(target_product)
        if canon is not None:
            return canon

        # 3) keyword-эвристика по названию + описанию.
        haystack = f"{name} {desc}".lower()
        matched = _match_by_text(haystack)
        if matched is not None:
            return matched

        # 4) дефолт.
        return DEFAULT_PRODUCT
    except Exception as exc:
        logger.warning("product_tags: classify_product упал, дефолт ОБЩАЯ — %s", exc)
        return DEFAULT_PRODUCT


# Промпт для LLM-добора (см. §5 ARCH-product-tags.md): маркеры продуктов,
# чтобы модель ловила эмоциональные заходы про PRODA без буквальных keywords.
# Маркеры ниже — ПРИМЕР (вымышленные заходы «Критика рынка», «История Олега»);
# замените их на заходы своего аккаунта.
_LLM_SYSTEM_PROMPT = """\
Ты классифицируешь рекламное объявление сервиса ACME по продукту.
Ответь СТРОГО ОДНИМ словом из набора: PRODA, PRODB, СТАРТ, ОБЩАЯ.

Маркеры продуктов:
- PRODA (в т.ч. эмоциональный заход без явных ключевых слов): «критика рынка»,
  «История Олега», «сравнение с конкурентами», «подключить
  PRODA/PRODC/PRODD», весенняя акция, пакет 2, пакет 4.
- PRODB: Продукт B, продвинутые клиенты, пакет 6, выход на следующий уровень.
- СТАРТ: стартовый пакет, новички, первый опыт.
- ОБЩАЯ: общий оффер без явной привязки к продукту/пакету.

Ответ — строго одно слово из {PRODA, PRODB, СТАРТ, ОБЩАЯ}, без пояснений."""


def _llm_classify_product(name: str, body: str = "") -> str:
    """LLM-добор продукта для случаев, где keyword-эвристика дала ОБЩАЯ.
    Разовый шаг бэкфилла (§5). Возвращает канон из VALID_PRODUCTS; fail-safe →
    DEFAULT_PRODUCT ('ОБЩАЯ'), НЕ бросает. Паттерн вызова Anthropic — как в
    services.brief_generator._generate_scenario_for_brief."""
    import config

    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        logger.warning(
            "product_tags: ANTHROPIC_API_KEY не задан — LLM-добор для '%s' пропущен, продукт = %s",
            name, DEFAULT_PRODUCT,
        )
        return DEFAULT_PRODUCT

    try:
        import anthropic

        model = getattr(config, "CLAUDE_SONNET_MODEL", "claude-sonnet-5")
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=16,
            # Классификация — reasoning не нужен, thinking только меняет
            # порядок блоков в response.content (см. извлечение текста ниже).
            thinking={"type": "disabled"},
            system=_LLM_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Название: {name}\nТекст: {body}"}],
        )
    except Exception as exc:
        # Кончились кредиты Anthropic → алерт (дедуп) + прерываем батч-цикл разметки
        from services.llm_credit_guard import raise_if_credit_error
        raise_if_credit_error("product_tags._llm_classify_product", exc)
        logger.warning("product_tags: LLM-добор продукта не удался для '%s' — %s", name, exc)
        return DEFAULT_PRODUCT

    if not response.content:
        logger.warning("product_tags: LLM вернул пустой ответ для '%s'", name)
        return DEFAULT_PRODUCT

    # Не полагаемся на content[0] — при thinking первым блоком может идти
    # ThinkingBlock без атрибута .text. Ищем первый текстовый блок явно.
    text_block = next((block for block in response.content if getattr(block, "type", None) == "text"), None)
    if text_block is None:
        logger.warning("product_tags: LLM ответ не содержит текстового блока для '%s'", name)
        return DEFAULT_PRODUCT

    answer = text_block.text.strip().upper()
    if answer not in VALID_PRODUCTS:
        logger.warning("product_tags: LLM вернул продукт вне реестра '%s' для '%s'", answer, name)
        return DEFAULT_PRODUCT

    return answer


# Регекс продуктового тега-суффикса — используется и для среза, и для проверки
# идемпотентности. Собирается из PRODUCTS, чтобы не дублировать список.
_TAG_NAMES = "|".join(re.escape(meta["tag"][1:-1]) for meta in PRODUCTS.values())
_TAG_SUFFIX_RE = re.compile(r"\s*\[(?:" + _TAG_NAMES + r")\]\s*$")


def strip_product_tag(ad_name: str) -> str:
    """Убирает продуктовый тег-суффикс (любого известного продукта) с конца имени.
    Используется парсерами/отчётами при необходимости чистого имени. Если тега
    нет — возвращает исходное имя."""
    if not ad_name:
        return ad_name
    return _TAG_SUFFIX_RE.sub("", ad_name).rstrip()


def format_ad_name(base_name: str, product: str) -> str:
    """Возвращает base_name с суффиксом ' [ПРОДУКТ]'. Идемпотентно: если тег
    (любого известного продукта) уже есть в конце — сначала срезаем его,
    потом добавляем актуальный. product не из VALID_PRODUCTS → возвращаем
    base_name без изменений."""
    if not base_name:
        logger.warning("product_tags: format_ad_name вызван с пустым именем, тег не добавлен")
        return base_name
    if product not in VALID_PRODUCTS:
        return base_name

    clean_name = strip_product_tag(base_name)
    tag = PRODUCTS[product]["tag"]
    return f"{clean_name} {tag}"
