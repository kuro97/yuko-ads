"""Supabase клиент — единая точка подключения."""

from supabase import create_client, Client
import config

_client: Client | None = None


def get_supabase() -> Client:
    """Возвращает Supabase клиент (SERVICE_ROLE_KEY, bypass RLS)."""
    global _client
    if _client is None:
        if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
            raise RuntimeError("SUPABASE_URL и SUPABASE_SERVICE_ROLE_KEY обязательны")
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY)
    return _client


def reset_client():
    """Сброс клиента (для тестов)."""
    global _client
    _client = None
