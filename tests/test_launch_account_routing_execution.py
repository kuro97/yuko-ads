"""Маршрутизация город→кабинет в МЕДИА и ИСПОЛНЕНИИ конвейера запуска.

Покрывает исполнительную половину мультикабинетного запуска (CityF
переехала в кабинет «ACME cabinet_b»):

- fb_token_provider: thread-контекст "offline:<account_id>" — тот же оффлайн
  FB_TOKEN, но account_id маршрутизированного кабинета; незарегистрированный
  кабинет отвергается (fail-closed);
- integrations.facebook: выбор контекста кабинета для executor/post-read
  (_manifest_account_context_name) и вход asset-recovery ретрая в кабинет цели;
- services.launch_recovery: оффлайн-инвентарь по ВСЕМ кабинетам карты
  и кабинет города в _resolve_city_scope со сверкой по карте роутинга.
"""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from datetime import datetime, timezone

import pytest

from integrations import facebook
from services import launch_recovery as recovery
from services.approval_checker_models import ActionKind, ActionOrigin, AssetRecoveryManifest
from services.fb_token_provider import (
    fb_account,
    get_fb_account_id,
    get_fb_token,
    offline_account_context,
    set_active_account,
)
from services.launch_checker import LaunchCheckBlocked
from services.owner_action_models import ActionAttemptAttestation


NOW = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
CABINET_A = "152882611033373"
CABINET_B = "29716040622546856"
ONLINE = "555000111"


@pytest.fixture
def routed_registry(monkeypatch):
    """Стабильный реестр кабинетов: cabinet_a дефолт, cabinet_b в карте роутинга.

    Карта повторяет расщепление кабинетов: L2 CityA уехал в cabinet_b,
    L1 и MQL остались в cabinet_a, CityF целиком в cabinet_b;
    PRODB-адсеты (тип PRODB) обоих городов — в cabinet_b.
    """
    monkeypatch.setattr("config.FB_ACCOUNT_ID", CABINET_A)
    monkeypatch.setattr("config.FB_ACCOUNT_ID_ONLINE", ONLINE)
    monkeypatch.setattr(
        "services.launch_routing.get_route_table",
        lambda: {
            ("CityA", "L2"): CABINET_B,
            ("CityA", "L1"): CABINET_A,
            ("CityA", "MQL"): CABINET_A,
            ("CityA", "PRODB"): CABINET_B,
            ("CityF", "L2"): CABINET_B,
            ("CityF", "L1"): CABINET_B,
            ("CityF", "PRODB"): CABINET_B,
        },
    )


# ---------------------------------------------------------------------------
# fb_token_provider: маршрутизированный оффлайн-контекст
# ---------------------------------------------------------------------------


def test_offline_account_context_default_is_plain_offline(routed_registry):
    assert offline_account_context(CABINET_A) is None
    assert offline_account_context(f"act_{CABINET_A}") is None


def test_offline_account_context_routed_cabinet(routed_registry):
    assert offline_account_context(CABINET_B) == f"offline:{CABINET_B}"
    assert offline_account_context(f"act_{CABINET_B}") == f"offline:{CABINET_B}"


def test_offline_account_context_unknown_cabinet_fails_closed(routed_registry):
    with pytest.raises(RuntimeError, match="не зарегистрирован"):
        offline_account_context("999999999")
    with pytest.raises(RuntimeError, match="невалидный"):
        offline_account_context("")
    with pytest.raises(RuntimeError, match="невалидный"):
        offline_account_context("act_abc")


def test_routed_context_returns_routed_account_id(routed_registry):
    with fb_account(f"offline:{CABINET_B}"):
        assert get_fb_account_id() == CABINET_B


def test_routed_context_uses_offline_token(routed_registry, monkeypatch):
    """Маршрутизированный кабинет живёт на ОБЫЧНОМ оффлайн токене."""
    monkeypatch.setattr("config.FB_TOKEN", "offline-token-shared")
    monkeypatch.setattr(
        "services.fb_credentials.get_active_token", lambda: None
    )
    with fb_account(f"offline:{CABINET_B}"):
        assert get_fb_token() == "offline-token-shared"


