from __future__ import annotations

import stat
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from datetime import timedelta

from integrations.trello import TrelloCardSnapshot
from integrations import facebook
from services.action_manifests import build_launch_manifest
from services import creative_intelligence as ci
from services import launch_staging
from services.action_adapter_launch import _reserve_manifest_authorization
from services.launch_checker import LaunchCheckBlocked
from services.approval_checker_models import (
    ActionKind,
    ActionObservation,
    ActionOrigin,
    ActionResult,
    CreativeSpec,
    LaunchDestination,
    LaunchSourceInput,
    MediaAssetSpec,
    MediaType,
    PlacementRole,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
_ATTEMPT_HASH = "a1" * 32


def _attestation(account_id: str = "10001", resource_id: str = "adset-1"):
    """Аттестация одобренной попытки — без неё provider CREATE недостижим.

    У `integrations.facebook` больше нет своей HTTP-сессии: единственный CREATE
    идёт через `facebook_ads_mutation_transport.create_ad`, и тот принимает
    только аттестованную попытку, выданную после одобрения владельца.
    """
    from services.owner_action_models import ActionAttemptAttestation

    return ActionAttemptAttestation(
        attempt_id="attempt-launch-1",
        permit_id="permit-launch-1",
        proposal_id="proposal-launch-1",
        decision_id="decision-launch-1",
        claim_id="claim-launch-1",
        operation_kind="CREATE_AD",
        account_id=account_id,
        resource_id=resource_id,
        payload_sha256=_ATTEMPT_HASH,
        consumed_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )


def _snapshot() -> TrelloCardSnapshot:
    return TrelloCardSnapshot(
        card_id="card-1",
        board_id="board-1",
        list_id="ready-1",
        name="Exact card",
        description="Description",
        due=None,
        due_complete=False,
        closed=False,
        date_last_activity=NOW,
        labels=(),
        attachments=(
            {
                "id": "attachment-1",
                "name": "creative",
                "url": "https://drive.google.com/file/d/exact",
                "mimeType": "image/png",
                "bytes": 3,
                "date": None,
            },
        ),
        content_sha256="a" * 64,
    )


def _source() -> LaunchSourceInput:
    return LaunchSourceInput(
        card_id="card-1",
        campaign_type="leadgen",
        requested_cities=("CityB",),
        as_carousel=False,
        origin_reference="test",
    )


def _patch_resolution(monkeypatch, root, paths):
    monkeypatch.setattr(launch_staging, "REPORT_CHECKER_STAGING_ROOT", root)
    monkeypatch.setattr(launch_staging, "_config_sha256", lambda: "b" * 64)
    monkeypatch.setattr(
        launch_staging,
        "_resolve_launch_source",
        lambda _source: launch_staging._ResolvedLaunchSource(
            snapshot=_snapshot(),
            media={"type": "image", "paths": paths},
            attachment_id="attachment-1",
            body="Cafe\u0301\r\nline",
            adset_type="L1",
            product="PRODB",
        ),
    )

    def destinations(_source, _resolved, assets, body_path, body_sha256):
        from config import FB_ACCOUNT_ID

        creatives = tuple(
            CreativeSpec(
                creative_id=f"creative-{index}",
                order_index=index,
                ad_name=f"CityB | Exact card / {index}",
                media_asset_ids=(asset.asset_id,),
                body_staged_relative_path=body_path,
                body_sha256=body_sha256,
                product="PRODB",
                page_id="page-1",
                lead_form_id="form-1",
                call_to_action="LEARN_MORE",
                instagram_actor_id="NONE",
                title="Title",
                link_url="https://example.com",
                expected_configured_status="ACTIVE",
            )
            for index, asset in enumerate(assets)
        )
        return (
            LaunchDestination(
                city="CityB",
                account_id=str(FB_ACCOUNT_ID).removeprefix("act_"),
                adset_id="1001",
                adset_type="L1",
                current_daily_budget=Decimal("10"),
                currency="USD",
                capacity_available=10,
                hard_reserve_slots=1,
                creatives=creatives,
                duplicate_signature="c" * 64,
            ),
        )

    monkeypatch.setattr(launch_staging, "_destinations", destinations)


# ---------------------------------------------------------------------------
# Маршрутизация город→кабинет в staged destinations
# ---------------------------------------------------------------------------

def _media_asset() -> MediaAssetSpec:
    return MediaAssetSpec(
        asset_id="asset-1",
        order_index=0,
        media_type=MediaType.IMAGE,
        placement_group_id=None,
        placement_role=PlacementRole.DEFAULT,
        staged_relative_path="media/000-a.jpg",
        original_attachment_id="attachment-1",
        mime_type="image/jpeg",
        size_bytes=3,
        content_sha256="a" * 64,
    )


_CABINET_A = "152882611033373"
_CABINET_B = "29716040622546856"


def _routing_resolved(adset_type: str = "L1") -> launch_staging._ResolvedLaunchSource:
    return launch_staging._ResolvedLaunchSource(
        snapshot=_snapshot(),
        media={"type": "image", "paths": ["a.jpg"]},
        attachment_id="attachment-1",
        body="Текст",
        adset_type=adset_type,
        product="PRODB",
    )


def _patch_destination_provider(monkeypatch, adsets, accounts):
    import agent.adset_discovery as adset_discovery
    import agent.scheduler as scheduler

    # Карта роутинга — дефолтная: локальный settings.json не должен влиять
    # на тесты маршрутизации.
    monkeypatch.setattr(scheduler, "load_settings", lambda: {})
    monkeypatch.setattr(
        facebook, "_resolve_launch_adsets", lambda *args, **kwargs: list(adsets)
    )
    monkeypatch.setattr(
        facebook, "_prepare_launch_media", lambda media: ("image", ["a.jpg"], [], (), ())
    )
    monkeypatch.setattr(
        facebook,
        "get_adset_capacity",
        lambda adset_id: {"daily_budget": "10", "available": 49},
    )
    monkeypatch.setattr(
        facebook,
        "_planned_city_names",
        lambda city, *args: (f"{city} | Exact card",),
    )
    monkeypatch.setattr(
        adset_discovery, "get_adset_accounts_dict", lambda: dict(accounts)
    )


def test_destinations_route_each_city_to_its_cabinet(monkeypatch):
    """Оффлайн-staging берёт кабинет пары из discovery, не из константы."""
    source = LaunchSourceInput(
        card_id="card-1",
        campaign_type="leadgen",
        requested_cities=("CityB", "CityF"),
        as_carousel=False,
        origin_reference="test",
    )
    _patch_destination_provider(
        monkeypatch,
        adsets=(("CityB", "1001"), ("CityF", "2002")),
        accounts={
            "CityB": {"L1": _CABINET_A},
            "CityF": {"L1": f"act_{_CABINET_B}"},
        },
    )

    destinations = launch_staging._destinations(
        source, _routing_resolved("L1"), (_media_asset(),), "text/body.txt", "b" * 64
    )

    assert {item.city: item.account_id for item in destinations} == {
        "CityB": _CABINET_A,
        "CityF": _CABINET_B,
    }


def test_destinations_l2_wave_goes_to_cabinet_b(monkeypatch):
    """Главный кейс миграции: L2-волна уходит в cabinet_b, не в cabinet_a."""
    source = LaunchSourceInput(
        card_id="card-1",
        campaign_type="leadgen",
        requested_cities=("CityB", "CityA"),
        as_carousel=False,
        origin_reference="test",
    )
    _patch_destination_provider(
        monkeypatch,
        adsets=(("CityB", "1001"), ("CityA", "1002")),
        accounts={
            "CityB": {"L2": _CABINET_B, "L1": _CABINET_A},
            "CityA": {"L2": _CABINET_B, "L1": _CABINET_A},
        },
    )

    destinations = launch_staging._destinations(
        source, _routing_resolved("L2"), (_media_asset(),), "text/body.txt", "b" * 64
    )

    assert {item.account_id for item in destinations} == {_CABINET_B}


def test_destinations_prodb_card_uses_prodb_route_not_card_language(monkeypatch):
    """PRODB-карточка: кабинет пары — по ключу PRODB (cabinet_b), язык — только лид-форма."""
    source = LaunchSourceInput(
        card_id="card-1",
        campaign_type="leadgen_prodb",
        requested_cities=("CityB", "CityF"),
        as_carousel=False,
        origin_reference="test",
    )
    _patch_destination_provider(
        monkeypatch,
        adsets=(("CityB", "9001"), ("CityF", "9006")),
        accounts={
            "CityB": {"L2": _CABINET_B, "L1": _CABINET_A, "PRODB": _CABINET_B},
            "CityF": {"L2": _CABINET_B, "L1": _CABINET_B, "PRODB": _CABINET_B},
        },
    )

    destinations = launch_staging._destinations(
        source, _routing_resolved("L1"), (_media_asset(),), "text/body.txt", "b" * 64
    )

    assert {item.city: item.account_id for item in destinations} == {
        "CityB": _CABINET_B,
        "CityF": _CABINET_B,
    }
    # adset_type назначения остаётся ЯЗЫКОМ карточки (из него launcher берёт язык).
    assert {item.adset_type for item in destinations} == {"L1"}


def test_destinations_prodb_card_without_prodb_cabinet_fails_closed(monkeypatch):
    source = LaunchSourceInput(
        card_id="card-1",
        campaign_type="leadgen_prodb",
        requested_cities=("CityB",),
        as_carousel=False,
        origin_reference="test",
    )
    _patch_destination_provider(
        monkeypatch,
        adsets=(("CityB", "9001"),),
        accounts={"CityB": {"L2": _CABINET_B, "L1": _CABINET_A}},
    )

    with pytest.raises(launch_staging.LaunchStagingError, match="CITY_ACCOUNT_UNROUTED"):
        launch_staging._destinations(
            source, _routing_resolved("L1"), (_media_asset(),), "text/body.txt", "b" * 64
        )


def test_destinations_website_uses_mql_route_not_card_language(monkeypatch):
    """Website-карточка на L2 льётся в MQL-инвентарь cabinet_a, а не в L2."""
    source = LaunchSourceInput(
        card_id="card-1",
        campaign_type="website",
        requested_cities=("CityB",),
        as_carousel=False,
        origin_reference="test",
    )
    _patch_destination_provider(
        monkeypatch,
        adsets=(("CityB", "3003"),),
        accounts={"CityB": {"L2": _CABINET_B, "MQL": _CABINET_A}},
    )

    destinations = launch_staging._destinations(
        source, _routing_resolved("L2"), (_media_asset(),), "text/body.txt", "b" * 64
    )

    assert [item.account_id for item in destinations] == [_CABINET_A]


def test_destinations_fail_closed_for_unrouted_city(monkeypatch):
    """Город без кабинета в карте — отказ, а не молчаливый cabinet_a."""
    source = LaunchSourceInput(
        card_id="card-1",
        campaign_type="leadgen",
        requested_cities=("CityB", "CityF"),
        as_carousel=False,
        origin_reference="test",
    )
    _patch_destination_provider(
        monkeypatch,
        adsets=(("CityB", "1001"), ("CityF", "2002")),
        accounts={"CityB": {"L1": _CABINET_A}},
    )

    with pytest.raises(launch_staging.LaunchStagingError, match="CITY_ACCOUNT_UNROUTED"):
        launch_staging._destinations(
            source, _routing_resolved("L1"), (_media_asset(),), "text/body.txt", "b" * 64
        )


def test_destinations_fail_closed_for_unrouted_type(monkeypatch):
    """У CityF нет маршрута MQL — website-запуск туда невозможен."""
    source = LaunchSourceInput(
        card_id="card-1",
        campaign_type="website",
        requested_cities=("CityF",),
        as_carousel=False,
        origin_reference="test",
    )
    _patch_destination_provider(
        monkeypatch,
        adsets=(("CityF", "2002"),),
        accounts={"CityF": {"MQL": _CABINET_B}},
    )

    with pytest.raises(launch_staging.LaunchStagingError, match="CITY_ACCOUNT_UNROUTED"):
        launch_staging._destinations(
            source, _routing_resolved("L1"), (_media_asset(),), "text/body.txt", "b" * 64
        )


def test_destinations_reject_inventory_disagreeing_with_route_map(monkeypatch):
    """Инвентарь зовёт L2 CityB в cabinet_a (спящий адсет), карта — в cabinet_b."""
    source = LaunchSourceInput(
        card_id="card-1",
        campaign_type="leadgen",
        requested_cities=("CityB",),
        as_carousel=False,
        origin_reference="test",
    )
    _patch_destination_provider(
        monkeypatch,
        adsets=(("CityB", "1001"),),
        accounts={"CityB": {"L2": _CABINET_A}},
    )

    with pytest.raises(
        launch_staging.LaunchStagingError, match="CITY_ACCOUNT_ROUTE_MISMATCH"
    ):
        launch_staging._destinations(
            source, _routing_resolved("L2"), (_media_asset(),), "text/body.txt", "b" * 64
        )


def test_destinations_reject_excluded_city_even_when_explicitly_requested(monkeypatch):
    """null в settings — настоящий стоп-кран, а не «город вне списка всех»."""
    import agent.scheduler as scheduler

    source = LaunchSourceInput(
        card_id="card-1",
        campaign_type="leadgen",
        requested_cities=("CityB",),
        as_carousel=False,
        origin_reference="test",
    )
    _patch_destination_provider(
        monkeypatch,
        adsets=(("CityB", "1001"),),
        accounts={"CityB": {"L1": _CABINET_A}},
    )
    monkeypatch.setattr(
        scheduler,
        "load_settings",
        lambda: {"launch_routing": {"cities": {"CityB": None}}},
    )

    with pytest.raises(launch_staging.LaunchStagingError, match="CITY_ACCOUNT_UNROUTED"):
        launch_staging._destinations(
            source, _routing_resolved("L1"), (_media_asset(),), "text/body.txt", "b" * 64
        )


def test_stage_is_atomic_fsynced_and_preserves_actual_order(tmp_path, monkeypatch):
    first = tmp_path / "z.png"
    second = tmp_path / "a.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(first), str(second)])

    prepared = launch_staging.stage_launch(_source(), manifest_id="manifest-1")

    assert prepared.staging_directory == root / "manifest-1"
    assert prepared.staging_directory.is_dir()
    assert (prepared.staging_directory / "manifest.complete").is_file()
    assert [asset.order_index for asset in prepared.media_assets] == [0, 1]
    assert [
        (prepared.staging_directory / asset.staged_relative_path).read_bytes()
        for asset in prepared.media_assets
    ] == [b"second", b"first"]
    assert stat.S_IMODE(prepared.staging_directory.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(
            (prepared.staging_directory / asset.staged_relative_path).stat().st_mode
        )
        == 0o600
        for asset in prepared.media_assets
    )
    assert launch_staging.verify_staged_launch(prepared) is True
    assert not list(root.glob(".manifest-1.*"))


def test_verify_detects_media_and_text_drift(tmp_path, monkeypatch):
    source_file = tmp_path / "asset.png"
    source_file.write_bytes(b"media")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(source_file)])
    prepared = launch_staging.stage_launch(_source(), manifest_id="manifest-2")
    asset_path = prepared.staging_directory / prepared.media_assets[0].staged_relative_path
    asset_path.write_bytes(b"changed")

    assert launch_staging.verify_staged_launch(prepared) is False


