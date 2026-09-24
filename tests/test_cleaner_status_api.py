"""Read-only cleaner status и отключённый direct cleanup route."""

from unittest.mock import Mock

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import TEST_API_KEY
from web.app import app


HEADERS = {"X-API-Key": TEST_API_KEY}


def _settings():
    return {
        "kill_switch": False,
        "cleaner": {
            "enabled": True,
            "proactive_enabled": True,
            "dry_run": False,
            "allow_irreversible_delete": True,
            "target_free": 5,
        },
        "replacement": {"enabled": True, "max_pending_hours": 48},
    }


def _durable_status():
    secret = "https://graph.test/ads?access_token=EAA_SUPER_SECRET&api_key=hidden"
    return {
        "generated_at": "2026-07-21T10:00:00+00:00",
        "last_run": {
            "run_id": "run-1",
            "run_kind": "PROACTIVE_DAILY",
            "workflow_id": None,
            "scheduled_date": "2026-07-21",
            "requested_mode": "dry_run",
            "effective_mode": "dry_run",
            "phase": "COMPLETED_WITH_WARNINGS",
            "discovered_count": 2,
            "eligible_count": 1,
            "would_delete_count": 1,
            "deleted_count": 0,
            "skipped_count": 1,
            "warning_count": 1,
            "error_count": 1,
            "started_at": "2026-07-21T09:59:00+00:00",
            "completed_at": "2026-07-21T10:00:00+00:00",
            "error": secret,
            "evidence": {
                "pressure": [
                    {
                        "adset_id": "adset-1",
                        "safe_candidate_count": 1,
                        "severity": "warning",
                        "reason": secret,
                    }
                ]
            },
        },
        "adsets": [
            {
                "account_kind": "offline",
                "account_id": "account-1",
                "source": "fb_api",
                "adset_id": "adset-1",
                "adset_name": "CityA L1",
                "effective_status": "ACTIVE",
                "inventory_complete": True,
                "used": 45,
                "available": 5,
                "active_count": 2,
            }
        ],
        "unresolved_claims": [
            {
                "claim_id": "claim-1",
                "run_id": "run-slot-1",
                "workflow_id": "workflow-1",
                "ad_id": "ad-zero-1",
                "adset_id": "adset-1",
                "purpose": "REPLACEMENT_SLOT",
                "state": "RECONCILE_REQUIRED",
                "claimed_at": "2026-07-21T06:00:00+00:00",
                "error": secret,
            }
        ],
        "replacement_workflows": [
            {
                "workflow_id": "workflow-1",
                "old_ad_id": "old-1",
                "adset_id": "adset-1",
                "phase": "WAITING_SLOT",
                "replacement_ad_id": None,
                "created_at": "2026-07-20T08:00:00+00:00",
                "last_error": secret,
            }
        ],
        "recovery_cases": [
            {
                "case_id": "case-1",
                "card_id": "card-1",
                "card_name": "Карточка",
                "source_completed_at": "2026-07-19T12:00:00+05:00",
                "phase": "NO_ACTION",
                "missing_cities": [],
                "media_manifest_sha256": "a" * 64,
                "last_error": secret,
                "city_plans": [
                    {
                        "plan_id": "plan-1",
                        "city": "CityA",
                        "account_kind": "offline",
                        "account_id": "account-1",
                        "adset_id": "adset-1",
                        "expected_ad_names": ["Ad one"],
                        "expected_ad_count": 1,
                        "reconcile_from": "2026-07-18T12:00:00+05:00",
                        "reconcile_until": "2026-07-21T10:00:00+05:00",
                        "phase": "COMPLETE",
                        "found_ad_ids": ["new-1"],
                        "media_manifest_sha256": "a" * 64,
                        "launch_attempt_key": None,
                        "capacity_available": 11,
                        "last_rechecked_at": "2026-07-21T09:55:00+05:00",
                        "last_error": secret,
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_status_is_authenticated_typed_read_only_and_redacted(monkeypatch):
    repository = Mock(return_value=_durable_status())
    settings = Mock(return_value=_settings())
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr("services.cleanup_repository.get_cleanup_status", repository)
    monkeypatch.setattr("services.autopilot.get_autopilot_config", settings)
    monkeypatch.setattr("integrations.facebook.get_adset_info", provider)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/adsets/cleaner/status", headers=HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["last_run"]["run_kind"] == "PROACTIVE_DAILY"
    assert payload["last_run"]["effective_mode"] == "dry_run"
    assert payload["adsets"][0] == {
        "account_kind": "offline",
        "adset_id": "adset-1",
        "adset_name": "CityA L1",
        "live_status": "ok",
        "used": 45,
        "available": 5,
        "active_count": 2,
        "safe_candidate_count": 1,
        "deficit_to_target": 0,
        "severity": "warning",
        "reason": "https://graph.test/ads?<redacted>",
    }
    assert payload["unresolved_claims"][0]["state"] == "RECONCILE_REQUIRED"
    assert payload["replacement_workflows"][0]["severity"] == "critical"
    assert payload["recovery_cases"][0]["phase"] == "NO_ACTION"
    assert payload["recovery_cases"][0]["city_plans"][0]["phase"] == "COMPLETE"
    assert payload["replacement_delete_enabled"] is True
    assert "SUPER_SECRET" not in response.text
    assert "hidden" not in response.text
    repository.assert_called_once_with("default")
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_status_reads_empty_initialized_durable_store(monkeypatch, tmp_path):
    from services import creative_intelligence as intelligence

    previous_db_path = intelligence.DB_PATH
    intelligence.init_kb(str(tmp_path / "cleaner-status.db"))
    provider = Mock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr("services.autopilot.get_autopilot_config", Mock(return_value=_settings()))
    monkeypatch.setattr("integrations.facebook.get_adset_info", provider)
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/adsets/cleaner/status", headers=HEADERS)
    finally:
        intelligence.DB_PATH = previous_db_path

    assert response.status_code == 200
    payload = response.json()
    assert payload["last_run"] is None
    assert payload["adsets"] == []
    assert payload["unresolved_claims"] == []
    assert payload["replacement_workflows"] == []
    assert payload["recovery_cases"] == []
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_status_requires_existing_api_key_middleware():
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/adsets/cleaner/status")

    assert response.status_code == 403
    assert "X-API-Key" in response.json()["detail"]


@pytest.mark.asyncio
async def test_status_sql_failure_is_generic_503_and_does_not_leak(monkeypatch, caplog):
    monkeypatch.setattr(
        "services.cleanup_repository.get_cleanup_status",
        Mock(side_effect=RuntimeError("access_token=SUPER_SECRET")),
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/adsets/cleaner/status", headers=HEADERS)

    assert response.status_code == 503
    assert response.json() == {"detail": "Cleaner state unavailable"}
    assert "SUPER_SECRET" not in response.text
    assert "SUPER_SECRET" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {}, {"confirm": "DELETE", "adset_ids": ["1"]}])
async def test_cleanup_stream_is_exact_409_before_any_provider_call(monkeypatch, body):
    # У integrations.facebook больше нет своей HTTP-сессии: чтения идут через
    # _throttled_get, мутации — только в execution-транспорте. Закрываем все
    # текущие точки, иначе тест «никого не дёрнули» проверял бы несуществующее.
    fb_get = Mock(side_effect=AssertionError("FB must not be called"))
    fb_status = Mock(side_effect=AssertionError("FB must not be called"))
    fb_create = Mock(side_effect=AssertionError("FB must not be called"))
    trello = Mock(side_effect=AssertionError("Trello must not be called"))
    monkeypatch.setattr("integrations.facebook._throttled_get", fb_get)
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.set_ad_status", fb_status
    )
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.create_ad", fb_create
    )
    monkeypatch.setattr("integrations.trello.session", trello)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        kwargs = {} if body is None else {"json": body}
        response = await client.post(
            "/api/adsets/cleanup-stream", headers=HEADERS, **kwargs
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Прямое удаление отключено; используйте безопасный cleaner workflow"
    }
    fb_get.assert_not_called()
    fb_status.assert_not_called()
    fb_create.assert_not_called()
    trello.assert_not_called()
