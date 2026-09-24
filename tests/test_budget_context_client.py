"""Юнит-тесты get_budget_context (services/cdp_client.py) — Шаг A.3 (T4).

Мокаем requests.get на границе HTTP — реальная сеть заблокирована
conftest._no_real_network (pytest_socket). Кеш сбрасываем в начале
каждого теста (_cache — модульный dict, переживает между тестами).

Проверяем: happy path (данные отдаются как есть, без фильтрации), city в
params, TTL 3600 (отдельный от get_daily_report — тот же кеш-словарь, но
свой ключ по path/params), HTML вместо JSON → CdpError, 401 → CdpError без
ретрая, таймаут → 1 ретрай, маскировка ключа.

Комментарии на русском.
"""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import cdp_client
from services.cdp_client import CdpError, get_budget_context, get_daily_report


@pytest.fixture(autouse=True)
def _clear_cdp_cache(monkeypatch):
    """Сбрасываем TTL-кеш клиента и ставим тестовый ключ перед каждым тестом."""
    cdp_client._cache.clear()
    monkeypatch.setenv("CDP_API_KEY", "cdp_test")
    yield
    cdp_client._cache.clear()


def _make_resp(status_code=200, json_data=None, content_type="application/json", text=""):
    """Хелпер: собирает мок-ответ requests.Response (как в test_cdp_client.py)."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"Content-Type": content_type}
    resp.json = MagicMock(return_value=json_data if json_data is not None else {})
    resp.text = text
    return resp


_SAMPLE_CTX = {
    "data_as_of": "2026-07-03T00:00:00Z",
    "month": "2026-07",
    "is_cold_start": False,
    "month_progress": {"day": 3, "days_in_month": 31, "expected_share": 0.0882},
    "engine_accuracy": [
        {"metric": "revenue_new", "target_month": "2026-06", "wape": 0.10},
    ],
    "cities": [
        {
            "city": "_total",
            "metrics": {
                "ad_spend": {
                    "plan": None,
                    "fact_mtd": 1000.0,
                    "expected_by_today": None,
                    "pace_vs_expected": None,
                    "forecast_eom": None,
                    "lo": None,
                    "hi": None,
                    "status": "no_plan",
                    "is_cost_metric": True,
                },
                "revenue_new": {
                    "plan": 100000000.0,
                    "fact_mtd": 6000000.0,
                    "expected_by_today": 10000000.0,
                    "pace_vs_expected": 0.6,
                    "forecast_eom": 120000000.0,
                    "lo": 100000000.0,
                    "hi": 140000000.0,
                    "status": "ahead",
                    "is_cost_metric": False,
                },
            },
        }
    ],
    "next_months": [],
    "semantics": {},
}


# --- happy path ---


@patch("services.cdp_client.requests.get")
def test_budget_context_happy_returns_payload_as_is(mock_get):
    """200 + application/json — payload отдаётся как есть, без фильтрации полей."""
    mock_get.return_value = _make_resp(json_data=_SAMPLE_CTX)

    result = get_budget_context()

    assert result == _SAMPLE_CTX
    assert mock_get.call_count == 1
    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["X-API-Key"] == "cdp_test"


@patch("services.cdp_client.requests.get")
def test_budget_context_empty_cities_is_valid(mock_get):
    """Пустой cities — валидный ответ, не ошибка, отдаётся как есть."""
    payload = {**_SAMPLE_CTX, "cities": []}
    mock_get.return_value = _make_resp(json_data=payload)

    result = get_budget_context()

    assert result["cities"] == []


# --- city-параметр ---


@patch("services.cdp_client.requests.get")
def test_city_param_goes_into_request(mock_get):
    """city='CityA' уходит в params запроса."""
    mock_get.return_value = _make_resp(json_data=_SAMPLE_CTX)

    get_budget_context(city="CityA")

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["city"] == "CityA"


@patch("services.cdp_client.requests.get")
def test_no_city_param_omitted_from_request(mock_get):
    """city не задан (None) — ключ 'city' отсутствует в params."""
    mock_get.return_value = _make_resp(json_data=_SAMPLE_CTX)

    get_budget_context()

    _, kwargs = mock_get.call_args
    assert "city" not in kwargs["params"]


# --- TTL 3600, независимость от TTL 600 у get_daily_report ---


@patch("services.cdp_client.requests.get")
def test_budget_context_ttl_cache_hits_on_repeat_call(mock_get):
    """Повторный вызов get_budget_context() без параметров — HTTP только 1 раз (TTL 3600)."""
    mock_get.return_value = _make_resp(json_data=_SAMPLE_CTX)

    get_budget_context()
    get_budget_context()

    assert mock_get.call_count == 1


@patch("services.cdp_client.requests.get")
def test_budget_context_ttl_is_3600_in_cache(mock_get):
    """Запись в кеше после вызова живёт ровно _BUDGET_CTX_TTL_SEC (3600), не 600."""
    mock_get.return_value = _make_resp(json_data=_SAMPLE_CTX)

    with patch("services.cdp_client.time.time", return_value=1_000_000.0):
        get_budget_context()

    cache_key = ("/analytics/budget-context", frozenset({}.items()))
    expires_at, cached_payload = cdp_client._cache[cache_key]

    assert expires_at == 1_000_000.0 + cdp_client._BUDGET_CTX_TTL_SEC
    assert cached_payload == _SAMPLE_CTX


@patch("services.cdp_client.requests.get")
def test_budget_context_does_not_share_cache_key_with_daily_report(mock_get):
    """get_budget_context и get_daily_report — разные пути в кеше, не пересекаются."""
    mock_get.side_effect = [
        _make_resp(json_data=_SAMPLE_CTX),
        _make_resp(json_data={"items": []}),
    ]

    get_budget_context()
    get_daily_report(date(2026, 7, 1), date(2026, 7, 2))

    assert mock_get.call_count == 2
    ctx_key = ("/analytics/budget-context", frozenset({}.items()))
    report_key = (
        "/analytics/daily-report",
        frozenset({"date_from": "2026-07-01", "date_to": "2026-07-02"}.items()),
    )
    assert ctx_key in cdp_client._cache
    assert report_key in cdp_client._cache


@patch("services.cdp_client.requests.get")
def test_daily_report_ttl_stays_600_after_budget_context_call(mock_get):
    """get_daily_report продолжает жить со своим TTL 600, budget-context (3600) его не меняет."""
    mock_get.side_effect = [
        _make_resp(json_data=_SAMPLE_CTX),
        _make_resp(json_data={"items": []}),
    ]

    with patch("services.cdp_client.time.time", return_value=2_000_000.0):
        get_budget_context()
        get_daily_report(date(2026, 7, 1), date(2026, 7, 2))

    report_key = (
        "/analytics/daily-report",
        frozenset({"date_from": "2026-07-01", "date_to": "2026-07-02"}.items()),
    )
    expires_at, _ = cdp_client._cache[report_key]
    assert expires_at == 2_000_000.0 + cdp_client._CACHE_TTL_SEC
    assert cdp_client._CACHE_TTL_SEC == 600
    assert cdp_client._BUDGET_CTX_TTL_SEC == 3600


# --- content-type / статусы ошибок ---


@patch("services.cdp_client.requests.get")
def test_html_instead_of_json_raises_cdp_error(mock_get):
    """200 + text/html (SPA-заглушка) — CdpError, .json() не вызывается."""
    resp = _make_resp(status_code=200, content_type="text/html; charset=utf-8", text="<html></html>")
    mock_get.return_value = resp

    with pytest.raises(CdpError):
        get_budget_context()

    resp.json.assert_not_called()


@patch("services.cdp_client.requests.get")
def test_401_raises_cdp_error_without_retry(mock_get):
    """401 — CdpError, ровно 1 вызов requests.get (без ретрая — не сетевая ошибка)."""
    mock_get.return_value = _make_resp(status_code=401, text="Unauthorized: key=cdp_test")

    with pytest.raises(CdpError) as excinfo:
        get_budget_context()

    assert mock_get.call_count == 1
    assert "cdp_test" not in str(excinfo.value)


# --- ретрай на сетевые ошибки ---


@patch("services.cdp_client.requests.get")
def test_timeout_retries_once_then_raises(mock_get):
    """Timeout дважды подряд — ровно 2 вызова requests.get, затем CdpError."""
    import requests as requests_module

    mock_get.side_effect = requests_module.exceptions.Timeout("read timed out")

    with pytest.raises(CdpError):
        get_budget_context()

    assert mock_get.call_count == 2


@patch("services.cdp_client.requests.get")
def test_timeout_retry_succeeds(mock_get):
    """1-й вызов — Timeout, 2-й — 200 JSON. Успех, ровно 2 вызова."""
    import requests as requests_module

    ok_resp = _make_resp(json_data=_SAMPLE_CTX)
    mock_get.side_effect = [requests_module.exceptions.Timeout("read timed out"), ok_resp]

    result = get_budget_context()

    assert result == _SAMPLE_CTX
    assert mock_get.call_count == 2


# --- маскировка ключа ---


@patch("services.cdp_client.requests.get")
def test_key_masked_in_error(mock_get):
    """Ключ, случайно попавший в тело ответа 500, маскируется в тексте исключения."""
    mock_get.return_value = _make_resp(
        status_code=500, text="Internal Server Error, key used: cdp_test"
    )

    with pytest.raises(CdpError) as excinfo:
        get_budget_context()

    assert "cdp_test" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


# --- отсутствие ключа ---


def test_missing_key_raises(monkeypatch):
    """CDP_API_KEY не задан — CdpError с понятным текстом."""
    monkeypatch.delenv("CDP_API_KEY", raising=False)

    with pytest.raises(CdpError, match="не задан"):
        get_budget_context()
