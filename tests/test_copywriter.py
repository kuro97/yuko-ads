"""Тесты генератора рекламных текстов (agent/copywriter.py)."""

from unittest.mock import patch, MagicMock

import pytest

from agent.copywriter import generate_ad_texts


class TestGenerateAdTexts:
    """Тесты функции generate_ad_texts."""

    @patch("agent.copywriter.anthropic.Anthropic")
    @patch("agent.copywriter.config")
    def test_generates_variants(self, mock_config, mock_anthropic_cls):
        """Генерирует 5 вариантов текста."""
        mock_config.ANTHROPIC_API_KEY = "test-key"
        mock_config.CLAUDE_HAIKU_MODEL = "claude-3-5-haiku-20241022"

        # Мокаем ответ Claude
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="1. Первый вариант текста.\n2. Второй вариант.\n3. Третий вариант.\n4. Четвёртый вариант.\n5. Пятый вариант.")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_cls.return_value = mock_client

        result = generate_ad_texts("Сервис ACME: бесплатная консультация по Продукту A", count=5)

        assert len(result) == 5
        assert all(isinstance(v, str) and len(v) > 0 for v in result)
        mock_client.messages.create.assert_called_once()

    @patch("agent.copywriter.anthropic.Anthropic")
    @patch("agent.copywriter.config")
    def test_l2_language(self, mock_config, mock_anthropic_cls):
        """Генерация на втором языке (l2) передаёт в промпт название L2 из конфига."""
        mock_config.ANTHROPIC_API_KEY = "test-key"
        mock_config.CLAUDE_HAIKU_MODEL = "claude-3-5-haiku-20241022"
        mock_config.L1_LANGUAGE_NAME = "русский"
        mock_config.L2_LANGUAGE_NAME = "английский"

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="1. First variant.\n2. Second variant.\n3. Third variant.")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_cls.return_value = mock_client

        result = generate_ad_texts("ACME service, Product A consultation", count=3, language="l2")

        assert len(result) == 3
        # Проверяем что в промпте указан второй язык
        call_args = mock_client.messages.create.call_args
        user_msg = call_args[1]["messages"][0]["content"]
        assert "английский" in user_msg

    @patch("agent.copywriter.config")
    def test_no_api_key(self, mock_config):
        """Без API-ключа — ValueError."""
        mock_config.ANTHROPIC_API_KEY = None

        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY не задан"):
            generate_ad_texts("Сервис ACME: бесплатная консультация по Продукту A")

    @patch("agent.copywriter.config")
    def test_short_prompt(self, mock_config):
        """Слишком короткий промпт — ValueError."""
        mock_config.ANTHROPIC_API_KEY = "test-key"

        with pytest.raises(ValueError, match="prompt слишком короткий"):
            generate_ad_texts("Короткий")

    @patch("agent.copywriter.config")
    def test_invalid_count(self, mock_config):
        """count вне диапазона 3-10 — ValueError."""
        mock_config.ANTHROPIC_API_KEY = "test-key"

        with pytest.raises(ValueError, match="count должен быть от 3 до 10"):
            generate_ad_texts("Сервис ACME: бесплатная консультация по Продукту A", count=15)

    @patch("agent.copywriter.anthropic.Anthropic")
    @patch("agent.copywriter.config")
    def test_claude_api_error(self, mock_config, mock_anthropic_cls):
        """Ошибка Claude API — RuntimeError."""
        mock_config.ANTHROPIC_API_KEY = "test-key"
        mock_config.CLAUDE_HAIKU_MODEL = "claude-3-5-haiku-20241022"

        import anthropic
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = anthropic.APIError(
            message="Rate limit",
            request=MagicMock(),
            body=None,
        )
        mock_anthropic_cls.return_value = mock_client

        with pytest.raises(RuntimeError, match="Claude API ошибка"):
            generate_ad_texts("Сервис ACME: бесплатная консультация по Продукту A")
