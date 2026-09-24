"""Единый lifecycle finalizer и fresh-live reconciliation."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services import creative_intelligence as ci
from services import launch_repository
from services.launch_checker import LaunchCheckBlocked
from services.launch_checker_runtime import (
    LaunchLifecycleOutcome,
    finalize_launch_authorization,
    reconcile_checked_launch_authorization,
)


NOW = datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc)
MEDIA_SHA = "a" * 64


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    ci.DB_PATH = None
    ci.init_kb(str(tmp_path / "lifecycle.db"))
    yield
    ci.DB_PATH = None


def _checked_plan(auth_id: str, names: tuple[str, ...]):
    proof = SimpleNamespace(auth_id=auth_id, secret="lifecycle-secret")
    plan = SimpleNamespace(
        check_id=f"check-{auth_id}",
        card_id=f"card-{auth_id}",
        card_name="Lifecycle card",
        request=SimpleNamespace(
            source="MANUAL",
            campaign_type="L1",
            actor="test",
            override_topic_veto=False,
            override_reason=None,
        ),
        media_sha256=MEDIA_SHA,
        targets=(
            SimpleNamespace(
                city="CityA",
                ordinal=0,
                account_kind="offline",
                account_id="123456",
                adset_id="1001",
                reserved_slots=len(names),
            ),
        ),
        expected_names_by_city={"CityA": names},
        authorization=proof,
    )
    launch_repository.reserve_authorization(
        plan,
        hashlib.sha256(proof.secret.encode()).hexdigest(),
        NOW,
    )
    return plan


def _scope(plan, ad_name: str):
    return SimpleNamespace(
        account_kind="offline",
        account_id="123456",
        city="CityA",
        adset_id="1001",
        ad_name=ad_name,
        media_sha256=MEDIA_SHA,
    )


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("BLOCKED", LaunchLifecycleOutcome.BLOCKED),
        ("FAILED", LaunchLifecycleOutcome.RELEASED),
        ("COMPLETED", LaunchLifecycleOutcome.RELEASED),
    ],
)
def test_zero_create_finalizer_never_leaves_reserved(requested, expected):
    plan = _checked_plan("auth-zero", ("CityA | Lifecycle card [PRODB]",))

    result = finalize_launch_authorization(
        plan.authorization,
        requested_outcome=requested,
        now=NOW,
    )

    assert result.outcome is expected
    assert result.created_ads == 0
    assert result.consumes_daily_slot is False
    assert launch_repository.get_reserved_slots("1001", NOW) == 0


def test_mid_card_exception_uses_durable_partial_and_consumes_slot():
    names = (
        "CityA | Lifecycle card / 1 [PRODB]",
        "CityA | Lifecycle card / 2 [PRODB]",
    )
    plan = _checked_plan("auth-partial", names)
    claim_id = launch_repository.claim_provider_create(
        plan.authorization,
        _scope(plan, names[0]),
        NOW,
    )
    launch_repository.record_provider_create_success(claim_id, "ad-1", NOW)

    result = finalize_launch_authorization(
        plan.authorization,
        requested_outcome="FAILED",
        now=NOW,
    )

    assert result.outcome is LaunchLifecycleOutcome.PARTIAL
    assert result.created_ad_ids == ("ad-1",)
    assert result.consumes_daily_slot is True


def test_unresolved_claim_finalizes_blocked_reconcile_not_failed():
    name = "CityA | Lifecycle card [PRODB]"
    plan = _checked_plan("auth-claim", (name,))
    launch_repository.claim_provider_create(
        plan.authorization,
        _scope(plan, name),
        NOW,
    )

    result = finalize_launch_authorization(
        plan.authorization,
        requested_outcome="FAILED",
        now=NOW,
    )

    assert result.outcome is LaunchLifecycleOutcome.BLOCKED_RECONCILE
    assert result.needs_reconcile is True
    assert result.consumes_daily_slot is False


def test_fresh_exact_inventory_rotates_only_unclaimed_missing(monkeypatch):
    names = (
        "CityA | Lifecycle card / 1 [PRODB]",
        "CityA | Lifecycle card / 2 [PRODB]",
    )
    plan = _checked_plan("auth-reconcile", names)
    launch_repository.claim_provider_create(
        plan.authorization,
        _scope(plan, names[0]),
        NOW,
    )
    launch_repository.expire_stale_reservations(NOW + timedelta(minutes=31))

    class CompleteInventory(list):
        inventory_complete = True

    inventory = CompleteInventory(
        [
            {
                "id": "ad-live-1",
                "name": names[0],
                "adset_id": "1001",
            }
        ]
    )
    monkeypatch.setattr(
        "integrations.facebook.fetch_complete_account_ad_inventory",
        lambda _kind, _account: inventory,
    )

    result = reconcile_checked_launch_authorization(
        plan,
        now=NOW + timedelta(minutes=31),
    )

    assert result.outcome is LaunchLifecycleOutcome.PARTIAL
    assert result.authorization is not None
    assert result.created_ad_ids == ("ad-live-1",)
    assert result.missing_names_by_city == {"CityA": (names[1],)}
    with pytest.raises(launch_repository.LaunchRepositoryBlocked) as old_proof:
        launch_repository.validate_authorization_media(
            plan.authorization,
            MEDIA_SHA,
            NOW + timedelta(minutes=32),
        )
    assert old_proof.value.code == "INVALID_AUTHORIZATION"


def test_fresh_inventory_ambiguity_is_typed_and_never_rotates(monkeypatch):
    name = "CityA | Lifecycle card [PRODB]"
    plan = _checked_plan("auth-ambiguous", (name,))
    launch_repository.claim_provider_create(
        plan.authorization,
        _scope(plan, name),
        NOW,
    )
    launch_repository.expire_stale_reservations(NOW + timedelta(minutes=31))

    class CompleteInventory(list):
        inventory_complete = True

    monkeypatch.setattr(
        "integrations.facebook.fetch_complete_account_ad_inventory",
        lambda _kind, _account: CompleteInventory(),
    )

    with pytest.raises(LaunchCheckBlocked) as captured:
        reconcile_checked_launch_authorization(
            plan,
            now=NOW + timedelta(minutes=31),
        )

    assert captured.value.code == "LIVE_RECONCILIATION_AMBIGUOUS"
    assert launch_repository.reconcile_authorization(
        plan.authorization.auth_id,
        NOW + timedelta(days=1),
    ).phase == "BLOCKED_RECONCILE"
