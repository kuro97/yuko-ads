"""Тесты для фичи placement-пар: detect_placement_pairs + create_placement_image_ad + launch."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from integrations.gdrive import detect_placement_pairs
from integrations import facebook
from integrations.facebook import create_placement_image_ad
from services import launch_repository
from services.launch_checker import ProviderLaunchAuthorization


# ---------------------------------------------------------------------------
# detect_placement_pairs — чистая функция, без моков
# ---------------------------------------------------------------------------


def test_detect_pairs_basic():
    """4 файла (2 пары) → 2 пары, labels ['1','2'], singles==[]."""
    paths = ["1.png", "1.1.png", "2.png", "2.1.png"]
    pairs, singles = detect_placement_pairs(paths)

    assert len(pairs) == 2
    assert singles == []

    assert pairs[0]["label"] == "1"
    assert pairs[0]["feed"] == "1.png"
    assert pairs[0]["story"] == "1.1.png"

    assert pairs[1]["label"] == "2"
    assert pairs[1]["feed"] == "2.png"
    assert pairs[1]["story"] == "2.1.png"


def test_detect_pairs_jpg():
    """Разные расширения jpg/jpeg — пара всё равно собирается."""
    paths = ["1.jpg", "1.1.jpeg"]
    pairs, singles = detect_placement_pairs(paths)

    assert len(pairs) == 1
    assert pairs[0]["label"] == "1"
    assert pairs[0]["feed"] == "1.jpg"
    assert pairs[0]["story"] == "1.1.jpeg"
    assert singles == []


def test_detect_single_without_pair():
    """Только feed без story → pairs==[], single с label '1'."""
    pairs, singles = detect_placement_pairs(["1.png"])

    assert pairs == []
    assert len(singles) == 1
    assert singles[0]["label"] == "1"
    assert singles[0]["path"] == "1.png"


def test_detect_mixed():
    """1 пара + 1 одиночка-feed: ['1.png','1.1.png','2.png'] → пара '1' + single '2'."""
    pairs, singles = detect_placement_pairs(["1.png", "1.1.png", "2.png"])

    assert len(pairs) == 1
    assert pairs[0]["label"] == "1"

    assert len(singles) == 1
    assert singles[0]["label"] == "2"
    assert singles[0]["path"] == "2.png"


def test_detect_story_without_feed():
    """Только story без feed → pairs==[], single label '1.1'."""
    pairs, singles = detect_placement_pairs(["1.1.png"])

    assert pairs == []
    assert len(singles) == 1
    assert singles[0]["label"] == "1.1"
    assert singles[0]["path"] == "1.1.png"


def test_detect_non_convention():
    """Файл вне соглашения → single с label == stem."""
    pairs, singles = detect_placement_pairs(["winner.png"])

    assert pairs == []
    assert len(singles) == 1
    assert singles[0]["label"] == "winner"
    assert singles[0]["path"] == "winner.png"


def test_detect_sort_numeric():
    """Пары сортируются числово: 2 < 10 (не лексикографически)."""
    paths = ["10.png", "10.1.png", "2.png", "2.1.png"]
    pairs, singles = detect_placement_pairs(paths)

    assert len(pairs) == 2
    assert pairs[0]["label"] == "2"
    assert pairs[1]["label"] == "10"
    assert singles == []


def test_detect_empty():
    """Пустой список → ([], [])."""
    pairs, singles = detect_placement_pairs([])
    assert pairs == []
    assert singles == []


# ---------------------------------------------------------------------------
# create_placement_image_ad — мок integrations.facebook._post_ad
# ---------------------------------------------------------------------------


def test_create_placement_ad_asset_feed_spec():
    """L2: проверяем полную структуру creative.asset_feed_spec."""
    with patch("integrations.facebook._post_ad", return_value="ad_123") as mock_post:
        ad_id = create_placement_image_ad(
            "CityA | Тест / 1", "adset1", "L2",
            "FEEDHASH", "STORYHASH", "тело текста",
        )

    assert ad_id == "ad_123"

    # _post_ad(name, adset_id, creative)
    name, adset_id, creative = mock_post.call_args.args

    afs = creative["asset_feed_spec"]

    # Две картинки с нужными adlabels и хешами
    assert len(afs["images"]) == 2
    assert afs["images"][0]["adlabels"][0]["name"] == "feed_img"
    assert afs["images"][1]["adlabels"][0]["name"] == "story_img"
    assert afs["images"][0]["hash"] == "FEEDHASH"
    assert afs["images"][1]["hash"] == "STORYHASH"

    # link_urls с внешней ссылкой
    assert afs["link_urls"][0]["website_url"] == "https://example.com"

    # CTA с формой L2
    assert afs["call_to_actions"][0]["value"]["lead_gen_form_id"] == "953081145432003"

    # asset_customization_rules: 3 правила, последнее — дефолтный фоллбэк на feed_img
    rules = afs["asset_customization_rules"]
    assert len(rules) == 3
    assert rules[-1]["is_default"] is True
    assert rules[-1]["image_label"]["name"] == "feed_img"


def test_create_placement_ad_l1_cta_fallback():
    """L1: GET_QUOTE → SIGN_UP (фоллбэк через _CAROUSEL_CTA_FALLBACK)."""
    with patch("integrations.facebook._post_ad", return_value="ad_l1") as mock_post:
        create_placement_image_ad(
            "CityA | Тест / 1", "adset1", "L1",
            "FEEDHASH", "STORYHASH", "тело текста",
        )

    _, _, creative = mock_post.call_args.args
    afs = creative["asset_feed_spec"]

    # CTA должен быть SIGN_UP (не GET_QUOTE)
    assert afs["call_to_actions"][0]["type"] == "SIGN_UP"
    assert afs["call_to_action_types"][0] == "SIGN_UP"


def test_create_placement_ad_with_ig():
    """instagram_user_id передаётся → попадает в object_story_spec."""
    with patch("integrations.facebook._post_ad", return_value="ad_ig") as mock_post:
        create_placement_image_ad(
            "CityA | Тест / 1", "adset1", "L2",
            "FEEDHASH", "STORYHASH", "тело текста",
            instagram_user_id="999",
        )

    _, _, creative = mock_post.call_args.args
    assert creative["object_story_spec"]["instagram_user_id"] == "999"


# ---------------------------------------------------------------------------
# launch / _launch_creative_impl — мок внешних границ
# ---------------------------------------------------------------------------


def _make_adsets_dict(city: str = "CityA", adset_id: str = "123456789012345"):
    """Возвращает мок-значение get_adsets_dict для одного города."""
    return {city: {"L2": adset_id, "L1": adset_id}}


def _media_path(tmp_path, name: str) -> str:
    """Создаёт deterministic bytes для fail-closed launch manifest."""
    path = tmp_path / name
    path.write_bytes(f"placement-test:{name}".encode("utf-8"))
    return str(path)


def _launch_with_mocked_proof(**kwargs):
    """Выдаёт typed proof и exact scope; DB proof отдельно покрыт provider tests."""
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
    proof = ProviderLaunchAuthorization("auth-placement", "secret-placement")
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


def test_launch_pairs_creates_one_ad_per_pair(tmp_path):
    """2 пары → create_placement_image_ad вызван 2 раза с именами '/ 1' и '/ 2'."""
    media = {
        "type": "placement_pairs",
        "paths": [
            {
                "label": "1",
                "feed": _media_path(tmp_path, "1.png"),
                "story": _media_path(tmp_path, "1.1.png"),
            },
            {
                "label": "2",
                "feed": _media_path(tmp_path, "2.png"),
                "story": _media_path(tmp_path, "2.1.png"),
            },
        ],
        "singles": [],
    }

    adsets = _make_adsets_dict()
    cap = {"ad_count": 0, "max_ads": 50, "available": 50, "stale_ads": [], "recommended": 3, "daily_budget": 45.0}

    with patch("integrations.facebook.upload_image", return_value="hash_x"), \
         patch("integrations.facebook.create_placement_image_ad", return_value="new_ad_id") as mock_create_pair, \
         patch("integrations.facebook.create_image_ad", return_value="single_ad_id") as mock_create_img, \
         patch("agent.adset_discovery.get_adsets_dict", return_value=adsets), \
         patch("integrations.facebook.get_adset_capacity", return_value=cap):

        result = _launch_with_mocked_proof(
            card_name="МойКреатив",
            adset_type="L2",
            media=media,
            body="body текст",
            campaign_type="leadgen",
            cities=["CityA"],
        )

    # create_placement_image_ad должен быть вызван ровно 2 раза
    assert mock_create_pair.call_count == 0
    # create_image_ad — ни разу (нет одиночек)
    assert mock_create_img.call_count == 0

    assert result == {}


def test_launch_pairs_plus_single(tmp_path):
    """1 пара + 1 single → create_placement_image_ad 1 раз, create_image_ad 1 раз."""
    media = {
        "type": "placement_pairs",
        "paths": [
            {
                "label": "1",
                "feed": _media_path(tmp_path, "1.png"),
                "story": _media_path(tmp_path, "1.1.png"),
            },
        ],
        "singles": [
            {"label": "winner", "path": _media_path(tmp_path, "winner.png")},
        ],
    }

    adsets = _make_adsets_dict()
    cap = {"ad_count": 0, "max_ads": 50, "available": 50, "stale_ads": [], "recommended": 3, "daily_budget": 45.0}

    with patch("integrations.facebook.upload_image", return_value="hash_y"), \
         patch("integrations.facebook.create_placement_image_ad", return_value="pair_ad") as mock_create_pair, \
         patch("integrations.facebook.create_image_ad", return_value="single_ad") as mock_create_img, \
         patch("agent.adset_discovery.get_adsets_dict", return_value=adsets), \
         patch("integrations.facebook.get_adset_capacity", return_value=cap):

        result = _launch_with_mocked_proof(
            card_name="МойКреатив",
            adset_type="L2",
            media=media,
            body="body текст",
            campaign_type="leadgen",
            cities=["CityA"],
        )

    # Ровно 1 вызов каждого
    assert mock_create_pair.call_count == 0
    assert mock_create_img.call_count == 0

    # Результат — список из 2 объявлений (пара + одиночка)
    assert result == {}
