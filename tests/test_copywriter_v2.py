"""Тесты для agent/copywriter_v2.py — генерация рекламных текстов v2."""

import pytest
from unittest.mock import patch, MagicMock

from agent.copywriter_v2 import AdBrief, AdVariant, GeneratedAdBatch, generate_ad_batch


# ---------------------------------------------------------------------------
# Вспомогательные функции для создания мок-ответов
# ---------------------------------------------------------------------------


def _make_tool_use_response(variants_data: list[dict], input_tokens=1000, output_tokens=500):
    """Создаёт мок ответа Claude с tool_use."""
    mock_response = MagicMock()
    mock_response.stop_reason = "tool_use"

    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "generate_ads"
    tool_block.input = {"variants": variants_data}

    mock_response.content = [tool_block]
    mock_response.usage = MagicMock()
    mock_response.usage.input_tokens = input_tokens
    mock_response.usage.output_tokens = output_tokens
    mock_response.usage.cache_creation_input_tokens = 0
    mock_response.usage.cache_read_input_tokens = 0
    mock_response.model = "claude-sonnet-4-20250514"
    return mock_response


def _make_variant_data(n=5):
    """Создаёт N вариантов для мока."""
    return [
        {
            "hook": f"Хук {i + 1}",
            "body": f"Тело текста {i + 1}",
            "cta": f"CTA {i + 1}",
            "angle": "social_proof",
            "hook_type": "question",
            "format": "short_post",
            "target_persona": "client_l1",
            "primary_language": "l1",
            "rationale": f"Работает потому что {i + 1}",
        }
        for i in range(n)
    ]


def _make_brief(**kwargs) -> AdBrief:
    """Создаёт базовый бриф с возможностью переопределить поля."""
    defaults = {
        "product": "Продукт A",
        "audience": "Клиенты 25-45 лет",
        "offer": "Бесплатный пробный период",
    }
    defaults.update(kwargs)
    return AdBrief(**defaults)


# ---------------------------------------------------------------------------
# Тест 1: базовая генерация батча с 5 вариантами
# ---------------------------------------------------------------------------


@patch("agent.copywriter_v2.config")
@patch("agent.copywriter_v2.anthropic.Anthropic")
def test_generates_variants(mock_anthropic_cls, mock_config):
    """generate_ad_batch возвращает (GeneratedAdBatch, usage_dict) с 5 вариантами."""
    mock_config.ANTHROPIC_API_KEY = "test-key"
    mock_config.CLAUDE_SONNET_MODEL = "claude-sonnet-4-20250514"

    # Настраиваем мок клиента
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_tool_use_response(
        _make_variant_data(5), input_tokens=1000, output_tokens=500
    )
    mock_anthropic_cls.return_value = mock_client

    brief = _make_brief(count=5)
    batch, usage = generate_ad_batch(brief)

    # Проверяем типы возвращаемых значений
    assert isinstance(batch, GeneratedAdBatch)
    assert isinstance(usage, dict)

    # Проверяем количество вариантов
    assert len(batch.variants) == 5
    assert all(isinstance(v, AdVariant) for v in batch.variants)

    # Проверяем поля usage
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 500
    assert "latency_ms" in usage
    assert "model" in usage

    # Claude API должен быть вызван ровно один раз
    mock_client.messages.create.assert_called_once()


# ---------------------------------------------------------------------------
# Тест 2: второй язык (L2) в промпте
# ---------------------------------------------------------------------------


