"""
Тесты деградации insights-запроса на чанки по кампаниям.

Сценарии:
1. Полный запрос успешен → чанки НЕ вызываются.
2. Полный запрос упал с code 1 "reduce data" → запрашиваем кампании,
   потом для каждой кампании insights-чанк → итог: все объявления собраны.
3. Один чанк упал → остальные собраны, logger.warning вызван.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_response(status_code: int, body: dict) -> MagicMock:
    """Создаёт мок HTTP-ответа."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.text = json.dumps(body)
    mock.ok = status_code == 200
    mock.json.return_value = body
    return mock


def _insights_rows(campaign_suffix: str) -> list[dict]:
    """Генерирует две строки insights для одной кампании."""
    return [
        {
            "ad_id": f"ad_{campaign_suffix}_1",
            "spend": "100.00",
            "impressions": "1000",
            "clicks": "50",
            "ctr": "5.0",
            "cpm": "100.0",
            "actions": [{"action_type": "lead", "value": "3"}],
        },
        {
            "ad_id": f"ad_{campaign_suffix}_2",
            "spend": "200.00",
            "impressions": "2000",
            "clicks": "80",
            "ctr": "4.0",
            "cpm": "100.0",
            "actions": [],
        },
    ]


def _reduce_data_response() -> MagicMock:
    """Ответ FB с ошибкой 'reduce the amount of data' (code 1)."""
    return _make_response(400, {
        "error": {
            "code": 1,
            "message": "Please reduce the amount of data you're asking for, then retry your request",
            "type": "OAuthException",
        }
    })


# ---------------------------------------------------------------------------
# Тест 1: полный запрос успешен — чанки НЕ вызываются
# ---------------------------------------------------------------------------

def test_full_request_success_no_chunking():
    """Полный запрос вернул 200 → _get_account_campaigns НЕ вызывается."""
    full_rows = _insights_rows("camp1") + _insights_rows("camp2")
    full_resp = _make_response(200, {"data": full_rows, "paging": {}})

    with patch("agent.analyzer._throttled_get", return_value=full_resp) as mock_get, \
         patch("agent.analyzer.get_fb_token", return_value="tok"), \
         patch("agent.analyzer.get_fb_account_id", return_value="123"), \
         patch("agent.analyzer._get_account_campaigns") as mock_campaigns:

        from agent.analyzer import _get_account_insights
        result = _get_account_insights("2026-06-01", "2026-06-07")

    # Кампании не должны запрашиваться
    mock_campaigns.assert_not_called()
    # Все 4 объявления в результате
    assert len(result) == 4
    assert "ad_camp1_1" in result
    assert "ad_camp2_2" in result


# ---------------------------------------------------------------------------
# Тест 2: полный запрос упал code 1 → чанки по кампаниям → итог 4 объявления
# ---------------------------------------------------------------------------

def test_chunked_on_reduce_data_error():
    """Полный запрос → code 1, переходим на чанки: 2 кампании × 2 объявления = 4."""
    reduce_resp = _reduce_data_response()
    camp_resp = _make_response(200, {
        "data": [{"id": "cmp1", "name": "Кампания 1"}, {"id": "cmp2", "name": "Кампания 2"}],
        "paging": {},
    })
    chunk1_resp = _make_response(200, {"data": _insights_rows("c1"), "paging": {}})
    chunk2_resp = _make_response(200, {"data": _insights_rows("c2"), "paging": {}})

    # Порядок вызовов _throttled_get:
    # 1-й: полный insights-запрос → reduce_data
    # 2-й: campaigns-запрос → 2 кампании
    # 3-й: insights chunk cmp1 → 2 строки
    # 4-й: insights chunk cmp2 → 2 строки
    call_responses = [reduce_resp, camp_resp, chunk1_resp, chunk2_resp]

    with patch("agent.analyzer._throttled_get", side_effect=call_responses), \
         patch("agent.analyzer.get_fb_token", return_value="tok"), \
         patch("agent.analyzer.get_fb_account_id", return_value="123"), \
         patch("agent.analyzer._campaigns_cache", {}):

        from agent.analyzer import _get_account_insights
        result = _get_account_insights("2026-06-01", "2026-06-07")

    assert len(result) == 4
    assert "ad_c1_1" in result
    assert "ad_c1_2" in result
    assert "ad_c2_1" in result
    assert "ad_c2_2" in result
    # Проверяем что лиды распарсились корректно
    assert result["ad_c1_1"]["leads"] == 3
    assert result["ad_c2_1"]["leads"] == 3


