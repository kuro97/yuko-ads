"""
Тест C2: sync_amo_data (integrations/amo.py) передаёт known_ad_ids (построенные из
ad_spends переданных ads) в match_leads_to_ads — матчинг по fb_ad_id доезжает до
/api/amo/sync (раньше матчил только по имени объявления).

Внешние границы (AMO API) мокаются: get_leads и match_leads_to_ads не должны реально
ходить в сеть (сеть глобально заблокирована pytest-socket в conftest.py).
"""

from unittest.mock import MagicMock, patch

from integrations import amo


# ---------------------------------------------------------------------------
# Happy path: known_ad_ids строится из ad_spends (ключи ads, приведённые к str)
# ---------------------------------------------------------------------------


@patch("integrations.amo.calc_ad_metrics")
@patch("integrations.amo.match_leads_to_ads")
@patch("integrations.amo.get_leads")
def test_sync_amo_data_passes_known_ad_ids(mock_get_leads, mock_match, mock_calc):
    """known_ad_ids, переданный в match_leads_to_ads, равен множеству ad_id/id из ads
    (строками) — иначе AMO-выгрузка матчит только по имени, теряя точный fb_ad_id-матч."""
    ads = [
        {"id": "111", "name": "CityA | L2 | Тест 1", "spend": 100.0},
        {"id": "222", "name": "CityB | L1 | Тест 2", "spend": 50.0},
        {"ad_id": "333", "name": "CityC | CR | Тест 3", "spend": 20.0},
    ]
    mock_get_leads.return_value = [{"id": 1, "fb_ad_id": "111"}]
    mock_match.return_value = {"111": [{"id": 1}]}
    mock_calc.return_value = {"111": {"payments": 1}}

    result = amo.sync_amo_data(ads, days=30)

    mock_get_leads.assert_called_once_with(30)
    assert mock_match.call_count == 1
    _, call_kwargs = mock_match.call_args
    known_ad_ids = call_kwargs["known_ad_ids"]

    # known_ad_ids — строковое множество ключей ad_spends (id или ad_id каждого объявления)
    assert known_ad_ids == {"111", "222", "333"}

    # fb_lookup тоже передан (существующее поведение матчинга по имени не отключено)
    assert "fb_lookup" in call_kwargs
    assert call_kwargs["fb_lookup"]["citya | l2 | тест 1"] == "111"

    # calc_ad_metrics вызван с результатом матчинга и ad_spends
    mock_calc.assert_called_once()
    calc_args, _ = mock_calc.call_args
    assert calc_args[0] == mock_match.return_value
    assert calc_args[1] == {"111": 100.0, "222": 50.0, "333": 20.0}

    assert result == {"111": {"payments": 1}}


@patch("integrations.amo.calc_ad_metrics")
@patch("integrations.amo.match_leads_to_ads")
@patch("integrations.amo.get_leads")
def test_sync_amo_data_matches_by_fb_ad_id(mock_get_leads, mock_match, mock_calc):
    """Лид с fb_ad_id, присутствующим в known_ad_ids, матчится по ad_id (не по имени) —
    здесь проверяем контракт вызова: match_leads_to_ads получает known_ad_ids непустым
    и именно с этим ad_id, чтобы matcher мог отдать приоритет точному fb_ad_id."""
    ads = [{"id": "555", "name": "Совершенно другое имя", "spend": 75.0}]
    mock_get_leads.return_value = [
        {"id": 42, "fb_ad_id": "555", "name_from_utm": "Не совпадает с именем рекламы"}
    ]
    mock_match.return_value = {"555": [{"id": 42}]}
    mock_calc.return_value = {"555": {"payments": 2}}

    amo.sync_amo_data(ads, days=7)

    _, call_kwargs = mock_match.call_args
    assert call_kwargs["known_ad_ids"] == {"555"}
    # Лиды переданы позиционным/именованным первым аргументом
    passed_leads = mock_match.call_args[0][0] if mock_match.call_args[0] else call_kwargs.get("leads")
    assert passed_leads == mock_get_leads.return_value


# ---------------------------------------------------------------------------
# Edge case: пустой список ads → known_ad_ids пустое множество, не падаем
# ---------------------------------------------------------------------------


@patch("integrations.amo.calc_ad_metrics")
@patch("integrations.amo.match_leads_to_ads")
@patch("integrations.amo.get_leads")
def test_sync_amo_data_empty_ads_gives_empty_known_ids(mock_get_leads, mock_match, mock_calc):
    """ads=[] → known_ad_ids пустое множество (не None, не падает), матч по имени
    выполняется как раньше (fb_lookup тоже пуст)."""
    mock_get_leads.return_value = []
    mock_match.return_value = {}
    mock_calc.return_value = {}

    result = amo.sync_amo_data([], days=30)

    _, call_kwargs = mock_match.call_args
    assert call_kwargs["known_ad_ids"] == set()
    assert call_kwargs["fb_lookup"] == {}
    assert result == {}


@patch("integrations.amo.calc_ad_metrics")
@patch("integrations.amo.match_leads_to_ads")
@patch("integrations.amo.get_leads")
def test_sync_amo_data_skips_ads_without_id(mock_get_leads, mock_match, mock_calc):
    """Объявление без 'id' и без 'ad_id' не попадает в known_ad_ids (falsy-ключ отфильтрован)."""
    ads = [
        {"name": "Без ID вообще", "spend": 10.0},
        {"id": "777", "name": "С ID", "spend": 5.0},
    ]
    mock_get_leads.return_value = []
    mock_match.return_value = {}
    mock_calc.return_value = {}

    amo.sync_amo_data(ads, days=30)

    _, call_kwargs = mock_match.call_args
    assert call_kwargs["known_ad_ids"] == {"777"}
