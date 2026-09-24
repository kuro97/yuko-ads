"""Тесты для services/fb_credentials.py — шифрование и хранение токенов."""
import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def creds_dir(tmp_path, monkeypatch):
    """Перенаправляем хранилище credentials во временную директорию."""
    import services.fb_credentials as mod
    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mod, "CREDENTIALS_FILE", tmp_path / "fb_credentials.json")
    monkeypatch.setattr(mod, "KEY_FILE", tmp_path / ".fernet_key")
    return tmp_path


class TestSaveAndLoad:
    def test_roundtrip(self, creds_dir):
        from services.fb_credentials import save_credentials, load_credentials, get_active_token

        save_credentials({
            "access_token": "my-secret-token",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            "user_id": "123",
        })

        creds = load_credentials()
        assert creds is not None
        assert "access_token_encrypted" in creds
        assert "my-secret-token" not in json.dumps(creds)  # токен зашифрован

        token = get_active_token()
        assert token == "my-secret-token"

    def test_load_no_file(self, creds_dir):
        from services.fb_credentials import load_credentials
        assert load_credentials() is None


class TestDelete:
    def test_delete_removes_file(self, creds_dir):
        from services.fb_credentials import save_credentials, delete_credentials, load_credentials

        save_credentials({"access_token": "tok", "expires_at": "2030-01-01T00:00:00"})
        assert load_credentials() is not None

        delete_credentials()
        assert load_credentials() is None


class TestGetActiveToken:
    def test_returns_decrypted(self, creds_dir):
        from services.fb_credentials import save_credentials, get_active_token

        save_credentials({
            "access_token": "decrypted-value",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        })
        assert get_active_token() == "decrypted-value"

    def test_returns_none_when_expired(self, creds_dir):
        from services.fb_credentials import save_credentials, get_active_token

        save_credentials({
            "access_token": "old-token",
            "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        })
        assert get_active_token() is None


class TestGetActiveAccountId:
    def test_returns_account_id(self, creds_dir):
        from services.fb_credentials import save_credentials, get_active_account_id

        save_credentials({
            "access_token": "tok",
            "account_id": "act_999",
            "expires_at": "2030-01-01T00:00:00",
        })
        assert get_active_account_id() == "act_999"

    def test_returns_none_when_no_creds(self, creds_dir):
        from services.fb_credentials import get_active_account_id
        assert get_active_account_id() is None


class TestIsTokenExpiringSoon:
    def test_expiring_soon(self, creds_dir):
        from services.fb_credentials import save_credentials, is_token_expiring_soon

        save_credentials({
            "access_token": "tok",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        })
        assert is_token_expiring_soon(days=7) is True

    def test_not_expiring_soon(self, creds_dir):
        from services.fb_credentials import save_credentials, is_token_expiring_soon

        save_credentials({
            "access_token": "tok",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        })
        assert is_token_expiring_soon(days=7) is False


class TestFernetKeyAutoGeneration:
    def test_auto_generates_key(self, creds_dir, monkeypatch):
        """Без env var и без файла — генерирует ключ автоматически."""
        monkeypatch.setattr("config.FERNET_KEY", None)

        from services.fb_credentials import save_credentials, get_active_token

        save_credentials({
            "access_token": "auto-key-token",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        })

        # Ключ записан в файл
        key_file = creds_dir / ".fernet_key"
        assert key_file.exists()

        # Токен расшифровывается
        assert get_active_token() == "auto-key-token"