def test_chunked_logs_warning_on_reduce_data(caplog):
    """При деградации на чанки выводится logger.warning с количеством кампаний."""
    import logging
    reduce_resp = _reduce_data_response()
    camp_resp = _make_response(200, {
        "data": [{"id": "cmp1", "name": "Кампания 1"}],
        "paging": {},
    })
    chunk_resp = _make_response(200, {"data": _insights_rows("cx"), "paging": {}})

    with patch("agent.analyzer._throttled_get", side_effect=[reduce_resp, camp_resp, chunk_resp]), \
         patch("agent.analyzer.get_fb_token", return_value="tok"), \
         patch("agent.analyzer.get_fb_account_id", return_value="123"), \
         patch("agent.analyzer._campaigns_cache", {}), \
         caplog.at_level(logging.WARNING, logger="agent.analyzer"):

        from agent.analyzer import _get_account_insights
        _get_account_insights("2026-06-01", "2026-06-07")

    assert any("reduce data" in rec.message for rec in caplog.records)
    assert any("1 кампаний" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Тест 3: один чанк упал → остальные собраны, warning залоггирован
# ---------------------------------------------------------------------------

def test_failed_chunk_skipped_others_collected(caplog):
    """Чанк cmp2 упал с другой ошибкой → пропускаем, cmp1 и cmp3 собраны."""
    import logging
    reduce_resp = _reduce_data_response()
    camp_resp = _make_response(200, {
        "data": [
            {"id": "cmp1", "name": "Кампания 1"},
            {"id": "cmp2", "name": "Кампания 2"},
            {"id": "cmp3", "name": "Кампания 3"},
        ],
        "paging": {},
    })
    chunk1_resp = _make_response(200, {"data": _insights_rows("c1"), "paging": {}})
    # Чанк cmp2 — тоже "reduce data" (код 1), FBApiError будет выброшен
    chunk2_fail = _reduce_data_response()
    chunk3_resp = _make_response(200, {"data": _insights_rows("c3"), "paging": {}})

    call_responses = [reduce_resp, camp_resp, chunk1_resp, chunk2_fail, chunk3_resp]

    with patch("agent.analyzer._throttled_get", side_effect=call_responses), \
         patch("agent.analyzer.get_fb_token", return_value="tok"), \
         patch("agent.analyzer.get_fb_account_id", return_value="123"), \
         patch("agent.analyzer._campaigns_cache", {}), \
         caplog.at_level(logging.WARNING, logger="agent.analyzer"):

        from agent.analyzer import _get_account_insights
        result = _get_account_insights("2026-06-01", "2026-06-07")

    # cmp1 и cmp3 — по 2 объявления, cmp2 — пропущен
    assert len(result) == 4
    assert "ad_c1_1" in result
    assert "ad_c3_1" in result
    # Предупреждение об упавшем чанке
    assert any("cmp2" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Тест 4: кеш кампаний работает — повторный вызов не идёт в FB
# ---------------------------------------------------------------------------

def test_campaigns_cache_prevents_double_request():
    """При повторном вызове _get_account_campaigns из кеша — FB не дёргается."""
    import time as _time
    from agent.analyzer import _campaigns_cache, _CAMPAIGNS_TTL

    account_id = "acc_cache_test"
    # Заполняем кеш
    _campaigns_cache[account_id] = {
        "data": [{"id": "c1", "name": "Test"}],
        "ts": _time.time(),  # свежий кеш
    }

    with patch("agent.analyzer._throttled_get") as mock_get, \
         patch("agent.analyzer.get_fb_token", return_value="tok"), \
         patch("agent.analyzer.get_fb_account_id", return_value=account_id):

        from agent.analyzer import _get_account_campaigns
        campaigns = _get_account_campaigns()

    # FB не вызывался — данные из кеша
    mock_get.assert_not_called()
    assert len(campaigns) == 1
    assert campaigns[0]["id"] == "c1"

    # Чистим кеш чтобы не влиять на другие тесты
    _campaigns_cache.pop(account_id, None)
