"""Offline contract tests production-adapters launch checker-а."""

from contextlib import nullcontext
from unittest.mock import patch

from services.launch_checker import (
    CheckerMode,
    LaunchCheckRequest,
    LaunchSource,
    LaunchTarget,
    PreparedLaunchMedia,
)
import pytest

from services.launch_checker_runtime import (
    _copy_media,
    _read_inventory,
    _resolve_targets,
    build_production_launch_checker,
)


def test_copy_media_applies_same_carousel_limit_without_mutating_input():
    raw = {"type": "image", "paths": [f"asset-{index}.jpg" for index in range(12)]}

    copied = _copy_media(raw, as_carousel=True)

    assert copied["type"] == "carousel"
    assert copied["paths"] == raw["paths"][:10]
    assert raw["type"] == "image"
    assert len(raw["paths"]) == 12


def test_runtime_inventory_counts_launch_and_replacement_reservations():
    target = LaunchTarget(
        city="CityA",
        ordinal=0,
        account_kind="offline",
        account_id="123",
        adset_id="1001",
        expected_names=("CityA | Fresh [PRODA]",),
    )
    snapshot = {
        "adset_id": "1001",
        # Живой кабинет адсета обязан совпадать с кабинетом цели города.
        "account_id": "123",
        "adset_effective_status": "ACTIVE",
        "inventory_complete": True,
        "ad_count": 1,
        "ads": [{"id": "old-1", "name": "CityA | Old"}],
    }
    with patch(
        "services.fb_token_provider.fb_account",
        side_effect=lambda _name: nullcontext(),
    ), patch(
        "services.fb_token_provider.get_fb_account_id",
        return_value="123",
    ), patch(
        "integrations.facebook.get_adset_info",
        return_value=snapshot,
    ), patch(
        "services.adset_cleaner._other_reserved_slots",
        return_value=2,
    ), patch(
        "services.launch_repository.get_reserved_slots",
        return_value=3,
    ):
        inventory = _read_inventory(target)

    assert inventory.available == 49
    assert inventory.other_reserved_slots == 5
    assert inventory.ads[0].name == "CityA | Old"


def test_runtime_inventory_reads_target_cabinet_not_process_default():
    """Оффлайн-цель в cabinet_b читается, даже когда process-default — cabinet_a."""
    target = LaunchTarget(
        city="CityF",
        ordinal=0,
        account_kind="offline",
        account_id="29716040622546856",
        adset_id="120084330793960265",
        expected_names=("CityF | Fresh [PRODA]",),
    )
    snapshot = {
        "adset_id": "120084330793960265",
        "account_id": "29716040622546856",
        "adset_effective_status": "ACTIVE",
        "inventory_complete": True,
        "ad_count": 0,
        "ads": [],
    }
    with patch(
        "services.fb_token_provider.fb_account",
        side_effect=AssertionError("offline не должен переключать FB-контекст"),
    ), patch(
        "services.fb_token_provider.get_fb_account_id",
        return_value="152882611033373",
    ), patch(
        "integrations.facebook.get_adset_info",
        return_value=snapshot,
    ), patch(
        "services.adset_cleaner._other_reserved_slots",
        return_value=0,
    ), patch(
        "services.launch_repository.get_reserved_slots",
        return_value=0,
    ):
        inventory = _read_inventory(target)

    assert inventory.adset_id == "120084330793960265"
    assert inventory.effective_status == "ACTIVE"
    assert inventory.available == 50


def test_runtime_inventory_blocks_adset_living_in_wrong_cabinet():
    """Адсет не в кабинете цели своего города — отказ, а не «куда получится»."""
    target = LaunchTarget(
        city="CityF",
        ordinal=0,
        account_kind="offline",
        account_id="29716040622546856",
        adset_id="9999",
        expected_names=("CityF | Fresh [PRODA]",),
    )
    snapshot = {
        "adset_id": "9999",
        "account_id": "152882611033373",
        "adset_effective_status": "ACTIVE",
        "inventory_complete": True,
        "ad_count": 0,
        "ads": [],
    }
    with patch(
        "integrations.facebook.get_adset_info",
        return_value=snapshot,
    ):
        with pytest.raises(RuntimeError, match="кабинете"):
            _read_inventory(target)


def _prepared_media() -> PreparedLaunchMedia:
    return PreparedLaunchMedia(
        media={"type": "image", "paths": ["a.jpg"]},
        media_sha256="a" * 64,
    )


_CABINET_A = "152882611033373"
_CABINET_B = "29716040622546856"


