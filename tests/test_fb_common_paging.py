"""
Тесты адаптивного page_limit в get_all_ads (fb_common.py).

Сценарии:
1. limit=500 → reduce data → limit=250 → OK с cursor → следующая страница OK → итог: все объявления.
2. Всё ок с первого раза → один запрос на страницу, без снижения limit.
3. Даже limit=10 отбит → FBApiError.
4. Кэш: после понижения до 125 повторный вызов стартует со 125, а не 500.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _make_response(status_code: int, body: dict) -> MagicMock:
    """Создаёт мок HTTP-ответа."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.text = json.dumps(body)
    mock.ok = status_code == 200
    mock.json.return_value = body
    return mock


def _reduce_data_response() -> MagicMock:
    """Ответ FB с ошибкой 'reduce the amount of data' (code 1)."""
    return _make_response(400, {
        "error": {
            "code": 1,
            "message": "Please reduce the amount of data you're asking for, then retry your request",
            "type": "OAuthException",
        }
    })


def _ads_page(ad_ids: list[str], next_cursor: str | None = None) -> MagicMock:
    """Ответ с объявлениями и опциональным cursor для пагинации."""
    paging: dict = {}
    if next_cursor:
        paging = {
            "cursors": {"after": next_cursor, "before": "bbb"},
            "next": f"https://graph.facebook.com/v21.0/act_123/ads?after={next_cursor}",
        }
    body = {
        "data": [
            {
                "id": ad_id,
                "name": f"Реклама {ad_id}",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
                "created_time": "2026-01-01T00:00:00+0000",
                "adset_id": "adset_1",
            }
            for ad_id in ad_ids
        ],
        "paging": paging,
    }
    return _make_response(200, body)


# Карта адсетов — adset_1 принадлежит нашему кабинету
_ADSET_MAP = {"adset_1": ("city_test", "type_test")}


@pytest.fixture(autouse=True)
def reset_page_limit_cache():
    """Сбрасываем кэш page_limit перед каждым тестом — изоляция между тестами."""
    import agent.fb_common as fb_common
    fb_common._last_good_page_limit["value"] = 500
    yield
    fb_common._last_good_page_limit["value"] = 500


# ---------------------------------------------------------------------------
# Тест 1: reduce data → снижаем limit → пагинация по cursor → итог: 4 объявления
# ---------------------------------------------------------------------------

def test_reduce_data_then_ok_with_cursor(caplog):
    """
    Запрос #1 (limit=500): 400 reduce data.
    Запрос #2 (limit=250): 200, 2 объявления, cursor=cursor_X.
    Запрос #3 (limit=250, after=cursor_X): 200, 2 объявления, нет next.
    Итог: 4 объявления собраны.
    """
    import logging

    resp_reduce = _reduce_data_response()
    resp_page1 = _ads_page(["ad_1", "ad_2"], next_cursor="cursor_X")
    resp_page2 = _ads_page(["ad_3", "ad_4"])

    side_effects = [resp_reduce, resp_page1, resp_page2]

    with patch("agent.fb_common._throttled_get", side_effect=side_effects) as mock_get, \
         patch("agent.fb_common.get_fb_token", return_value="test_token"), \
         patch("agent.fb_common.get_fb_account_id", return_value="123"), \
         caplog.at_level(logging.WARNING, logger="agent.fb_common"):

        from agent.fb_common import get_all_ads
        result = get_all_ads(_ADSET_MAP)

    # Все 4 объявления в результате
    assert len(result) == 4
    assert "ad_1" in result
    assert "ad_2" in result
    assert "ad_3" in result
    assert "ad_4" in result

    # Предупреждение о снижении limit
    assert any("reduce data" in rec.message for rec in caplog.records)
    assert any("250" in rec.message for rec in caplog.records)

    # Проверяем что запросы шли с правильными параметрами
    calls = mock_get.call_args_list
    assert len(calls) == 3

    # Запрос #1: limit=500, без after
    params1 = calls[0][1].get("params") or calls[0][0][1] if len(calls[0][0]) > 1 else calls[0][1]["params"]
    assert params1["limit"] == 500
    assert "after" not in params1

    # Запрос #2: limit=250 (сниженный), без after (та же страница)
    params2 = calls[1][1]["params"]
    assert params2["limit"] == 250
    assert "after" not in params2

    # Запрос #3: limit=250, after=cursor_X
    params3 = calls[2][1]["params"]
    assert params3["limit"] == 250
    assert params3["after"] == "cursor_X"


