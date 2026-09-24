"""
Copywriter v2 — генератор рекламных текстов через Claude Sonnet.

Отличия от v1:
  - Structured outputs через tool_use (Pydantic модели)
  - Few-shot примеры из KB (winners + losers)
  - Prompt caching для system prompt и few-shot блока
  - Claude Sonnet вместо Haiku
"""

import logging
import time

import anthropic
import config
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Системный промпт v2 — полный контекст ACME
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_V2 = """Ты — старший копирайтер сервиса ACME.

## О бренде ACME (шаблон — замените фактами своей компании)
- ACME — сервис с двумя продуктовыми линиями: «Продукт A» (PRODA) и «Продукт B» (PRODB)
- Несколько точек в разных городах
- Также есть онлайн-формат
- Своя методика работы, N+ клиентов, персональный менеджер на связи
- Целевая аудитория: взрослые 25-45 лет, которые ищут решение своей задачи

## Факты и цифры ACME (шаблон — используй в текстах только подтверждённые факты своей компании)
- N+ клиентов за всё время
- X лет на рынке
- N экспертов в команде
- Точки в нескольких городах + онлайн-формат
- Своя методика — не шаблонное решение, а система с консультацией и персональным менеджером
- Менеджер на связи — не бросаем клиента после покупки
- Бесплатная консультация — сначала разбираем задачу клиента, потом предлагаем план

## Продуктовые линии и сезонность (шаблон)
- PRODA («Продукт A») — основной спрос в сезон A
- PRODB («Продукт B») — основной спрос в сезон B
- PRODC / PRODD / PRODE («Продукт C/D/E») — дополнительные линии, если они есть

## Контекст аудитории (шаблон — замените инсайтами своего рынка)
- Клиент хочет результата без лишнего риска и потери времени
- Бонус и выгодные условия — сильный аргумент: экономия заметна в деньгах
- Доверие к экспертам и отзывам реальных клиентов
- Покупка — осознанное решение: клиенту важно понимать, за что он платит

## ТАБУ-слова и фразы (НИКОГДА не использовать)
- "гарантируем результат" — нельзя гарантировать, это обман
- "100% результат" — невозможно, нарушает закон о рекламе
- "другие компании плохие" — не критикуем конкурентов
- "вы всё делаете неправильно" — не оскорбляем клиента
- "без усилий / мгновенно" — обесценивает реальную работу клиента

## Формат вывода
Для каждого варианта создай структурированный рекламный текст:
- hook: цепляющая первая строка (1 предложение)
- body: основной текст с аргументацией (2-4 предложения)
- cta: призыв к действию (1 предложение)
- angle: угол подачи (один из указанных)
- hook_type: тип хука (один из указанных)
- format: формат текста
  Форматы:
  - static_post: текст для статичного поста (картинка + подпись). Короткий, ёмкий, 2-4 предложения в body.
  - video_script: сценарий для видеоролика. Hook = первые 3 секунды (голос/текст на экране). Body = основная часть сценария (что говорит/показывает спикер, 15-30 секунд). CTA = финальный призыв.
  - story: текст для Instagram/Facebook story. Максимально короткий, 1-2 предложения, визуально-ориентированный.
- target_persona: для кого этот текст
- primary_language: язык текста
- rationale: почему этот вариант должен работать (1-2 предложения)

## Правила генерации
1. Каждый вариант должен использовать РАЗНЫЙ угол подачи (angle)
2. Каждый вариант должен использовать РАЗНЫЙ тип хука (hook_type)
3. Не повторяй одну и ту же структуру — разнообразие важнее качества одного текста
4. Пиши конкретно: цифры > абстракции ("до конца акции 47 дней" > "акция скоро закончится")
5. CTA должен быть конкретным: "Оставьте номер" > "Узнайте больше"
6. Используй эмоциональные триггеры из контекста аудитории
7. Для второго языка (L2): пиши естественно, не переводи буквально с основного
8. Для video_script: пиши как разговорную речь, не как текст для чтения. Спикер говорит на камеру.
"""


# ---------------------------------------------------------------------------
# Pydantic модели
# ---------------------------------------------------------------------------

class AdBrief(BaseModel):
    """Бриф для генерации рекламных текстов."""
    product: str = Field(description="Продукт («Продукт A», «Продукт B», etc.)")
    audience: str = Field(description="Целевая аудитория")
    offer: str = Field(description="Оффер")
    format: str = Field(default="static_post", description="static_post / video_script / story")
    city: str | None = Field(default=None, description="Город")
    product_line: str | None = Field(
        default=None,
        description="Продуктовая линия: PRODA / PRODB / PRODC / PRODD / PRODE",
    )
    language: str = Field(default="l1", description="l1 (основной) / l2 (второй язык)")
    count: int = Field(default=5, ge=2, le=10, description="Количество вариантов")
    angles: list[str] = Field(default=[], description="Желаемые углы")
    hook_types: list[str] = Field(default=[], description="Желаемые типы хуков")