def test_stage_rejects_symlink_source_and_cleans_temp(tmp_path, monkeypatch):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"media")
    linked = tmp_path / "linked.png"
    linked.symlink_to(outside)
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(linked)])

    with pytest.raises(launch_staging.LaunchStagingError, match="MEDIA_SOURCE_UNSAFE"):
        launch_staging.stage_launch(_source(), manifest_id="manifest-3")

    assert not (root / "manifest-3").exists()
    assert not list(root.glob(".manifest-3.*"))


def test_release_retains_unknown_and_removes_only_terminal(tmp_path, monkeypatch):
    source_file = tmp_path / "asset.png"
    source_file.write_bytes(b"media")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(source_file)])
    prepared = launch_staging.stage_launch(_source(), manifest_id="manifest-4")

    launch_staging.release_staging(
        prepared.manifest_id,
        terminal_result=ActionResult.UNKNOWN,
    )
    assert prepared.staging_directory.exists()

    launch_staging.release_staging(
        prepared.manifest_id,
        terminal_result=ActionResult.CONFIRMED,
    )
    assert not prepared.staging_directory.exists()


def test_existing_manifest_is_never_overwritten(tmp_path, monkeypatch):
    source_file = tmp_path / "asset.png"
    source_file.write_bytes(b"media")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(source_file)])
    launch_staging.stage_launch(_source(), manifest_id="manifest-5")

    with pytest.raises(launch_staging.LaunchStagingError, match="STAGING_ALREADY_EXISTS"):
        launch_staging.stage_launch(_source(), manifest_id="manifest-5")


