"""Manual web PAUSE — approval-first: эндпоинт только создаёт предложение.

Ручная пауза из веба больше не мутирует Facebook: `execute_pause` (алиас
`propose_pause`) отдаёт квитанцию предложения, эндпоинт возвращает 202
`proposal_created`, и НИ решения PAUSED, НИ уведомления «объявление отключено»
на этом шаге быть не может — иначе интерфейс и база рапортовали бы об
отключении, которого владелец ещё не одобрил.
"""

from unittest.mock import Mock

import httpx
import pytest
from httpx import ASGITransport

import web.app as app_module
from services.owner_action_models import ProposalKind, ProposalOrigin
from tests.gateway_test_helpers import blocked_outcome, install_proposal_recorder
from tests.conftest import TEST_API_KEY
from web.app import app


HEADERS = {
    "X-API-Key": TEST_API_KEY,
    "Idempotency-Key": "22222222-2222-4222-8222-222222222222",
}


async def _pause(ad_id: str = "1", payload: dict | None = None):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/api/analytics/{ad_id}/pause",
            headers=HEADERS,
            json=payload or {},
        )


@pytest.fixture
def producer_boundary(tmp_path, monkeypatch):
    """Живой propose_pause на фейковом inventory + запрет прямых мутаций FB."""
    from services import adset_pause_guard

    def fake_inventory(ad_ids):
        inventories: dict = {}
        for index, ad_id in enumerate(ad_ids):
            adset_id = str(100 + index)
            replacement = f"replacement-{ad_id}"
            context = {
                current_id: {
                    "ad_id": current_id,
                    "adset_id": adset_id,
                    "name": current_id,
                    "configured_status": "ACTIVE",
                    "effective_status": "ACTIVE",
                }
                for current_id in (str(ad_id), replacement)
            }
            inventories[adset_id] = {
                "adset_id": adset_id,
                "active_ids": {str(ad_id), replacement},
                "candidate_context": {str(ad_id): context[str(ad_id)]},
                "inventory_context": context,
                "complete": True,
                "pages_read": 1,
                "error": None,
            }
        return inventories

    def fake_exact_contexts(ad_ids, *, require_names=True):
        del require_names
        wanted = {str(ad_id) for ad_id in ad_ids}
        contexts = {
            ad_id: context
            for inventory in adset_pause_guard.fetch_pause_inventory(list(ad_ids)).values()
            for ad_id, context in (inventory.get("candidate_context") or {}).items()
            if ad_id in wanted
        }
        return contexts, None

    monkeypatch.setattr(adset_pause_guard, "fetch_pause_inventory", fake_inventory)
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_pause_inventory",
        lambda ad_ids: adset_pause_guard.fetch_pause_inventory(list(ad_ids)),
    )
    monkeypatch.setattr(
        "services.action_producer_gateway.fetch_exact_ad_contexts", fake_exact_contexts
    )
    return install_proposal_recorder(monkeypatch, tmp_path)


@pytest.mark.asyncio
async def test_pause_endpoint_creates_proposal_and_records_nothing(
    monkeypatch, producer_boundary
):
    """Успешный путь: 202 proposal_created, без записи PAUSED и без notify.

    Раньше тест ожидал 200/CONFIRMED и ровно один save_decision — тогда веб сам
    паузил рекламу. Теперь подтверждать нечего: PAUSED и уведомление появятся
    только на execution boundary после одобрения владельца.
    """
    save_decision = Mock()
    notify = Mock()
    monkeypatch.setattr(app_module.decisions_repo, "save_decision", save_decision)
    monkeypatch.setattr(app_module, "notify", notify)

    response = await _pause(payload={"ad_name": "Обычная реклама", "spend": 10})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "proposal_created"
    assert body["state"] == "PENDING_OWNER"
    assert body["proposal_id"]
    producer_boundary.assert_proposed(
        "1",
        kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.WEB,
        action_kind="PAUSE_AD",
    )
    save_decision.assert_not_called()
    notify.assert_not_called()
    producer_boundary.assert_no_direct_provider_mutation()


@pytest.mark.asyncio
async def test_replacement_enqueued_is_not_counted_as_pause_or_undo(monkeypatch):
    facade = Mock(
        return_value=blocked_outcome(
            "replacement_enqueued",
            action="REPLACEMENT_ENQUEUED",
            workflow_id="workflow-1",
        )
    )
    save_decision = Mock()
    notify = Mock()
    monkeypatch.setattr("services.action_producer_gateway.execute_pause", facade)
    monkeypatch.setattr(app_module.decisions_repo, "save_decision", save_decision)
    monkeypatch.setattr(app_module, "notify", notify)

    response = await _pause(payload={"ad_name": "Последняя реклама"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "replacement_enqueued",
        "workflow_id": "workflow-1",
    }
    save_decision.assert_not_called()
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_last_active_without_enabled_replacement_is_409(monkeypatch):
    save_decision = Mock()
    monkeypatch.setattr(
        "services.action_producer_gateway.execute_pause",
        Mock(return_value=blocked_outcome("last_active_no_replacement")),
    )
    monkeypatch.setattr(app_module.decisions_repo, "save_decision", save_decision)

    response = await _pause(payload={"ad_name": "Последняя реклама"})

    assert response.status_code == 409
    assert "нет другой ACTIVE-рекламы" in response.json()["detail"]
    assert "last_active_no_replacement" not in response.text
    save_decision.assert_not_called()


@pytest.mark.asyncio
async def test_pause_endpoint_upstream_failure_is_generic_502(monkeypatch):
    monkeypatch.setattr(
        "services.action_producer_gateway.execute_pause",
        Mock(return_value=blocked_outcome("replacement_mapping_unknown")),
    )

    response = await _pause()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "APPROVAL_DENIED"
    assert "replacement_mapping_unknown" not in response.text


@pytest.mark.asyncio
async def test_pause_endpoint_token_exception_is_sanitized_502(monkeypatch, caplog):
    monkeypatch.setattr(
        "services.action_producer_gateway.execute_pause",
        Mock(
            side_effect=RuntimeError(
                "https://graph.test?access_token=SUPER_SECRET&x=1"
            )
        ),
    )

    response = await _pause()

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "CHECKER_UNAVAILABLE"
    assert "SUPER_SECRET" not in response.text
    assert "SUPER_SECRET" not in caplog.text