class AdVariant(BaseModel):
    """Один вариант рекламного текста (structured output от LLM)."""
    hook: str = Field(description="Первая строка — хук")
    body: str = Field(description="Основной текст")
    cta: str = Field(description="Призыв к действию")
    angle: str = Field(description="Угол подачи (slug)")
    hook_type: str = Field(description="Тип хука (slug)")
    format: str = Field(description="Формат: short_post / long_post / story")
    target_persona: str = Field(description="Целевая персона")
    primary_language: str = Field(description="Язык: l1 / l2")
    rationale: str = Field(description="Почему должен работать")


class GeneratedAdBatch(BaseModel):
    """Батч сгенерированных вариантов."""
    variants: list[AdVariant]


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _language_label(language: str) -> str:
    """Название языка для промпта: "l2" → второй язык, иначе основной (L1).

    Названия берутся из config (L1_LANGUAGE_NAME / L2_LANGUAGE_NAME).
    """
    if language == "l2":
        return str(getattr(config, "L2_LANGUAGE_NAME", "английский"))
    return str(getattr(config, "L1_LANGUAGE_NAME", "русский"))


def _build_system_message() -> list[dict]:
    """Строит system message с cache_control для prompt caching.

    Returns:
        list[dict]: system blocks для Anthropic API
        Формат: [{"type": "text", "text": SYSTEM_PROMPT_V2, "cache_control": {"type": "ephemeral"}}]
    """
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT_V2,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _build_few_shot_block(
    winners: list[dict],
    losers: list[dict],
    brief: AdBrief,
) -> str:
    """Формирует текстовый блок few-shot примеров для user message.

    Winners: top-K по spend (filtered by city/product_line/language если указаны в brief).
    Losers: worst-3 по CPL (для контраста — чтобы LLM знала что НЕ делать).

    Returns:
        str: отформатированный блок примеров для вставки в user message
    """
    if not winners and not losers:
        return "Нет примеров в KB."

    parts = ["## Примеры из базы знаний (для ориентира)\n"]

    for w in winners:
        ad_body = (w.get("ad_body") or "")[:200]
        city = w.get("city", "?")
        hook_rate = w.get("hook_rate", 0)
        cpl = w.get("cpl", 0)
        leads = w.get("leads", 0)
        angle = w.get("angle") or ""
        hook_type = w.get("hook_type") or ""

        parts.append(
            f"ПРИМЕР-ПОБЕДИТЕЛЬ:\n"
            f"Текст: {ad_body}\n"
            f"Город: {city}, Hook Rate: {hook_rate}%, CPL: ${cpl}, Leads: {leads}"
            + (f", Angle: {angle}" if angle else "")
            + (f", Hook Type: {hook_type}" if hook_type else "")
            + "\n"
        )

    for lsr in losers:
        ad_body = (lsr.get("ad_body") or "")[:200]
        cpl = lsr.get("cpl", 0)
        ctr = lsr.get("ctr", 0)

        parts.append(
            f"ПРИМЕР-ПРОИГРАВШИЙ (избегай такого подхода):\n"
            f"Текст: {ad_body}\n"
            f"CPL: ${cpl}, CTR: {ctr}%\n"
        )

    return "\n".join(parts)


def _build_user_message(
    brief: AdBrief,
    few_shot_block: str,
    learnings: list[str] | None = None,
) -> list[dict]:
    """Строит user message: few-shot примеры + (опционально) выводы + бриф с cache_control.

    Args:
        brief: бриф для генерации
        few_shot_block: блок примеров из KB
        learnings: список проверенных выводов (statements), вставляется перед брифом

    Returns:
        list[dict]: content blocks для Anthropic API
        Формат: [
            {"type": "text", "text": few_shot_block, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": brief_text},  # включает блок выводов если переданы
        ]
    """
    lang_label = _language_label(brief.language)

    # Формируем текст брифа
    brief_lines = [
        f"Напиши {brief.count} вариантов рекламного текста. Язык текста: {lang_label}.",
        "",
        f"Продукт: {brief.product}",
        f"Аудитория: {brief.audience}",
        f"Оффер: {brief.offer}",
        f"Формат: {brief.format}",
    ]
    if brief.city:
        brief_lines.append(f"Город: {brief.city}")
    if brief.product_line:
        brief_lines.append(f"Продуктовая линия: {brief.product_line}")
    if brief.angles:
        brief_lines.append(f"Желаемые углы: {', '.join(brief.angles)}")
    if brief.hook_types:
        brief_lines.append(f"Желаемые хуки: {', '.join(brief.hook_types)}")

    # Минимальное покрытие разнообразия
    min_angles = min(brief.count, 5)
    min_personas = min(brief.count, 3)
    brief_lines.append("")
    brief_lines.append(
        f"Покрой минимум {min_angles} разных angles и {min_personas} разные personas."
    )

    # Вставляем блок проверенных выводов перед брифом (если есть)
    if learnings:
        learnings_lines = ["## Проверенные выводы (учитывай)"]
        for lesson in learnings:
            learnings_lines.append(f"- {lesson}")
        learnings_lines.append("")
        brief_text = "\n".join(learnings_lines) + "\n".join(brief_lines)
    else:
        brief_text = "\n".join(brief_lines)

    return [
        {
            "type": "text",
            "text": few_shot_block,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": brief_text,
        },
    ]


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------