def test_private_facebook_executor_without_db_proof_has_zero_provider_mutations(
    tmp_path,
    monkeypatch,
):
    source_file = tmp_path / "asset.png"
    source_file.write_bytes(b"media")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(source_file)])
    prepared = launch_staging.stage_launch(_source(), manifest_id="manifest-6")
    manifest = build_launch_manifest(
        prepared,
        origin=ActionOrigin.WEB,
        idempotency_key=str(uuid.uuid4()),
        now=prepared.staged_at,
    )
    uploads: list[str] = []
    monkeypatch.setattr(
        facebook,
        "upload_image",
        lambda _path: uploads.append("upload") or "image-hash",
    )
    monkeypatch.setattr(facebook, "get_fb_token", lambda: "test-token")
    provider_create = MagicMock()
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.create_ad", provider_create
    )

    # Даже с валидной аттестацией попытки без DB-backed authorization executor
    # обязан отказать до любой загрузки медиа и до единственного CREATE.
    with pytest.raises(LaunchCheckBlocked) as exc_info:
        facebook._execute_launch_manifest_unchecked(
            manifest,
            attempt=_attestation(),
        )

    assert exc_info.value.code == "AUTHORIZATION_REQUIRED"
    assert uploads == []
    provider_create.assert_not_called()


def test_private_facebook_executor_uses_db_claim_and_confirm(
    tmp_path,
    monkeypatch,
):
    source_file = tmp_path / "asset.png"
    source_file.write_bytes(b"media")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(source_file)])
    prepared = launch_staging.stage_launch(_source(), manifest_id="manifest-6-proof")
    manifest = build_launch_manifest(
        prepared,
        origin=ActionOrigin.WEB,
        idempotency_key=str(uuid.uuid4()),
        now=prepared.staged_at,
    )
    ci.DB_PATH = None
    ci.init_kb(str(tmp_path / "staged-provider.db"))
    proof = _reserve_manifest_authorization(manifest, prepared.staged_at)
    posts: list[dict] = []
    monkeypatch.setattr(facebook, "_provider_now", lambda: prepared.staged_at)
    monkeypatch.setattr(facebook, "upload_image", lambda _path: "image-hash")
    monkeypatch.setattr(facebook, "get_fb_token", lambda: "test-token")
    monkeypatch.setattr(
        facebook,
        "fetch_complete_account_ad_inventory",
        lambda _kind, _account_id: [],
    )
    monkeypatch.setattr(facebook, "_get_launch_hard_reserve_slots", lambda: 1)
    monkeypatch.setattr(
        facebook,
        "_get_other_launch_reserved_slots",
        lambda *_args: 0,
    )

    attempt = _attestation(
        account_id=manifest.destinations[0].account_id.removeprefix("act_"),
        resource_id=manifest.destinations[0].adset_id,
    )

    def create_attested(passed_attempt, **kwargs):
        # Транспорт получает ИМЕННО ту аттестацию, с которой вызван executor:
        # без неё одобрение владельца нельзя связать с конкретной мутацией.
        assert passed_attempt is attempt
        posts.append(kwargs)
        return "7001"

    def get(_url, *, params):
        creative = posts[-1]["creative"]
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "id": "7001",
                "name": posts[-1]["name"],
                "adset_id": posts[-1]["adset_id"],
                "creative": {"id": "8001", **creative},
            },
        )

    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.create_ad", create_attested
    )
    monkeypatch.setattr(facebook, "_throttled_get", get)

    try:
        created_ids = facebook._execute_launch_manifest_unchecked(
            manifest,
            attempt=attempt,
            authorization=proof,
        )
    finally:
        ci.DB_PATH = None

    assert created_ids == ("7001",)
    assert [item["name"] for item in posts] == [
        manifest.destinations[0].creatives[0].ad_name
    ]
    assert posts[0]["adset_id"] == manifest.destinations[0].adset_id
    assert posts[0]["payload_sha256"] == attempt.payload_sha256


