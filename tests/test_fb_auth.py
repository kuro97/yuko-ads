"""Тесты для services/fb_auth.py — Facebook OAuth хелперы."""
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _mock_config(monkeypatch):
    """Мокаем конфиг для всех тестов."""
    monkeypatch.setattr("services.fb_auth.FB_APP_ID", "test-app-id")
    monkeypatch.setattr("services.fb_auth.FB_APP_SECRET", "test-secret")
    monkeypatch.setattr("services.fb_auth.FB_REDIRECT_URI", "http://localhost:8000/auth/facebook/callback")


class TestGetAuthUrl:
    def test_contains_scope(self):
        from services.fb_auth import get_auth_url
        url = get_auth_url("test-state")
        assert "ads_management" in url
        assert "ads_read" in url
        assert "business_management" in url
        assert "email" in url

    def test_contains_state(self):
        from services.fb_auth import get_auth_url
        url = get_auth_url("my-csrf-state")
        assert "my-csrf-state" in url

    def test_contains_client_id(self):
        from services.fb_auth import get_auth_url
        url = get_auth_url("s")
        assert "test-app-id" in url


class TestExchangeCode:
    @patch("services.fb_auth.requests.get")
    def test_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "short-token", "token_type": "bearer"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from services.fb_auth import exchange_code
        result = exchange_code("auth-code-123")
        assert result["access_token"] == "short-token"
        mock_get.assert_called_once()

    @patch("services.fb_auth.requests.get")
    def test_fb_error(self, mock_get):
        from requests.exceptions import HTTPError
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = HTTPError("400 Bad Request")
        mock_get.return_value = mock_resp

        from services.fb_auth import exchange_code
        with pytest.raises(HTTPError):
            exchange_code("bad-code")


class TestExchangeLongLived:
    @patch("services.fb_auth.requests.get")
    def test_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "long-lived-token",
            "token_type": "bearer",
            "expires_in": 5183944,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from services.fb_auth import exchange_long_lived
        result = exchange_long_lived("short-token")
        assert result["access_token"] == "long-lived-token"
        assert result["expires_in"] == 5183944

    @patch("services.fb_auth.requests.get")
    def test_error(self, mock_get):
        from requests.exceptions import HTTPError
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = HTTPError("400")
        mock_get.return_value = mock_resp

        from services.fb_auth import exchange_long_lived
        with pytest.raises(HTTPError):
            exchange_long_lived("bad-token")


class TestGetAdAccounts:
    @patch("services.fb_auth.requests.get")
    def test_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"id": "act_123", "name": "Test Account", "currency": "USD", "account_status": 1},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from services.fb_auth import get_ad_accounts
        accounts = get_ad_accounts("token")
        assert len(accounts) == 1
        assert accounts[0]["name"] == "Test Account"

    @patch("services.fb_auth.requests.get")
    def test_empty(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": []}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from services.fb_auth import get_ad_accounts
        assert get_ad_accounts("token") == []


class TestDebugToken:
    @patch("services.fb_auth.requests.get")
    def test_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {"is_valid": True, "expires_at": 1735689600, "scopes": ["ads_read"]}
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from services.fb_auth import debug_token
        result = debug_token("some-token")
        assert result["is_valid"] is True
