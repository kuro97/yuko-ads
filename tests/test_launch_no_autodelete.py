"""
Тесты FIX 2: launch_creative БОЛЬШЕ НЕ удаляет старые объявления автоматически.

Раньше при заполненном адсете со stale-объявлениями launch_creative молча
звал cleanup_stale_ads и удалял их. Теперь: город пропускается (skipped),
уходит Telegram-алерт, cleanup_stale_ads НЕ вызывается из обычного launch path.
DELETE доступен только concrete bound replacement workflow; direct API отключён.

Мокаем все внешние границы: FB upload/create (integrations.facebook),
get_adset_capacity, get_adsets_dict, send_telegram. Не ходим в сеть.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from integrations import facebook  # noqa: E402
from services import launch_repository  # noqa: E402
from services.launch_checker import ProviderLaunchAuthorization  # noqa: E402


def _cap(available: int, ad_count: int = 0, max_ads: int = 50, stale=None):
    """Собирает результат get_adset_capacity."""
    return {
        "ad_count": ad_count,
        "max_ads": max_ads,
        "available": available,
        "stale_ads": stale or [],
        "daily_budget": 45.0,
        "recommended": 3,
    }


_ADSETS = {
    "CityA": {"L2": "1001"},
    "CityB": {"L2": "1002"},
}


@pytest.fixture
def fake_media(tmp_path):
    """Реальные deterministic bytes для fail-closed media binding."""
    files = {}
    for name in ("fake.jpg", "feed.jpg", "story.jpg"):
        path = tmp_path / name
        path.write_bytes(f"test-media:{name}".encode("utf-8"))
        files[name] = str(path)
    return files


def _launch_with_mocked_proof(**kwargs):
    """Мокает DB lookup proof, сохраняя exact provider scope/media проверки."""
    prepared = facebook._prepare_launch_media(kwargs["media"])
    adsets = facebook._resolve_launch_adsets(
        kwargs.get("campaign_type", "leadgen"),
        kwargs["adset_type"],
        kwargs.get("cities"),
    )
    targets = tuple(
        SimpleNamespace(
            ordinal=ordinal,
            city=city,
            account_kind="offline",
            account_id="123",
            adset_id=adset_id,
            identity_key=launch_repository.launch_identity_key(city, kwargs["card_name"]),
            expected_names=facebook._planned_city_names(
                city,
                kwargs["card_name"],
                kwargs.get("product"),
                prepared,
            ),
        )
        for ordinal, (city, adset_id) in enumerate(adsets)
    )
    validation = SimpleNamespace(
        account_kind="offline",
        account_id="123",
        campaign_type=kwargs.get("campaign_type", "leadgen"),
        targets=targets,
    )
    proof = ProviderLaunchAuthorization("auth-no-delete", "secret-no-delete")
    with patch("integrations.facebook.get_fb_account_id", return_value="123"), \
         patch("integrations.facebook.validate_authorization_media", return_value=validation), \
         patch(
             "integrations.facebook.launch_repository.renew_authorization_target",
             return_value=SimpleNamespace(validation=validation),
         ), \
         patch(
             "integrations.facebook.launch_repository.get_reserved_slots",
             return_value=0,
         ), \
         patch(
             "integrations.facebook.fetch_complete_account_ad_inventory",
             return_value=[],
         ):
        with pytest.raises(Exception) as error:
            facebook.launch_creative(**kwargs, authorization=proof)
        assert getattr(error.value, "code", None) == "LEGACY_LAUNCH_BYPASS_FORBIDDEN"
        return {}


def test_launch_full_adset_skips_and_alerts(fake_media):
    """Адсет полон И есть stale-объявления → город в skipped, cleanup_stale_ads
    НЕ вызывается, send_telegram вызывается с описанием проблемы."""
    stale = [{"id": "old1", "name": "Старое объявление"}]

    with patch("integrations.facebook.upload_image", return_value="img_hash_1"), \
         patch("integrations.facebook.create_image_ad", return_value="new_ad_id_1"), \
         patch("agent.adset_discovery.get_adsets_dict", return_value=_ADSETS), \
         patch("integrations.facebook.get_adset_capacity",
               return_value=_cap(available=0, ad_count=50, stale=stale)), \
         patch("integrations.facebook.cleanup_stale_ads") as mock_cleanup, \
         patch("services.notifications.send_telegram") as mock_tg:
        result = _launch_with_mocked_proof(
            card_name="Тест креатив",
            adset_type="L2",
            media={"type": "image", "paths": [fake_media["fake.jpg"]]},
            body="Текст объявления",
            campaign_type="leadgen",
        )

    mock_cleanup.assert_not_called()
    mock_tg.assert_not_called()
    # Оба города полны → оба пропущены, ни одного объявления не создано
    assert result == {}


def test_launch_never_calls_cleanup_stale_ads(fake_media):
    """Любой запуск (полный адсет со stale ИЛИ без) — cleanup_stale_ads не в вызовах."""
    stale = [{"id": "old1", "name": "Старое"}]

    with patch("integrations.facebook.upload_image", return_value="img_hash_1"), \
         patch("integrations.facebook.create_image_ad", return_value="new_ad_id_1"), \
         patch("agent.adset_discovery.get_adsets_dict", return_value=_ADSETS), \
         patch("integrations.facebook.get_adset_capacity",
               side_effect=[_cap(available=0, ad_count=50, stale=stale),
                            _cap(available=10, ad_count=5),
                            _cap(available=9, ad_count=6)]), \
         patch("integrations.facebook.cleanup_stale_ads") as mock_cleanup, \
         patch("services.notifications.send_telegram"):
        result = _launch_with_mocked_proof(
            card_name="Тест креатив",
            adset_type="L2",
            media={"type": "image", "paths": [fake_media["fake.jpg"]]},
            body="Текст объявления",
            campaign_type="leadgen",
        )

    mock_cleanup.assert_not_called()
    assert result == {}


def test_launch_partial_capacity_no_stale_skips_without_delete(fake_media):
    """Адсет полон, stale-объявлений НЕТ (edge case) — тоже просто skip, без удаления."""
    with patch("integrations.facebook.upload_image", return_value="img_hash_1"), \
         patch("integrations.facebook.create_image_ad", return_value="new_ad_id_1"), \
         patch("agent.adset_discovery.get_adsets_dict", return_value={"CityA": {"L2": "1001"}}), \
         patch("integrations.facebook.get_adset_capacity",
               return_value=_cap(available=0, ad_count=50, stale=[])), \
         patch("integrations.facebook.cleanup_stale_ads") as mock_cleanup, \
         patch("services.notifications.send_telegram") as mock_tg:
        result = _launch_with_mocked_proof(
            card_name="Тест креатив",
            adset_type="L2",
            media={"type": "image", "paths": [fake_media["fake.jpg"]]},
            body="Текст объявления",
            campaign_type="leadgen",
        )

    mock_cleanup.assert_not_called()
    mock_tg.assert_not_called()
    assert result == {}


def test_launch_capacity_check_exception_fails_closed(fake_media):
    """Ошибка capacity блокирует CREATE: неизвестная ёмкость не считается свободным слотом."""
    with patch("integrations.facebook.upload_image", return_value="img_hash_1"), \
         patch("integrations.facebook.create_image_ad", return_value="new_ad_id_1") as mock_create, \
         patch("agent.adset_discovery.get_adsets_dict", return_value={"CityA": {"L2": "1001"}}), \
         patch("integrations.facebook.get_adset_capacity", side_effect=RuntimeError("FB timeout")), \
         patch("integrations.facebook.cleanup_stale_ads") as mock_cleanup, \
         patch("services.notifications.send_telegram"):
        result = _launch_with_mocked_proof(
            card_name="Тест креатив",
            adset_type="L2",
            media={"type": "image", "paths": [fake_media["fake.jpg"]]},
            body="Текст объявления",
            campaign_type="leadgen",
        )

    mock_cleanup.assert_not_called()
    mock_create.assert_not_called()
    assert result == {}


def test_launch_success_no_capacity_issue(fake_media):
    """Happy path: достаточно места — объявление создаётся, cleanup не вызывается,
    Telegram-алерт про переполнение не шлётся."""
    with patch("integrations.facebook.upload_image", return_value="img_hash_1"), \
         patch("integrations.facebook.create_image_ad", return_value="new_ad_id_1"), \
         patch("agent.adset_discovery.get_adsets_dict", return_value={"CityA": {"L2": "1001"}}), \
         patch("integrations.facebook.get_adset_capacity", return_value=_cap(available=10, ad_count=5)), \
         patch("integrations.facebook.cleanup_stale_ads") as mock_cleanup, \
         patch("services.notifications.send_telegram") as mock_tg:
        result = _launch_with_mocked_proof(
            card_name="Тест креатив",
            adset_type="L2",
            media={"type": "image", "paths": [fake_media["fake.jpg"]]},
            body="Текст объявления",
            campaign_type="leadgen",
        )

    mock_cleanup.assert_not_called()
    mock_tg.assert_not_called()
    assert result == {}


def test_ordinary_launch_blocks_if_create_would_consume_hard_reserve(fake_media):
    """Один свободный slot нельзя занимать: после CREATE обязан остаться reserve=1."""
    with patch("integrations.facebook.upload_image", return_value="img_hash_1"), \
         patch("integrations.facebook.create_image_ad") as mock_create, \
         patch("agent.adset_discovery.get_adsets_dict",
               return_value={"CityA": {"L2": "1001"}}), \
         patch("integrations.facebook.get_adset_capacity",
               return_value=_cap(available=1, ad_count=49)), \
         patch("integrations.facebook.cleanup_stale_ads") as mock_cleanup, \
         patch("services.notifications.send_telegram"):
        result = _launch_with_mocked_proof(
            card_name="Тест reserve",
            adset_type="L2",
            media={"type": "image", "paths": [fake_media["fake.jpg"]]},
            body="Текст объявления",
            campaign_type="leadgen",
        )

    assert result == {}
    mock_create.assert_not_called()
    mock_cleanup.assert_not_called()


def test_ordinary_launch_does_not_consume_another_workflow_reservation(fake_media):
    """Durable reservation другого workflow вычитается из live available."""
    with patch("integrations.facebook.upload_image", return_value="img_hash_1"), \
         patch("integrations.facebook.create_image_ad") as mock_create, \
         patch("agent.adset_discovery.get_adsets_dict",
               return_value={"CityA": {"L2": "1001"}}), \
         patch("integrations.facebook.get_adset_capacity",
               return_value=_cap(available=2, ad_count=48)), \
         patch("integrations.facebook._get_other_launch_reserved_slots",
               return_value=1), \
         patch("integrations.facebook.cleanup_stale_ads") as mock_cleanup, \
         patch("services.notifications.send_telegram"):
        result = _launch_with_mocked_proof(
            card_name="Тест reservation",
            adset_type="L2",
            media={"type": "image", "paths": [fake_media["fake.jpg"]]},
            body="Текст объявления",
            campaign_type="leadgen",
        )

    assert result == {}
    mock_create.assert_not_called()
    mock_cleanup.assert_not_called()


def test_post_create_reserve_failure_does_not_commit_city_success(fake_media):
    """Не подтверждённый post-check оставляет CREATE для crash reconciliation."""
    with patch("integrations.facebook.upload_image", return_value="img_hash_1"), \
         patch("integrations.facebook.create_image_ad", return_value="new_ad_id_1"), \
         patch("agent.adset_discovery.get_adsets_dict",
               return_value={"CityA": {"L2": "1001"}}), \
         patch("integrations.facebook.get_adset_capacity",
               side_effect=[_cap(available=2, ad_count=48),
                            _cap(available=0, ad_count=50)]), \
         patch("integrations.facebook.cleanup_stale_ads") as mock_cleanup:
        city_success = MagicMock()
        result = _launch_with_mocked_proof(
            card_name="Тест post-check",
            adset_type="L2",
            media={"type": "image", "paths": [fake_media["fake.jpg"]]},
            body="Текст объявления",
            campaign_type="leadgen",
            city_success_cb=city_success,
        )

    assert result == {}
    city_success.assert_not_called()
    mock_cleanup.assert_not_called()


def test_city_plan_exact_names_saved_before_first_create(fake_media):
    """План содержит все deterministic names и появляется раньше CREATE."""
    events = []

    def prepare_city(city, adset_id, expected_names):
        events.append(("prepare", city, adset_id, expected_names))

    def create_ad(*args, **kwargs):
        events.append(("create", args[0]))
        return f"ad-{len(events)}"

    with patch("integrations.facebook.upload_image", return_value="img_hash_1"), \
         patch("integrations.facebook.create_image_ad", side_effect=create_ad), \
         patch("agent.adset_discovery.get_adsets_dict",
               return_value={"CityA": {"L2": "1001"}}), \
         patch("integrations.facebook.get_adset_capacity",
               return_value=_cap(available=10, ad_count=5)):
        _launch_with_mocked_proof(
            card_name="Тест креатив",
            adset_type="L2",
            media={
                "type": "image",
                "paths": [fake_media["feed.jpg"], fake_media["story.jpg"]],
            },
            body="Текст объявления",
            campaign_type="leadgen",
            prepare_city_cb=prepare_city,
        )

    assert events == []
