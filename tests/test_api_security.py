"""Wave 2 security tests: auth-matrix для GET, rate limiting, Trello error
hygiene и hardening AMO webhook.

Мокаем google.genai через sys.modules ДО импорта web.app (как в остальных
auth-тестах) — пакет google-generativeai в dev не установлен.
"""

import logging
import sys
import uuid
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
import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from agent.database import init_db  # noqa: E402

from web.app import app  # noqa: E402
from web.rate_limit import limiter  # noqa: E402
from integrations.trello import (  # noqa: E402
    TrelloError,
    redact_trello_secrets,
    safe_request,
)
import integrations.trello as trello_module  # noqa: E402

CORRECT_KEY = "test-secret-key"   # == conftest TEST_API_KEY / config.API_KEY
WRONG_KEY = "wrong-key"
WEBHOOK_PATH = "/api/v1/amo/webhooks/lead-created"
WEBHOOK_SECRET = "webhook-secret-xyz-123"


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test_security.db")
    init_db(db_path=db_path, json_path=str(tmp_path / "nonexistent.json"))
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def rate_limited():
    """Включает in-memory лимитер на время теста (в pytest он по умолчанию off)."""
    limiter.reset()
    prev = limiter.enabled
    limiter.enabled = True
    yield limiter
    limiter.enabled = prev
    limiter.reset()


# ===========================================================================
# 2.1 Auth policy — все /api закрыты, публично только health/root/static/webhook
# ===========================================================================

def test_api_all_methods_no_key_rejected(client):
    """Любой /api метод без X-API-Key отклоняется (403) — middleware до роутинга."""
    cases = [
        ("GET", "/api/overview"),
        ("GET", "/api/analytics"),
        ("POST", "/api/settings"),
        ("POST", "/api/scheduler/run"),
        ("PUT", "/api/anything"),
        ("PATCH", "/api/anything"),
        ("DELETE", "/api/winners/1"),
    ]
    for method, path in cases:
        resp = client.request(method, path)
        assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"


def test_api_wrong_key_rejected_no_key_variant(client):
    """GET/POST /api с неверным ключом → 403 (имя с 'no_key' → conftest не инжектит)."""
    assert client.get("/api/overview", headers={"X-API-Key": WRONG_KEY}).status_code == 403
    assert client.post("/api/settings", json={}, headers={"X-API-Key": WRONG_KEY}).status_code == 403


def test_public_paths_no_key_ok(client):
    """/health, /, /static доступны без ключа (имя с 'no_key' → без инъекции ключа)."""
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/static/index.html").status_code == 200


def test_webhook_exact_path_exempt_no_key(client):
    """Точный путь вебхука exempt от X-API-Key (своя проверка секрета → 401, не 403)."""
    with patch("web.amo_webhook_routes.AMO_WEBHOOK_SECRET", WEBHOOK_SECRET):
        resp = client.post(WEBHOOK_PATH, data={})
    assert resp.status_code != 403  # не режется общим middleware


def test_valid_key_reaches_handler(client):
    """С верным ключом GET /api/launch-status не режется (не 401/403/429)."""
    resp = client.get("/api/launch-status", headers={"X-API-Key": CORRECT_KEY})
    assert resp.status_code not in (401, 403, 429)


# ===========================================================================
# 2.2 Rate limiting — 429 без вызова провайдера
# ===========================================================================

def test_heavy_refresh_rate_limited_provider_not_called(client, rate_limited):
    """POST /api/overview/refresh (тяжёлый, 2/min): 3-й запрос → 429, провайдер
    (get_overview) вызван РОВНО 2 раза — 429 не доходит до провайдера."""
    with patch("web.overview_routes.get_overview", return_value={"ok": True}) as mock_go:
        r1 = client.post("/api/overview/refresh?days=14", headers={"X-API-Key": CORRECT_KEY})
        r2 = client.post("/api/overview/refresh?days=14", headers={"X-API-Key": CORRECT_KEY})
        r3 = client.post("/api/overview/refresh?days=14", headers={"X-API-Key": CORRECT_KEY})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert mock_go.call_count == 2  # провайдер не вызван на отклонённом запросе