def _resolve_targets_with(accounts, language="L1", campaign_type="leadgen"):
    """Гоняет _resolve_targets на дефолтной карте роутинга с заданным инвентарём."""
    request = LaunchCheckRequest(
        source=LaunchSource.CRON,
        campaign_type=campaign_type,
        cities=None,
        as_carousel=False,
    )
    with patch("agent.scheduler.load_settings", return_value={}), patch(
        "integrations.facebook._prepare_launch_media",
        return_value=("image", ["a.jpg"], [], (), ()),
    ), patch(
        "integrations.facebook._resolve_launch_adsets",
        return_value=[("CityA", "1001"), ("CityF", "2002")],
    ), patch(
        "integrations.facebook._planned_city_names",
        side_effect=lambda city, *args: (f"{city} | Тест",),
    ), patch(
        "agent.adset_discovery.get_adset_accounts_dict",
        return_value=accounts,
    ):
        return _resolve_targets(
            {"id": "card-1", "name": "Тест"},
            request,
            language,
            "PRODA",
            _prepared_media(),
        )


def test_resolve_targets_assigns_pair_cabinet_from_discovery():
    """Кабинет каждой оффлайн-цели — из discovery по паре (город, тип)."""
    targets = _resolve_targets_with(
        {
            "CityA": {"L1": _CABINET_A, "L2": _CABINET_B},
            "CityF": {"L1": f"act_{_CABINET_B}"},
        }
    )

    assert [(target.city, target.account_id) for target in targets] == [
        ("CityA", _CABINET_A),
        ("CityF", _CABINET_B),
    ]
    assert all(target.account_kind == "offline" for target in targets)


def test_resolve_targets_l2_wave_goes_to_cabinet_b():
    """L2-волна после миграции идёт в cabinet_b для обоих городов."""
    targets = _resolve_targets_with(
        {
            "CityA": {"L2": _CABINET_B, "L1": _CABINET_A},
            "CityF": {"L2": _CABINET_B},
        },
        language="L2",
    )

    assert {target.account_id for target in targets} == {_CABINET_B}


def test_resolve_targets_prodb_card_looks_up_cabinet_by_prodb_key():
    """PRODB-карточка (leadgen_prodb): кабинет пары берётся по ключу PRODB, не по языку.

    Раньше route_type отдавал язык, и L1 PRODB-карточка искала бы
    L1-кабинет (cabinet_a) — при том что PRODB-адсеты живут в cabinet_b.
    """
    targets = _resolve_targets_with(
        {
            "CityA": {"L2": _CABINET_B, "L1": _CABINET_A, "PRODB": _CABINET_B},
            "CityF": {"L2": _CABINET_B, "L1": _CABINET_B, "PRODB": _CABINET_B},
        },
        language="L1",
        campaign_type="leadgen_prodb",
    )

    assert {target.account_id for target in targets} == {_CABINET_B}


def test_resolve_targets_prodb_card_without_prodb_cabinet_fails_closed():
    """Инвентарь без ключа PRODB — отказ, а не откат в L1-кабинет по языку."""
    with pytest.raises(ValueError, match="не привязан"):
        _resolve_targets_with(
            {
                "CityA": {"L2": _CABINET_B, "L1": _CABINET_A},
                "CityF": {"L2": _CABINET_B, "L1": _CABINET_B},
            },
            language="L1",
            campaign_type="leadgen_prodb",
        )


def test_resolve_targets_fails_closed_for_city_without_cabinet():
    """Пара без записи в карте кабинетов = отказ, не молчаливый cabinet_a."""
    with pytest.raises(ValueError, match="не привязан"):
        _resolve_targets_with({"CityA": {"L1": _CABINET_A}})


def test_resolve_targets_fails_closed_when_inventory_disagrees_with_map():
    """Инвентарь зовёт L2 CityA в cabinet_a, карта — в cabinet_b → отказ."""
    with pytest.raises(ValueError, match="расходится"):
        _resolve_targets_with(
            {"CityA": {"L2": _CABINET_A}, "CityF": {"L2": _CABINET_B}},
            language="L2",
        )


def test_factory_reuses_exact_cached_media_without_drive_download():
    prepared = PreparedLaunchMedia(
        media={"type": "image", "paths": ["cached.jpg"]},
        media_sha256="a" * 64,
    )
    checker = build_production_launch_checker(
        CheckerMode.ENFORCE,
        prepared_media_by_card={"card-1": prepared},
    )
    request = LaunchCheckRequest(
        source=LaunchSource.CRON,
        campaign_type="leadgen",
        cities=("CityA",),
        as_carousel=False,
    )

    with patch(
        "services.launch_checker_runtime._prepare_media",
        side_effect=AssertionError("Drive download не должен повторяться"),
    ):
        actual = checker.prepare_media({"id": "card-1"}, request)

    assert actual is prepared