@patch("agent.copywriter_v2.config")
@patch("agent.copywriter_v2.anthropic.Anthropic")
def test_l2_language(mock_anthropic_cls, mock_config):
    """brief.language='l2' → в user message указан второй язык из конфига."""
    mock_config.ANTHROPIC_API_KEY = "test-key"
    mock_config.CLAUDE_SONNET_MODEL = "claude-sonnet-4-20250514"
    mock_config.L1_LANGUAGE_NAME = "русский"
    mock_config.L2_LANGUAGE_NAME = "английский"

    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_tool_use_response(
        _make_variant_data(2)
    )
    mock_anthropic_cls.return_value = mock_client

    brief = _make_brief(language="l2", count=2)
    generate_ad_batch(brief)

    # Перехватываем аргументы вызова
    call_kwargs = mock_client.messages.create.call_args[1]
    messages = call_kwargs["messages"]

    # Ищем название второго языка в user message
    user_content_texts = []
    for msg in messages:
        content = msg.get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                user_content_texts.append(block["text"])

    all_text = "\n".join(user_content_texts)

    # В user message должно быть упоминание второго языка
    assert "английский" in all_text, (
        f"Ожидалось 'английский' в user message, но текст: {all_text[:300]}"
    )


# ---------------------------------------------------------------------------
# Тест 3: нет API-ключа → ValueError
# ---------------------------------------------------------------------------


@patch("agent.copywriter_v2.config")
def test_no_api_key(mock_config):
    """config.ANTHROPIC_API_KEY = None → ValueError с сообщением об ошибке."""
    mock_config.ANTHROPIC_API_KEY = None

    brief = _make_brief()
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        generate_ad_batch(brief)


# ---------------------------------------------------------------------------
# Тест 4: пустой product → ValueError
# ---------------------------------------------------------------------------


@patch("agent.copywriter_v2.config")
def test_empty_product(mock_config):
    """brief.product = '' → ValueError с сообщением о product."""
    mock_config.ANTHROPIC_API_KEY = "test-key"

    # Pydantic разрешает пустую строку для product (нет min_length),
    # проверка происходит внутри generate_ad_batch
    brief = AdBrief(product="", audience="Клиенты", offer="Бесплатная консультация")
    with pytest.raises(ValueError, match="product"):
        generate_ad_batch(brief)


# ---------------------------------------------------------------------------
# Тест 5: невалидный count → ValueError
# ---------------------------------------------------------------------------


def test_invalid_count():
    """brief.count < 2 или > 10 → ValidationError от Pydantic (ge=2, le=10)."""
    # count=1 — ниже минимума
    with pytest.raises(Exception, match="count|greater_than_equal|value_error"):
        AdBrief(product="Продукт A", audience="Клиенты", offer="Бесплатная консультация", count=1)

    # count=15 — выше максимума
    with pytest.raises(Exception, match="count|less_than_equal|value_error"):
        AdBrief(product="Продукт A", audience="Клиенты", offer="Бесплатная консультация", count=15)


# ---------------------------------------------------------------------------
# Тест 6: ошибка Claude API → RuntimeError
# ---------------------------------------------------------------------------


@patch("agent.copywriter_v2.config")
@patch("agent.copywriter_v2.anthropic.Anthropic")
def test_claude_api_error(mock_anthropic_cls, mock_config):
    """Claude API бросает Exception → RuntimeError."""
    mock_config.ANTHROPIC_API_KEY = "test-key"
    mock_config.CLAUDE_SONNET_MODEL = "claude-sonnet-4-20250514"

    import anthropic

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = anthropic.APIError(
        message="rate limit exceeded",
        request=MagicMock(),
        body=None,
    )
    mock_anthropic_cls.return_value = mock_client

    brief = _make_brief()
    with pytest.raises(RuntimeError):
        generate_ad_batch(brief)


# ---------------------------------------------------------------------------
# Тест 7: prompt caching — system message содержит cache_control
# ---------------------------------------------------------------------------