def test_rate_limiter_unit_sliding_window():
    """Юнит: allow пускает limit раз, затем режет; окно освобождается по времени."""
    from web.rate_limit import RateLimiter
    rl = RateLimiter()
    rl.enabled = True
    # limit=2, window=60; инжектим monotonic-время через now
    assert rl.allow("k", 2, window=60, now=1000.0) is True
    assert rl.allow("k", 2, window=60, now=1001.0) is True
    assert rl.allow("k", 2, window=60, now=1002.0) is False   # превышен
    # через 61с первые два выпали из окна → снова можно
    assert rl.allow("k", 2, window=60, now=1063.0) is True


def test_rate_limiter_disabled_is_transparent():
    """Выключенный лимитер всегда пропускает (гарантия невлияния на прод-выкл)."""
    from web.rate_limit import RateLimiter
    rl = RateLimiter()  # enabled=False по умолчанию
    for i in range(100):
        assert rl.allow("k", 1, now=float(i)) is True


def test_provider_get_classified_as_10(client, rate_limited):
    """provider-backed GET имеет лимит 10/min: 11-й запрос → 429."""
    with patch("web.app.get_done_list_id", return_value="l1"), \
         patch("web.app.get_unlaunched_cards", return_value=[]):
        statuses = [
            client.get("/api/cards", headers={"X-API-Key": CORRECT_KEY}).status_code
            for _ in range(11)
        ]
    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


# ===========================================================================
# 2.3 Trello error hygiene — key/token не утекают в ответ и логи
# ===========================================================================

def test_redact_trello_secrets_masks_key_token():
    s = "for url: https://api.trello.com/1/cards?key=ABC123&token=DEF456&access_token=GHI789"
    out = redact_trello_secrets(s)
    assert "ABC123" not in out and "DEF456" not in out and "GHI789" not in out
    assert "key=***" in out and "token=***" in out and "access_token=***" in out


def test_trello_cards_error_hides_secrets(client, caplog):
    """GET /api/cards при HTTPError с key/token в URL → ответ и логи без секретов."""
    leaky = (
        "403 Client Error: Forbidden for url: "
        "https://api.trello.com/1/boards/x/lists?key=SECRETKEY123&token=SECRETTOKEN456"
    )
    with patch("web.app.get_done_list_id", side_effect=requests.HTTPError(leaky)):
        with caplog.at_level(logging.WARNING):
            resp = client.get("/api/cards", headers={"X-API-Key": CORRECT_KEY})

    assert resp.status_code == 500
    body = resp.text
    assert "SECRETKEY123" not in body and "SECRETTOKEN456" not in body
    assert "ref:" in resp.json().get("detail", "")

    logtext = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRETKEY123" not in logtext and "SECRETTOKEN456" not in logtext
    assert "key=***" in logtext or "token=***" in logtext


def test_launch_trello_error_hides_secrets(client, caplog):
    """POST /api/launch при HTTPError Trello с key/token в URL → ответ и логи без секретов.

    launch_card() зовёт get_done_list_id()/get_unlaunched_cards(), которые при
    сбое Trello поднимают requests.HTTPError с ?key=..&token=.. в URL. Раньше
    обработчик отдавал str(e) → живые креды в HTTP-ответ. Теперь маскируем через
    _safe_trello_detail (как остальные Trello-эндпоинты).
    """
    leaky = (
        "401 Client Error: Unauthorized for url: "
        "https://api.trello.com/1/boards/x/lists?key=LAUNCHKEY777&token=LAUNCHTOKEN888"
    )
    with patch("web.app.get_done_list_id", side_effect=requests.HTTPError(leaky)):
        with caplog.at_level(logging.WARNING):
            resp = client.post(
                "/api/launch/card123?campaign_type=leadgen",
                    headers={
                        "X-API-Key": CORRECT_KEY,
                        "Idempotency-Key": str(uuid.uuid4()),
                    },
            )

    assert resp.status_code == 500
    body = resp.text
    assert "LAUNCHKEY777" not in body and "LAUNCHTOKEN888" not in body
    assert "ref:" in resp.json().get("detail", "")

    logtext = "\n".join(r.getMessage() for r in caplog.records)
    assert "LAUNCHKEY777" not in logtext and "LAUNCHTOKEN888" not in logtext

    # Резерv запуска освобождён (running сброшен в except) — иначе следующий запуск 409.
    status = client.get("/api/launch-status", headers={"X-API-Key": CORRECT_KEY})
    assert status.json().get("running") is False