# ---------------------------------------------------------------------------
# Тест 2: всё ок с первого раза → один запрос, limit не меняется
# ---------------------------------------------------------------------------

def test_no_reduce_data_single_request():
    """Первый запрос 200 → только один вызов _throttled_get."""
    resp_ok = _ads_page(["ad_10", "ad_20"])

    with patch("agent.fb_common._throttled_get", return_value=resp_ok) as mock_get, \
         patch("agent.fb_common.get_fb_token", return_value="test_token"), \
         patch("agent.fb_common.get_fb_account_id", return_value="123"):

        from agent.fb_common import get_all_ads
        result = get_all_ads(_ADSET_MAP)

    assert len(result) == 2
    assert "ad_10" in result
    assert "ad_20" in result

    # Ровно один запрос
    assert mock_get.call_count == 1

    # limit не изменился — остался 500
    params = mock_get.call_args[1]["params"]
    assert params["limit"] == 500


# ---------------------------------------------------------------------------
# Тест 3: даже limit=10 отбит → FBApiError
# ---------------------------------------------------------------------------

def test_reduce_data_even_at_min_limit_raises():
    """
    Все попытки (500 → 250 → 125 → 62 → 31 → 15 → 7) — reduce data.
    Когда limit упал ниже 10, бросаем FBApiError.
    """
    # Все ответы — reduce data
    reduce_resp = _reduce_data_response()

    with patch("agent.fb_common._throttled_get", return_value=reduce_resp), \
         patch("agent.fb_common.get_fb_token", return_value="test_token"), \
         patch("agent.fb_common.get_fb_account_id", return_value="123"):

        from agent.fb_common import get_all_ads, FBApiError

        with pytest.raises(FBApiError):
            get_all_ads(_ADSET_MAP)


# ---------------------------------------------------------------------------
# Тест 4: кэш page_limit — повторный вызов стартует с последнего успешного limit
# ---------------------------------------------------------------------------

def test_cache_starts_with_last_good_limit():
    """
    Первый вызов: 500 → reduce → 250 → reduce → 125 → OK (кэш=125).
    Второй вызов: должен стартовать сразу с 125, не с 500.
    """
    import agent.fb_common as fb_common
    from agent.fb_common import get_all_ads

    # Первый вызов: понижаем до 125
    side_effects_call1 = [
        _reduce_data_response(),   # 500 — отбит
        _reduce_data_response(),   # 250 — отбит
        _ads_page(["ad_A"]),       # 125 — успех
    ]

    with patch("agent.fb_common._throttled_get", side_effect=side_effects_call1), \
         patch("agent.fb_common.get_fb_token", return_value="test_token"), \
         patch("agent.fb_common.get_fb_account_id", return_value="123"):
        get_all_ads(_ADSET_MAP)

    # После первого вызова кэш должен быть 125
    assert fb_common._last_good_page_limit["value"] == 125

    # Второй вызов: первый же запрос должен идти с limit=125
    resp_second = _ads_page(["ad_B"])
    with patch("agent.fb_common._throttled_get", return_value=resp_second) as mock_get2, \
         patch("agent.fb_common.get_fb_token", return_value="test_token"), \
         patch("agent.fb_common.get_fb_account_id", return_value="123"):
        get_all_ads(_ADSET_MAP)

    first_call_params = mock_get2.call_args_list[0][1]["params"]
    assert first_call_params["limit"] == 125, (
        f"Ожидали limit=125 (из кэша), получили {first_call_params['limit']}"
    )
