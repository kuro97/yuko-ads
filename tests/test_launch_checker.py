"""Офлайн-тесты pure planning и fail-closed launch checker."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from services.launch_checker import (
    CheckerMode,
    LaunchCheckBlocked,
    LaunchCheckRequest,
    LaunchCheckStatus,
    LaunchChecker,
    LaunchSource,
    LaunchTarget,
    LiveAd,
    LiveAdsetInventory,
    PreparedLaunchMedia,
    ProviderLaunchAuthorization,
    bind_provider_authorization,
    check_candidate,
    get_bound_provider_authorization,
    hashed_api_key_actor,
    launch_identity_key,
    normalize_launch_name,
    validate_authorization_media,
)


NOW = datetime(2026, 7, 22, 7, 0, tzinfo=timezone.utc)
MEDIA_SHA = "a" * 64


def _card(name: str = "Свежая реклама") -> dict:
    return {"id": "card-1", "name": name, "desc": "", "pos": 10.0}


def _request(source: LaunchSource = LaunchSource.CRON, **changes) -> LaunchCheckRequest:
    values = {
        "source": source,
        "campaign_type": "leadgen_prodb",
        "cities": ("CityA", "CityB"),
        "as_carousel": False,
    }
    values.update(changes)
    return LaunchCheckRequest(**values)


class FakeRepository:
    def __init__(self) -> None:
        self.events = []
        self.reservations = []
        self.validation = None
        self.reserve_error: Exception | None = None

    def append_launch_audit(self, event) -> None:
        self.events.append(event)

    def reserve_authorization(self, plan, secret_sha256, now) -> None:
        if self.reserve_error:
            raise self.reserve_error
        self.reservations.append((plan, secret_sha256, now))

    def validate_authorization_media(self, proof, actual_media_sha256, now):
        self.validation = (proof, actual_media_sha256, now)
        return {"phase": "RESERVED"}


class RepositoryBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        self.reasons = ("repository deny",)
        self.check_id = "repository-check"


def _targets(card, request, language, product, media):
    del card, language, product, media
    return tuple(
        LaunchTarget(
            city=city,
            ordinal=ordinal,
            account_kind="offline",
            account_id="123",
            adset_id=adset_id,
            expected_names=(f"{city} | Свежая реклама [PRODB]",),
        )
        for ordinal, (city, adset_id) in enumerate(
            zip(request.cities or (), ("20", "3"), strict=True)
        )
    )


def _inventory(target: LaunchTarget) -> LiveAdsetInventory:
    return LiveAdsetInventory(
        adset_id=target.adset_id,
        effective_status="ACTIVE",
        inventory_complete=True,
        ads=(),
        ad_count=0,
        max_ads=50,
        available=50,
    )


def _checker(
    mode: CheckerMode,
    repository: FakeRepository,
    *,
    inventory=_inventory,
    locks: list[str] | None = None,
    targets=_targets,
) -> LaunchChecker:
    lock_order = locks if locks is not None else []

    @contextmanager
    def lock(adset_id: str):
        lock_order.append(adset_id)
        yield

    return LaunchChecker(
        mode=mode,
        repository=repository,
        prepare_media=lambda card, request: PreparedLaunchMedia(
            {"type": "video", "paths": ("one.mp4",)}, MEDIA_SHA
        ),
        resolve_language=lambda card: "L1",
        resolve_product=lambda card: "PRODB",
        resolve_targets=targets,
        read_inventory=inventory,
        adset_lock=lock,
        now=lambda: NOW,
    )


@pytest.mark.parametrize(
    "value",
    (
        "  CityA   |  Тест [PRODB] ",
        "citya\t| тест [prodb]",
        "CityA | Тест ［PRODB］",
    ),
)
def test_name_normalization_nfkc_case_space_and_product_tag(value):
    assert normalize_launch_name(value) == "citya | тест"


def test_identity_omits_asset_suffix_and_product_tag():
    assert launch_identity_key(" CityA ", "Тест [PRODB]") == "citya | тест"


@pytest.mark.parametrize(
    "entry",
    (
        "2026-07-01T00:00:00+05:00",
        {"complete": True},
        {"complete": True, "ad_ids": [], "legacy_unverified": True},
    ),
)
def test_legacy_evidence_fails_closed_without_state_change(entry):
    state = {"launched_ever": {"card-1": entry}}
    before = repr(state)
    result = check_candidate(_card(), _request(), state, check_id="check-1")
    assert result.reason_codes == ("UNKNOWN_LEGACY",)
    assert result.status is LaunchCheckStatus.BLOCKED
    assert repr(state) == before


def test_automatic_veto_is_vetoed_even_with_override_fields():
    result = check_candidate(
        _card("Акция на бонус"),
        _request(override_topic_veto=True, override_reason="явное разрешение"),
        {},
        check_id="check-1",
    )
    assert result.reason_codes == ("TOPIC_OVERRIDE_INVALID",)


def test_manual_topic_override_requires_reason_and_hashed_actor():
    actor = hashed_api_key_actor("test-api-key")
    result = check_candidate(
        _card("Акция на БОНУС"),
        _request(
            LaunchSource.MANUAL,
            override_topic_veto=True,
            override_reason="Подтверждено владельцем",
            actor=actor,
        ),
        {},
        check_id="check-1",
    )
    assert result.status is LaunchCheckStatus.NEEDS_FULL_PREFLIGHT


def test_observe_preflight_returns_plan_without_authorization_or_reservation():
    repository = FakeRepository()
    plan = _checker(CheckerMode.OBSERVE, repository).prepare_and_reserve(
        _card(), _request(), {}
    )
    assert plan.authorization is None
    assert repository.reservations == []
    assert [event.event_type for event in repository.events] == ["CANDIDATE", "PREFLIGHT"]


def test_enforce_locks_numeric_order_and_delegates_one_atomic_reserve():
    repository = FakeRepository()
    locks: list[str] = []
    plan = _checker(CheckerMode.ENFORCE, repository, locks=locks).prepare_and_reserve(
        _card(), _request(), {}
    )
    assert locks == ["3", "20"]
    assert isinstance(plan.authorization, ProviderLaunchAuthorization)
    assert len(repository.reservations) == 1
    assert len(repository.reservations[0][1]) == 64
    assert "redacted" in repr(plan.authorization)
    assert plan.authorization.secret not in repr(plan.authorization)


def test_capacity_is_checked_for_every_target_before_reserve():
    repository = FakeRepository()

    def full_second(target):
        result = _inventory(target)
        if target.city == "CityB":
            return LiveAdsetInventory(
                adset_id=target.adset_id,
                effective_status="ACTIVE",
                inventory_complete=True,
                ads=tuple(LiveAd(str(i), f"old-{i}") for i in range(49)),
                ad_count=49,
                max_ads=50,
                available=1,
            )
        return result

    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _checker(
            CheckerMode.ENFORCE, repository, inventory=full_second
        ).prepare_and_reserve(_card(), _request(), {})
    assert exc_info.value.code == "CAPACITY_BLOCKED"
    assert repository.reservations == []


@pytest.mark.parametrize(
    "live_names,code",
    (
        (("CityA | Свежая реклама [PRODB]",), "ALREADY_EXISTS"),
        (("CityA | Свежая реклама / другой [PRODB]",), "DUPLICATE_LIVE"),
    ),
)
def test_live_exact_and_identity_duplicates_fail_closed(live_names, code):
    repository = FakeRepository()

    def inventory(target):
        names = live_names if target.city == "CityA" else ()
        return LiveAdsetInventory(
            adset_id=target.adset_id,
            effective_status="ACTIVE",
            inventory_complete=True,
            ads=tuple(LiveAd(str(i), name) for i, name in enumerate(names)),
            ad_count=len(names),
            max_ads=50,
            available=50 - len(names),
        )

    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _checker(CheckerMode.ENFORCE, repository, inventory=inventory).prepare_and_reserve(
            _card(), _request(), {}
        )
    assert exc_info.value.code == code


@pytest.mark.parametrize(
    "live_names,code",
    (
        (("CityA | Свежая реклама [PRODB]",), "ALREADY_EXISTS"),
        (("CityA | Свежая реклама / другой [PRODB]",), "DUPLICATE_LIVE"),
    ),
)
def test_observe_gate_skips_already_launched_city_and_keeps_the_rest(live_names, code):
    """Город, уже запущенный прошлым прогоном, пропускается гейтом автозапуска,
    а не держит остальные города карточки. ENFORCE-путь по-прежнему fail-closed (тест выше)."""
    repository = FakeRepository()

    def inventory(target):
        names = live_names if target.city == "CityA" else ()
        return LiveAdsetInventory(
            adset_id=target.adset_id, effective_status="ACTIVE", inventory_complete=True,
            ads=tuple(LiveAd(str(i), name) for i, name in enumerate(names)),
            ad_count=len(names), max_ads=50, available=50 - len(names),
        )

    plan = _checker(CheckerMode.OBSERVE, repository, inventory=inventory).prepare_and_reserve(
        _card(), _request(), {}
    )
    assert [target.city for target in plan.targets] == ["CityB"]
    assert [(city, skipped_code) for city, skipped_code, _reason in plan.skipped_targets] == [("CityA", code)]
    assert "CityA" not in plan.expected_names_by_city
    assert repository.reservations == []


def test_observe_gate_skips_city_without_capacity_but_blocks_when_nothing_left():
    repository = FakeRepository()

    def full(target, cities=("CityB",)):
        if target.city not in cities:
            return _inventory(target)
        return LiveAdsetInventory(
            adset_id=target.adset_id, effective_status="ACTIVE", inventory_complete=True,
            ads=tuple(LiveAd(str(i), f"old-{i}") for i in range(49)), ad_count=49, max_ads=50, available=1,
        )

    plan = _checker(CheckerMode.OBSERVE, repository, inventory=full).prepare_and_reserve(_card(), _request(), {})
    assert [target.city for target in plan.targets] == ["CityA"]
    assert plan.skipped_targets[0][:2] == ("CityB", "CAPACITY_BLOCKED")

    with pytest.raises(LaunchCheckBlocked) as info:  # все города без слотов — карточка заблокирована
        _checker(
            CheckerMode.OBSERVE, repository, inventory=lambda t: full(t, ("CityA", "CityB"))
        ).prepare_and_reserve(_card(), _request(), {})
    assert info.value.code == "CAPACITY_BLOCKED"


def test_observe_gate_unverified_inventory_still_blocks_whole_card():
    """Неизвестное состояние адсета (не «уже запущен» и не «нет слотов») — прежний отказ карточки."""
    repository = FakeRepository()

    def broken(target):
        result = _inventory(target)
        if target.city == "CityB":
            return LiveAdsetInventory(
                adset_id=target.adset_id, effective_status="PAUSED", inventory_complete=True,
                ads=(), ad_count=0, max_ads=50, available=50,
            )
        return result

    with pytest.raises(LaunchCheckBlocked) as info:
        _checker(CheckerMode.OBSERVE, repository, inventory=broken).prepare_and_reserve(_card(), _request(), {})
    assert info.value.code == "INVENTORY_UNVERIFIED"


# ---------------------------------------------------------------------------
# Маршрутизация город→кабинет: один оффлайн-запуск может нести
# несколько кабинетов, оффлайн/онлайн смешивать по-прежнему нельзя.
# ---------------------------------------------------------------------------

def _make_targets(rows):
    """rows: (city, account_kind, account_id, adset_id)."""

    def resolver(card, request, language, product, media):
        del card, request, language, product, media
        return tuple(
            LaunchTarget(
                city=city,
                ordinal=ordinal,
                account_kind=kind,
                account_id=account_id,
                adset_id=adset_id,
                expected_names=(f"{city} | Свежая реклама [PRODB]",),
            )
            for ordinal, (city, kind, account_id, adset_id) in enumerate(rows)
        )

    return resolver


def test_offline_launch_carries_city_cabinets_and_ignores_city_order():
    """«Все города» в двух кабинетах: cabinet_a + cabinet_b в одном плане.

    Порядок городов в запросе не обязан совпадать с порядком discovery-скана
    по кабинетам — сверка exact множеством, пропуск города остаётся отказом.
    """
    repository = FakeRepository()
    resolver = _make_targets(
        (
            ("CityA", "offline", "111", "20"),
            ("CityF", "offline", "29716040622546856", "30"),
        )
    )
    plan = _checker(CheckerMode.OBSERVE, repository, targets=resolver).prepare_and_reserve(
        _card(),
        _request(cities=("CityF", "CityA")),
        {},
    )
    assert {target.city: target.account_id for target in plan.targets} == {
        "CityA": "111",
        "CityF": "29716040622546856",
    }


def test_missing_requested_city_is_still_blocked():
    repository = FakeRepository()
    resolver = _make_targets((("CityA", "offline", "111", "20"),))
    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _checker(CheckerMode.OBSERVE, repository, targets=resolver).prepare_and_reserve(
            _card(),
            _request(cities=("CityF", "CityA")),
            {},
        )
    assert exc_info.value.code == "PREFLIGHT_INVALID"


def test_mixing_offline_and_online_kinds_is_blocked():
    repository = FakeRepository()
    resolver = _make_targets(
        (
            ("CityA", "offline", "111", "20"),
            ("CityB", "online", "222", "3"),
        )
    )
    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _checker(CheckerMode.OBSERVE, repository, targets=resolver).prepare_and_reserve(
            _card(), _request(), {}
        )
    assert exc_info.value.code == "PREFLIGHT_INVALID"


def test_online_launch_cannot_mix_accounts():
    repository = FakeRepository()
    resolver = _make_targets(
        (
            ("CityA", "online", "111", "20"),
            ("CityB", "online", "222", "3"),
        )
    )
    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _checker(CheckerMode.OBSERVE, repository, targets=resolver).prepare_and_reserve(
            _card(), _request(), {}
        )
    assert exc_info.value.code == "PREFLIGHT_INVALID"


def test_repository_typed_collision_is_translated():
    repository = FakeRepository()
    repository.reserve_error = RepositoryBlocked("DUPLICATE_RESERVED")
    with pytest.raises(LaunchCheckBlocked) as exc_info:
        _checker(CheckerMode.ENFORCE, repository).prepare_and_reserve(
            _card(), _request(), {}
        )
    assert exc_info.value.code == "DUPLICATE_RESERVED"
    assert exc_info.value.check_id == "repository-check"


def test_bound_proof_context_is_reset_and_secret_never_repr():
    proof = ProviderLaunchAuthorization("auth-1", "top-secret")
    with bind_provider_authorization(proof):
        assert get_bound_provider_authorization() is proof
    with pytest.raises(LaunchCheckBlocked, match="AUTHORIZATION_REQUIRED"):
        get_bound_provider_authorization()


def test_media_validation_delegates_to_db_backed_repository_before_provider():
    repository = FakeRepository()
    proof = ProviderLaunchAuthorization("auth-1", "top-secret")
    result = validate_authorization_media(
        proof, MEDIA_SHA, repository=repository, now=NOW
    )
    assert result == {"phase": "RESERVED"}
    assert repository.validation == (proof, MEDIA_SHA, NOW)