@patch("agent.copywriter_v2.config")
@patch("agent.copywriter_v2.anthropic.Anthropic")
def test_prompt_caching_headers(mock_anthropic_cls, mock_config):
    """System message содержит cache_control: {type: ephemeral} для prompt caching."""
    mock_config.ANTHROPIC_API_KEY = "test-key"
    mock_config.CLAUDE_SONNET_MODEL = "claude-sonnet-4-20250514"

    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_tool_use_response(
        _make_variant_data(2)
    )
    mock_anthropic_cls.return_value = mock_client

    brief = _make_brief(count=2)
    generate_ad_batch(brief)

    call_kwargs = mock_client.messages.create.call_args[1]
    system_blocks = call_kwargs["system"]

    # Хотя бы один system блок должен содержать cache_control ephemeral
    has_cache_control = any(
        isinstance(block, dict) and block.get("cache_control") == {"type": "ephemeral"}
        for block in system_blocks
    )
    assert has_cache_control, (
        f"Ожидался cache_control в system blocks, но получено: {system_blocks}"
    )


# ---------------------------------------------------------------------------
# Тест 8: few-shot примеры попадают в user message
# ---------------------------------------------------------------------------


@patch("agent.copywriter_v2.config")
@patch("agent.copywriter_v2.anthropic.Anthropic")
def test_few_shot_included(mock_anthropic_cls, mock_config):
    """winners/losers из few-shot попадают в user message или system message."""
    mock_config.ANTHROPIC_API_KEY = "test-key"
    mock_config.CLAUDE_SONNET_MODEL = "claude-sonnet-4-20250514"

    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_tool_use_response(
        _make_variant_data(2)
    )
    mock_anthropic_cls.return_value = mock_client

    winners = [{"ad_body": "winner text", "cpl": 10.0, "hook_rate": 40.0}]
    losers = [{"ad_body": "loser text", "cpl": 80.0, "ctr": 0.3}]

    brief = _make_brief(count=2)
    generate_ad_batch(brief, few_shot_winners=winners, few_shot_losers=losers)

    call_kwargs = mock_client.messages.create.call_args[1]

    # Собираем весь текст из messages и system
    all_text_parts = []

    # Из messages (user content)
    for msg in call_kwargs.get("messages", []):
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                all_text_parts.append(block["text"])

    # Из system blocks
    for block in call_kwargs.get("system", []):
        if isinstance(block, dict) and block.get("type") == "text":
            all_text_parts.append(block.get("text", ""))

    combined = "\n".join(all_text_parts)

    assert "winner text" in combined, (
        f"Ожидался 'winner text' в запросе к Claude, но не найден. Текст: {combined[:400]}"
    )
    assert "loser text" in combined, (
        f"Ожидался 'loser text' в запросе к Claude, но не найден. Текст: {combined[:400]}"
    )


# ---------------------------------------------------------------------------
# Тест 9: продуктовая линия попадает в бриф, старое свойство читает её же
# ---------------------------------------------------------------------------


@patch("agent.copywriter_v2.config")
@patch("agent.copywriter_v2.anthropic.Anthropic")
def test_product_line_in_prompt(mock_anthropic_cls, mock_config):
    """product_line попадает в бриф и в промпт."""
    mock_config.ANTHROPIC_API_KEY = "test-key"
    mock_config.CLAUDE_SONNET_MODEL = "claude-sonnet-4-20250514"

    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_tool_use_response(_make_variant_data(2))
    mock_anthropic_cls.return_value = mock_client

    brief = _make_brief(product_line="PRODB", count=2)
    assert brief.product_line == "PRODB"
    generate_ad_batch(brief)

    call_kwargs = mock_client.messages.create.call_args[1]
    texts = [
        block["text"]
        for msg in call_kwargs["messages"]
        for block in msg.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    assert "Продуктовая линия: PRODB" in "\n".join(texts)


@patch("agent.copywriter_v2.config")
def test_unknown_language_rejected(mock_config):
    """Язык вне l1/l2 → ValueError до вызова API."""
    mock_config.ANTHROPIC_API_KEY = "test-key"

    brief = _make_brief(language="xx")
    with pytest.raises(ValueError, match="language"):
        generate_ad_batch(brief)
