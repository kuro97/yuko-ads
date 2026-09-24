"""Single-ad cleanup authorization и запрет Facebook DELETE — без сети.

Жизненный цикл capability (issue → consume → revoke, привязка, TTL) остался в
силе: он общий для cleanup-воркфлоу. А вот сам DELETE удалён — у операции нет
typed owner contract, поэтому `facebook.cleanup_stale_ads` отвечает
`ForbiddenMutation` до любого HTTP. Отсюда важное следствие, которое здесь и
проверяется: отказ НЕ сжигает capability владельца и не пишет reconcile-статус.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import integrations.facebook as facebook
from integrations.facebook_ads_mutation_transport import ForbiddenMutation
from services import cleanup_authorization as authorization_service
from services.cleanup_authorization import (
    CleanupAuthorizationError,
    CleanupDeleteAuthorization,
    consume_delete_authorization,
    issue_delete_authorization,
    revoke_delete_authorization,
)
from services.cleanup_repository import CleanupClaimError, CleanupDeleteClaim


def _fresh_local_zero() -> dict:
    return {
        "kb_found": True,
        "local_spend_usd": 0.0,
        "local_impressions": 0,
        "local_clicks": 0,
        "local_leads": 0,
        "local_payments": 0,
        "any_positive_delivery": False,
        "any_positive_outcome": False,
        "complete": True,
        "error": None,
    }


@pytest.fixture(autouse=True)
def _clear_capability_registries():
    authorization_service._AUTHORIZATIONS.clear()
    authorization_service._ISSUED_CLAIM_IDS.clear()
    facebook._CLEANUP_GUARD_REGISTRY.clear()
    with patch(
        "services.cleanup_authorization.mark_cleanup_delete_http_started"
    ), patch(
        "services.cleanup_authorization.revoke_cleanup_delete_authorization"
    ), patch(
        "integrations.facebook._fetch_cleanup_lifetime_zero",
        return_value={"spend": 0.0, "impressions": 0, "clicks": 0},
    ), patch(
        "integrations.facebook._prepare_current_cleanup_delete_boundary",
        return_value=(
            _fresh_local_zero(),
            "a" * 64,
            {"config_hash": "b" * 64, "config_generation": 7},
        ),
    ):
        yield
    authorization_service._AUTHORIZATIONS.clear()
    authorization_service._ISSUED_CLAIM_IDS.clear()
    facebook._CLEANUP_GUARD_REGISTRY.clear()


def _claim(
    *,
    claim_id: str = "claim-1",
    workflow_id: str = "workflow-1",
    adset_id: str = "adset-1",
    ad_id: str = "old-1",
    state: str = "CLAIMED",
) -> CleanupDeleteClaim:
    return CleanupDeleteClaim(
        claim_id=claim_id,
        run_id="run-1",
        workflow_id=workflow_id,
        ad_id=ad_id,
        ad_name="Старое объявление",
        adset_id=adset_id,
        purpose="REPLACEMENT_SLOT",
        state=state,
        capacity_before=0,
        claimed_at=datetime.now(timezone.utc),
    )


def _issue(claim: CleanupDeleteClaim) -> CleanupDeleteAuthorization:
    with patch(
        "services.cleanup_authorization.create_cleanup_delete_authorization",
    ):
        return issue_delete_authorization(claim)


def _capacity(*, deleted: bool = False) -> dict:
    stale_ads = [] if deleted else [{
        "id": "old-1",
        "name": "Старое объявление",
        "adset_id": "adset-1",
        "status": "PAUSED",
        "effective_status": "PAUSED",
        "created_time": "2026-05-01T00:00:00+0000",
    }]
    ad_ids = ["active-1"] if deleted else ["old-1", "active-1"]
    return {
        "adset_id": "adset-1",
        "adset_effective_status": "ACTIVE",
        "name": "CityA L1",
        "daily_budget": 30.0,
        "ad_count": len(ad_ids),
        "max_ads": 50,
        "available": 50 - len(ad_ids),
        "recommended": 2,
        "stale_ads": stale_ads,
        "ad_ids": ad_ids,
        "effective_active_ids": ["active-1"],
        "effective_active_count": 1,
        "unknown_effective_status_ids": [],
        "fetched_ad_count": len(ad_ids),
        "inventory_complete": True,
    }


def _guard_and_authorization():
    claim = _claim()
    authorization = _issue(claim)
    with patch("integrations.facebook.get_adset_capacity", return_value=_capacity()):
        guarded = facebook.get_cleanup_capacity(
            "adset-1",
            candidate_id="old-1",
        )
    return guarded, authorization


def test_authorization_exact_binding_is_single_use():
    claim = _claim()
    authorization = _issue(claim)

    consume_delete_authorization(
        authorization,
        claim_id=claim.claim_id,
        workflow_id=claim.workflow_id,
        adset_id=claim.adset_id,
        ad_id=claim.ad_id,
        live_capacity_before=claim.capacity_before,
    )

    with pytest.raises(CleanupAuthorizationError, match="consumed_or_forged"):
        consume_delete_authorization(
            authorization,
            claim_id=claim.claim_id,
            workflow_id=claim.workflow_id,
            adset_id=claim.adset_id,
            ad_id=claim.ad_id,
            live_capacity_before=claim.capacity_before,
        )


def test_authorization_mismatch_consumes_capability():
    claim = _claim()
    authorization = _issue(claim)

    with pytest.raises(CleanupAuthorizationError, match="binding_mismatch"):
        consume_delete_authorization(
            authorization,
            claim_id=claim.claim_id,
            workflow_id=claim.workflow_id,
            adset_id=claim.adset_id,
            ad_id="other-ad",
            live_capacity_before=claim.capacity_before,
        )
    with pytest.raises(CleanupAuthorizationError, match="consumed_or_forged"):
        consume_delete_authorization(
            authorization,
            claim_id=claim.claim_id,
            workflow_id=claim.workflow_id,
            adset_id=claim.adset_id,
            ad_id=claim.ad_id,
            live_capacity_before=claim.capacity_before,
        )


def test_authorization_expiry_and_reissue_are_blocked():
    claim = _claim()
    start = datetime(2026, 7, 20, tzinfo=timezone.utc)
    with patch("services.cleanup_authorization._now", return_value=start), patch(
        "services.cleanup_authorization.create_cleanup_delete_authorization",
    ):
        authorization = issue_delete_authorization(claim, ttl=timedelta(seconds=1))
    with patch(
        "services.cleanup_authorization._now",
        return_value=start + timedelta(seconds=1),
    ):
        with pytest.raises(CleanupAuthorizationError, match="expired"):
            consume_delete_authorization(
                authorization,
                claim_id=claim.claim_id,
                workflow_id=claim.workflow_id,
                adset_id=claim.adset_id,
                ad_id=claim.ad_id,
                live_capacity_before=claim.capacity_before,
            )
    with patch(
        "services.cleanup_authorization.create_cleanup_delete_authorization",
    ):
        with pytest.raises(CleanupAuthorizationError, match="already_issued"):
            issue_delete_authorization(claim)


def test_authorization_requires_exact_durable_claim():
    claim = _claim()

    with patch(
        "services.cleanup_authorization.create_cleanup_delete_authorization",
        side_effect=CleanupClaimError("missing"),
    ):
        with pytest.raises(CleanupAuthorizationError, match="not_authorizable"):
            issue_delete_authorization(claim)
    with patch(
        "services.cleanup_authorization.create_cleanup_delete_authorization",
        side_effect=CleanupClaimError("mismatch"),
    ):
        with pytest.raises(CleanupAuthorizationError, match="not_authorizable"):
            issue_delete_authorization(claim)
    with pytest.raises(CleanupAuthorizationError, match="not_authorizable"):
        issue_delete_authorization(replace(claim, state="RECONCILE_REQUIRED"))


def test_forged_replaced_and_revoked_authorizations_are_blocked():
    claim = _claim()
    genuine = _issue(claim)
    forged = CleanupDeleteAuthorization(
        token_id=genuine.token_id,
        claim_id=genuine.claim_id,
        workflow_id=genuine.workflow_id,
        adset_id=genuine.adset_id,
        ad_id=genuine.ad_id,
        purpose=genuine.purpose,
        expires_at=genuine.expires_at,
    )

    for fake in (forged, replace(genuine)):
        with pytest.raises(CleanupAuthorizationError, match="consumed_or_forged"):
            consume_delete_authorization(
                fake,
                claim_id=claim.claim_id,
                workflow_id=claim.workflow_id,
                adset_id=claim.adset_id,
                ad_id=claim.ad_id,
                live_capacity_before=claim.capacity_before,
            )
    revoke_delete_authorization(claim.claim_id)
    with pytest.raises(CleanupAuthorizationError, match="consumed_or_forged"):
        consume_delete_authorization(
            genuine,
            claim_id=claim.claim_id,
            workflow_id=claim.workflow_id,
            adset_id=claim.adset_id,
            ad_id=claim.ad_id,
            live_capacity_before=claim.capacity_before,
        )


def _assert_authorization_still_usable(authorization, claim: CleanupDeleteClaim) -> None:
    """Отказ операции не должен сжигать capability владельца."""
    consume_delete_authorization(
        authorization,
        claim_id=claim.claim_id,
        workflow_id=claim.workflow_id,
        adset_id=claim.adset_id,
        ad_id=claim.ad_id,
        live_capacity_before=claim.capacity_before,
    )


@pytest.mark.parametrize(
    "stale_ads,count",
    [
        ([{"id": "old-1", "name": "Старое объявление", "adset_id": "adset-1"}], 1),
        ([], 1),
        ([{"id": "old-1"}, {"id": "old-2"}], 1),
        ([{"id": "old-1"}], 2),
    ],
    ids=["exact-single", "empty", "two-candidates", "count-mismatch"],
)
def test_facebook_delete_is_forbidden_for_every_capability_shape(stale_ads, count):
    """Ни корректный singular-вход, ни любой некорректный не доходят до HTTP.

    Раньше корректный вход удалял объявление, а некорректный ловил
    CleanupGuardError. Теперь DELETE удалён целиком, поэтому проверяем
    единственный оставшийся исход: ForbiddenMutation без обращения к FB.
    """
    claim = _claim()
    authorization = _issue(claim)
    with patch("integrations.facebook.get_adset_capacity", return_value=_capacity()):
        guarded = facebook.get_cleanup_capacity("adset-1", candidate_id="old-1")

    with patch("integrations.facebook.get_adset_capacity") as capacity, \
         patch("services.adset_pause_guard.adset_mutation_lock") as lock:
        with pytest.raises(ForbiddenMutation, match="DELETE_ARCHIVE_OPERATION_FORBIDDEN"):
            facebook.cleanup_stale_ads(
                stale_ads,
                count,
                guard_evidence=guarded["cleanup_guard_evidence"],
                authorization=authorization,
            )

    capacity.assert_not_called()
    lock.assert_not_called()
    _assert_authorization_still_usable(authorization, claim)


def test_facebook_delete_binding_mismatch_blocks_before_http():
    """Чужая authorization тоже упирается в запрет операции, а не в HTTP."""
    with patch("integrations.facebook.get_adset_capacity", return_value=_capacity()):
        guarded = facebook.get_cleanup_capacity("adset-1", candidate_id="old-1")
    wrong_claim = _claim(claim_id="claim-2", ad_id="old-2")
    wrong_authorization = _issue(wrong_claim)

    with patch("services.adset_pause_guard.adset_mutation_lock") as lock:
        with pytest.raises(ForbiddenMutation, match="DELETE_ARCHIVE_OPERATION_FORBIDDEN"):
            facebook.cleanup_stale_ads(
                [{"id": "old-1", "name": "Старое объявление"}],
                1,
                guard_evidence=guarded["cleanup_guard_evidence"],
                authorization=wrong_authorization,
            )

    lock.assert_not_called()
    # Чужая capability не тронута — её нельзя погасить чужим отказом
    _assert_authorization_still_usable(wrong_authorization, wrong_claim)


def test_forbidden_delete_never_writes_reconcile_state():
    """Без HTTP нет неопределённости — reconcile-статус не пишется никогда.

    Прежде таймаут/500/невалидный ответ FB переводили операцию в
    RECONCILE_REQUIRED и запрещали повтор. Теперь HTTP не происходит вовсе,
    поэтому durable-состояние обязано остаться нетронутым, а повтор — отказным
    ровно так же, без деградации в «требуется разбор».
    """
    claim = _claim()
    authorization = _issue(claim)
    with patch("integrations.facebook.get_adset_capacity", return_value=_capacity()):
        guarded = facebook.get_cleanup_capacity("adset-1", candidate_id="old-1")

    with patch(
        "services.cleanup_authorization.mark_cleanup_delete_http_started"
    ) as http_started, patch(
        "services.cleanup_repository.finish_cleanup_delete"
    ) as finish:
        for _ in range(2):
            with pytest.raises(ForbiddenMutation, match="DELETE_ARCHIVE_OPERATION_FORBIDDEN"):
                facebook.cleanup_stale_ads(
                    [{"id": "old-1", "name": "Старое объявление"}],
                    1,
                    guard_evidence=guarded["cleanup_guard_evidence"],
                    authorization=authorization,
                )

    http_started.assert_not_called()
    finish.assert_not_called()
    _assert_authorization_still_usable(authorization, claim)


def test_cleanup_all_adsets_is_disabled_before_any_external_call():
    with patch("agent.adset_discovery.get_adsets_dict") as discovery, patch(
        "integrations.facebook.get_adset_capacity"
    ) as capacity, patch(
        "services.adset_pause_guard.adset_mutation_lock"
    ) as lock:
        with pytest.raises(facebook.CleanupGuardError, match="direct_cleanup_disabled"):
            facebook.cleanup_all_adsets()

    discovery.assert_not_called()
    capacity.assert_not_called()
    lock.assert_not_called()
