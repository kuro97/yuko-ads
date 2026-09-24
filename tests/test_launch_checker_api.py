"""Offline API-контракт launch checker-а: без Trello/Drive/Facebook сети."""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from services.launch_checker import (
    LaunchCheckBlocked,
    LaunchSource,
    ProviderLaunchAuthorization,
    hashed_api_key_actor,
)
from services.launch_checker_runtime import (
    LaunchAuthorizationFinalization,
    LaunchLifecycleOutcome,
)
from web.app import app, launch_status, _run_checked_web_launch


API_KEY = "test-secret-key"
HEADERS = {
    "X-API-Key": API_KEY,
    "Idempotency-Key": "22222222-2222-4222-8222-222222222222",
}


def _mutation_headers() -> dict[str, str]:
    return {**HEADERS, "Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture
def client():
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
    return TestClient(app)


def _plan(*, authorization=True):
    proof = (
        ProviderLaunchAuthorization("launch-auth-public-id", "top-secret-proof")
        if authorization
        else None
    )
    return SimpleNamespace(
        check_id="launch-check-1",
        authorization=proof,
        media={"type": "video", "paths": ["/tmp/checked-video.mp4"]},
    )


class _Checker:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.requests = []

    def prepare_and_reserve(self, card, request, state):
        self.requests.append((card, request, state))
        if self.error is not None:
            raise self.error
        return self.result


def test_cards_are_raw_top_order_with_cheap_checker_counts(client):
    cards = [
        {"id": "legacy", "name": "Legacy", "desc": "", "labels": [], "pos": 30.0},
        {"id": "fresh", "name": "Fresh", "desc": "", "labels": [], "pos": 10.0},
        {"id": "veto", "name": "Акция с бонусом", "desc": "", "labels": [], "pos": 20.0},
        {"id": "no-media", "name": "No media", "desc": "", "labels": [], "pos": 40.0},
    ]
    state = {"launched_ever": {"legacy": "2026-01-01"}, "launch_attempts": {}}

    def drive_link(card_id):
        return None if card_id == "no-media" else "https://drive.google.com/file/d/media"

    with patch("web.app.get_done_list_id", return_value="ready"), patch(
        "web.app.get_unlaunched_cards", return_value=cards
    ), patch("web.app.get_card_drive_link", side_effect=drive_link), patch(
        "web.app._load_web_launch_state", return_value=state
    ), patch("web.app.score_creative", return_value={"level": "LOW", "value": 0}), patch(
        "integrations.gdrive.download_media"
    ) as download_media, patch("web.app._get_web_launch_checker") as full_checker:
        response = client.get("/api/cards", headers=HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert [card["id"] for card in payload["cards"]] == [
        "fresh", "veto", "legacy", "no-media"
    ]
    assert payload["count"] == payload["raw_count"] == 4
    assert payload["eligible_count"] == 0
    assert payload["preflight_pending_count"] == 1
    assert payload["blocked_count"] == 3
    by_id = {card["id"]: card for card in payload["cards"]}
    assert by_id["fresh"]["launch_status"] == "needs_full_preflight"
    assert by_id["veto"]["reason_codes"] == ["TOPIC_VETO"]
    assert by_id["veto"]["topic_override_available"] is True
    assert by_id["legacy"]["reason_codes"] == ["UNKNOWN_LEGACY"]
    assert by_id["no-media"]["reason_codes"] == ["MEDIA_LINK_MISSING"]
    download_media.assert_not_called()
    full_checker.assert_not_called()


def test_checker_deny_is_typed_409_and_never_starts_background(client):
    card = {"id": "c1", "name": "Fresh", "desc": "", "labels": [], "pos": 1.0}
    checker = _Checker(
        error=LaunchCheckBlocked(
            "CAPACITY_BLOCKED", ("CityA L1: 50/50",), "launch-check-denied"
        )
    )
    with patch("web.app._exact_live_launch_card", return_value=card), patch(
        "web.app._get_web_checker_mode", return_value="enforce"
    ), patch("web.app._get_web_launch_checker", return_value=checker), patch(
        "web.app._load_web_launch_state", return_value={}
    ), patch("web.app._run_checked_web_launch") as runner:
        response = client.post("/api/launch/c1", headers=_mutation_headers())

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "status": "blocked",
        "check_id": "launch-check-denied",
        "reason_codes": ["CAPACITY_BLOCKED"],
        "reasons": ["CityA L1: 50/50"],
    }
    assert launch_status["running"] is False
    assert launch_status["outcome"] == "blocked"
    runner.assert_not_called()


def test_batch_topic_override_has_hashed_actor_and_proof_only_in_closure(client):
    card = {
        "id": "veto", "name": "Шанс на бонус", "desc": "", "labels": [], "pos": 1.0,
    }
    plan = _plan()
    checker = _Checker(result=plan)
    reason = "Ручное решение владельца"
    with patch("web.app._exact_live_launch_card", return_value=card), patch(
        "web.app._get_web_checker_mode", return_value="enforce"
    ), patch("web.app._get_web_launch_checker", return_value=checker), patch(
        "web.app._load_web_launch_state", return_value={}
    ), patch("web.app._run_checked_web_launch") as runner:
        response = client.post(
            "/api/launch/topic-override",
            params={"source": "batch", "override_topic_veto": "true", "override_reason": reason},
            headers=_mutation_headers(),
        )

    assert response.status_code == 202
    request = checker.requests[0][1]
    assert request.source is LaunchSource.BATCH
    assert request.override_topic_veto is True
    assert request.override_reason == reason
    assert request.actor == hashed_api_key_actor(API_KEY)
    assert API_KEY not in response.text
    assert "top-secret-proof" not in response.text
    assert "top-secret-proof" not in repr(launch_status)
    assert response.json()["auth_id"] == "launch-auth-public-id"
    assert runner.call_args.args[1] is plan


def test_observe_mode_returns_409_and_cleans_prepared_media(client):
    card = {"id": "c1", "name": "Fresh", "desc": "", "labels": [], "pos": 1.0}
    plan = _plan(authorization=False)
    checker = _Checker(result=plan)
    with patch("web.app._exact_live_launch_card", return_value=card), patch(
        "web.app._get_web_checker_mode", return_value="observe"
    ), patch("web.app._get_web_launch_checker", return_value=checker), patch(
        "web.app._load_web_launch_state", return_value={}
    ), patch("services.launch_checker_runtime.cleanup_prepared_media") as cleanup, patch(
        "web.app._run_checked_web_launch"
    ) as runner:
        response = client.post("/api/launch/c1", headers=_mutation_headers())

    assert response.status_code == 409
    assert response.json()["detail"]["reason_codes"] == ["CHECKER_OBSERVE"]
    assert launch_status["running"] is False
    cleanup.assert_called_once_with(plan.media)
    runner.assert_not_called()


@pytest.mark.parametrize("outcome", ["succeeded", "partial"])
def test_background_passes_only_scope_to_sealed_launcher(outcome):
    card = {"id": "c1", "name": "Fresh", "desc": "", "labels": ["PRODB"], "pos": 1.0}
    plan = _plan()
    launch_status.update({"running": True, "outcome": "running", "log": []})

    def launch_side_effect(**kwargs):
        kwargs["status"]["outcome"] = outcome
        kwargs["status"]["running"] = False
        return {"CityA": "ad-1"}

    finalization = LaunchAuthorizationFinalization(
        auth_id=plan.authorization.auth_id,
        outcome=LaunchLifecycleOutcome.RELEASED,
        total_ads=1 if outcome == "succeeded" else 2,
        created_ads=1,
        created_ad_ids=("ad-1",),
        needs_reconcile=False,
        consumes_daily_slot=True,
    )
    with patch("web.app.launch_single", side_effect=launch_side_effect) as launch, patch(
        "web.app._finish_web_launch_authorization",
        return_value=finalization,
    ) as finish, patch(
        "services.launch_checker_runtime.cleanup_prepared_media"
    ) as cleanup:
        _run_checked_web_launch(
            card, plan, "leadgen_prodb", ["CityA"], False, str(uuid.uuid4())
        )

    kwargs = launch.call_args.kwargs
    assert "authorization" not in kwargs
    assert "prepared_media" not in kwargs
    assert kwargs["trello_labels"] == ["PRODB"]
    assert str(uuid.UUID(kwargs["idempotency_key"])) == kwargs["idempotency_key"]
    finish.assert_called_once_with(plan, "RELEASED")
    cleanup.assert_called_once_with(plan.media)
    assert launch_status["outcome"] == outcome
    assert launch_status["running"] is False
    assert "top-secret-proof" not in repr(launch_status)


@pytest.mark.parametrize(
    "durable_outcome,created_ads,expected_web_outcome",
    [
        (LaunchLifecycleOutcome.PARTIAL, 1, "partial"),
        (LaunchLifecycleOutcome.BLOCKED_RECONCILE, 1, "partial"),
        (LaunchLifecycleOutcome.BLOCKED_RECONCILE, 0, "blocked"),
        (LaunchLifecycleOutcome.RELEASED, 0, "failed"),
    ],
)
def test_background_status_comes_from_sealed_launcher(
    durable_outcome,
    created_ads,
    expected_web_outcome,
):
    card = {"id": "c1", "name": "Fresh", "desc": "", "labels": [], "pos": 1.0}
    plan = _plan()
    launch_status.update({"running": True, "outcome": "running", "log": []})
    finalization = LaunchAuthorizationFinalization(
        auth_id=plan.authorization.auth_id,
        outcome=durable_outcome,
        total_ads=2,
        created_ads=created_ads,
        created_ad_ids=tuple(f"ad-{index}" for index in range(created_ads)),
        needs_reconcile=durable_outcome is LaunchLifecycleOutcome.BLOCKED_RECONCILE,
        consumes_daily_slot=created_ads > 0,
    )
    def gateway_launch(**kwargs):
        kwargs["status"]["outcome"] = expected_web_outcome
        kwargs["status"]["running"] = False
        return {}

    with patch("web.app.launch_single", side_effect=gateway_launch), patch(
        "web.app._finish_web_launch_authorization",
        return_value=finalization,
    ) as finish, patch("services.launch_checker_runtime.cleanup_prepared_media"):
        _run_checked_web_launch(
            card, plan, "leadgen", ["CityA"], False, str(uuid.uuid4())
        )

    finish.assert_called_once_with(plan, "RELEASED")
    assert launch_status["outcome"] == expected_web_outcome
    assert launch_status["running"] is False


def test_sse_done_contains_blocked_or_partial_terminal_outcome(client):
    for outcome in ("blocked", "partial"):
        launch_status.update(
            {
                "running": False,
                "outcome": outcome,
                "check_id": "launch-check-terminal",
                "reason_codes": ["TEST_BLOCK"] if outcome == "blocked" else [],
                "reasons": ["Запуск заблокирован"] if outcome == "blocked" else [],
                "current": "Fresh",
                "progress": 1,
                "total": 5,
                "step": "",
                "step_pct": None,
                "log": [],
            }
        )
        response = client.get("/api/launch-stream", headers=HEADERS)
        assert response.status_code == 200
        assert f'"outcome": "{outcome}"' in response.text
        assert "event: done" in response.text
        assert "data: finished" not in response.text
