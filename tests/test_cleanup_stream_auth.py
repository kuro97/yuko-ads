"""Security regressions для навсегда отключённого direct cleanup route.

Route обязан отвечать 403/503/409 ДО любого обращения к провайдеру. Точки
провайдера с переходом на approval-first изменились: у `integrations.facebook`
больше нет HTTP-сессии — чтения идут через `_throttled_get`, а мутации живут
только в `integrations.facebook_ads_mutation_transport`. `_provider_guards`
закрывает их все, поэтому тест ловит и попытку чтения, и попытку мутации.
"""

from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent.database import init_db
from web.app import app


CORRECT_KEY = "test-secret-key"
WRONG_KEY = "wrong-key"
CLEANUP_PATH = "/api/adsets/cleanup-stream"
DISABLED_DETAIL = "Прямое удаление отключено; используйте безопасный cleaner workflow"


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test_cleanup.db")
    init_db(db_path=db_path, json_path=str(tmp_path / "nonexistent.json"))
    return TestClient(app, raise_server_exceptions=False)


_PROVIDER_TARGETS = (
    "integrations.facebook._throttled_get",
    "integrations.facebook_ads_mutation_transport.set_ad_status",
    "integrations.facebook_ads_mutation_transport.create_ad",
    "integrations.facebook_ads_mutation_transport.session",
    "integrations.trello.session",
    "web.app.history_repo.save_history_entry",
)


@contextmanager
def _provider_guards():
    """Закрывает все текущие provider-точки и отдаёт их mocks для проверки."""
    with ExitStack() as stack:
        mocks = {
            target: stack.enter_context(patch(target))
            for target in _PROVIDER_TARGETS
        }
        yield mocks


def _assert_no_provider_touched(mocks: dict) -> None:
    for target, mock in mocks.items():
        assert not mock.called, f"route дотянулся до провайдера: {target}"


def test_old_get_url_with_secret_query_never_executes_cleanup(client):
    with _provider_guards() as mocks:
        response = client.get(
            CLEANUP_PATH,
            params={"api_key": CORRECT_KEY, "confirm": "DELETE"},
        )

    assert response.status_code == 403
    assert CORRECT_KEY not in response.text
    _assert_no_provider_touched(mocks)


def test_authenticated_get_is_also_exact_409_before_provider(client):
    with _provider_guards() as mocks:
        response = client.get(
            CLEANUP_PATH,
            headers={"X-API-Key": CORRECT_KEY},
            params={"api_key": "must-not-be-used", "confirm": "DELETE"},
        )

    assert response.status_code == 409
    assert response.json() == {"detail": DISABLED_DETAIL}
    assert "must-not-be-used" not in response.text
    _assert_no_provider_touched(mocks)


@pytest.mark.parametrize(
    "headers,expected_status",
    [({}, 403), ({"X-API-Key": WRONG_KEY}, 403)],
)
def test_auth_rejects_before_disabled_handler(client, headers, expected_status):
    with _provider_guards() as mocks:
        response = client.post(
            CLEANUP_PATH,
            headers=headers,
            json={"adset_ids": ["adset_1"], "confirm": "DELETE"},
        )

    assert response.status_code == expected_status
    assert "X-API-Key" in response.json()["detail"]
    _assert_no_provider_touched(mocks)


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {},
        {"content": b"not-json", "headers": {"content-type": "application/json"}},
        {"json": {}},
        {"json": {"confirm": "DELETE"}},
        {"json": {"adset_ids": "wrong-type", "confirm": "DELETE"}},
        {"json": {"adset_ids": [], "confirm": "DELETE"}},
        {"json": {"adset_ids": ["unknown"], "confirm": "DELETE"}},
        {"json": {"adset_ids": ["adset_1"], "confirm": "DELETE"}},
    ],
)
def test_any_authenticated_body_returns_exact_409_before_parsing_or_provider(
    client, request_kwargs
):
    request_kwargs = dict(request_kwargs)
    with _provider_guards() as mocks:
        headers = {"X-API-Key": CORRECT_KEY, **request_kwargs.pop("headers", {})}
        response = client.post(CLEANUP_PATH, headers=headers, **request_kwargs)

    assert response.status_code == 409
    assert response.json() == {"detail": DISABLED_DETAIL}
    _assert_no_provider_touched(mocks)


def test_api_key_not_configured_is_fail_closed_before_handler(client):
    with patch("config.API_KEY", None), _provider_guards() as mocks:
        response = client.post(
            CLEANUP_PATH,
            headers={"X-API-Key": "anything"},
            json={"adset_ids": ["adset_1"], "confirm": "DELETE"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "API_KEY не настроен — запросы к API отключены"
    }
    _assert_no_provider_touched(mocks)


def test_disabled_response_never_echoes_secret_payload(client):
    secret = "EAA_SUPER_SECRET"
    with _provider_guards() as mocks:
        response = client.post(
            CLEANUP_PATH,
            headers={"X-API-Key": CORRECT_KEY},
            json={
                "confirm": "DELETE",
                "url": f"https://graph.test/ads?access_token={secret}",
            },
        )

    assert response.status_code == 409
    assert response.json() == {"detail": DISABLED_DETAIL}
    assert secret not in response.text
    _assert_no_provider_touched(mocks)


def test_index_html_has_no_cleanup_secret_in_query_or_eventsource():
    html = (
        Path(__file__).parent.parent / "web" / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert "api_key=" not in html
    assert "cleanup-stream?" not in html
    assert "EventSource(API + '/api/adsets/cleanup-stream" not in html
