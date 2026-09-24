"""Integration-тесты для роутера /api/v1/amo/webhooks/lead-created."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

# Изолированное приложение — только с нужным роутером, без google.genai и прочих тяжёлых deps
import web.amo_webhook_routes
from web.amo_webhook_routes import router as amo_router

_app = FastAPI()
_app.include_router(amo_router)

# URL эндпоинта
ENDPOINT = "/api/v1/amo/webhooks/lead-created"

# TestClient без raise_server_exceptions чтобы видеть реальные HTTP-коды
client = TestClient(_app, raise_server_exceptions=False)

# ---------------------------------------------------------------------------
# Фикстура: мокаем process_lead_created на время всей группы тестов
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_process_lead():
    """Мокает process_lead_created чтобы не делать реальных AMO вызовов."""
    with patch.object(
        web.amo_webhook_routes,
        "process_lead_created",
        return_value={"status": "applied", "reason": "ok", "lead_id": 0},
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# 1. POST без ?secret= → 401
# ---------------------------------------------------------------------------

def test_webhook_401_no_secret():
    """Запрос без query-параметра secret должен вернуть 401."""
    with patch.object(web.amo_webhook_routes, "AMO_WEBHOOK_SECRET", "right_secret"):
        r = client.post(ENDPOINT, data={})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 2. POST с неверным secret → 401
# ---------------------------------------------------------------------------

def test_webhook_401_wrong_secret():
    """Неверный секрет должен вернуть 401."""
    with patch.object(web.amo_webhook_routes, "AMO_WEBHOOK_SECRET", "right_secret"):
        r = client.post(f"{ENDPOINT}?secret=wrong", data={})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 3. Пустой секрет в конфиге → 401 даже с любым query secret
# ---------------------------------------------------------------------------

def test_webhook_401_empty_secret_config():
    """Если AMO_WEBHOOK_SECRET не настроен — любой запрос → 401."""
    with patch.object(web.amo_webhook_routes, "AMO_WEBHOOK_SECRET", ""):
        r = client.post(f"{ENDPOINT}?secret=anything", data={})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 4. Только leads[add] считаются — leads[update] игнорируются
# ---------------------------------------------------------------------------

def test_webhook_accepts_add_only():
    """Из add+update в очередь попадает только add-лид."""
    with patch.object(web.amo_webhook_routes, "AMO_WEBHOOK_SECRET", "s"):
        r = client.post(
            f"{ENDPOINT}?secret=s",
            data={
                "leads[add][0][id]": "123",
                "leads[update][0][id]": "456",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["count"] == 1
    assert body["lead_ids"] == [123]


# ---------------------------------------------------------------------------
# 5. Пустой body → 200 status=ignored count=0
# ---------------------------------------------------------------------------

def test_webhook_ignores_no_leads():
    """Пустой form-body возвращает ignored с count=0."""
    with patch.object(web.amo_webhook_routes, "AMO_WEBHOOK_SECRET", "s"):
        r = client.post(f"{ENDPOINT}?secret=s", data={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ignored"
    assert body["count"] == 0


# ---------------------------------------------------------------------------
# 6. Несколько leads[add] → count=3
# ---------------------------------------------------------------------------

def test_webhook_parses_multiple_adds():
    """Три leads[add] → count=3 и все id в ответе."""
    with patch.object(web.amo_webhook_routes, "AMO_WEBHOOK_SECRET", "s"):
        r = client.post(
            f"{ENDPOINT}?secret=s",
            data={
                "leads[add][0][id]": "1",
                "leads[add][1][id]": "2",
                "leads[add][2][id]": "3",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["count"] == 3
    assert sorted(body["lead_ids"]) == [1, 2, 3]


# ---------------------------------------------------------------------------
# 7. Нечисловой id пропускается → count=0
# ---------------------------------------------------------------------------

def test_webhook_invalid_id_skipped():
    """leads[add][0][id]=abc не является числом — пропускается, count=0."""
    with patch.object(web.amo_webhook_routes, "AMO_WEBHOOK_SECRET", "s"):
        r = client.post(
            f"{ENDPOINT}?secret=s",
            data={"leads[add][0][id]": "abc"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ignored"
    assert body["count"] == 0


# ---------------------------------------------------------------------------
# 8. BackgroundTask вызывает _safe_process (а внутри него — process_lead_created)
# ---------------------------------------------------------------------------

def test_webhook_calls_process_lead_in_background(mock_process_lead):
    """process_lead_created должен быть вызван через background task."""
    with patch.object(web.amo_webhook_routes, "AMO_WEBHOOK_SECRET", "s"):
        r = client.post(
            f"{ENDPOINT}?secret=s",
            data={"leads[add][0][id]": "777"},
        )
    assert r.status_code == 200
    # TestClient выполняет background tasks синхронно перед возвратом ответа
    mock_process_lead.assert_called_once_with(777)
