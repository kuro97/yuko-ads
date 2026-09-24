"""
Тесты FIX 3: fail-closed поведение middleware _require_api_key при незаданном ключе.

Раньше config.API_KEY имел дефолт "dev-secret-key" — незаданный ключ давал
предсказуемый секрет, известный всем кто читал код. Теперь дефолта нет
(os.getenv("API_KEY") может быть None), и middleware при отсутствии ключа
отклоняет мутирующие запросы (503), а не пропускает их с дефолтным паролем.

Важно: autouse-фикстура set_test_jwt_secret (conftest.py) патчит
config.API_KEY = TEST_API_KEY во ВСЕХ тестах. Чтобы протестировать fail-closed,
патчим config.API_KEY = None ВНУТРИ теста (context manager перекрывает autouse).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

_google_mock = MagicMock()
_genai_mock = MagicMock()
_genai_types_mock = MagicMock()
sys.modules.setdefault("google", _google_mock)
sys.modules.setdefault("google.genai", _genai_mock)
sys.modules.setdefault("google.genai.types", _genai_types_mock)
_google_mock.genai = _genai_mock

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from agent.database import init_db  # noqa: E402

from web.app import app  # noqa: E402

CORRECT_KEY = "test-secret-key"


@pytest.fixture
def client(tmp_path):
    """TestClient с чистой временной БД."""
    db_path = str(tmp_path / "test_fail_closed.db")
    init_db(db_path=db_path, json_path=str(tmp_path / "nonexistent.json"))
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. config.API_KEY=None + POST не-exempt → 503
# ---------------------------------------------------------------------------

def test_missing_api_key_mutating_returns_503(client):
    """POST /api/settings без настроенного API_KEY (None) → 503, а не пропуск."""
    with patch("config.API_KEY", None):
        resp = client.post("/api/settings", json={"auto_apply": True})

    assert resp.status_code == 503
    assert "API_KEY" in resp.json().get("detail", "")


def test_missing_api_key_empty_string_also_returns_503(client):
    """config.API_KEY="" (пустая строка) — тоже трактуется как «не настроен» → 503 (edge case)."""
    with patch("config.API_KEY", ""):
        resp = client.post("/api/settings", json={"auto_apply": True})

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 2. config.API_KEY=None + GET /api → тоже fail-closed 503 (Wave 2)
# ---------------------------------------------------------------------------

def test_missing_api_key_get_api_returns_503(client):
    """Wave 2: GET /api/** при незаданном API_KEY → 503 (fail-closed).

    Раньше GET был открыт; теперь весь /api закрыт, поэтому без настроенного
    серверного ключа читать API тоже нельзя. Используем /api/launch-status —
    in-memory, без внешних сетевых вызовов (middleware режет до роутинга)."""
    with patch("config.API_KEY", None):
        resp = client.get("/api/launch-status")

    assert resp.status_code == 503
    assert "API_KEY" in resp.json().get("detail", "")


def test_missing_api_key_public_path_still_allowed(client):
    """Публичные пути (/health, /) не зависят от API_KEY — открыты и при None."""
    with patch("config.API_KEY", None):
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200


def test_missing_api_key_exempt_path_still_allowed(client):
    """POST на exempt-путь (вебхук AMO) при незаданном API_KEY не должен получать 503
    от нашего middleware — exempt проверяется раньше (edge case)."""
    with patch("config.API_KEY", None):
        resp = client.post("/api/v1/amo/webhooks/lead-created", data={})

    assert resp.status_code != 503
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# 3. config.API_KEY задан (как на проде) — поведение не меняется
# ---------------------------------------------------------------------------

def test_present_api_key_normal_behavior_correct_key(client):
    """С заданным API_KEY: верный ключ → НЕ 403 и НЕ 503 (обычное поведение)."""
    with patch("config.API_KEY", CORRECT_KEY):
        resp = client.post(
            "/api/settings",
            json={"auto_apply": True},
            headers={"X-API-Key": CORRECT_KEY},
        )

    assert resp.status_code != 403
    assert resp.status_code != 503


def test_present_api_key_wrong_key_returns_403_not_503(client):
    """С заданным API_KEY: неверный ключ → 403 (не 503 — ключ настроен, просто неверный)."""
    with patch("config.API_KEY", CORRECT_KEY):
        resp = client.post(
            "/api/settings",
            json={"auto_apply": True},
            headers={"X-API-Key": "wrong-key"},
        )

    assert resp.status_code == 403
