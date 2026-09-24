"""Юнит-тесты get_payments (services/cdp_client.py) — ARCH-cdp-payments, T5.

Мокаем requests.get на границе HTTP — реальная сеть заблокирована
conftest._no_real_network (pytest_socket). Кеш сбрасываем в начале
каждого теста (_cache — модульный dict, переживает между тестами).

Проверяем: пагинация до конца (в т.ч. стоп на пустой странице при лживом
total — защита от бесконечного цикла), эксклюзивный date_to (запрос
to+1, фильтр doc_date на клиенте), отсев expense/transfer (всегда) и
фильтр по direction, пустой/HTML/401 ответы, TTL-кеш 1800 (свой,
не пересекается с daily-report/budget-context), отсутствие ключа
(маскировка), ключ ответа items ИЛИ data (алиас).

Комментарии на русском.
"""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import cdp_client
from services.cdp_client import CdpError, get_payments


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


def _payment(pid, deal_id=1, amount=10000.0, direction="income", doc_date="2026-06-15"):
    """Хелпер: собирает минимальную запись платежа ERP."""
    return {
        "id": pid,
        "deal_id": deal_id,
        "contact_id": 500,
        "client_id": 900,
        "amount": amount,
        "direction": direction,
        "doc_date": doc_date,
        "synced_at": "2026-06-15T20:00:00Z",
    }


# --- happy path: одна страница ---


@patch("services.cdp_client.requests.get")
def test_payments_happy_single_page(mock_get):
    """200 JSON, total=2, 2 income в окне — 2 записи, поля на месте."""
    items = [
        _payment(1, deal_id=111, amount=50000.0, doc_date="2026-06-10"),
        _payment(2, deal_id=222, amount=75000.0, doc_date="2026-06-20"),
    ]
    mock_get.return_value = _make_resp(json_data={"items": items, "total": 2})

    result = get_payments(date(2026, 6, 1), date(2026, 6, 30))

    assert result == items
    assert mock_get.call_count == 1
    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["X-API-Key"] == "cdp_test"
    assert kwargs["params"]["page"] == 1
    assert kwargs["params"]["page_size"] == 100


# --- пагинация до конца ---


@patch("services.cdp_client.requests.get")
def test_payments_paginates_to_end(mock_get):
    """total=250, 3 страницы по 100/100/50 — 250 записей, requests.get вызван 3 раза."""
    page1 = [_payment(i, deal_id=1) for i in range(1, 101)]
    page2 = [_payment(i, deal_id=1) for i in range(101, 201)]
    page3 = [_payment(i, deal_id=1) for i in range(201, 251)]
    mock_get.side_effect = [
        _make_resp(json_data={"items": page1, "total": 250}),
        _make_resp(json_data={"items": page2, "total": 250}),
        _make_resp(json_data={"items": page3, "total": 250}),
    ]

    result = get_payments(date(2026, 6, 1), date(2026, 6, 30))

    assert len(result) == 250
    assert mock_get.call_count == 3
    pages_requested = [call.kwargs["params"]["page"] for call in mock_get.call_args_list]
    assert pages_requested == [1, 2, 3]


# --- стоп на пустой странице (защита от бесконечности) ---


@patch("services.cdp_client.requests.get")
def test_payments_stops_on_empty_page(mock_get):
    """total=999 (врёт), 2-я страница пустая — стоп, без бесконечного цикла."""
    page1 = [_payment(i, deal_id=1) for i in range(1, 101)]
    mock_get.side_effect = [
        _make_resp(json_data={"items": page1, "total": 999}),
        _make_resp(json_data={"items": [], "total": 999}),
    ]

    result = get_payments(date(2026, 6, 1), date(2026, 6, 30))

    assert len(result) == 100
    # ровно 2 вызова — на пустой странице цикл остановился, а не продолжил до total
    assert mock_get.call_count == 2


# --- эксклюзивный date_to ---


@patch("services.cdp_client.requests.get")
def test_payments_exclusive_date_to_requests_plus_one(mock_get):
    """Окно 06-01..06-30 — в params запроса date_to == '2026-07-01' (эксклюзивный +1 день)."""
    mock_get.return_value = _make_resp(json_data={"items": [], "total": 0})

    get_payments(date(2026, 6, 1), date(2026, 6, 30))

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["date_from"] == "2026-06-01"
    assert kwargs["params"]["date_to"] == "2026-07-01"


