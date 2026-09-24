"""
Тесты D1: check-and-set POST /api/launch/{card_id} под одним lock.

Проверяем:
1. Два конкурентных запроса при уже занятом флаге running → 409 у второго.
2. Карточка не найдена → 404 и резерв running сбрасывается (следующий запуск возможен).
3. Любая другая ошибка внутри launch_card → running сбрасывается (не залипает).
4. Нормальный запуск при running=False → started.

Всё замокано (Trello, launch_single), без сети.
"""

import sys
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from agent.database import init_db
from web.app import app, launch_status, _launch_lock
from services.launch_checker import ProviderLaunchAuthorization
from tests.conftest import _make_test_token


@pytest.fixture
def client(tmp_path):
    """TestClient с чистой БД и сброшенным launch_status перед каждым тестом."""
    launch_status.update({
        "running": False, "outcome": "failed", "check_id": None,
        "reason_codes": [], "reasons": [], "current": "", "progress": 0,
        "total": 0, "step": "", "step_pct": None, "log": [],
    })
    db_path = str(tmp_path / "test_decisions.db")
    init_db(db_path=db_path, json_path=str(tmp_path / "nonexistent.json"))
    return TestClient(app)


@pytest.fixture
def headers():
    """Заголовки с JWT токеном (X-API-Key добавляется автоматически фикстурой conftest)."""
    token = _make_test_token()
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(uuid.uuid4()),
    }


# ---------------------------------------------------------------------------
# Тест 1: второй запуск при running=True (уже идёт) → 409
# ---------------------------------------------------------------------------

@patch("web.app.launch_single")
@patch("web.app.get_unlaunched_cards", return_value=[{"id": "c1", "name": "Test Card", "desc": ""}])
@patch("web.app.get_done_list_id", return_value="list123")
def test_second_launch_while_running_returns_409(mock_list, mock_cards, mock_launch, client, headers):
    """Двойной клик: пока running=True (эмулируем занятый флаг под тем же lock,
    как это увидел бы конкурентный запрос) — второй POST получает 409, а не 200."""
    # Эмулируем состояние «первый запрос уже зарезервировал слот» —
    # именно так его увидит второй конкурентный запрос под _launch_lock.
    with _launch_lock:
        launch_status["running"] = True

    resp = client.post("/api/launch/c1", headers=headers)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason_codes"] == ["LAUNCH_IN_PROGRESS"]
    assert "Уже идёт запуск" in detail["reasons"][0]
    # launch_single НЕ должен быть вызван — второй запрос не должен был стартовать фон
    mock_launch.assert_not_called()


# ---------------------------------------------------------------------------
# Тест 2: запуск при running=False успешно стартует (первый клик)
# ---------------------------------------------------------------------------

def test_launch_when_idle_starts(client, headers):
    """При running=False запрос стартует — 200 status=started, running становится True."""
    assert launch_status["running"] is False
    card = {"id": "c1", "name": "Test Card", "desc": "", "labels": [], "pos": 1.0}
    plan = type("Plan", (), {
        "check_id": "launch-check-test",
        "authorization": ProviderLaunchAuthorization("launch-auth-test", "secret"),
        "media": {"type": "video", "paths": ["/tmp/test.mp4"]},
    })()
    checker = type("Checker", (), {
        "prepare_and_reserve": lambda self, *_args: plan,
    })()
    with patch("web.app._exact_live_launch_card", return_value=card), patch(
        "web.app._get_web_checker_mode", return_value="enforce"
    ), patch("web.app._get_web_launch_checker", return_value=checker), patch(
        "web.app._load_web_launch_state", return_value={}
    ), patch("web.app._run_checked_web_launch"):
        resp = client.post("/api/launch/c1", headers=headers)

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "started"
    assert data["card"] == "Test Card"
    # После check-and-set флаг зарезервирован (фоновая задача снимет его в finally)
    assert launch_status["running"] is True


# ---------------------------------------------------------------------------
# Тест 3: карточка не найдена (404) → резерв running освобождается,
# повторный запуск снова возможен (не залипает на True навсегда)
# ---------------------------------------------------------------------------

def test_404_releases_running_flag(client, headers):
    """404 (карточка не найдена) освобождает running — повторный запуск не получает 409."""
    found = {"id": "missing_card", "name": "Now Found", "desc": "", "labels": [], "pos": 1.0}
    plan = type("Plan", (), {
        "check_id": "launch-check-test",
        "authorization": ProviderLaunchAuthorization("launch-auth-test", "secret"),
        "media": {"type": "video", "paths": ["/tmp/test.mp4"]},
    })()
    checker = type("Checker", (), {
        "prepare_and_reserve": lambda self, *_args: plan,
    })()
    with patch(
        "web.app._exact_live_launch_card",
        side_effect=[HTTPException(status_code=404, detail="Карточка не найдена"), found],
    ), patch("web.app._get_web_checker_mode", return_value="enforce"), patch(
        "web.app._get_web_launch_checker", return_value=checker
    ), patch("web.app._load_web_launch_state", return_value={}), patch(
        "web.app._run_checked_web_launch"
    ):
        resp = client.post("/api/launch/missing_card", headers=headers)

        assert resp.status_code == 404
        assert launch_status["running"] is False

        # Повторный запуск сразу после 404 не должен упереться в 409
        resp2 = client.post("/api/launch/missing_card", headers=headers)
        assert resp2.status_code == 202
        assert resp2.json()["status"] == "started"


# ---------------------------------------------------------------------------
# Тест 4: непредвиденная ошибка (500) освобождает running тоже
# ---------------------------------------------------------------------------

@patch("web.app.launch_single")
@patch("web.app.get_unlaunched_cards", side_effect=RuntimeError("Trello недоступен"))
@patch("web.app.get_done_list_id", return_value="list123")
def test_500_releases_running_flag(mock_list, mock_cards, mock_launch, client, headers):
    """Непредвиденное исключение → 500, running сброшен (не залипает)."""
    resp = client.post("/api/launch/c1", headers=headers)

    assert resp.status_code == 500
    assert launch_status["running"] is False
