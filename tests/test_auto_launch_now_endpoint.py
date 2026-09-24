"""Тесты валидации max_launches на POST /api/autopilot/auto-launch-now.

Потолок поднят с 3 до 5 (в сезон автозапуск карточек нужен быстрее).
Дефолт (1) не менялся.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта web.app
_google_mock = MagicMock()
_genai_mock = MagicMock()
_genai_types_mock = MagicMock()
sys.modules.setdefault("google", _google_mock)
sys.modules.setdefault("google.genai", _genai_mock)
sys.modules.setdefault("google.genai.types", _genai_types_mock)
_google_mock.genai = _genai_mock

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from web.app import app  # noqa: E402

API_KEY = "test-secret-key"
HEADERS = {"X-API-Key": API_KEY}


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=True)


def _fake_result(mode, max_launches, *, source):
    return {
        "mode": mode,
        "recommendations": [],
        "launched": [],
        "blocked": [],
        "raw_count": 0,
        "eligible_count": 0,
        "blocked_count": 0,
        "skipped_reason": None,
        "error": None,
    }


def test_max_launches_5_проходит(client):
    """max_launches=5 (новый потолок) — запрос проходит, run_auto_launch вызван с 5."""
    with patch("services.auto_launch.run_auto_launch", side_effect=_fake_result) as mocked:
        resp = client.post(
            "/api/autopilot/auto-launch-now",
            json={"mode": "dry_run", "max_launches": 5},
            headers=HEADERS,
        )
    assert resp.status_code == 200, resp.text
    from services.launch_checker import LaunchSource

    mocked.assert_called_once_with(
        "dry_run",
        5,
        source=LaunchSource.AUTO_LAUNCH_NOW,
    )


def test_max_launches_6_отклонён(client):
    """max_launches=6 (выше нового потолка 5) → 400."""
    resp = client.post(
        "/api/autopilot/auto-launch-now",
        json={"mode": "dry_run", "max_launches": 6},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "от 1 до 5" in resp.json()["detail"]


def test_max_launches_0_отклонён(client):
    """max_launches=0 — ниже нижней границы → 400."""
    resp = client.post(
        "/api/autopilot/auto-launch-now",
        json={"mode": "dry_run", "max_launches": 0},
        headers=HEADERS,
    )
    assert resp.status_code == 400
