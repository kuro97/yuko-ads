"""Offline-проверки обязательного proof на Facebook CREATE-границе."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from integrations import facebook
from services import creative_intelligence as ci
from services import launch_repository
from services.approval_checker_models import (
    ActionKind,
    ActionOrigin,
    CreativeSpec,
    LaunchDestination,
    LaunchManifest,
    MediaAssetSpec,
    MediaType,
    PlacementRole,
    TrelloPrecondition,
)
from services.launch_checker import LaunchCheckBlocked, ProviderLaunchAuthorization
from services.owner_action_models import ActionAttemptAttestation
from integrations.facebook_ads_mutation_transport import MutationOutcomeUnknown


NOW = datetime(2026, 7, 22, 7, 0, tzinfo=timezone.utc)
PAYLOAD_SHA256 = "f" * 64


@pytest.fixture(autouse=True)
def isolated_launch_db(tmp_path, monkeypatch):
    """Provider tests никогда не используют production decisions.db или сеть."""
    ci.DB_PATH = None
    db_path = str(tmp_path / "provider-launch.db")
    ci.init_kb(db_path)
    monkeypatch.setattr(facebook, "_provider_now", lambda: NOW)
    monkeypatch.setattr(
        facebook,
        "fetch_complete_account_ad_inventory",
        lambda _account_kind, _account_id: [],
    )
    monkeypatch.setattr(
        facebook,
        "get_adset_capacity",
        lambda _adset_id: {
            "ad_count": 0,
            "max_ads": 50,
            "available": 50,
            "stale_ads": [],
        },
    )
    monkeypatch.setattr(facebook, "_get_launch_hard_reserve_slots", lambda: 1)
    monkeypatch.setattr(
        facebook,
        "_get_other_launch_reserved_slots",
        lambda *_args: 0,
    )
    yield db_path
    ci.DB_PATH = None


@pytest.fixture
def image_media(tmp_path) -> dict:
    path = tmp_path / "asset.jpg"
    path.write_bytes(b"exact-provider-media")
    return {"type": "image", "paths": [str(path)]}


def _proof(auth_id: str = "auth-provider", secret: str = "provider-secret"):
    return ProviderLaunchAuthorization(auth_id=auth_id, secret=secret)


def _reserve(
    media_sha256: str,
    *,
    proof: ProviderLaunchAuthorization | None = None,
    account_id: str = "123",
    adset_id: str = "1001",
    card_name: str = "Свежая карточка",
    ad_name: str = "CityA | Свежая карточка [PRODB]",
    now: datetime = NOW,
) -> ProviderLaunchAuthorization:
    authorization = proof or _proof()
    target = SimpleNamespace(
        city="CityA",
        ordinal=0,
        account_kind="offline",
        account_id=account_id,
        adset_id=adset_id,
        reserved_slots=1,
    )
    plan = SimpleNamespace(
        check_id=f"check-{authorization.auth_id}",
        card_id="card-provider",
        card_name=card_name,
        request=SimpleNamespace(
            source="MANUAL",
            campaign_type="leadgen",
            actor="api-key:sha256:actor",
            override_topic_veto=False,
            override_reason=None,
        ),
        media_sha256=media_sha256,
        targets=(target,),
        expected_names_by_city={"CityA": (ad_name,)},
        authorization=authorization,
    )
    launch_repository.reserve_authorization(
        plan,
        hashlib.sha256(authorization.secret.encode()).hexdigest(),
        now,
    )
    return authorization


def _patch_preupload_boundaries(monkeypatch, *, account_id: str = "123") -> list[str]:
    mutations: list[str] = []
    monkeypatch.setattr(
        "agent.adset_discovery.get_adsets_dict",
        lambda: {"CityA": {"L1": "1001"}},
    )
    monkeypatch.setattr(facebook, "get_fb_account_id", lambda: account_id)
    monkeypatch.setattr(
        facebook,
        "fetch_complete_account_ad_inventory",
        lambda _account_kind, _account_id: [],
    )
    monkeypatch.setattr(
        facebook,
        "upload_image",
        lambda _path: mutations.append("upload") or "image-hash",
    )
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.create_ad",
        lambda *_args, **_kwargs: mutations.append("post") or "777",
    )
    return mutations


def _owner_attempt(
    *,
    account_id: str = "123",
    resource_id: str = "1001",
) -> ActionAttemptAttestation:
    return ActionAttemptAttestation(
        attempt_id=str(uuid.uuid4()),
        permit_id=str(uuid.uuid4()),
        proposal_id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        claim_id=str(uuid.uuid4()),
        operation_kind="CREATE_AD",
        account_id=account_id,
        resource_id=resource_id,
        payload_sha256=PAYLOAD_SHA256,
        consumed_at=NOW,
    )


def _launch(media: dict, authorization, **changes):
    values = {
        "card_name": "Свежая карточка",
        "adset_type": "L1",
        "media": media,
        "body": "Текст",
        "campaign_type": "leadgen",
        "cities": ["CityA"],
        "product": "PRODB",
        "authorization": authorization,
    }
    values.update(changes)
    if not isinstance(authorization, ProviderLaunchAuthorization):
        return facebook.launch_creative(**values)
    prepared = facebook._prepare_launch_media(media)
    media_sha256 = facebook._launch_media_sha256_from_evidence(
        prepared[0],
        prepared[4],
    )
    validation = facebook.validate_authorization_media(
        authorization,
        media_sha256,
        repository=launch_repository,
        now=facebook._provider_now(),
    )
    adsets = facebook._resolve_launch_adsets(
        values["campaign_type"], values["adset_type"], values["cities"]
    )
    source_path = Path(media["paths"][0])
    body_path = source_path.parent / "provider-body.txt"
    body_path.write_text(values["body"], encoding="utf-8")
    asset = MediaAssetSpec(
        asset_id="asset-provider",
        order_index=0,
        media_type=MediaType.IMAGE,
        placement_group_id=None,
        placement_role=PlacementRole.DEFAULT,
        staged_relative_path=source_path.name,
        original_attachment_id="attachment-provider",
        mime_type="image/jpeg",
        size_bytes=source_path.stat().st_size,
        content_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
    )
    body_sha256 = hashlib.sha256(values["body"].encode()).hexdigest()
    destinations = tuple(
        LaunchDestination(
            city=city,
            account_id=str(validation.account_id),
            adset_id=str(adset_id),
            adset_type=values["adset_type"],
            current_daily_budget=Decimal("45"),
            currency="USD",
            capacity_available=50,
            hard_reserve_slots=1,
            creatives=tuple(
                CreativeSpec(
                    creative_id=f"creative-{ordinal}-{index}",
                    order_index=index,
                    ad_name=name,
                    media_asset_ids=(asset.asset_id,),
                    body_staged_relative_path=body_path.name,
                    body_sha256=body_sha256,
                    product=values.get("product") or "ОБЩАЯ",
                    page_id="page-provider",
                    lead_form_id="form-provider",
                    call_to_action="LEARN_MORE",
                    instagram_actor_id="NONE",
                    title="Title",
                    link_url="https://example.com",
                    expected_configured_status="ACTIVE",
                )
                for index, name in enumerate(
                    facebook._planned_city_names(
                        city,
                        values["card_name"],
                        values.get("product"),
                        prepared,
                    )
                )
            ),
            duplicate_signature="d" * 64,
        )
        for ordinal, (city, adset_id) in enumerate(adsets)
    )
    canonical_key = str(uuid.uuid4())
    manifest = LaunchManifest(
        kind=ActionKind.LAUNCH,
        manifest_id=f"provider-{uuid.uuid4().hex}",
        origin=ActionOrigin.WEB,
        idempotency_key=canonical_key,
        prepared_at=NOW,
        config_version_sha256="c" * 64,
        staging_root=str(source_path.parent.parent),
        staging_directory=str(source_path.parent),
        trello=TrelloPrecondition(
            card_id="card-provider",
            board_id="board-provider",
            ready_list_id="ready-provider",
            expected_list_id="ready-provider",
            expected_due_complete=False,
            expected_closed=False,
            date_last_activity=NOW,
            attachment_ids=("attachment-provider",),
            attachment_manifest_sha256="a" * 64,
            labels_sha256="a" * 64,
            card_content_sha256="a" * 64,
        ),
        card_name_sha256=hashlib.sha256(values["card_name"].encode()).hexdigest(),
        media_manifest_sha256=media_sha256,
        media_assets=(asset,),
        campaign_type=values["campaign_type"],
        destinations=destinations,
    )
    with patch.object(
        facebook, "_verify_staged_manifest_files", return_value=source_path.parent
    ), patch("config.FB_ACCOUNT_ID", str(validation.account_id)):
        return facebook._execute_launch_manifest_unchecked(
            manifest,
            attempt=_owner_attempt(account_id=str(validation.account_id)),
            authorization=authorization,
        )


def _ad_row(db_path: str):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT phase, claim_id, created_ad_id FROM launch_authorization_ads"
        ).fetchone()
    finally:
        connection.close()


def test_direct_low_level_create_without_bound_context_is_blocked(monkeypatch):
    typed_create = MagicMock()
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.create_ad",
        typed_create,
    )

    with pytest.raises(LaunchCheckBlocked) as exc_info:
        facebook.create_image_ad(
            "CityA | Свежая карточка [PRODB]",
            "1001",
            "L1",
            "image-hash",
            "Текст",
        )

    assert exc_info.value.code == "AUTHORIZATION_REQUIRED"
    typed_create.assert_not_called()


@pytest.mark.parametrize("authorization", (None, object()), ids=("observe", "wrong-type"))
def test_launch_creative_without_server_proof_has_zero_mutations(
    image_media,
    monkeypatch,
    authorization,
):
    mutations = _patch_preupload_boundaries(monkeypatch)

    with pytest.raises(LaunchCheckBlocked):
        _launch(image_media, authorization)

    assert mutations == []


def test_forged_proof_is_blocked_before_upload(image_media, monkeypatch):
    media_sha256 = facebook.calculate_launch_media_sha256(image_media)
    proof = _reserve(media_sha256)
    forged = ProviderLaunchAuthorization(proof.auth_id, "forged-secret")
    mutations = _patch_preupload_boundaries(monkeypatch)

    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _launch(image_media, forged)

    assert exc_info.value.code == "INVALID_AUTHORIZATION"
    assert mutations == []


def test_expired_proof_is_blocked_before_upload(image_media, monkeypatch):
    media_sha256 = facebook.calculate_launch_media_sha256(image_media)
    proof = _reserve(media_sha256, now=NOW - timedelta(minutes=31))
    mutations = _patch_preupload_boundaries(monkeypatch)

    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _launch(image_media, proof)

    assert exc_info.value.code == "AUTHORIZATION_EXPIRED"
    assert mutations == []


def test_media_drift_is_blocked_before_upload(image_media, monkeypatch, tmp_path):
    proof = _reserve(facebook.calculate_launch_media_sha256(image_media))
    image_media["paths"][0] = str(tmp_path / "changed.jpg")
    with open(image_media["paths"][0], "wb") as changed:
        changed.write(b"changed-provider-media")
    mutations = _patch_preupload_boundaries(monkeypatch)

    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _launch(image_media, proof)

    assert exc_info.value.code == "MEDIA_DRIFT"
    assert mutations == []


def test_account_drift_is_blocked_before_upload(image_media, monkeypatch):
    proof = _reserve(
        facebook.calculate_launch_media_sha256(image_media),
        account_id="999",
    )
    mutations = _patch_preupload_boundaries(monkeypatch, account_id="123")

    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _launch(image_media, proof)

    assert exc_info.value.code == "PROVIDER_SCOPE_DRIFT"
    assert mutations == []


@pytest.mark.parametrize(
    "reserve_changes,launch_changes",
    (
        ({"adset_id": "9999"}, {}),
        (
            {"card_name": "Другая карточка", "ad_name": "CityA | Другая карточка [PRODB]"},
            {},
        ),
    ),
    ids=("adset-drift", "name-drift"),
)
def test_target_scope_drift_is_blocked_before_upload(
    image_media,
    monkeypatch,
    reserve_changes,
    launch_changes,
):
    proof = _reserve(
        facebook.calculate_launch_media_sha256(image_media),
        **reserve_changes,
    )
    mutations = _patch_preupload_boundaries(monkeypatch)

    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _launch(image_media, proof, **launch_changes)

    assert exc_info.value.code == "PROVIDER_SCOPE_DRIFT"
    assert mutations == []


def test_launch_creative_with_valid_proof_still_has_zero_mutations(
    image_media,
    monkeypatch,
):
    media_sha256 = facebook.calculate_launch_media_sha256(image_media)
    proof = _reserve(media_sha256)
    mutations = _patch_preupload_boundaries(monkeypatch)
    with pytest.raises(LaunchCheckBlocked) as exc_info:
        facebook.launch_creative(
            card_name="Свежая карточка",
            adset_type="L1",
            media=image_media,
            body="Текст",
            cities=["CityA"],
            product="PRODB",
            authorization=proof,
        )

    assert exc_info.value.code == "LEGACY_LAUNCH_BYPASS_FORBIDDEN"
    assert mutations == []


def test_external_duplicate_after_preflight_blocks_ads_post(
    image_media,
    monkeypatch,
):
    proof = _reserve(facebook.calculate_launch_media_sha256(image_media))
    mutations = _patch_preupload_boundaries(monkeypatch)
    reads = 0

    def inventory(_account_kind, _account_id):
        nonlocal reads
        reads += 1
        if reads == 1:
            return []
        return [
            {
                "id": "external-ad",
                "name": "CityA | Свежая карточка / external [PRODB]",
                "adset_id": "1001",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
            }
        ]

    monkeypatch.setattr(facebook, "fetch_complete_account_ad_inventory", inventory)
    monkeypatch.setattr(
        "services.adset_pause_guard.adset_mutation_lock",
        lambda _adset_id: nullcontext(),
    )

    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _launch(image_media, proof)

    assert exc_info.value.code == "DUPLICATE_LIVE"
    assert mutations == ["upload"]


def test_capacity_recheck_excludes_current_auth_and_counts_other_reservations(
    image_media,
    monkeypatch,
):
    proof = _reserve(facebook.calculate_launch_media_sha256(image_media))
    mutations = _patch_preupload_boundaries(monkeypatch)
    rows = [
        {
            "id": f"existing-{index}",
            "name": f"Other {index}",
            "adset_id": "1001",
            "status": "PAUSED",
            "effective_status": "PAUSED",
        }
        for index in range(47)
    ]
    monkeypatch.setattr(
        facebook,
        "fetch_complete_account_ad_inventory",
        lambda _kind, _account_id: rows,
    )
    seen_excludes: list[str | None] = []

    def reserved(_adset_id, _now, *, exclude_auth_id=None):
        seen_excludes.append(exclude_auth_id)
        return 2

    monkeypatch.setattr(launch_repository, "get_reserved_slots", reserved)
    monkeypatch.setattr(
        "services.adset_pause_guard.adset_mutation_lock",
        lambda _adset_id: nullcontext(),
    )

    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _launch(image_media, proof)

    assert exc_info.value.code == "CAPACITY_BLOCKED"
    assert seen_excludes == [proof.auth_id]
    assert mutations == []


def test_city_renewal_after_ttl_blocks_before_ads_post(
    isolated_launch_db,
    image_media,
    monkeypatch,
):
    proof = _reserve(facebook.calculate_launch_media_sha256(image_media))
    current_now = [NOW]
    mutations = _patch_preupload_boundaries(monkeypatch)
    monkeypatch.setattr(facebook, "_provider_now", lambda: current_now[0])

    def upload(_path):
        mutations.append("upload")
        current_now[0] = NOW + timedelta(minutes=31)
        return "image-hash"

    monkeypatch.setattr(facebook, "upload_image", upload)
    monkeypatch.setattr(
        "services.adset_pause_guard.adset_mutation_lock",
        lambda _adset_id: nullcontext(),
    )

    with pytest.raises(LaunchCheckBlocked):
        _launch(image_media, proof)

    assert mutations == ["upload"]
    row = _ad_row(isolated_launch_db)
    # Renew fail-closed: истёкший target освобождён без CREATE claim.
    assert row["phase"] == "RELEASED"
    assert row["claim_id"] is None


def test_post_ad_claims_then_posts_then_confirms(
    isolated_launch_db,
    image_media,
    monkeypatch,
):
    media_sha256 = facebook.calculate_launch_media_sha256(image_media)
    proof = _reserve(media_sha256)
    events: list[str] = []
    real_claim = launch_repository.claim_provider_create
    real_confirm = launch_repository.record_provider_create_success

    def claim(*args, **kwargs):
        events.append("claim")
        return real_claim(*args, **kwargs)

    def confirm(*args, **kwargs):
        events.append("confirm")
        return real_confirm(*args, **kwargs)

    def post(*_args, **_kwargs):
        events.append("post")
        return "777"

    monkeypatch.setattr(launch_repository, "claim_provider_create", claim)
    monkeypatch.setattr(launch_repository, "record_provider_create_success", confirm)
    monkeypatch.setattr(facebook, "get_fb_account_id", lambda: "123")
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.create_ad",
        post,
    )
    monkeypatch.setattr(
        facebook,
        "_throttled_get",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=200,
            json=lambda: {
                "id": "777",
                "name": "CityA | Свежая карточка [PRODB]",
                "adset_id": "1001",
                "creative": {"id": "creative-1"},
            },
        ),
    )

    with facebook._bind_provider_launch_context(
        proof,
        attempt=_owner_attempt(),
        media_sha256=media_sha256,
        account_kind="offline",
        account_id="123",
    ):
        ad_id = facebook._post_ad(
            "CityA | Свежая карточка [PRODB]",
            "1001",
            {"creative_id": "creative-1"},
        )

    assert ad_id == "777"
    assert events == ["claim", "post", "confirm"]
    row = _ad_row(isolated_launch_db)
    assert (row["phase"], row["created_ad_id"]) == ("CREATED", "777")
    connection = sqlite3.connect(isolated_launch_db)
    try:
        binding = connection.execute(
            """SELECT phase,ad_id,creative_id,expected_fingerprint,verified_fingerprint
               FROM launch_provider_ad_bindings"""
        ).fetchone()
    finally:
        connection.close()
    assert binding[:3] == ("VERIFIED", "777", "creative-1")
    assert binding[3] == binding[4]


def test_graph_failure_leaves_create_started_without_false_success(
    isolated_launch_db,
    image_media,
    monkeypatch,
):
    media_sha256 = facebook.calculate_launch_media_sha256(image_media)
    proof = _reserve(media_sha256)
    monkeypatch.setattr(facebook, "get_fb_account_id", lambda: "123")
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.create_ad",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MutationOutcomeUnknown("provider outcome unknown")
        ),
    )

    with (
        facebook._bind_provider_launch_context(
            proof,
            attempt=_owner_attempt(),
            media_sha256=media_sha256,
            account_kind="offline",
            account_id="123",
        ),
        pytest.raises(MutationOutcomeUnknown, match="provider outcome unknown"),
    ):
        facebook._post_ad(
            "CityA | Свежая карточка [PRODB]",
            "1001",
            {"creative_id": "creative-1"},
        )

    row = _ad_row(isolated_launch_db)
    assert row["phase"] == "CREATE_STARTED"
    assert row["claim_id"]
    assert row["created_ad_id"] is None
