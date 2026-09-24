"""Тесты API эндпоинтов FastAPI (Supabase + JWT auth)."""

import sys
from pathlib import Path
from unittest.mock import patch
import tempfile
import uuid
from agent.database import init_db

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient
from web.app import app, launch_status
from agent.fb_common import FBApiError
from services.launch_checker import ProviderLaunchAuthorization
from tests.conftest import _make_test_token


@pytest.fixture
def client(tmp_path):
    """TestClient для FastAPI с чистой БД."""
    launch_status.update({
        "running": False, "outcome": "failed", "check_id": None,
        "reason_codes": [], "reasons": [], "current": "", "progress": 0,
        "total": 0, "step": "", "step_pct": None, "log": [],
    })
    # Чистая БД для каждого теста (json_path=несуществующий чтобы не мигрировать)
    db_path = str(tmp_path / "test_decisions.db")
    init_db(db_path=db_path, json_path=str(tmp_path / "nonexistent.json"))
    return TestClient(app)


@pytest.fixture
def headers():
    """Заголовки с JWT токеном."""
    token = _make_test_token()
    return {"Authorization": f"Bearer {token}"}


def test_index_page(client):
    """Главная страница отдаётся."""
    resp = client.get("/")
    assert resp.status_code == 200



def test_launch_status_with_auth(client, headers):
    """Статус запуска с авторизацией."""
    resp = client.get("/api/launch-status", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is False


def test_history_endpoint(client, headers):
    """Эндпоинт истории возвращает список."""
    resp = client.get("/api/history", headers=headers)
    assert resp.status_code == 200
    assert "history" in resp.json()


def test_decisions_endpoint(client, headers):
    """Эндпоинт решений возвращает список."""
    resp = client.get("/api/decisions", headers=headers)
    assert resp.status_code == 200
    assert "decisions" in resp.json()


@patch("web.app.get_done_list_id", return_value="list123")
@patch("web.app.get_unlaunched_cards", return_value=[])
def test_cards_empty(mock_cards, mock_list, client, headers):
    """Пустой список карточек."""
    resp = client.get("/api/cards", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_learning_cached_empty(client, headers):
    """Кэш обучения при пустом Supabase."""
    resp = client.get("/api/learning/cached", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "rankings" in data


def test_amo_save_with_recalc(client, headers):
    """Сохранение AMO с метриками — возвращает пересчитанную рекомендацию."""
    resp = client.post("/api/amo/test123", json={
        "qual_pct": 25, "romi": 300, "payments": 3,
        "days_running": 14, "leads": 12, "cpl": 20, "spend": 240,
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "saved"
    assert data["recommendation"] == "ОСТАВИТЬ"  # ROMI 300% >= 200%
    assert "ROMI" in data["reason"]


def test_amo_save_without_metrics(client, headers):
    """Сохранение AMO без метрик — нет пересчёта."""
    resp = client.post("/api/amo/test456", json={
        "qual_pct": 10, "romi": None, "payments": None,
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "saved"
    assert "recommendation" not in data


def test_amo_get(client, headers):
    """Получение AMO данных."""
    resp = client.get("/api/amo", headers=headers)
    assert resp.status_code == 200


def test_launch_calls_service(client, headers):
    """Запуск карточки передаёт checked plan в фоновый runner."""
    card = {
        "id": "c1", "name": "Test Card", "desc": "тест", "labels": [],
        "pos": 1.0, "closed": False,
    }
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
    ), patch("web.app._run_checked_web_launch") as mock_launch:
        resp = client.post(
            "/api/launch/c1",
            headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        )
    assert resp.status_code == 202
    assert resp.json()["status"] == "started"
    assert resp.json()["card"] == "Test Card"
    assert resp.json()["auth_id"] == "launch-auth-test"
    assert "secret" not in resp.text
    mock_launch.assert_called_once()


# --- FB API ошибки → 502 ---

@patch("web.app.get_cached_analytics", side_effect=FBApiError("Server Error", 500))
def test_analytics_fb_error_returns_502(mock_analytics, client, headers):
    """FB API 500 → HTTP 502."""
    resp = client.get("/api/analytics", headers=headers)
    assert resp.status_code == 502
    assert "FB API недоступен" in resp.json()["detail"]


@patch("web.app.run_learner", side_effect=FBApiError("Rate limit", 429))
def test_learning_fb_error_returns_502(mock_learner, client, headers):
    """FB API rate limit → HTTP 502."""
    resp = client.get("/api/learning", headers=headers)
    assert resp.status_code == 502
    assert "FB API недоступен" in resp.json()["detail"]


# --- GET /api/hypotheses ---

@patch("web.app.run_learner", return_value={"hypotheses": [
    {"type": "smart", "priority": "HIGH", "title": "Test H1"},
    {"type": "smart", "priority": "MED", "title": "Test H2"},
    {"type": "scale", "priority": "LOW", "title": "Test H3"},
]})
def test_hypotheses_endpoint(mock_learner, client, headers):
    """GET /api/hypotheses возвращает список гипотез."""
    resp = client.get("/api/hypotheses", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["hypotheses"]) == 3


@patch("web.app.run_learner", return_value={"hypotheses": [
    {"type": "smart", "priority": "HIGH", "title": "H1"},
    {"type": "scale", "priority": "MED", "title": "H2"},
]})
def test_hypotheses_filter_type(mock_learner, client, headers):
    """GET /api/hypotheses?type=smart фильтрует по типу."""
    resp = client.get("/api/hypotheses?type=smart", headers=headers)
    data = resp.json()
    assert data["total"] == 1
    assert data["hypotheses"][0]["type"] == "smart"


@patch("web.app.run_learner", return_value={"hypotheses": [
    {"type": "smart", "priority": "HIGH", "title": "H1"},
    {"type": "smart", "priority": "MED", "title": "H2"},
]})
def test_hypotheses_filter_priority(mock_learner, client, headers):
    """GET /api/hypotheses?priority=HIGH фильтрует по приоритету."""
    resp = client.get("/api/hypotheses?priority=HIGH", headers=headers)
    data = resp.json()
    assert data["total"] == 1
    assert data["hypotheses"][0]["priority"] == "HIGH"


@patch("web.app.run_learner", side_effect=FBApiError("Error", 500))
def test_hypotheses_fb_error_502(mock_learner, client, headers):
    """FB API ошибка → 502."""
    resp = client.get("/api/hypotheses", headers=headers)
    assert resp.status_code == 502





@patch("web.app.get_ads_with_metrics", side_effect=FBApiError("Timeout", 504))
def test_amo_sync_fb_error_returns_502(mock_ads, client, headers):
    """FB API timeout при AMO sync → HTTP 502."""
    resp = client.post("/api/amo/sync?days=7", headers=headers)
    assert resp.status_code == 502
    assert "FB API недоступен" in resp.json()["detail"]


# --- Тесты JWT аутентификации ---






# --- Тесты /api/decisions/history ---

def test_decisions_history_empty(client, headers):
    """История решений пустая."""
    resp = client.get("/api/decisions/history", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["decisions"] == []


def test_decisions_history_with_dismiss(client, headers):
    """Dismiss сохраняет решение."""
    client.post("/api/analytics/ad1/dismiss", headers=headers)
    resp = client.get("/api/decisions/history", headers=headers)
    assert resp.status_code == 200


# --- Тесты timeseries ---

@patch("web.app.get_daily_insights", return_value={
    "ad1": [
        {"date": "2026-03-01", "spend": 15.5, "leads": 2, "cpl": 7.75},
        {"date": "2026-03-02", "spend": 12.0, "leads": 1, "cpl": 12.0},
    ],
})
def test_timeseries_returns_data(mock_daily, client, headers):
    """GET /api/metrics/timeseries возвращает daily breakdown."""
    resp = client.get("/api/metrics/timeseries?ad_ids=ad1&days=7", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "timeseries" in data
    assert "ad1" in data["timeseries"]
    assert len(data["timeseries"]["ad1"]) == 2


def test_timeseries_empty_ids(client, headers):
    """Пустые ad_ids → пустой результат."""
    resp = client.get("/api/metrics/timeseries?ad_ids=", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["timeseries"] == {}


@patch("web.app.get_daily_insights", side_effect=FBApiError("Rate limit", 429))
def test_timeseries_fb_error_returns_502(mock_daily, client, headers):
    """FB API ошибка при timeseries → 502."""
    resp = client.get("/api/metrics/timeseries?ad_ids=ad1", headers=headers)
    assert resp.status_code == 502
    assert "FB API недоступен" in resp.json()["detail"]


# --- Скоринг карточек ---

@patch("web.app.score_creative", return_value={"level": "HIGH", "value": 0.85, "reason": "Похож на: Winner Ad"})
@patch("web.app.detect_media_hint", return_value="video")
@patch("web.app.get_card_drive_link", return_value="https://drive.google.com/file/d/123")
@patch("web.app._load_web_launch_state", return_value={})
@patch("web.app.get_unlaunched_cards", return_value=[{"id": "c1", "name": "Test Card", "desc": "", "shortLink": "abc", "pos": 1.0, "closed": False}])
@patch("web.app.get_done_list_id", return_value="list123")
def test_cards_with_score(mock_list, mock_cards, mock_state, mock_drive, mock_media, mock_score, client, headers):
    """Карточки содержат поле score из scorer."""
    resp = client.get("/api/cards", headers=headers)
    assert resp.status_code == 200
    cards = resp.json()["cards"]
    assert len(cards) == 1
    assert cards[0]["score"]["level"] == "HIGH"
    assert cards[0]["score"]["value"] == 0.85
    mock_score.assert_called_once_with("Test Card")


@patch("web.app.score_creative", side_effect=RuntimeError("model crash"))
@patch("web.app.detect_media_hint", return_value="video")
@patch("web.app.get_card_drive_link", return_value="https://drive.google.com/file/d/123")
@patch("web.app._load_web_launch_state", return_value={})
@patch("web.app.get_unlaunched_cards", return_value=[{"id": "c1", "name": "Test Card", "desc": "", "shortLink": "abc", "pos": 1.0, "closed": False}])
@patch("web.app.get_done_list_id", return_value="list123")
def test_cards_score_fallback(mock_list, mock_cards, mock_state, mock_drive, mock_media, mock_score, client, headers):
    """При ошибке scorer карточка получает default LOW score."""
    resp = client.get("/api/cards", headers=headers)
    assert resp.status_code == 200
    cards = resp.json()["cards"]
    assert len(cards) == 1
    assert cards[0]["score"]["level"] == "LOW"
    assert cards[0]["score"]["value"] == 0


# --- Тесты auth endpoints ---

