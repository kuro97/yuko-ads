"""Тесты генератора рекламных текстов (services/text_generator.py)."""

from unittest.mock import patch, MagicMock

import pytest

from services.text_generator import generate_ad_texts


class TestTextGenerator:
    """Тесты функции generate_ad_texts (OpenAI)."""

    @patch("services.text_generator.openai.OpenAI")
    @patch("services.text_generator.config")
    def test_generates_variants(self, mock_config, mock_openai_cls):
        """Генерирует 5 вариантов текста."""
        mock_config.OPENAI_API_KEY = "test-key"
        mock_config.OPENAI_MODEL = "gpt-4o-mini"

        mock_message = MagicMock()
        mock_message.content = (
            "1. Первый вариант текста.\n"
            "2. Второй вариант.\n"
            "3. Третий вариант.\n"
            "4. Четвёртый вариант.\n"
            "5. Пятый вариант."
        )
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai_cls.return_value = mock_client

        result = generate_ad_texts(
            "Сервис ACME: бесплатная консультация по Продукту A", count=5
        )

        assert len(result) == 5
        assert all(isinstance(v, str) and len(v) > 0 for v in result)
        mock_client.chat.completions.create.assert_called_once()

    @patch("services.text_generator.openai.OpenAI")
    @patch("services.text_generator.config")
    def test_l2_language(self, mock_config, mock_openai_cls):
        """Генерация на втором языке (l2) передаёт в промпт название L2 из конфига."""
        mock_config.OPENAI_API_KEY = "test-key"
        mock_config.OPENAI_MODEL = "gpt-4o-mini"
        mock_config.L1_LANGUAGE_NAME = "русский"
        mock_config.L2_LANGUAGE_NAME = "английский"

        mock_message = MagicMock()
        mock_message.content = (
            "1. First variant.\n2. Second variant.\n3. Third variant."
        )
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai_cls.return_value = mock_client

        result = generate_ad_texts("ACME service, Product A consultation", count=3, language="l2")

        assert len(result) == 3
        call_args = mock_client.chat.completions.create.call_args
        messages = call_args[1]["messages"]
        user_msg = messages[1]["content"]
        assert "английский" in user_msg

    @patch("services.text_generator.config")
    def test_no_api_key(self, mock_config):
        """Без API-ключа — ValueError."""
        mock_config.OPENAI_API_KEY = None

        with pytest.raises(ValueError, match="OPENAI_API_KEY не задан"):
            generate_ad_texts("Сервис ACME: бесплатная консультация по Продукту A")

    @patch("services.text_generator.config")
    def test_short_prompt(self, mock_config):
        """Слишком короткий промпт — ValueError."""
        mock_config.OPENAI_API_KEY = "test-key"

        with pytest.raises(ValueError, match="prompt слишком короткий"):
            generate_ad_texts("Короткий")

    @patch("services.text_generator.config")
    def test_invalid_count(self, mock_config):
        """count вне диапазона 3-10 — ValueError."""
        mock_config.OPENAI_API_KEY = "test-key"

        with pytest.raises(ValueError, match="count должен быть от 3 до 10"):
            generate_ad_texts(
                "Сервис ACME: бесплатная консультация по Продукту A", count=15
            )

    @patch("services.text_generator.openai.OpenAI")
    @patch("services.text_generator.config")
    def test_openai_api_error(self, mock_config, mock_openai_cls):
        """Ошибка OpenAI API — RuntimeError."""
        mock_config.OPENAI_API_KEY = "test-key"
        mock_config.OPENAI_MODEL = "gpt-4o-mini"

        import openai as openai_mod

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = openai_mod.APIError(
            message="Rate limit",
            request=MagicMock(),
            body=None,
        )
        mock_openai_cls.return_value = mock_client

        with pytest.raises(RuntimeError, match="OpenAI API ошибка"):
            generate_ad_texts("Сервис ACME: бесплатная консультация по Продукту A")

    @patch("services.text_generator.openai.OpenAI")
    @patch("services.text_generator.config")
    def test_empty_response(self, mock_config, mock_openai_cls):
        """Пустой ответ от OpenAI — RuntimeError."""
        mock_config.OPENAI_API_KEY = "test-key"
        mock_config.OPENAI_MODEL = "gpt-4o-mini"

        mock_message = MagicMock()
        mock_message.content = ""
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai_cls.return_value = mock_client

        with pytest.raises(RuntimeError, match="OpenAI вернул пустой ответ"):
            generate_ad_texts("Сервис ACME: бесплатная консультация по Продукту A")
