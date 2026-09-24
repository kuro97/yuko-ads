"""
Тесты контекста "bot" в провайдере токена/аккаунта Facebook.
Зеркалят поведение контекста "online": happy path (env задан → значение),
error case (env пуст → RuntimeError). Существующие FB-аккаунты не затрагиваем.
"""
import pytest

from services.fb_token_provider import (
    fb_account,
    get_fb_token,
    get_fb_account_id,
)


def test_get_fb_token_bot_context_returns_token(monkeypatch):
    """Happy path: в контексте "bot" при заданном FB_TOKEN_BOT возвращается он."""
    monkeypatch.setattr("config.FB_TOKEN_BOT", "bot-token-123")
    with fb_account("bot"):
        assert get_fb_token() == "bot-token-123"


def test_get_fb_token_bot_context_empty_raises(monkeypatch):
    """Error case: в контексте "bot" при пустом FB_TOKEN_BOT — RuntimeError."""
    monkeypatch.setattr("config.FB_TOKEN_BOT", "")
    with fb_account("bot"):
        with pytest.raises(RuntimeError, match="FB_TOKEN_BOT"):
            get_fb_token()


def test_get_fb_account_id_bot_context_returns_id(monkeypatch):
    """Happy path: в контексте "bot" при заданном FB_ACCOUNT_ID_BOT возвращается id."""
    monkeypatch.setattr("config.FB_ACCOUNT_ID_BOT", "999888777")
    with fb_account("bot"):
        assert get_fb_account_id() == "999888777"


def test_get_fb_account_id_bot_context_strips_act_prefix(monkeypatch):
    """Edge case: префикс act_ отрезается, как и для online-контекста."""
    monkeypatch.setattr("config.FB_ACCOUNT_ID_BOT", "act_999888777")
    with fb_account("bot"):
        assert get_fb_account_id() == "999888777"


def test_get_fb_account_id_bot_context_empty_raises(monkeypatch):
    """Error case: в контексте "bot" при пустом FB_ACCOUNT_ID_BOT — RuntimeError."""
    monkeypatch.setattr("config.FB_ACCOUNT_ID_BOT", "")
    with fb_account("bot"):
        with pytest.raises(RuntimeError, match="FB_ACCOUNT_ID_BOT"):
            get_fb_account_id()