def generate_ad_batch(
    brief: AdBrief,
    few_shot_winners: list[dict] | None = None,
    few_shot_losers: list[dict] | None = None,
    learnings: list[str] | None = None,
) -> tuple[GeneratedAdBatch, dict]:
    """Генерирует батч рекламных текстов через Claude Sonnet.

    Args:
        brief: структурированный бриф
        few_shot_winners: top-K winners из KB (dict с ad_body, hook_rate, cpl, city и др.)
        few_shot_losers: 2-3 losers из KB для контраста
        learnings: проверенные выводы (statements из таблицы learnings, confidence='confirmed').
                   Если None или [] — поведение прежнее, блок выводов не добавляется.

    Returns:
        tuple: (GeneratedAdBatch, usage_dict)
        usage_dict содержит: input_tokens, output_tokens, cache_creation_input_tokens,
                             cache_read_input_tokens, latency_ms, model

    Raises:
        ValueError: невалидные данные или нет API-ключа
        RuntimeError: ошибка Claude API
    """
    # Валидация входных данных
    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY не задан")

    if not brief.product or not brief.product.strip():
        raise ValueError("product обязателен")

    if brief.count < 2 or brief.count > 10:
        raise ValueError("count должен быть от 2 до 10")

    if brief.language not in ("l1", "l2"):
        raise ValueError("language должен быть l1 или l2")

    winners = few_shot_winners or []
    losers = few_shot_losers or []

    # Формируем компоненты запроса
    system_messages = _build_system_message()
    few_shot_block = _build_few_shot_block(winners, losers, brief)
    user_content = _build_user_message(brief, few_shot_block, learnings=learnings)

    # Few-shot блок добавляем в system как второй кешируемый блок
    if winners or losers:
        system_messages.append(
            {
                "type": "text",
                "text": few_shot_block,
                "cache_control": {"type": "ephemeral"},
            }
        )

    # Tool schema для structured output
    tool_schema = {
        "name": "generate_ads",
        "description": "Сгенерированные варианты рекламных текстов",
        "input_schema": GeneratedAdBatch.model_json_schema(),
    }

    client = anthropic.Anthropic(api_key=api_key)

    logger.info(
        "Генерация батча: count=%d, language=%s, model=%s",
        brief.count,
        brief.language,
        config.CLAUDE_SONNET_MODEL,
    )

    # Замер времени выполнения
    start_time = time.monotonic()

    try:
        response = client.messages.create(
            model=config.CLAUDE_SONNET_MODEL,
            max_tokens=4096,
            system=system_messages,
            messages=[{"role": "user", "content": user_content}],
            tools=[tool_schema],
            tool_choice={"type": "tool", "name": "generate_ads"},
        )
    except anthropic.APITimeoutError:
        raise RuntimeError("Claude API таймаут")
    except anthropic.APIError as exc:
        raise RuntimeError(f"Claude API ошибка: {exc}")

    latency_ms = int((time.monotonic() - start_time) * 1000)

    # Проверяем что ответ не пустой
    if not response.content:
        raise RuntimeError("Claude вернул пустой ответ")

    # Парсим structured output из tool_use блока
    batch: GeneratedAdBatch | None = None
    for block in response.content:
        if block.type == "tool_use" and block.name == "generate_ads":
            try:
                batch = GeneratedAdBatch.model_validate(block.input)
            except Exception as exc:
                logger.error("Не удалось распарсить structured output: %s", exc)
                logger.debug("Raw input: %s", block.input)
                raise RuntimeError("Не удалось распарсить structured output") from exc
            break

    if batch is None:
        raise RuntimeError("Claude вернул пустой ответ")

    # Собираем usage метрики
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        "latency_ms": latency_ms,
        "model": config.CLAUDE_SONNET_MODEL,
    }

    logger.info(
        "Батч сгенерирован: %d вариантов, %d in/%d out tokens, %dms",
        len(batch.variants),
        usage["input_tokens"],
        usage["output_tokens"],
        latency_ms,
    )

    return batch, usage
