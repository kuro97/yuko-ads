"""
Copywriter — генератор рекламных текстов через Claude API.

Использует claude-3-5-haiku для генерации вариантов
рекламных текстов для сервиса ACME.
"""

import re

import anthropic
import config


def _language_label(language: str) -> str:
    """Название языка для промпта: "l2" → второй язык, всё остальное → основной (L1).

    Названия берутся из config (L1_LANGUAGE_NAME / L2_LANGUAGE_NAME).
    """
    if (language or "").strip().lower() == "l2":
        return str(getattr(config, "L2_LANGUAGE_NAME", "английский"))
    return str(getattr(config, "L1_LANGUAGE_NAME", "русский"))


def generate_ad_texts(prompt: str, count: int = 5, language: str = "l1") -> list[str]:
    """Генерирует варианты рекламных текстов через Claude API.

    Args:
        prompt: описание продукта / оффера / аудитории
        count: количество вариантов (от 3 до 10)
        language: язык — "l1" (основной, по умолчанию) или "l2" (второй язык)

    Returns:
        Список рекламных текстов

    Raises:
        ValueError: невалидные данные или нет API-ключа
        RuntimeError: ошибка Claude API
    """
    # Валидация
    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY не задан")

    if len(prompt) < 10:
        raise ValueError("prompt слишком короткий (минимум 10 символов)")

    if count < 3 or count > 10:
        raise ValueError("count должен быть от 3 до 10")

    model = getattr(config, "CLAUDE_HAIKU_MODEL", "claude-haiku-4-5")

    # Системный промпт — роль копирайтера ACME
    system_prompt = (
        "Ты — опытный копирайтер сервиса ACME. "
        "ACME продаёт две продуктовые линии: «Продукт A» (PRODA) и «Продукт B» (PRODB). "
        "У компании несколько точек и онлайн-формат. "
        "Пиши убедительно, конкретно, без канцелярита. "
        "Каждый текст должен содержать призыв к действию (CTA)."
    )

    lang_label = _language_label(language)

    user_prompt = (
        f"Напиши {count} вариантов рекламного текста. Язык текста: {lang_label}.\n\n"
        f"Задание: {prompt}\n\n"
        "Требования:\n"
        "- 2-4 предложения на вариант\n"
        "- Разные подходы (эмоциональный, рациональный, социальное доказательство, срочность)\n"
        "- Призыв к действию в конце\n\n"
        f"Выведи ровно {count} вариантов, пронумерованных: 1. 2. 3. и т.д."
    )

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APITimeoutError:
        raise RuntimeError("Claude API таймаут")
    except anthropic.APIError as e:
        raise RuntimeError(f"Claude API ошибка: {e}")

    # Извлекаем текст
    raw = ""
    if response.content:
        raw = response.content[0].text.strip()

    if not raw:
        raise RuntimeError("Claude вернул пустой ответ")

    # Парсим нумерованный список
    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    variants = []
    for line in lines:
        cleaned = re.sub(r"^\d+[\.\)\-\s]+", "", line).strip()
        if cleaned:
            variants.append(cleaned)

    return variants[:count]