@patch("services.cdp_client.requests.get")
def test_payments_filters_by_doc_date_window(mock_get):
    """Платежи с doc_date 06-30 и 07-01 — 07-01 отброшен (вне окна [06-01, 06-30])."""
    items = [
        _payment(1, deal_id=1, doc_date="2026-06-30"),  # в окне (включительно)
        _payment(2, deal_id=1, doc_date="2026-07-01"),  # вне окна — сервер отдал лишнее
    ]
    mock_get.return_value = _make_resp(json_data={"items": items, "total": 2})

    result = get_payments(date(2026, 6, 1), date(2026, 6, 30))

    assert len(result) == 1
    assert result[0]["id"] == 1
    assert result[0]["doc_date"] == "2026-06-30"


# --- отсев expense/transfer, фильтр direction ---


@patch("services.cdp_client.requests.get")
def test_payments_ignores_expense_transfer(mock_get):
    """Смесь income/refund/expense/transfer — остались только income+refund."""
    items = [
        _payment(1, direction="income"),
        _payment(2, direction="refund"),
        _payment(3, direction="expense"),
        _payment(4, direction="transfer"),
    ]
    mock_get.return_value = _make_resp(json_data={"items": items, "total": 4})

    result = get_payments(date(2026, 6, 1), date(2026, 6, 30))

    directions = {p["id"]: p["direction"] for p in result}
    assert directions == {1: "income", 2: "refund"}


@patch("services.cdp_client.requests.get")
def test_payments_direction_filter(mock_get):
    """direction='income', смесь income/refund — только income."""
    items = [
        _payment(1, direction="income"),
        _payment(2, direction="refund"),
    ]
    mock_get.return_value = _make_resp(json_data={"items": items, "total": 2})

    result = get_payments(date(2026, 6, 1), date(2026, 6, 30), direction="income")

    assert len(result) == 1
    assert result[0]["id"] == 1
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["direction"] == "income"


@patch("services.cdp_client.requests.get")
def test_payments_direction_none_omits_param(mock_get):
    """direction не задан (None) — ключ 'direction' отсутствует в params запроса."""
    mock_get.return_value = _make_resp(json_data={"items": [], "total": 0})

    get_payments(date(2026, 6, 1), date(2026, 6, 30))

    _, kwargs = mock_get.call_args
    assert "direction" not in kwargs["params"]


# --- пустой список ---


@patch("services.cdp_client.requests.get")
def test_payments_empty_returns_list(mock_get):
    """total=0, items=[] — валидный пустой результат."""
    mock_get.return_value = _make_resp(json_data={"items": [], "total": 0})

    result = get_payments(date(2026, 6, 1), date(2026, 6, 30))

    assert result == []


# --- HTML вместо JSON ---


@patch("services.cdp_client.requests.get")
def test_payments_html_instead_of_json_raises(mock_get):
    """200 + text/html (SPA-заглушка) — CdpError, .json() не вызывается."""
    resp = _make_resp(status_code=200, content_type="text/html; charset=utf-8", text="<html></html>")
    mock_get.return_value = resp

    with pytest.raises(CdpError):
        get_payments(date(2026, 6, 1), date(2026, 6, 30))

    resp.json.assert_not_called()


# --- 401 без ретрая ---


@patch("services.cdp_client.requests.get")
def test_payments_401_raises(mock_get):
    """401 — CdpError, ключ не в тексте ошибки, ровно 1 вызов (без ретрая)."""
    mock_get.return_value = _make_resp(status_code=401, text="Unauthorized: key=cdp_test")

    with pytest.raises(CdpError) as excinfo:
        get_payments(date(2026, 6, 1), date(2026, 6, 30))

    assert "cdp_test" not in str(excinfo.value)
    assert mock_get.call_count == 1


# --- таймаут → 1 ретрай ---


@patch("services.cdp_client.requests.get")
def test_payments_timeout_retries_once_then_raises(mock_get):
    """Timeout дважды подряд — ровно 2 вызова requests.get, затем CdpError."""
    import requests as requests_module

    mock_get.side_effect = requests_module.exceptions.Timeout("read timed out")

    with pytest.raises(CdpError):
        get_payments(date(2026, 6, 1), date(2026, 6, 30))

    assert mock_get.call_count == 2


@patch("services.cdp_client.requests.get")
def test_payments_timeout_retry_succeeds(mock_get):
    """1-й вызов — Timeout, 2-й — 200 JSON. Успех, ровно 2 вызова."""
    import requests as requests_module

    ok_resp = _make_resp(json_data={"items": [], "total": 0})
    mock_get.side_effect = [requests_module.exceptions.Timeout("read timed out"), ok_resp]

    result = get_payments(date(2026, 6, 1), date(2026, 6, 30))

    assert result == []
    assert mock_get.call_count == 2


# --- маскировка ключа ---