def test_staged_media_replacement_after_verify_blocks_before_upload(
    tmp_path,
    monkeypatch,
):
    source_file = tmp_path / "race.png"
    source_file.write_bytes(b"verified-media")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(source_file)])
    prepared = launch_staging.stage_launch(_source(), manifest_id="manifest-race")
    manifest = build_launch_manifest(
        prepared,
        origin=ActionOrigin.WEB,
        idempotency_key=str(uuid.uuid4()),
        now=prepared.staged_at,
    )
    ci.DB_PATH = None
    ci.init_kb(str(tmp_path / "race-provider.db"))
    proof = _reserve_manifest_authorization(manifest, prepared.staged_at)
    real_verify = facebook._verify_staged_manifest_files

    def verify_then_replace(current_manifest):
        directory = real_verify(current_manifest)
        replacement = tmp_path / "attacker-replacement.png"
        replacement.write_bytes(b"different-media")
        target = directory / current_manifest.media_assets[0].staged_relative_path
        __import__("os").replace(replacement, target)
        return directory

    upload = MagicMock()
    provider_create = MagicMock()
    monkeypatch.setattr(facebook, "_verify_staged_manifest_files", verify_then_replace)
    monkeypatch.setattr(facebook, "upload_image", upload)
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.create_ad", provider_create
    )

    try:
        with pytest.raises(LaunchCheckBlocked) as error:
            facebook._execute_launch_manifest_unchecked(
                manifest,
                attempt=_attestation(),
                authorization=proof,
            )
    finally:
        ci.DB_PATH = None

    assert error.value.code == "LAUNCH_INPUT_DRIFT"
    upload.assert_not_called()
    provider_create.assert_not_called()