def test_safe_request_raises_trelloerror_no_secret(caplog):
    """safe_request на сбое → TrelloError без секретов; лог redacted."""
    leaky = requests.HTTPError(
        "500 for url: https://api.trello.com/1/cards?key=AAA111&token=BBB222"
    )
    with patch.object(trello_module.session, "request", side_effect=leaky):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(TrelloError) as exc_info:
                safe_request("POST", trello_module.BASE + "/cards")

    # correlation id есть, секретов в сообщении нет
    assert exc_info.value.correlation_id
    assert "AAA111" not in str(exc_info.value) and "BBB222" not in str(exc_info.value)
    logtext = "\n".join(r.getMessage() for r in caplog.records)
    assert "AAA111" not in logtext and "BBB222" not in logtext


# ===========================================================================
# 2.4 AMO webhook — constant-time secret, header/query, 0 мутаций при отказе
# ===========================================================================

def test_webhook_invalid_secret_no_mutation_no_key(client):
    """Неверный секрет → 401 и бизнес-логика (process_lead_created) не вызвана."""
    with patch("web.amo_webhook_routes.AMO_WEBHOOK_SECRET", WEBHOOK_SECRET), \
         patch("web.amo_webhook_routes.process_lead_created") as mock_proc:
        resp = client.post(
            WEBHOOK_PATH + "?secret=totally-wrong",
            data={"leads[add][0][id]": "123"},
        )
    assert resp.status_code == 401
    mock_proc.assert_not_called()


def test_webhook_empty_server_secret_rejected_no_key(client):
    """Пустой серверный AMO_WEBHOOK_SECRET → всегда 401 (fail-closed), 0 мутаций."""
    with patch("web.amo_webhook_routes.AMO_WEBHOOK_SECRET", ""), \
         patch("web.amo_webhook_routes.process_lead_created") as mock_proc:
        resp = client.post(
            WEBHOOK_PATH + "?secret=anything",
            data={"leads[add][0][id]": "123"},
        )
    assert resp.status_code == 401
    mock_proc.assert_not_called()


def test_webhook_valid_query_secret_accepted(client):
    """Валидный секрет в query (backward-compat) → 200 + бизнес-логика вызвана."""
    with patch("web.amo_webhook_routes.AMO_WEBHOOK_SECRET", WEBHOOK_SECRET), \
         patch(
             "web.amo_webhook_routes.process_lead_created",
             return_value={"status": "ok", "reason": "r"},
         ) as mock_proc:
        resp = client.post(
            WEBHOOK_PATH + f"?secret={WEBHOOK_SECRET}",
            data={"leads[add][0][id]": "123"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    mock_proc.assert_called_once_with(123)


def test_webhook_valid_header_secret_accepted(client):
    """Валидный секрет в заголовке X-Webhook-Secret → 200 (предпочтительный путь)."""
    with patch("web.amo_webhook_routes.AMO_WEBHOOK_SECRET", WEBHOOK_SECRET), \
         patch(
             "web.amo_webhook_routes.process_lead_created",
             return_value={"status": "ok", "reason": "r"},
         ) as mock_proc:
        resp = client.post(
            WEBHOOK_PATH,
            data={"leads[add][0][id]": "123"},
            headers={"X-Webhook-Secret": WEBHOOK_SECRET},
        )
    assert resp.status_code == 200
    mock_proc.assert_called_once_with(123)