@patch("services.cdp_client.requests.get")
def test_payments_key_masked_in_error(mock_get):
    """Ключ, случайно попавший в тело ответа 500, маскируется в тексте исключения."""
    mock_get.return_value = _make_resp(
        status_code=500, text="Internal Server Error, key used: cdp_test"
    )

    with pytest.raises(CdpError) as excinfo:
        get_payments(date(2026, 6, 1), date(2026, 6, 30))

    assert "cdp_test" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


# --- TTL-кеш 1800, свой ключ (не пересекается с daily-report/budget-context) ---


@patch("services.cdp_client.requests.get")
def test_payments_ttl_cache_hits(mock_get):
    """2 одинаковых запроса — requests.get за 1 страницу вызван 1 раз."""
    mock_get.return_value = _make_resp(json_data={"items": [], "total": 0})

    get_payments(date(2026, 6, 1), date(2026, 6, 30))
    get_payments(date(2026, 6, 1), date(2026, 6, 30))

    assert mock_get.call_count == 1


@patch("services.cdp_client.requests.get")
def test_payments_ttl_is_1800_in_cache(mock_get):
    """Запись в кеше после вызова живёт ровно _PAYMENTS_TTL_SEC (1800), не 600/3600."""
    mock_get.return_value = _make_resp(json_data={"items": [], "total": 0})

    with patch("services.cdp_client.time.time", return_value=1_000_000.0):
        get_payments(date(2026, 6, 1), date(2026, 6, 30))

    cache_key = (
        "/revenue",
        frozenset(
            {
                "date_from": "2026-06-01",
                "date_to": "2026-07-01",
                "page": 1,
                "page_size": 100,
            }.items()
        ),
    )
    expires_at, cached_payload = cdp_client._cache[cache_key]

    assert expires_at == 1_000_000.0 + cdp_client._PAYMENTS_TTL_SEC
    assert cdp_client._PAYMENTS_TTL_SEC == 1800
    assert cached_payload == {"items": [], "total": 0}


@patch("services.cdp_client.requests.get")
def test_payments_cache_key_does_not_collide_with_daily_report_or_budget_context(mock_get):
    """Ключ кеша /revenue не пересекается с /analytics/daily-report и /analytics/budget-context."""
    from services.cdp_client import get_budget_context, get_daily_report

    mock_get.side_effect = [
        _make_resp(json_data={"items": [], "total": 0}),  # get_payments
        _make_resp(json_data={"items": []}),  # get_daily_report
        _make_resp(json_data={"cities": []}),  # get_budget_context
    ]

    with patch("services.cdp_client.time.time", return_value=3_000_000.0):
        get_payments(date(2026, 6, 1), date(2026, 6, 30))
        get_daily_report(date(2026, 6, 1), date(2026, 6, 30))
        get_budget_context()

    # 3 разных пути — 3 отдельных HTTP-вызова, ни один не попал в чужой кеш-хит
    assert mock_get.call_count == 3
    payments_key = (
        "/revenue",
        frozenset(
            {
                "date_from": "2026-06-01",
                "date_to": "2026-07-01",
                "page": 1,
                "page_size": 100,
            }.items()
        ),
    )
    daily_key = (
        "/analytics/daily-report",
        frozenset({"date_from": "2026-06-01", "date_to": "2026-06-30"}.items()),
    )
    budget_key = ("/analytics/budget-context", frozenset({}.items()))
    assert payments_key in cdp_client._cache
    assert daily_key in cdp_client._cache
    assert budget_key in cdp_client._cache
    # и TTL у каждой записи свой (время запросов зафиксировано patch'ем — сравнение детерминированное)
    assert cdp_client._cache[payments_key][0] - cdp_client._cache[daily_key][0] == (
        cdp_client._PAYMENTS_TTL_SEC - cdp_client._CACHE_TTL_SEC
    )


# --- отсутствие ключа ---


def test_payments_missing_key_raises(monkeypatch):
    """CDP_API_KEY не задан — CdpError с понятным текстом «не задан»."""
    monkeypatch.delenv("CDP_API_KEY", raising=False)

    with pytest.raises(CdpError, match="не задан"):
        get_payments(date(2026, 6, 1), date(2026, 6, 30))


# --- ключ ответа items ИЛИ data (алиас) ---


@patch("services.cdp_client.requests.get")
def test_payments_data_key_alias(mock_get):
    """Ответ с ключом 'data' вместо 'items' — распарсен корректно."""
    items = [_payment(1, deal_id=1, doc_date="2026-06-15")]
    mock_get.return_value = _make_resp(json_data={"data": items, "total": 1})

    result = get_payments(date(2026, 6, 1), date(2026, 6, 30))

    assert result == items