def test_facebook_post_observation_requires_full_unique_created_ids(
    tmp_path,
    monkeypatch,
):
    source_file = tmp_path / "asset.png"
    source_file.write_bytes(b"media")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(source_file)])
    prepared = launch_staging.stage_launch(_source(), manifest_id="manifest-7")
    manifest = build_launch_manifest(
        prepared,
        origin=ActionOrigin.WEB,
        idempotency_key=str(uuid.uuid4()),
        now=prepared.staged_at,
    )

    with pytest.raises(Exception, match="неполный"):
        facebook._observe_launch_manifest_postcondition(manifest, (), NOW)

    expected = ActionObservation(
        observed_at=NOW,
        digest="d" * 64,
        target_state="ACTIVE|PENDING_REVIEW",
        subject_ids=("ad-1",),
        unrelated_state_digest="e" * 64,
    )
    monkeypatch.setattr(
        "services.approval_source_facebook.read_action_postcondition",
        lambda item, created_ids, now: expected,
    )
    inventory_calls: list[tuple[str, tuple[str, ...] | None]] = []

    def _fake_inventory(account_id, adset_ids=None):
        inventory_calls.append((str(account_id), tuple(adset_ids) if adset_ids else None))
        return [
            {
                "id": "ad-1",
                "name": manifest.destinations[0].creatives[0].ad_name,
                "adset_id": manifest.destinations[0].adset_id,
                "status": "ACTIVE",
                "effective_status": "PENDING_REVIEW",
                "creative": {"id": "creative-live-1"},
            }
        ]

    monkeypatch.setattr(
        facebook,
        "fetch_ads_for_launch_reconciliation",
        _fake_inventory,
    )

    observed = facebook._observe_launch_manifest_postcondition(
        manifest,
        ("ad-1",),
        NOW,
    )

    assert observed == expected
    assert manifest.kind is ActionKind.LAUNCH
    # Регрессия: post-read без фильтра по адсетам читал весь
    # кабинет и на cabinet_a упирался в потолок страниц — карточки оставались 1 из N.
    assert inventory_calls == [
        (
            manifest.destinations[0].account_id.removeprefix("act_"),
            tuple(sorted({str(d.adset_id) for d in manifest.destinations})),
        )
    ]


