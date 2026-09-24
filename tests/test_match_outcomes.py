"""
Тесты для эндпоинта POST /api/brain/match-outcomes и крона _cron_match_outcomes.

Проверяют:
- Эндпоинт вызывает attach_amo_outcomes с правильным months_back
- X-API-Key обязателен (403 без ключа)
- Крон-гейт _should_run_match_outcomes: True при часах {0, 6, 12, 18} и нет слота
- Крон-гейт: False при других часах
- Крон-гейт: False при повторном запуске в тот же слот
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Мокаем google.genai до импорта модулей проекта
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())

CORRECT_KEY = "test-secret-key"

# Часовой пояс CityA UTC+5
_TZ_LOCAL = timezone(timedelta(hours=5))


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    """TestClient с временной KB и замоканными внешними сервисами."""
    db_path = str(tmp_path / "test_brain_outcomes.db")

    from services.creative_intelligence import init_kb
    init_kb(db_path)

    with patch("services.creative_intelligence.DB_PATH", db_path):
        from fastapi.testclient import TestClient
        from web.app import app
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mock_attach_outcomes():
    """Мок attach_amo_outcomes — возвращает успешный результат."""
    result = {
        "windows": 2,
        "leads_fetched": 500,
        "ads_matched": 15,
        "ads_updated": 15,
    }
    with patch("services.amo_outcomes.attach_amo_outcomes", return_value=result) as m:
        yield m


# ---------------------------------------------------------------------------
# Тест 1: POST /api/brain/match-outcomes — 200 с правильными полями
# ---------------------------------------------------------------------------


def test_match_outcomes_returns_200(client, mock_attach_outcomes):
    """POST /api/brain/match-outcomes с X-API-Key → 200 + все поля ответа."""
    resp = client.post(
        "/api/brain/match-outcomes",
        json={"months_back": 2},
        headers={"X-API-Key": CORRECT_KEY},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["windows"] == 2
    assert data["leads_fetched"] == 500
    assert data["ads_matched"] == 15
    assert data["ads_updated"] == 15


# ---------------------------------------------------------------------------
# Тест 2: эндпоинт вызывает attach_amo_outcomes с правильным months_back
# ---------------------------------------------------------------------------


def test_match_outcomes_passes_months_back(client):
    """Эндпоинт передаёт months_back=3 в attach_amo_outcomes корректно."""
    mock_result = {
        "windows": 3,
        "leads_fetched": 700,
        "ads_matched": 20,
        "ads_updated": 20,
    }

    with patch("services.amo_outcomes.attach_amo_outcomes", return_value=mock_result) as mock_fn:
        resp = client.post(
            "/api/brain/match-outcomes",
            json={"months_back": 3},
            headers={"X-API-Key": CORRECT_KEY},
        )

    assert resp.status_code == 200
    # Проверяем что вызвали с months_back=3 и batch_days=30
    mock_fn.assert_called_once_with(3, 30)


# ---------------------------------------------------------------------------
# Тест 3: дефолтный months_back=2
# ---------------------------------------------------------------------------


def test_match_outcomes_default_months_back(client):
    """Без указания months_back используется дефолт 2."""
    mock_result = {
        "windows": 2,
        "leads_fetched": 400,
        "ads_matched": 10,
        "ads_updated": 10,
    }

    with patch("services.amo_outcomes.attach_amo_outcomes", return_value=mock_result) as mock_fn:
        resp = client.post(
            "/api/brain/match-outcomes",
            json={},
            headers={"X-API-Key": CORRECT_KEY},
        )

    assert resp.status_code == 200
    # months_back должен быть 2 (дефолт)
    mock_fn.assert_called_once_with(2, 30)


# ---------------------------------------------------------------------------
# Тест 4: без X-API-Key → 403
# ---------------------------------------------------------------------------


def test_match_outcomes_no_key_returns_403(client):
    """POST /api/brain/match-outcomes без X-API-Key → 403."""
    resp = client.post("/api/brain/match-outcomes", json={"months_back": 2})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Тест 5: неверный X-API-Key → 403
# ---------------------------------------------------------------------------


def test_match_outcomes_wrong_key_returns_403(client):
    """POST /api/brain/match-outcomes с неверным ключом → 403."""
    resp = client.post(
        "/api/brain/match-outcomes",
        json={"months_back": 2},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Тест 6: attach_amo_outcomes возвращает error → 503
# ---------------------------------------------------------------------------


def test_match_outcomes_amo_error_returns_503(client):
    """Если attach_amo_outcomes вернул {'error': ...} → 503."""
    error_result = {"error": "AMO недоступна: timeout", "ads_updated": 0}

    with patch("services.amo_outcomes.attach_amo_outcomes", return_value=error_result):
        resp = client.post(
            "/api/brain/match-outcomes",
            json={"months_back": 2},
            headers={"X-API-Key": CORRECT_KEY},
        )

    assert resp.status_code == 503
    assert "AMO" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Тест 7: крон-гейт — час==6 и нет state-файла → True
# ---------------------------------------------------------------------------


def test_should_run_at_valid_hour_no_prior_run(tmp_path):
    """_should_run_match_outcomes → True при час==6 (из {0,6,12,18}) и нет state-файла."""
    from web.app import _should_run_match_outcomes

    now = datetime(2026, 6, 24, 6, 15, tzinfo=_TZ_LOCAL)

    # Подменяем путь к state-файлу на несуществующий
    with patch("web.app._MATCH_OUTCOMES_STATE", tmp_path / "nonexistent_state.json"):
        result = _should_run_match_outcomes(now)

    assert result is True


# ---------------------------------------------------------------------------
# Тест 8: крон-гейт — час не из {0, 6, 12, 18} → False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hour", [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 19, 20, 21, 22, 23])
def test_should_not_run_at_other_hours(hour, tmp_path):
    """_should_run_match_outcomes → False при любом часу не из {0, 6, 12, 18}."""
    from web.app import _should_run_match_outcomes

    now = datetime(2026, 6, 24, hour, 0, tzinfo=_TZ_LOCAL)

    with patch("web.app._MATCH_OUTCOMES_STATE", tmp_path / "state.json"):
        result = _should_run_match_outcomes(now)

    assert result is False


# ---------------------------------------------------------------------------
# Тест 9: крон-гейт — слот 6 уже выполнен сегодня → False (дедупликация)
# ---------------------------------------------------------------------------


def test_should_not_run_if_already_ran_today(tmp_path):
    """_should_run_match_outcomes → False если слот 2026-06-24-6 уже выполнен."""
    from web.app import _should_run_match_outcomes

    state_file = tmp_path / "match_outcomes_state.json"
    # Сохраняем что слот 6 уже выполнен сегодня
    state_file.write_text(json.dumps({"slots": {"2026-06-24-6": True}}))

    now = datetime(2026, 6, 24, 6, 30, tzinfo=_TZ_LOCAL)

    with patch("web.app._MATCH_OUTCOMES_STATE", state_file):
        result = _should_run_match_outcomes(now)

    assert result is False


# ---------------------------------------------------------------------------
# Тест 10: крон-гейт — слот вчерашнего дня в state, сегодня новый слот → True
# ---------------------------------------------------------------------------


def test_should_run_if_only_yesterday_slot_exists(tmp_path):
    """_should_run_match_outcomes → True если в state есть только вчерашние слоты."""
    from web.app import _should_run_match_outcomes

    state_file = tmp_path / "match_outcomes_state.json"
    # Вчерашний слот — не блокирует сегодняшний
    state_file.write_text(json.dumps({"slots": {"2026-06-23-6": True}}))

    now = datetime(2026, 6, 24, 6, 0, tzinfo=_TZ_LOCAL)

    with patch("web.app._MATCH_OUTCOMES_STATE", state_file):
        result = _should_run_match_outcomes(now)

    assert result is True


# ---------------------------------------------------------------------------
# Тест 11: _cron_match_outcomes не дублирует при двух тиках в 04:xx
# ---------------------------------------------------------------------------


def test_cron_does_not_duplicate_in_same_hour(tmp_path):
    """Два тика крона в 06:xx → attach_amo_outcomes вызывается только один раз.

    После рефакторинга гейт проверяет часы {0, 6, 12, 18} — используем час 6.
    """
    from web.app import _cron_match_outcomes

    call_count = []

    def fake_attach(months_back, batch_days):
        call_count.append(1)
        return {"windows": 2, "leads_fetched": 100, "ads_matched": 5, "ads_updated": 5}

    state_file = tmp_path / "match_outcomes_state.json"

    # Час 6 — допустимый (входит в {0, 6, 12, 18})
    now = datetime(2026, 6, 24, 6, 15, tzinfo=_TZ_LOCAL)

    with patch("web.app._MATCH_OUTCOMES_STATE", state_file), \
         patch("web.app.datetime") as mock_dt, \
         patch("services.amo_outcomes.attach_amo_outcomes", side_effect=fake_attach):

        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # Первый тик
        _cron_match_outcomes()
        # Второй тик в тот же час
        _cron_match_outcomes()

    # attach_amo_outcomes должен быть вызван только один раз
    assert len(call_count) == 1