def test_forged_routed_context_fails_closed(routed_registry):
    """Подделанное имя контекста с кабинетом вне реестра — отказ, не запрос."""
    set_active_account("offline:999999999")
    try:
        with pytest.raises(RuntimeError, match="не зарегистрирован"):
            get_fb_account_id()
    finally:
        set_active_account(None)


# ---------------------------------------------------------------------------
# facebook: выбор контекста кабинета манифеста (executor + post-read)
# ---------------------------------------------------------------------------


def test_manifest_account_context_online(routed_registry):
    assert (
        facebook._manifest_account_context_name(
            ONLINE,
            "manifest-1",
            block_code="INVALID_LAUNCH_MANIFEST",
            block_reason="r",
        )
        == "online"
    )


def test_manifest_account_context_default_offline(routed_registry):
    assert (
        facebook._manifest_account_context_name(
            f"act_{CABINET_A}",
            "manifest-1",
            block_code="INVALID_LAUNCH_MANIFEST",
            block_reason="r",
        )
        is None
    )


def test_manifest_account_context_routed_offline(routed_registry):
    assert (
        facebook._manifest_account_context_name(
            CABINET_B,
            "manifest-1",
            block_code="INVALID_LAUNCH_MANIFEST",
            block_reason="r",
        )
        == f"offline:{CABINET_B}"
    )


def test_manifest_account_context_unknown_blocks_with_given_code(routed_registry):
    with pytest.raises(LaunchCheckBlocked) as error:
        facebook._manifest_account_context_name(
            "424242",
            "manifest-1",
            block_code="LAUNCH_POSTCONDITION_INCOMPLETE",
            block_reason="нет в реестре",
        )
    assert error.value.code == "LAUNCH_POSTCONDITION_INCOMPLETE"
    assert error.value.check_id == "manifest-1"


# ---------------------------------------------------------------------------
# facebook: asset-recovery ретрай входит в кабинет ЦЕЛИ
# ---------------------------------------------------------------------------


def _recovery_manifest(account_id: str) -> AssetRecoveryManifest:
    return AssetRecoveryManifest(
        kind=ActionKind.ASSET_RECOVERY,
        manifest_id=str(uuid.uuid4()),
        origin=ActionOrigin.ASSET_RECOVERY,
        idempotency_key=str(uuid.uuid4()),
        prepared_at=NOW,
        account_kind="offline",
        account_id=account_id,
        campaign_type="asset_recovery",
        source="RECOVERY",
        city="CityF",
        source_ad_id="20001",
        source_adset_id="30001",
        source_adset_name="L1 CityD",
        adset_type="L1",
        source_ad_name="CityD | Exact creative [PRODA]",
        source_creative_id="40001",
        source_identity_sha256="a" * 64,
        target_adset_id="30002",
        target_adset_name="L1 CityF",
        target_ad_name="CityF | Exact creative [PRODA]",
        target_identity_key="cityf | exact creative",
        pre_inventory_sha256="b" * 64,
        capacity_available=2,
        hard_reserve_slots=1,
    )


def _recovery_attestation(manifest: AssetRecoveryManifest) -> ActionAttemptAttestation:
    return ActionAttemptAttestation(
        attempt_id=str(uuid.uuid4()),
        permit_id=str(uuid.uuid4()),
        proposal_id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        claim_id=str(uuid.uuid4()),
        operation_kind="RECOVER_AD",
        account_id=manifest.account_id,
        resource_id=manifest.target_adset_id,
        payload_sha256="e" * 64,
        consumed_at=NOW,
    )


def test_asset_recovery_create_enters_routed_cabinet(routed_registry, monkeypatch):
    """CREATE ретрая уходит в cabinet_b: контекст входит в кабинет манифеста."""
    manifest = _recovery_manifest(CABINET_B)
    seen: dict[str, str] = {}

    def fake_create(attempt, *, account_id, **kwargs):
        # Внутри контекста активен кабинет цели, не cabinet_a.
        seen["context_account"] = get_fb_account_id()
        seen["transport_account"] = account_id
        return "70001"

    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.create_ad", fake_create
    )

    ad_id = facebook._execute_asset_recovery_manifest_unchecked(
        manifest,
        _recovery_attestation(manifest),
    )

    assert ad_id == "70001"
    assert seen["context_account"] == CABINET_B
    assert seen["transport_account"] == CABINET_B


