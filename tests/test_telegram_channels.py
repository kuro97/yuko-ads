"""
Юнит-тесты выбора токена в send_telegram по параметру channel.

channel="health" + TELEGRAM_HEALTH_BOT_TOKEN задан → URL содержит health-токен.
channel="health" + TELEGRAM_HEALTH_BOT_TOKEN не задан → URL содержит основной токен.
channel="ads" (дефолт) → всегда URL содержит основной токен.
"""

import sys
from unittest.mock import patch, MagicMock

import pytest


def _make_mock_requests(ok: bool = True):
    """Создаёт мок requests с нужным json-ответом."""
    mock_requests = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"ok": ok}
    mock_requests.post.return_value = mock_resp
    return mock_requests


class TestSendTelegramChannelSelection:
    """Проверяет выбор токена в send_telegram по параметру channel."""

    def test_health_channel_with_health_token_uses_health_token(self):
        """channel='health' + health-токен задан → URL содержит health-токен."""
        mock_requests = _make_mock_requests()

        # Патчим конфиг и requests через lazy import внутри send_telegram
        with patch.dict(sys.modules, {"requests": mock_requests}), \
             patch("config.TELEGRAM_BOT_TOKEN", "main-bot-token-123"), \
             patch("config.TELEGRAM_CHAT_ID", "999888"), \
             patch("config.TELEGRAM_HEALTH_BOT_TOKEN", "health-bot-token-456"):
            import services.notifications as notif_mod
            result = notif_mod.send_telegram("тест пульс", channel="health")

        assert result is True
        mock_requests.post.assert_called_once()
        called_url = mock_requests.post.call_args[0][0]
        assert "health-bot-token-456" in called_url
        assert "main-bot-token-123" not in called_url

    def test_health_channel_without_health_token_falls_back_to_main(self):
        """channel='health' + health-токен не задан (None) → URL содержит основной токен."""
        mock_requests = _make_mock_requests()

        with patch.dict(sys.modules, {"requests": mock_requests}), \
             patch("config.TELEGRAM_BOT_TOKEN", "main-bot-token-123"), \
             patch("config.TELEGRAM_CHAT_ID", "999888"), \
             patch("config.TELEGRAM_HEALTH_BOT_TOKEN", None):
            import services.notifications as notif_mod
            result = notif_mod.send_telegram("тест пульс без health токена", channel="health")

        assert result is True
        mock_requests.post.assert_called_once()
        called_url = mock_requests.post.call_args[0][0]
        assert "main-bot-token-123" in called_url

    def test_ads_channel_always_uses_main_token(self):
        """channel='ads' → всегда основной токен, даже если health задан."""
        mock_requests = _make_mock_requests()

        with patch.dict(sys.modules, {"requests": mock_requests}), \
             patch("config.TELEGRAM_BOT_TOKEN", "main-bot-token-123"), \
             patch("config.TELEGRAM_CHAT_ID", "999888"), \
             patch("config.TELEGRAM_HEALTH_BOT_TOKEN", "health-bot-token-456"):
            import services.notifications as notif_mod
            result = notif_mod.send_telegram("автопилот отчёт", channel="ads")

        assert result is True
        mock_requests.post.assert_called_once()
        called_url = mock_requests.post.call_args[0][0]
        assert "main-bot-token-123" in called_url
        assert "health-bot-token-456" not in called_url

    def test_default_channel_uses_main_token(self):
        """Без явного channel (дефолт ads) → основной токен."""
        mock_requests = _make_mock_requests()

        with patch.dict(sys.modules, {"requests": mock_requests}), \
             patch("config.TELEGRAM_BOT_TOKEN", "main-bot-token-123"), \
             patch("config.TELEGRAM_CHAT_ID", "999888"), \
             patch("config.TELEGRAM_HEALTH_BOT_TOKEN", "health-bot-token-456"):
            import services.notifications as notif_mod
            result = notif_mod.send_telegram("дефолтный вызов")

        assert result is True
        mock_requests.post.assert_called_once()
        called_url = mock_requests.post.call_args[0][0]
        assert "main-bot-token-123" in called_url
        assert "health-bot-token-456" not in called_url