# ---------------------------------------------------------------------------
# Exact claim «база:N» против полного staging (регрессия LAUNCH_STAGING_DRIFT,
# все запуски горели, потому что verify требовал per-claim id)
# ---------------------------------------------------------------------------

def _claim_prepared(prepared, ordinal: int = 0):
    """Режет PreparedLaunch как _owner_launch_plan: «база:N», один creative."""
    from dataclasses import replace

    destination = prepared.destinations[0]
    return replace(
        prepared,
        manifest_id=f"{prepared.manifest_id}:{ordinal}",
        destinations=(
            replace(destination, creatives=(destination.creatives[ordinal],)),
        ),
    )


def test_verify_accepts_exact_claim_subset(tmp_path, monkeypatch):
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(first), str(second)])
    prepared = launch_staging.stage_launch(_source(), manifest_id="manifest-claim")

    assert launch_staging.verify_staged_launch(prepared) is True
    for ordinal in range(len(prepared.destinations[0].creatives)):
        assert launch_staging.verify_staged_launch(
            _claim_prepared(prepared, ordinal)
        ) is True, f"claim {ordinal} обязан проходить verify"


def test_verify_rejects_claim_with_foreign_creative(tmp_path, monkeypatch):
    from dataclasses import replace

    source_file = tmp_path / "asset.png"
    source_file.write_bytes(b"media")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(source_file)])
    prepared = launch_staging.stage_launch(_source(), manifest_id="manifest-claim2")
    claim = _claim_prepared(prepared, 0)

    tampered_creative = replace(
        claim.destinations[0].creatives[0], ad_name="CityB | Чужое имя"
    )
    tampered = replace(
        claim,
        destinations=(
            replace(claim.destinations[0], creatives=(tampered_creative,)),
        ),
    )
    assert launch_staging.verify_staged_launch(tampered) is False


def test_verify_rejects_claim_with_foreign_adset(tmp_path, monkeypatch):
    from dataclasses import replace

    source_file = tmp_path / "asset.png"
    source_file.write_bytes(b"media")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(source_file)])
    prepared = launch_staging.stage_launch(_source(), manifest_id="manifest-claim3")
    claim = _claim_prepared(prepared, 0)

    foreign = replace(
        claim,
        destinations=(replace(claim.destinations[0], adset_id="9999"),),
    )
    assert launch_staging.verify_staged_launch(foreign) is False


def test_base_manifest_verify_stays_byte_strict(tmp_path, monkeypatch):
    """Полный манифест по-прежнему сверяется байт-в-байт."""
    source_file = tmp_path / "asset.png"
    source_file.write_bytes(b"media")
    root = tmp_path / "stable"
    _patch_resolution(monkeypatch, root, [str(source_file)])
    prepared = launch_staging.stage_launch(_source(), manifest_id="manifest-claim4")
    marker = prepared.staging_directory / "manifest.complete"
    marker.write_bytes(marker.read_bytes() + b" ")

    assert launch_staging.verify_staged_launch(prepared) is False