def test_asset_recovery_unknown_cabinet_still_drifts(routed_registry, monkeypatch):
    """Кабинет вне реестра: контекст не подменяется, ambient-проверка держит."""
    manifest = _recovery_manifest("999999999")
    create = []
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport.create_ad",
        lambda *args, **kwargs: create.append(1) or "70002",
    )

    with pytest.raises(
        facebook.AssetRecoveryCreateNotStarted, match="account_drift"
    ):
        facebook._execute_asset_recovery_manifest_unchecked(
            manifest,
            _recovery_attestation(manifest),
        )
    assert create == []


# ---------------------------------------------------------------------------
# facebook: медиа грузится в кабинет ЦЕЛИ (adimages маршрутизированного кабинета)
# ---------------------------------------------------------------------------


def test_upload_image_goes_to_routed_cabinet(routed_registry, monkeypatch, tmp_path):
    """image_hash не переносится между кабинетами: upload обязан идти в act_
    кабинета цели, а не в cabinet_a."""
    from services.launch_checker import ProviderLaunchAuthorization

    image = tmp_path / "creative.jpg"
    image.write_bytes(b"tiny-image-bytes")
    proof = ProviderLaunchAuthorization(auth_id="auth-routed", secret="s")
    manifest = _recovery_manifest(CABINET_B)
    attempt = _recovery_attestation(manifest)
    captured: dict[str, str] = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"images": {"creative.jpg": {"hash": "routed-hash"}}}

    def fake_asset(passed_attempt, *, account_id, url, **kwargs):
        assert passed_attempt is attempt
        captured["account_id"] = account_id
        captured["url"] = url
        return _Response()

    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport._create_launch_asset",
        fake_asset,
    )

    with fb_account(f"offline:{CABINET_B}"):
        with facebook._bind_provider_launch_context(
            proof,
            attempt=attempt,
            media_sha256="f" * 64,
            account_kind="offline",
            account_id=CABINET_B,
        ):
            image_hash = facebook.upload_image(str(image))

    assert image_hash == "routed-hash"
    assert captured["account_id"] == CABINET_B
    assert captured["url"].endswith(f"/act_{CABINET_B}/adimages")


def test_upload_image_blocks_when_context_cabinet_differs(
    routed_registry, monkeypatch, tmp_path
):
    """Дрейф кабинета между проверкой и загрузкой = отказ до HTTP."""
    from services.launch_checker import ProviderLaunchAuthorization

    image = tmp_path / "creative.jpg"
    image.write_bytes(b"tiny-image-bytes")
    proof = ProviderLaunchAuthorization(auth_id="auth-drift", secret="s")
    manifest = _recovery_manifest(CABINET_B)
    attempt = _recovery_attestation(manifest)
    called = []
    monkeypatch.setattr(
        "integrations.facebook_ads_mutation_transport._create_launch_asset",
        lambda *args, **kwargs: called.append(1),
    )

    # Контекст остался в cabinet_a, а проверенный launch context несёт cabinet_b.
    with facebook._bind_provider_launch_context(
        proof,
        attempt=attempt,
        media_sha256="f" * 64,
        account_kind="offline",
        account_id=CABINET_B,
    ):
        with pytest.raises(LaunchCheckBlocked) as error:
            facebook.upload_image(str(image))

    assert error.value.code == "PROVIDER_SCOPE_DRIFT"
    assert called == []


# ---------------------------------------------------------------------------
# facebook: read-only инвентарь маршрутизированного кабинета (зависимость
# верификатора запуска: launch_verify._fetch_watchdog_live_ads читает кабинет
# КАЖДОЙ цели, включая cabinet_b)
# ---------------------------------------------------------------------------


