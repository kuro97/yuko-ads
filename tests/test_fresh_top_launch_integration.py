"""Сквозные offline-проверки запуска свежих верхних карточек.

Тесты связывают публичные входы с единым launch checker/provider proof и
явно запрещают сетевые и необратимые побочные действия в deny-сценариях.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent import launcher
from services import auto_launch
from services.approval_checker_models import ActionOrigin
from services.launch_checker import (
    CheckedLaunchPlan,
    LaunchCheckBlocked,
    LaunchCheckRequest,
    LaunchSource,
    LaunchTarget,
    ProviderLaunchAuthorization,
)
from web.app import app, launch_status


API_KEY = "test-secret-key"
HEADERS = {
    "X-API-Key": API_KEY,
    "Idempotency-Key": "22222222-2222-4222-8222-222222222222",
}
MEDIA_SHA = "a" * 64


def _request(source: LaunchSource) -> LaunchCheckRequest:
    return LaunchCheckRequest(
        source=source,
        campaign_type="leadgen",
        cities=("CityA",),
        as_carousel=False,
        actor="system-test",
    )


def _plan(source: LaunchSource, *, card_id: str = "fresh-1") -> CheckedLaunchPlan:
    request = _request(source)
    target = LaunchTarget(
        city="CityA",
        ordinal=0,
        account_kind="offline",
        account_id="123",
        adset_id="1001",
        expected_names=("CityA | Свежая реклама [PRODA]",),
        identity_key="citya | свежая реклама",
        reserved_slots=1,
    )
    return CheckedLaunchPlan(
        check_id=f"check-{source.value.lower()}",
        card_id=card_id,
        card_name="Свежая реклама",
        trello_pos=1.0,
        request=request,
        product="PRODA",
        language="L1",
        media={"type": "image", "paths": ["/tmp/offline-fresh-top.jpg"]},
        media_sha256=MEDIA_SHA,
        targets=(target,),
        expected_names_by_city={"CityA": target.expected_names},
        authorization=ProviderLaunchAuthorization(
            f"auth-{source.value.lower()}", f"secret-{source.value.lower()}"
        ),
        plan_sha256="b" * 64,
    )


def _reset_launch_status() -> None:
    launch_status.clear()
    launch_status.update(
        {
            "running": False,
            "outcome": "failed",
            "check_id": None,
            "reason_codes": [],
            "reasons": [],
            "current": "",
            "progress": 0,
            "total": 0,
            "step": "",
            "step_pct": None,
            "log": [],
        }
    )


def test_agent_run_passes_only_scope_and_uuid_to_sealed_launcher():
    card = {
        "id": "fresh-1",
        "name": "Свежая реклама",
        "desc": "",
        "labels": ["PRODA"],
        "pos": 1.0,
    }
    plan = _plan(LaunchSource.AGENT_RUN)

    with patch("agent.launcher.get_done_list_id", return_value="ready"), patch(
        "agent.launcher.get_unlaunched_cards", return_value=[card]
    ), patch("agent.launcher.launch_single", return_value={"CityA": "ad-1"}) as launch:
        result = launcher.run(plan_provider=lambda _card: plan)

    assert len(result) == 1
    assert "authorization" not in launch.call_args.kwargs
    assert "prepared_media" not in launch.call_args.kwargs
    assert launch.call_args.kwargs["campaign_type"] == plan.request.campaign_type
    canonical_key = launch.call_args.kwargs["idempotency_key"]
    assert str(uuid.UUID(canonical_key)) == canonical_key


@pytest.mark.parametrize("source", (LaunchSource.CRON, LaunchSource.AUTO_LAUNCH_NOW))
def test_auto_paths_execute_only_the_exact_enforce_plan_with_provider_proof(
    tmp_path, monkeypatch, source
):
    state_path = tmp_path / f"auto-{source.value}.json"
    monkeypatch.setattr(auto_launch, "_AUTO_LAUNCH_STATE_FILE", state_path)
    monkeypatch.setattr(
        auto_launch,
        "_AUTO_LAUNCH_RUN_LOCK_FILE",
        tmp_path / f"auto-{source.value}.lock",
    )
    auto_launch._save_auto_launch_state(
        {
            "schema_version": 2,
            "launched_today": [],
            "launched_ever": {},
            "launch_attempts": {},
            "last_launch_date": None,
        }
    )
    card = {
        "id": "fresh-1",
        "name": "Свежая реклама",
        "desc": "",
        "labels": ["PRODA"],
        "pos": 1.0,
    }
    executed: list[tuple[str, object, LaunchSource]] = []

    class Checker:
        def __init__(self, mode: str):
            self.mode = mode

        def prepare_and_reserve(self, checked_card, request, _state):
            assert checked_card["id"] == "fresh-1"
            plan = _plan(request.source)
            if self.mode == "observe":
                return SimpleNamespace(
                    check_id=plan.check_id,
                    card_id=plan.card_id,
                    request=request,
                    media=plan.media,
                    media_sha256=plan.media_sha256,
                    authorization=None,
                )
            return plan

    def checker_factory(mode, **_kwargs):
        return Checker(str(mode))

    def propose_launch(**kwargs):
        # Producer доводит карточку только до предложения владельцу.
        status = kwargs["status"]
        status["proposal_state"] = "PENDING_OWNER"
        status["outcome"] = "pending_owner"
        status["running"] = False
        executed.append((kwargs["card_id"], kwargs["origin"], source))
        return {"proposal_id": f"proposal-{kwargs['card_id']}"}

    config = {
        "enabled": True,
        "kill_switch": False,
        "launch_enabled": True,
        "max_launches_per_day": 5,
        "launch_checker": {"mode": "enforce"},
    }
    with patch("services.auto_launch._get_autopilot_config", return_value=config), patch(
        "services.auto_launch._analyze_coverage", return_value={}
    ), patch("services.auto_launch._get_done_list_id", return_value="ready"), patch(
        "services.auto_launch._get_unlaunched_cards", return_value=[card]
    ), patch("services.auto_launch._get_launch_checker", side_effect=checker_factory), patch(
        "agent.launcher.launch_single", side_effect=propose_launch
    ), patch("services.auto_launch._send_telegram"):
        result = auto_launch.run_auto_launch("active", 1, source=source)

    assert result["error"] is None
    # Запуск не выполнен — владельцу отправлено ровно одно предложение
    assert result["launched"] == []
    assert [item["card_id"] for item in result["proposals"]] == ["fresh-1"]
    # Legacy provider proof в launcher больше не передаётся: он бы заблокировал
    # запуск как LEGACY_LAUNCH_BYPASS_FORBIDDEN. Вместо него — closed-list origin.
    assert executed == [("fresh-1", ActionOrigin.AUTO_LAUNCH, source)]


def test_selected_first_five_typed_blocks_never_substitute_position_six_or_mutate():
    _reset_launch_status()
    selected = [f"card-{index}" for index in range(1, 6)]
    cards = {
        f"card-{index}": {
            "id": f"card-{index}",
            "name": f"Карточка {index}",
            "desc": "",
            "labels": [],
            "pos": float(index),
        }
        for index in range(1, 7)
    }
    codes = {
        "card-1": "CAPACITY_BLOCKED",
        "card-2": "UNKNOWN_LEGACY",
        "card-3": "TOPIC_VETO",
        "card-4": "DUPLICATE_LIVE",
        "card-5": "INVENTORY_UNVERIFIED",
    }
    checked_ids: list[str] = []

    class BlockingChecker:
        def prepare_and_reserve(self, card, _request, _state):
            checked_ids.append(card["id"])
            code = codes[card["id"]]
            raise LaunchCheckBlocked(code, (f"blocked {card['id']}",), f"check-{card['id']}")

    client = TestClient(app)
    with patch("web.app._exact_live_launch_card", side_effect=lambda card_id: cards[card_id]), patch(
        "web.app._get_web_checker_mode", return_value="enforce"
    ), patch("web.app._get_web_launch_checker", return_value=BlockingChecker()), patch(
        "web.app._load_web_launch_state", return_value={}
    ), patch("web.app._run_checked_web_launch") as background, patch(
        "integrations.facebook.upload_image"
    ) as upload, patch(
        "integrations.facebook_ads_mutation_transport.create_ad"
    ) as provider_create, patch(
        "integrations.facebook_ads_mutation_transport.set_adset_budget"
    ) as budget_mutation, patch(
        "integrations.facebook.cleanup_stale_ads"
    ) as delete_ads, patch("integrations.trello.mark_card_done") as trello_done:
            responses = [
                client.post(
                    f"/api/launch/{card_id}",
                    headers={
                        **HEADERS,
                        "Idempotency-Key": str(uuid.uuid4()),
                    },
                )
                for card_id in selected
            ]

    assert [response.status_code for response in responses] == [409] * 5
    assert [response.json()["detail"]["reason_codes"][0] for response in responses] == [
        codes[card_id] for card_id in selected
    ]
    assert checked_ids == selected
    assert "card-6" not in checked_ids
    background.assert_not_called()
    upload.assert_not_called()
    # Единственные реальные точки мутации FB живут в execution-транспорте
    provider_create.assert_not_called()
    budget_mutation.assert_not_called()
    delete_ads.assert_not_called()
    trello_done.assert_not_called()


def test_cards_response_counts_raw_eligible_and_blocked_without_calling_full_preflight():
    _reset_launch_status()
    cards = [
        {"id": "fresh", "name": "Fresh", "desc": "", "labels": [], "pos": 1.0},
        {"id": "veto", "name": "Про бонус", "desc": "", "labels": [], "pos": 2.0},
        {"id": "legacy", "name": "Legacy", "desc": "", "labels": [], "pos": 3.0},
        {"id": "complete", "name": "Complete", "desc": "", "labels": [], "pos": 4.0},
        {"id": "partial", "name": "Partial", "desc": "", "labels": [], "pos": 5.0},
        {"id": "missing", "name": "Missing", "desc": "", "labels": [], "pos": 6.0},
    ]
    state = {
        "launched_ever": {
            "legacy": "2026-07-01T00:00:00+05:00",
            "complete": {"complete": True, "ad_ids": ["ad-1"]},
            "partial": {"complete": False, "ad_ids": ["ad-2"]},
        },
        "launch_attempts": {},
    }
    client = TestClient(app)

    with patch("web.app.get_done_list_id", return_value="ready"), patch(
        "web.app.get_unlaunched_cards", return_value=cards
    ), patch(
        "web.app.get_card_drive_link",
        side_effect=lambda card_id: None if card_id == "missing" else "drive-link",
    ), patch("web.app._load_web_launch_state", return_value=state), patch(
        "web.app.score_creative", return_value={"level": "LOW", "value": 0}
    ), patch("web.app._get_web_launch_checker") as full_preflight, patch(
        "integrations.gdrive.download_media"
    ) as media_download:
        response = client.get("/api/cards", headers=HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == payload["raw_count"] == 6
    assert payload["eligible_count"] == 0
    assert payload["preflight_pending_count"] == 1
    assert payload["blocked_count"] == 5
    assert payload["raw_count"] == (
        payload["eligible_count"]
        + payload["preflight_pending_count"]
        + payload["blocked_count"]
    )
    full_preflight.assert_not_called()
    media_download.assert_not_called()
