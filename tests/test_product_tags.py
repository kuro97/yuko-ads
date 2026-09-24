"""
Тесты services/product_tags.py: классификатор продукта, форматтер имени,
регресс парсеров города/темы на именах с продуктовым тегом-суффиксом.

См. docs/specs/ARCH-product-tags.md §9 (задача T1).
"""

from unittest.mock import MagicMock, patch

from services.product_tags import (
    DEFAULT_PRODUCT,
    VALID_PRODUCTS,
    _llm_classify_product,
    classify_product,
    format_ad_name,
    normalize_product,
    strip_product_tag,
)


# ---------------------------------------------------------------------------
# classify_product — приоритет источников
# ---------------------------------------------------------------------------

def test_classify_by_label_proda():
    assert classify_product("тема", trello_labels=["PRODA"]) == "PRODA"


def test_classify_by_label_prodb():
    assert classify_product("x", trello_labels=["PRODB запуск"]) == "PRODB"


def test_classify_by_label_start():
    assert classify_product("x", trello_labels=["Стартовый пакет"]) == "СТАРТ"


def test_classify_default_obshaya():
    assert classify_product("просто консультация") == "ОБЩАЯ"


def test_classify_priority_label_over_product():
    """Trello-метка важнее target_product из KB."""
    result = classify_product("x", trello_labels=["PRODB"], target_product="PRODA")
    assert result == "PRODB"


def test_classify_priority_product_over_keyword():
    """target_product из KB важнее keyword-эвристики по имени."""
    result = classify_product("пакет 6", target_product="PRODA")
    assert result == "PRODA"


def test_classify_keyword_prodb():
    assert classify_product("CityB | Тема А / пакет 6") == "PRODB"


def test_classify_keyword_prodb_package_word_form():
    """Keyword «пакет 6» ловится и в словоформе («пакете 6») — единственный пакет PRODB."""
    assert classify_product("Что входит в пакете 6") == "PRODB"


def test_classify_keyword_proda():
    assert classify_product("Весенняя акция в PRODA") == "PRODA"


def test_classify_keyword_start():
    assert classify_product("Пакет для новичков") == "СТАРТ"


def test_classify_none_safety_returns_obshaya():
    """None на всех аргументах не должен ронять функцию."""
    assert classify_product(None, None, None, None) == "ОБЩАЯ"


def test_classify_never_raises_on_garbage_labels():
    """Мусорные (не-строковые) метки в списке не должны ронять функцию."""
    result = classify_product("x", trello_labels=[None, 123, ""])
    assert result in VALID_PRODUCTS


def test_classify_word_boundary_inner_substring_is_not_prodb():
    """«непродвинутых» содержит подстроку «продвинут» (keyword PRODB), но НЕ должен
    ловиться как PRODB — keyword-регекс проверяет границу слова слева."""
    assert classify_product("Гайд для непродвинутых пользователей") == "ОБЩАЯ"


def test_classify_does_not_call_llm(monkeypatch):
    """classify_product — чистая функция, LLM НЕ зовёт даже на «неочевидном»
    эмоциональном заходе без keyword-совпадений (LLM-добор — отдельный шаг
    бэкфилла, не эта функция)."""
    def _boom(*args, **kwargs):
        raise AssertionError("classify_product не должен звать _llm_classify_product")

    monkeypatch.setattr("services.product_tags._llm_classify_product", _boom)

    result = classify_product("критика рынка")

    assert result == "ОБЩАЯ"


# ---------------------------------------------------------------------------
# normalize_product
# ---------------------------------------------------------------------------

def test_normalize_product_known_alias():
    assert normalize_product("PRODD") == "PRODA"


def test_normalize_product_garbage_returns_none():
    assert normalize_product("zzz") is None


def test_normalize_product_empty_returns_none():
    assert normalize_product(None) is None
    assert normalize_product("") is None


# ---------------------------------------------------------------------------
# _llm_classify_product — fail-safe без сети, паттерн brief_generator
# ---------------------------------------------------------------------------

