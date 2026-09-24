from __future__ import annotations

import inspect
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from services.action_manifests import (
    build_action_batch,
    build_launch_manifest,
    build_pause_manifest,
    build_scale_manifest,
    build_unpause_manifest,
)
from services.approval_checker_models import (
    ActionKind,
    ActionOrigin,
    CreativeSpec,
    FactCategory,
    FactClaim,
    LaunchDestination,
    MediaAssetSpec,
    MediaType,
    Metric,
    PauseCandidate,
    PaymentEvidence,
    PlacementRole,
    PreparedLaunch,
    ScaleCandidate,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    TrelloPrecondition,
    UnpauseCandidate,
    manifest_sha256,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _window() -> TimeWindow:
    return TimeWindow(NOW - timedelta(days=7), NOW, "Etc/GMT-5", "TRAILING_7D")


def _prepared_launch() -> PreparedLaunch:
    trello = TrelloPrecondition(
        card_id="card-1",
        board_id="board-1",
        ready_list_id="ready-1",
        expected_list_id="ready-1",
        expected_due_complete=False,
        expected_closed=False,
        date_last_activity=NOW - timedelta(minutes=1),
        attachment_ids=("attachment-1",),
        attachment_manifest_sha256=SHA_A,
        labels_sha256=SHA_A,
        card_content_sha256=SHA_A,
    )
    asset = MediaAssetSpec(
        asset_id="asset-1",
        order_index=0,
        media_type=MediaType.VIDEO,
        placement_group_id=None,
        placement_role=PlacementRole.DEFAULT,
        staged_relative_path="media/asset-1.mp4",
        original_attachment_id="attachment-1",
        mime_type="video/mp4",
        size_bytes=123,
        content_sha256=SHA_A,
    )
    creative = CreativeSpec(
        creative_id="creative-1",
        order_index=0,
        ad_name="exact-name",
        media_asset_ids=(asset.asset_id,),
        body_staged_relative_path="text/creative-1.txt",
        body_sha256=SHA_B,
        product="PRODA",
        page_id="page-1",
        lead_form_id="form-1",
        call_to_action="LEARN_MORE",
        instagram_actor_id="instagram-1",
        title="title",
        link_url=None,
        expected_configured_status="ACTIVE",
    )
    destination = LaunchDestination(
        city="CityB",
        account_id="account-1",
        adset_id="adset-1",
        adset_type="L2",
        current_daily_budget=Decimal("100"),
        currency="USD",
        capacity_available=8,
        hard_reserve_slots=2,
        creatives=(creative,),
        duplicate_signature=SHA_A,
    )
    return PreparedLaunch(
        manifest_id=str(uuid.uuid4()),
        staged_at=NOW - timedelta(seconds=1),
        staging_root=Path("/tmp/approval-staging"),
        staging_directory=Path("/tmp/approval-staging/manifest"),
        trello=trello,
        card_name="Карточка",
        card_name_sha256=SHA_A,
        campaign_type="PRODA",
        config_version_sha256=SHA_A,
        media_assets=(asset,),
        destinations=(destination,),
        media_manifest_sha256=SHA_A,
    )


def _payment() -> PaymentEvidence:
    return PaymentEvidence(
        payment_id="payment-1",
        contract_number="contract-1",
        amo_lead_id=101,
        fb_ad_id="ad-1",
        amount_lcy=Decimal("120000"),
        direction="INCOME",
        contract_net_lcy=Decimal("120000"),
        document_at=NOW - timedelta(days=1),
        fetched_at=NOW,
    )


def _scale_facts() -> tuple[FactClaim, ...]:
    ad = SubjectRef(SubjectKind.AD, "ad-1", "adset-1")
    return (
        FactClaim(
            "fb",
            None,
            FactCategory.BUSINESS_METRIC,
            ad,
            Metric.SPEND,
            Decimal("10"),
            SourceSystem.FACEBOOK,
            _window(),
            "USD",
        ),
        FactClaim(
            "amo",
            None,
            FactCategory.BUSINESS_METRIC,
            ad,
            Metric.QUALS,
            2,
            SourceSystem.AMO,
            _window(),
        ),
        FactClaim(
            "cdp",
            None,
            FactCategory.BUSINESS_METRIC,
            ad,
            Metric.REVENUE,
            Decimal("100000"),
            SourceSystem.CDP_ERP,
            _window(),
            "LCY",
        ),
    )


def test_launch_factory_copies_only_exact_staged_contract() -> None:
    prepared = _prepared_launch()
    key = str(uuid.uuid4())

    manifest = build_launch_manifest(
        prepared,
        origin=ActionOrigin.AUTO_LAUNCH,
        idempotency_key=key,
        now=NOW,
    )

    assert manifest.kind is ActionKind.LAUNCH
    assert manifest.manifest_id == prepared.manifest_id
    assert manifest.media_assets == prepared.media_assets
    assert manifest.destinations == prepared.destinations
    assert manifest.staging_directory == str(prepared.staging_directory)
    changed = replace(
        prepared, destinations=(replace(prepared.destinations[0], city="CityA"),)
    )
    assert manifest_sha256(manifest) != manifest_sha256(
        build_launch_manifest(
            changed, origin=ActionOrigin.AUTO_LAUNCH, idempotency_key=key, now=NOW
        )
    )


@pytest.mark.parametrize(
    "invalid_key",
    [
        str(uuid.uuid1()),
        str(uuid.uuid3(uuid.NAMESPACE_URL, "launch")),
        str(uuid.uuid5(uuid.NAMESPACE_URL, "launch")),
        str(uuid.uuid4()).upper(),
    ],
)
def test_launch_manifest_rejects_noncanonical_or_non_v4_key(invalid_key):
    with pytest.raises(ValueError, match="canonical UUID4"):
        build_launch_manifest(
            _prepared_launch(),
            origin=ActionOrigin.WEB,
            idempotency_key=invalid_key,
            now=NOW,
        )


def test_pause_factory_claims_fb_amo_cdp_and_rejects_payment_without_revenue() -> None:
    candidate = PauseCandidate(
        ad_id="ad-1",
        adset_id="adset-1",
        display_name="не попадает в manifest",
        reason_code="LOW_ROMI",
        decision_window=_window(),
        expected_before_status="ACTIVE",
        expected_after_status="PAUSED",
        spend=Decimal("459"),
        spend_currency="USD",
        leads=46,
        quals=9,
        payments=(_payment(),),
        revenue_lcy=Decimal("120000"),
        pre_inventory_sha256=SHA_A,
        sibling_active_ids=("ad-2",),
        replacement_ad_id=None,
    )
    manifest = build_pause_manifest(
        candidate,
        origin=ActionOrigin.AUTOPILOT_LIVE,
        idempotency_key=str(uuid.uuid4()),
        now=NOW,
    )

    assert manifest.kind is ActionKind.PAUSE
    assert {fact.source for fact in manifest.facts} == {
        SourceSystem.FACEBOOK,
        SourceSystem.AMO,
        SourceSystem.CDP_ERP,
    }
    payments = next(fact for fact in manifest.facts if fact.metric is Metric.PAYMENTS)
    assert payments.value == 1
    assert all("не попадает" not in fact.claim_id for fact in manifest.facts)

    with pytest.raises(ValueError, match="payments > 0"):
        build_pause_manifest(
            replace(candidate, revenue_lcy=Decimal("0")),
            origin=ActionOrigin.AUTOPILOT_LIVE,
            idempotency_key=str(uuid.uuid4()),
            now=NOW,
        )


def test_unpause_and_scale_factories_are_exact_and_batch_hash_is_bound() -> None:
    unpause = build_unpause_manifest(
        UnpauseCandidate("ad-1", "adset-1", "name", "PAUSED", "ACTIVE", SHA_A),
        origin=ActionOrigin.WEB,
        idempotency_key=str(uuid.uuid4()),
        now=NOW,
    )
    assert unpause.kind is ActionKind.UNPAUSE
    assert unpause.expected_after_status == "ACTIVE"
    assert unpause.pre_inventory_sha256 == SHA_A
    changed_unpause = replace(unpause, pre_inventory_sha256=SHA_B)
    assert manifest_sha256(unpause) != manifest_sha256(changed_unpause)

    scale_candidate = ScaleCandidate(
        adset_id="adset-1",
        expected_status="ACTIVE",
        current_budget=Decimal("100"),
        target_budget=Decimal("115"),
        currency="USD",
        facebook_window=_window(),
        outcome_window=_window(),
        candidate_ad_ids=("ad-1",),
        facts=_scale_facts(),
    )
    scale = build_scale_manifest(
        scale_candidate,
        origin=ActionOrigin.BUDGET_SCALER,
        idempotency_key=str(uuid.uuid4()),
        now=NOW,
    )
    assert scale.kind is ActionKind.SCALE
    assert scale.candidate_ad_ids == scale_candidate.candidate_ad_ids
    assert scale.facebook_window == scale_candidate.facebook_window
    assert scale.outcome_window == scale_candidate.outcome_window
    assert manifest_sha256(scale) != manifest_sha256(
        replace(scale, candidate_ad_ids=("ad-2",))
    )
    batch = build_action_batch(
        (scale,),
        correlation_id="scale-run",
        idempotency_key=str(uuid.uuid4()),
        now=NOW,
    )
    assert batch.subject_ids == ("adset:adset-1",)
    assert batch.manifest_sha256 == manifest_sha256(batch)


def test_delete_archive_and_dict_callbacks_are_unrepresentable() -> None:
    assert {kind.value for kind in ActionKind} == {
        "LAUNCH",
        "ASSET_RECOVERY",
        "PAUSE",
        "UNPAUSE",
        "SCALE",
    }
    for factory in (
        build_launch_manifest,
        build_pause_manifest,
        build_unpause_manifest,
        build_scale_manifest,
    ):
        assert "callback" not in inspect.signature(factory).parameters
        with pytest.raises((AttributeError, TypeError, ValueError)):
            factory(  # type: ignore[arg-type]
                {"kind": "DELETE"},
                origin=ActionOrigin.WEB,
                idempotency_key=str(uuid.uuid4()),
                now=NOW,
            )