def test_complete_inventory_reads_routed_offline_cabinet(
    routed_registry, monkeypatch
):
    monkeypatch.setattr(
        "services.fb_token_provider.fb_account", lambda _name=None: nullcontext()
    )
    monkeypatch.setattr(facebook, "get_fb_account_id", lambda: CABINET_A)
    monkeypatch.setattr(facebook, "get_fb_token", lambda: "token")

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "data": [
                    {
                        "id": "ad-1",
                        "name": "CityF | Креатив [PRODA]",
                        "account_id": CABINET_B,
                        "adset_id": "2001",
                        "created_time": "2026-08-07T10:00:00+0000",
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                    }
                ]
            }

    urls: list[str] = []

    def fake_get(url, *, params):
        urls.append(url)
        return _Response()

    monkeypatch.setattr(facebook, "_throttled_get", fake_get)

    rows = facebook.fetch_complete_account_ad_inventory("offline", CABINET_B)

    assert [row["id"] for row in rows] == ["ad-1"]
    assert rows[0]["account_id"] == CABINET_B
    assert urls and f"/act_{CABINET_B}/ads" in urls[0]


def test_complete_inventory_unregistered_cabinet_fails_closed(
    routed_registry, monkeypatch
):
    monkeypatch.setattr(
        "services.fb_token_provider.fb_account", lambda _name=None: nullcontext()
    )
    monkeypatch.setattr(facebook, "get_fb_account_id", lambda: CABINET_A)
    http = []
    monkeypatch.setattr(
        facebook, "_throttled_get", lambda *a, **k: http.append(1)
    )

    with pytest.raises(facebook.AdsetInventoryError, match="scope_mismatch"):
        facebook.fetch_complete_account_ad_inventory("offline", "999999999")
    assert http == []


# ---------------------------------------------------------------------------
# launch_recovery: инвентарь по всем кабинетам и кабинет города
# ---------------------------------------------------------------------------


def test_offline_inventory_scans_every_routed_cabinet(routed_registry, monkeypatch):
    fetched: list[tuple[str, str]] = []
    monkeypatch.setattr(recovery, "_resolve_account_id", lambda _kind: CABINET_A)
    monkeypatch.setattr(
        recovery,
        "_fetch_account_inventory",
        lambda kind, account_id: fetched.append((kind, account_id))
        or [{"id": f"ad-{account_id}", "account_kind": kind, "account_id": account_id}],
    )

    inventory = recovery.fetch_complete_launch_inventory(("offline",))

    assert fetched == [("offline", CABINET_A), ("offline", CABINET_B)]
    assert [row["account_id"] for row in inventory["offline"]] == [CABINET_A, CABINET_B]


def test_offline_inventory_fails_closed_without_routing(routed_registry, monkeypatch):
    """Карта роутинга нечитаема → отказ аудита, а не молчаливый скан cabinet_a."""

    def broken():
        raise RuntimeError("routing down")

    monkeypatch.setattr(recovery, "_resolve_account_id", lambda _kind: CABINET_A)
    monkeypatch.setattr("services.launch_routing.accounts_to_scan", broken)

    with pytest.raises(recovery.RecoveryAuditError, match="launch_routing_unavailable"):
        recovery.fetch_complete_launch_inventory(("offline",))


def test_normalize_live_adset_payload_captures_pair_accounts():
    """Кабинет фиксируется по паре: L2 CityA в cabinet_b, L1 — в cabinet_a."""
    payload = {
        "source": "fb_api",
        "leadgen": {
            "CityA": {"L2": "1001", "L1": "1002"},
            "CityF": {"L2": "2001", "L1": "2002"},
        },
        "mql": {},
        "accounts": {
            "CityA": {"L2": f"act_{CABINET_B}", "L1": CABINET_A},
            "CityF": {"L2": f"act_{CABINET_B}", "L1": CABINET_B},
        },
    }

    discovered = recovery._normalize_live_adset_payload("offline", CABINET_A, payload)

    assert discovered.accounts == {
        "CityA": {"L2": CABINET_B, "L1": CABINET_A},
        "CityF": {"L2": CABINET_B, "L1": CABINET_B},
    }


