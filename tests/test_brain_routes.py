"""
Unit-тесты для web/brain_routes.py (эндпоинты /api/brain/*).

Проверяют:
- POST /api/brain/backfill — 200 с полями ответа
- POST /api/brain/backfill без X-API-Key — 403
- GET /api/brain/status — 200, секции backfill/labeling/outcomes/learnings
- POST /api/brain/mine — 200, learnings_written

Мокаем сервисы через patch, не тянем FB/AMO/Gemini.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# Мокаем google.genai до импорта модулей проекта
_google_mock = MagicMock()
_genai_mock = MagicMock()
sys.modules.setdefault("google", _google_mock)
sys.modules.setdefault("google.genai", _genai_mock)
sys.modules.setdefault("google.generativeai", MagicMock())

# Ключ для аутентификации из conftest
CORRECT_KEY = "test-secret-key"
WRONG_KEY = "wrong-key"


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    """TestClient с временной БД (через init_kb) и замоканными внешними сервисами."""
    db_path = str(tmp_path / "test_brain.db")

    # Используем init_kb — он применяет все миграции идемпотентно
    from services.creative_intelligence import init_kb
    init_kb(db_path)

    with patch("services.creative_intelligence.DB_PATH", db_path):
        from fastapi.testclient import TestClient
        from web.app import app
        yield TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Тест 1: POST /api/brain/backfill — 200 с корректными полями ответа
# ---------------------------------------------------------------------------


def test_backfill_returns_200_with_fields(client):
    """POST /api/brain/backfill с X-API-Key → 200 + все поля ответа."""
    mock_result = {
        "fetched": 3,
        "upserted": 3,
        "rate_limited": False,
        "cursor_after": "cursor_abc",
        "done": False,
    }

    with patch("services.creative_backfill.sync_backfill_increment", return_value=mock_result):
        resp = client.post(
            "/api/brain/backfill",
            json={"max_ads": 5},
            headers={"X-API-Key": CORRECT_KEY},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["fetched"] == 3
    assert data["upserted"] == 3
    assert data["rate_limited"] is False
    assert data["cursor_after"] == "cursor_abc"
    assert data["done"] is False


# ---------------------------------------------------------------------------
# Тест 2: POST /api/brain/backfill без X-API-Key → 403
# ---------------------------------------------------------------------------


def test_backfill_no_key_returns_403(client):
    """POST /api/brain/backfill без X-API-Key → 403 от middleware."""
    resp = client.post("/api/brain/backfill", json={"max_ads": 5})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Тест 3: POST /api/brain/backfill с неверным X-API-Key → 403
# ---------------------------------------------------------------------------


def test_backfill_wrong_key_returns_403(client):
    """POST /api/brain/backfill с неверным X-API-Key → 403."""
    resp = client.post(
        "/api/brain/backfill",
        json={"max_ads": 5},
        headers={"X-API-Key": WRONG_KEY},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Тест 4: GET /api/brain/status — 200, все секции присутствуют
# ---------------------------------------------------------------------------


def test_status_returns_200_with_sections(client):
    """GET /api/brain/status без ключа → 200 + backfill/labeling/outcomes/learnings."""
    resp = client.get("/api/brain/status")

    assert resp.status_code == 200
    data = resp.json()

    # Проверяем структуру ответа
    assert "backfill" in data
    assert "labeling" in data
    assert "outcomes" in data
    assert "learnings" in data

    # backfill: нужные поля
    bf = data["backfill"]
    assert "fetched_total" in bf
    assert "done" in bf
    assert "cursor_set" in bf

    # labeling: нужные поля
    lb = data["labeling"]
    assert "total_with_body" in lb
    assert "labeled" in lb
    assert "pending" in lb

    # outcomes: нужные поля
    oc = data["outcomes"]
    assert "total" in oc
    assert "matched" in oc
    assert "pending" in oc

    # learnings: нужные поля
    lrn = data["learnings"]
    assert "total" in lrn
    assert "by_confidence" in lrn
    assert "pattern_miner" in lrn
    assert "manual" in lrn


# ---------------------------------------------------------------------------
# Тест 5: GET /api/brain/status — открытый эндпоинт (GET без ключа не 403)
# ---------------------------------------------------------------------------


def test_status_is_open_get_endpoint(client):
    """GET /api/brain/status не требует X-API-Key (GET открыт)."""
    resp = client.get("/api/brain/status")
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Тест 6: POST /api/brain/mine — 200, learnings_written
# ---------------------------------------------------------------------------


def test_mine_returns_200(client):
    """POST /api/brain/mine с X-API-Key → 200 + learnings_written."""
    mock_result = {
        "learnings_written": 2,
        "slices_evaluated": 5,
        "baseline": {"avg_qual_pct": 12.5, "avg_cpl": 8.0, "n_ads": 20, "total_spend": 500.0},
    }

    with patch("services.pattern_miner.mine_patterns", return_value=mock_result):
        resp = client.post(
            "/api/brain/mine",
            headers={"X-API-Key": CORRECT_KEY},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["learnings_written"] == 2
    assert data["slices_evaluated"] == 5
    assert "baseline" in data


# ---------------------------------------------------------------------------
# Тест 7: POST /api/brain/mine без X-API-Key → 403
# ---------------------------------------------------------------------------


def test_mine_no_key_returns_403(client):
    """POST /api/brain/mine без X-API-Key → 403."""
    resp = client.post("/api/brain/mine")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Тест 8: POST /api/brain/backfill с reset=True — вызывает reset_backfill_state
# ---------------------------------------------------------------------------


def test_backfill_reset_calls_reset(client):
    """POST /api/brain/backfill с reset=True вызывает reset_backfill_state перед инкрементом."""
    mock_result = {
        "fetched": 0,
        "upserted": 0,
        "rate_limited": False,
        "cursor_after": None,
        "done": False,
    }

    reset_called = []

    def fake_reset():
        reset_called.append(True)

    with patch("services.creative_backfill.reset_backfill_state", side_effect=fake_reset), \
         patch("services.creative_backfill.sync_backfill_increment", return_value=mock_result):
        resp = client.post(
            "/api/brain/backfill",
            json={"max_ads": 5, "reset": True},
            headers={"X-API-Key": CORRECT_KEY},
        )

    assert resp.status_code == 200
    assert len(reset_called) == 1


def test_metrics_backfill_range_surfaces_incomplete_error(client):
    """Partial Graph fetch явно виден оператору, а не выглядит успешным range."""
    incomplete = {
        "fetched": 2,
        "upserted": 2,
        "rate_limited": False,
        "complete": False,
        "error": "partial_pagination",
    }
    with patch("services.metrics_backfill.backfill_range", return_value=incomplete):
        response = client.post(
            "/api/brain/backfill-metrics",
            json={"date_from": "2026-07-01", "date_to": "2026-07-03"},
            headers={"X-API-Key": CORRECT_KEY},
        )

    assert response.status_code == 200
    assert response.json()["complete"] is False
    assert response.json()["error"] == "partial_pagination"


def test_metrics_backfill_status_surfaces_hourly_legacy_veto(client):
    """Read-only status показывает старый legacy row и fail-closed причину."""
    from services.creative_intelligence import _get_connection

    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO ad_hourly_metrics (ad_id, datetime_hour, actions_lead) VALUES (?, ?, ?)",
            ("ad-old", "2025-01-01T08:00:00", 7),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get("/api/brain/backfill-metrics/status")

    assert response.status_code == 200
    status = response.json()["hourly_lead_semantics"]
    assert status["legacy_rows"] == 1
    assert status["older_fail_closed_rows"] == 1
    assert status["blocked_reason"] == (
        "hourly_legacy_outside_48h_requires_explicit_provider_refetch"
    )
