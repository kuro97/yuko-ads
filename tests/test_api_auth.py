"""Тесты middleware X-API-Key для мутирующих эндпоинтов.

Мокаем google.genai через sys.modules ДО импорта web.app,
так как в среде разработки пакет google-generativeai не установлен.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile

# Добавляем корень проекта в path
sys.path.insert(0, str(Path(__file__).parent.parent))

# --- Мокаем google.genai до импорта web.app ---
_google_mock = MagicMock()
_genai_mock = MagicMock()
_genai_types_mock = MagicMock()
sys.modules.setdefault("google", _google_mock)
sys.modules.setdefault("google.genai", _genai_mock)
sys.modules.setdefault("google.genai.types", _genai_types_mock)
# Дополнительно патчим атрибут genai внутри google-мока
_google_mock.genai = _genai_mock

import pytest
from fastapi.testclient import TestClient
from agent.database import init_db


# Импортируем app только после моков выше
from web.app import app  # noqa: E402


# TEST_API_KEY из conftest (через autouse fixture set_test_jwt_secret)
# config.API_KEY будет замокан как "test-secret-key"
CORRECT_KEY = "test-secret-key"
WRONG_KEY = "wrong-key"

# Полный путь вебхука AMO (prefix роутера + эндпоинт)
AMO_WEBHOOK_PATH = "/api/v1/amo/webhooks/lead-created"


@pytest.fixture
def client(tmp_path):
    """TestClient с чистой временной БД."""
    db_path = str(tmp_path / "test_auth.db")
    init_db(db_path=db_path, json_path=str(tmp_path / "nonexistent.json"))
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. Публичные пути (health/root/static) открыты; /api закрыт для GET тоже
# ---------------------------------------------------------------------------

def test_get_health_no_key_is_allowed(client):
    """GET /health без X-API-Key должен вернуть 200 (открытый эндпоинт)."""
    resp = client.get("/health")
    assert resp.status_code == 200


def test_get_root_no_key_is_allowed(client):
    """GET / (дашборд) без X-API-Key — публичен (200)."""
    resp = client.get("/")
    assert resp.status_code == 200


def test_get_static_no_key_is_allowed(client):
    """Статика (/static/...) без ключа доступна (200) — там нет секретов."""
    resp = client.get("/static/index.html")
    assert resp.status_code == 200


def test_get_api_no_key_returns_403(client):
    """Wave 2: GET /api/** без X-API-Key теперь ЗАКРЫТ (403), а не открыт.

    Middleware отклоняет ДО роутинга — внешний провайдер (FB/overview) не
    вызывается, поэтому тест не ходит в сеть."""
    resp = client.get("/api/overview")
    assert resp.status_code == 403
    assert "X-API-Key" in resp.json().get("detail", "")


def test_get_api_wrong_key_returns_403(client):
    """GET /api/** с неверным ключом → 403."""
    resp = client.get("/api/overview", headers={"X-API-Key": WRONG_KEY})
    assert resp.status_code == 403


def test_get_api_correct_key_not_403(client):
    """GET /api/** с верным ключом не режется middleware (не 403/не 401)."""
    resp = client.get("/api/launch-status", headers={"X-API-Key": CORRECT_KEY})
    assert resp.status_code not in (401, 403)


# ---------------------------------------------------------------------------
# 2. POST без X-API-Key → 403
# ---------------------------------------------------------------------------

def test_post_settings_no_key_returns_403(client):
    """POST /api/settings без X-API-Key должен вернуть 403."""
    resp = client.post("/api/settings", json={"auto_apply": True})
    assert resp.status_code == 403
    assert "X-API-Key" in resp.json().get("detail", "")


def test_post_scheduler_no_key_returns_403(client):
    """POST /api/scheduler/run без X-API-Key должен вернуть 403."""
    resp = client.post("/api/scheduler/run", json={})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 3. POST с неверным X-API-Key → 403
# ---------------------------------------------------------------------------

def test_post_settings_wrong_key_returns_403(client):
    """POST /api/settings с неверным X-API-Key должен вернуть 403."""
    resp = client.post(
        "/api/settings",
        json={"auto_apply": False},
        headers={"X-API-Key": WRONG_KEY},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 4. POST с верным X-API-Key → НЕ 403
# ---------------------------------------------------------------------------

def test_post_settings_correct_key_not_403(client):
    """POST /api/settings с верным X-API-Key не должен возвращать 403 от middleware.
    Допустимы 200 / 422 / 500 (внутренняя логика) — но не 403.
    """
    resp = client.post(
        "/api/settings",
        json={"auto_apply": True},
        headers={"X-API-Key": CORRECT_KEY},
    )
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# 5. POST на вебхук AMO без X-API-Key → НЕ 403 (исключение из middleware)
# ---------------------------------------------------------------------------

def test_amo_webhook_exempt_from_api_key_middleware(client):
    """POST на вебхук AMO без X-API-Key не должен получать 403 от нашего middleware.
    У вебхука собственная 401-проверка секрета — это нормально.
    """
    resp = client.post(AMO_WEBHOOK_PATH, data={})
    # 401 — ожидаемый ответ от собственной проверки секрета вебхука.
    # Главное — не 403 от нашего middleware.
    assert resp.status_code != 403