def test_llm_classify_no_api_key_returns_default(monkeypatch):
    import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)

    result = _llm_classify_product("критика рынка")

    assert result == DEFAULT_PRODUCT


def test_llm_classify_success(monkeypatch):
    import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = "  proda  "
    fake_response = MagicMock()
    fake_response.content = [fake_text_block]

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_response
        mock_anthropic_cls.return_value = mock_client

        result = _llm_classify_product("Критика рынка", "История Олега про выбор сервиса")

    assert result == "PRODA"
    _, call_kwargs = mock_client.messages.create.call_args
    assert call_kwargs.get("thinking") == {"type": "disabled"}


def test_llm_classify_thinking_block_first(monkeypatch):
    """Первый блок — ThinkingBlock (без .text), продукт берём из TextBlock дальше."""
    import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    fake_thinking_block = MagicMock(spec=["type", "thinking"])
    fake_thinking_block.type = "thinking"
    fake_thinking_block.thinking = "рассуждения модели"

    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = "PRODB"

    fake_response = MagicMock()
    fake_response.content = [fake_thinking_block, fake_text_block]

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_response
        mock_anthropic_cls.return_value = mock_client

        result = _llm_classify_product("пакет 6", "выход на следующий уровень")

    assert result == "PRODB"


def test_llm_classify_answer_outside_registry_returns_default(monkeypatch):
    import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = "непонятно"
    fake_response = MagicMock()
    fake_response.content = [fake_text_block]

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_response
        mock_anthropic_cls.return_value = mock_client

        result = _llm_classify_product("что-то невнятное")

    assert result == DEFAULT_PRODUCT


def test_llm_classify_error_returns_default(monkeypatch):
    import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("Claude API таймаут")
        mock_anthropic_cls.return_value = mock_client

        result = _llm_classify_product("x")

    assert result == DEFAULT_PRODUCT


# ---------------------------------------------------------------------------
# format_ad_name / strip_product_tag
# ---------------------------------------------------------------------------

def test_format_ad_name_adds_tag():
    result = format_ad_name("CityA | Тема / Хук", "PRODA")
    assert result == "CityA | Тема / Хук [PRODA]"


def test_format_ad_name_idempotent():
    result = format_ad_name("X [PRODB]", "PRODB")
    assert result == "X [PRODB]"


def test_format_ad_name_changes_tag():
    result = format_ad_name("X [PRODB]", "PRODA")
    assert result == "X [PRODA]"


def test_format_ad_name_invalid_product_unchanged():
    assert format_ad_name("X", "ФУ") == "X"


def test_format_ad_name_empty_name_unchanged():
    assert format_ad_name("", "PRODA") == ""


def test_strip_product_tag_removes_suffix():
    assert strip_product_tag("CityA | Тема / Хук [PRODA]") == "CityA | Тема / Хук"


def test_strip_product_tag_no_tag_passthrough():
    assert strip_product_tag("CityA | Тема / Хук") == "CityA | Тема / Хук"


def test_strip_product_tag_empty_string():
    assert strip_product_tag("") == ""


# ---------------------------------------------------------------------------
# Регресс парсеров города/темы на именах с тегом-суффиксом (AC4)
# ---------------------------------------------------------------------------

def test_regress_creative_briefs_extract_city():
    from services.creative_briefs import extract_city

    assert extract_city("CityA | Тема / Хук [PRODA]") == "CityA"


def test_regress_insights_extract_topic():
    from services.insights import extract_topic

    assert extract_topic("CityA | Тема / Хук [PRODA]") == "Тема"


def test_regress_creative_briefs_extract_topic_documented_exception():
    """creative_briefs.extract_topic НЕ режет по '/' — тег остаётся внутри
    строки. Это задокументированное исключение (§8 спеки), не баг: там topic —
    отображаемое поле, не ключ группировки."""
    from services.creative_briefs import extract_topic

    assert extract_topic("CityA | Тема / Хук [PRODA]") == "Тема / Хук [PRODA]"


def test_regress_evening_report_city_from_ad_name():
    from services.evening_report import _city_from_ad_name

    assert _city_from_ad_name("CityB | X [PRODB]") == "CityB"