def test_normalize_live_adset_payload_missing_city_account_is_review():
    payload = {
        "source": "fb_api",
        "leadgen": {"CityF": {"L2": "2001"}},
        "mql": {},
        "accounts": {},
    }

    with pytest.raises(
        recovery.RecoveryReviewRequired, match="adset_discovery_account_missing"
    ):
        recovery._normalize_live_adset_payload("offline", CABINET_A, payload)


def test_normalize_live_adset_payload_missing_one_type_is_review():
    """Кабинет есть для L2, но не для L1 — половина правды не проходит."""
    payload = {
        "source": "fb_api",
        "leadgen": {"CityA": {"L2": "1001", "L1": "1002"}},
        "mql": {},
        "accounts": {"CityA": {"L2": CABINET_B}},
    }

    with pytest.raises(
        recovery.RecoveryReviewRequired,
        match="adset_discovery_account_missing:offline:CityA:L1",
    ):
        recovery._normalize_live_adset_payload("offline", CABINET_A, payload)


def _cityf_live(city_account: str) -> dict[str, recovery.LiveAdsetDiscovery]:
    """Инвентарь с расщеплённой CityA; кабинет L2 CityF — параметр теста."""
    return {
        "offline": recovery.LiveAdsetDiscovery(
            account_kind="offline",
            account_id=CABINET_A,
            leadgen={
                "CityA": {"L2": "1001", "L1": "1002"},
                "CityF": {"L2": "2001", "L1": "2002"},
            },
            mql={},
            accounts={
                "CityA": {"L2": CABINET_B, "L1": CABINET_A},
                "CityF": {"L2": city_account, "L1": city_account},
            },
        ),
    }


def _scope_patches(monkeypatch, language: str = "L2"):
    monkeypatch.setattr(
        "services.auto_launch._resolve_launch_account",
        lambda _campaign_type: ("offline", CABINET_A),
    )
    monkeypatch.setattr(
        "integrations.trello.detect_language", lambda _name, _desc: language
    )


def test_city_scope_returns_routed_cabinet_for_cityf(routed_registry, monkeypatch):
    _scope_patches(monkeypatch)
    card = {"id": "card-1", "name": "Креатив", "desc": "", "labels": ["PRODA"]}

    kind, account_id, adset_id = recovery._resolve_city_scope(
        card,
        "CityF",
        _cityf_live(CABINET_B),
    )

    assert (kind, account_id, adset_id) == ("offline", CABINET_B, "2001")


def test_city_scope_route_mismatch_is_review(routed_registry, monkeypatch):
    """Discovery видит город в cabinet_a, карта шлёт в cabinet_b → REVIEW."""
    _scope_patches(monkeypatch)
    card = {"id": "card-1", "name": "Креатив", "desc": "", "labels": ["PRODA"]}

    with pytest.raises(
        recovery.RecoveryReviewRequired, match="city_scope_route_mismatch"
    ):
        recovery._resolve_city_scope(card, "CityF", _cityf_live(CABINET_A))


def test_city_scope_unrouted_city_is_review(routed_registry, monkeypatch):
    _scope_patches(monkeypatch)
    card = {"id": "card-1", "name": "Креатив", "desc": "", "labels": ["PRODA"]}
    live = {
        "offline": recovery.LiveAdsetDiscovery(
            account_kind="offline",
            account_id=CABINET_A,
            leadgen={"CityG": {"L2": "3001"}},
            mql={},
            accounts={"CityG": {"L2": CABINET_A}},
        ),
    }

    with pytest.raises(recovery.RecoveryReviewRequired, match="city_scope_unrouted"):
        recovery._resolve_city_scope(card, "CityG", live)


def test_city_scope_splits_one_city_between_cabinets(routed_registry, monkeypatch):
    """Ретрай L2 CityA идёт в cabinet_b, ретрай L1 того же города — в cabinet_a."""
    card = {"id": "card-1", "name": "Креатив", "desc": "", "labels": ["PRODA"]}

    _scope_patches(monkeypatch, language="L2")
    assert recovery._resolve_city_scope(card, "CityA", _cityf_live(CABINET_B)) == (
        "offline",
        CABINET_B,
        "1001",
    )

    _scope_patches(monkeypatch, language="L1")
    assert recovery._resolve_city_scope(card, "CityA", _cityf_live(CABINET_B)) == (
        "offline",
        CABINET_A,
        "1002",
    )


def test_normalize_live_adset_payload_accepts_prodb_type():
    """Discovery отдаёт leadgen[city]["PRODB"] — это не ambiguous."""
    payload = {
        "source": "fb_api",
        "leadgen": {"CityA": {"L2": "1001", "L1": "1002", "PRODB": "9001"}},
        "mql": {},
        "accounts": {"CityA": {"L2": CABINET_B, "L1": CABINET_A, "PRODB": CABINET_B}},
    }

    discovered = recovery._normalize_live_adset_payload("offline", CABINET_A, payload)

    assert discovered.leadgen["CityA"]["PRODB"] == "9001"
    assert discovered.accounts["CityA"]["PRODB"] == CABINET_B


def _prodb_live(with_prodb: bool = True) -> dict[str, recovery.LiveAdsetDiscovery]:
    """Инвентарь CityA: PRODA-пара L2/L1 плюс (опционально) PRODB-адсет в cabinet_b."""
    leadgen = {"L2": "1001", "L1": "1002"}
    accounts = {"L2": CABINET_B, "L1": CABINET_A}
    if with_prodb:
        leadgen["PRODB"] = "9001"
        accounts["PRODB"] = CABINET_B
    return {
        "offline": recovery.LiveAdsetDiscovery(
            account_kind="offline",
            account_id=CABINET_A,
            leadgen={"CityA": leadgen},
            mql={},
            accounts={"CityA": accounts},
        ),
    }


def test_city_scope_prodb_card_uses_prodb_adset(routed_registry, monkeypatch):
    """PRODB-карточка (метка «PRODB» → leadgen_prodb) идёт в PRODB-адсет cabinet_b.

    Раньше recovery брал leadgen[city][language] и для PRODB-карточки
    на L1 находил L1-адсет cabinet_a — сверка с картой давала
    route_mismatch, а ретрай в правильный адсет был невозможен.
    """
    _scope_patches(monkeypatch, language="L1")
    card = {"id": "card-1", "name": "PRODB креатив", "desc": "", "labels": ["PRODB"]}

    assert recovery._campaign_type(card) == "leadgen_prodb"
    assert recovery._resolve_city_scope(card, "CityA", _prodb_live()) == (
        "offline",
        CABINET_B,
        "9001",
    )


def test_city_scope_prodb_card_without_prodb_adset_is_review(routed_registry, monkeypatch):
    """Нет PRODB-адсета в инвентаре — REVIEW, а не откат в L1-адсет по языку."""
    _scope_patches(monkeypatch, language="L1")
    card = {"id": "card-1", "name": "PRODB креатив", "desc": "", "labels": ["PRODB"]}

    with pytest.raises(
        recovery.RecoveryReviewRequired,
        match="city_scope_missing:offline:CityA:PRODB",
    ):
        recovery._resolve_city_scope(card, "CityA", _prodb_live(with_prodb=False))


def test_city_scope_type_mismatch_inside_city_is_review(routed_registry, monkeypatch):
    """Инвентарь считает L2 CityA cabinet_a-овским, карта — cabinet_b → REVIEW."""
    _scope_patches(monkeypatch, language="L2")
    card = {"id": "card-1", "name": "Креатив", "desc": "", "labels": ["PRODA"]}
    live = {
        "offline": recovery.LiveAdsetDiscovery(
            account_kind="offline",
            account_id=CABINET_A,
            leadgen={"CityA": {"L2": "1001", "L1": "1002"}},
            mql={},
            accounts={"CityA": {"L2": CABINET_A, "L1": CABINET_A}},
        ),
    }

    with pytest.raises(
        recovery.RecoveryReviewRequired, match="city_scope_route_mismatch"
    ):
        recovery._resolve_city_scope(card, "CityA", live)
