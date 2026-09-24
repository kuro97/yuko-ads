"""Юнит-тесты клиента CDP Acme (services/cdp_client.py).

Мокаем requests.get на границе HTTP — реальная сеть заблокирована
conftest._no_real_network (pytest_socket). Кеш сбрасываем в начале
каждого теста (_cache — модульный dict, переживает между тестами).

Комментарии на русском.
"""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import cdp_client
from services.cdp_client import CdpError, get_daily_report, get_plan_fact_summary


@pytest.fixture(autouse=True)
def _clear_cdp_cache(monkeypatch):
    """Сбрасываем TTL-кеш клиента и ставим тестовый ключ перед каждым тестом."""
    cdp_client._cache.clear()
    monkeypatch.setenv("CDP_API_KEY", "cdp_test")
    yield
    cdp_client._cache.clear()


def _make_resp(status_code=200, json_data=None, content_type="application/json", text=""):
    """Хелпер: собирает мок-ответ requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"Content-Type": content_type}
    resp.json = MagicMock(return_value=json_data if json_data is not None else {})
    resp.text = text
    return resp


# --- get_daily_report: happy path ---


@patch("services.cdp_client.requests.get")
def test_daily_report_happy(mock_get):
    """200 + application/json, items внутри окна — возвращает список как есть."""
    items = [
        {
            "report_date": "2026-07-01",
            "city": "CityA",
            "usd_rate": 100.0,
            "ad_spend": 100.0,
            "revenue_new": 100000.0,
            "drr_new": 4.12,
        },
        {
            "report_date": "2026-07-02",
            "city": "CityB",
            "usd_rate": 105.0,
            "ad_spend": 50.0,
            "revenue_new": 60000.0,
            "drr_new": 3.0,
        },
    ]
    mock_get.return_value = _make_resp(json_data={"items": items})

    result = get_daily_report(date(2026, 7, 1), date(2026, 7, 2))

    assert result == items
    assert mock_get.call_count == 1
    # проверяем что URL/params ушли корректно
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["date_from"] == "2026-07-01"
    assert kwargs["params"]["date_to"] == "2026-07-02"
    assert kwargs["headers"]["X-API-Key"] == "cdp_test"


@patch("services.cdp_client.requests.get")
def test_daily_report_filters_out_of_window(mock_get):
    """Элементы с report_date вне [date_from, date_to] отбрасываются."""
    items = [
        {"report_date": "2026-06-30", "city": "CityA", "usd_rate": 100.0,
         "ad_spend": 10.0, "revenue_new": 1000.0, "drr_new": 1.0},  # до окна
        {"report_date": "2026-07-01", "city": "CityA", "usd_rate": 100.0,
         "ad_spend": 20.0, "revenue_new": 2000.0, "drr_new": 1.0},  # в окне
        {"report_date": "2026-07-03", "city": "CityA", "usd_rate": 100.0,
         "ad_spend": 30.0, "revenue_new": 3000.0, "drr_new": 1.0},  # после окна
    ]
    mock_get.return_value = _make_resp(json_data={"items": items})

    result = get_daily_report(date(2026, 7, 1), date(2026, 7, 2))

    assert len(result) == 1
    assert result[0]["report_date"] == "2026-07-01"


# --- content-type / статусы ошибок ---


@patch("services.cdp_client.requests.get")
def test_html_instead_of_json_raises(mock_get):
    """200 + text/html (SPA-заглушка) — CdpError, .json() НЕ вызывается."""
    resp = _make_resp(status_code=200, content_type="text/html; charset=utf-8", text="<html></html>")
    mock_get.return_value = resp

    with pytest.raises(CdpError):
        get_daily_report(date(2026, 7, 1), date(2026, 7, 2))

    resp.json.assert_not_called()


@patch("services.cdp_client.requests.get")
def test_401_raises_cdp_error(mock_get):
    """401 — CdpError, ключ в тексте ошибки не светится."""
    mock_get.return_value = _make_resp(status_code=401, text="Unauthorized: key=cdp_test")

    with pytest.raises(CdpError) as excinfo:
        get_daily_report(date(2026, 7, 1), date(2026, 7, 2))

    assert "cdp_test" not in str(excinfo.value)
    # 401 — не сетевая ошибка, ретрая быть не должно
    assert mock_get.call_count == 1


@patch("services.cdp_client.requests.get")
def test_422_raises_cdp_error(mock_get):
    """422 (например, неверный формат month) — CdpError без ретрая."""
    mock_get.return_value = _make_resp(status_code=422, text="Unprocessable Entity")

    with pytest.raises(CdpError):
        get_plan_fact_summary(date(2026, 7, 15))

    assert mock_get.call_count == 1


# --- ретрай на сетевые ошибки ---


@patch("services.cdp_client.requests.get")
def test_timeout_retries_once_then_raises(mock_get):
    """Timeout дважды подряд — ровно 2 вызова requests.get, затем CdpError."""
    import requests as requests_module

    mock_get.side_effect = requests_module.exceptions.Timeout("read timed out")

    with pytest.raises(CdpError):
        get_daily_report(date(2026, 7, 1), date(2026, 7, 2))

    assert mock_get.call_count == 2


@patch("services.cdp_client.requests.get")
def test_timeout_retry_succeeds(mock_get):
    """1-й вызов — Timeout, 2-й — 200 JSON. Успех, ровно 2 вызова."""
    import requests as requests_module

    ok_resp = _make_resp(json_data={"items": []})
    mock_get.side_effect = [requests_module.exceptions.Timeout("read timed out"), ok_resp]

    result = get_daily_report(date(2026, 7, 1), date(2026, 7, 2))

    assert result == []
    assert mock_get.call_count == 2


# --- отсутствие ключа ---


def test_missing_key_raises(monkeypatch):
    """CDP_API_KEY не задан — CdpError с понятным текстом, без падения процесса."""
    monkeypatch.delenv("CDP_API_KEY", raising=False)

    with pytest.raises(CdpError, match="не задан"):
        get_daily_report(date(2026, 7, 1), date(2026, 7, 2))


# --- TTL-кеш ---


@patch("services.cdp_client.requests.get")
def test_ttl_cache_hits(mock_get):
    """Повторный вызов с теми же params — requests.get вызван только 1 раз."""
    mock_get.return_value = _make_resp(json_data={"items": []})

    get_daily_report(date(2026, 7, 1), date(2026, 7, 2))
    get_daily_report(date(2026, 7, 1), date(2026, 7, 2))

    assert mock_get.call_count == 1


@patch("services.cdp_client.requests.get")
def test_ttl_cache_different_params_makes_new_request(mock_get):
    """Разные params (другое окно) — новый HTTP-запрос, не кеш-хит."""
    mock_get.return_value = _make_resp(json_data={"items": []})

    get_daily_report(date(2026, 7, 1), date(2026, 7, 2))
    get_daily_report(date(2026, 7, 3), date(2026, 7, 4))

    assert mock_get.call_count == 2


# --- маскировка ключа ---


@patch("services.cdp_client.requests.get")
def test_key_masked_in_error(mock_get):
    """Ключ, случайно попавший в тело ответа 500, маскируется в тексте исключения."""
    mock_get.return_value = _make_resp(
        status_code=500, text="Internal Server Error, key used: cdp_test"
    )

    with pytest.raises(CdpError) as excinfo:
        get_daily_report(date(2026, 7, 1), date(2026, 7, 2))

    assert "cdp_test" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


# --- month → YYYY-MM-01 ---


@patch("services.cdp_client.requests.get")
def test_plan_fact_month_full_date(mock_get):
    """month=date(2026,7,15) должен уйти в params как полная дата '2026-07-01'."""
    mock_get.return_value = _make_resp(json_data={"month": "2026-07-01", "cities": []})

    get_plan_fact_summary(date(2026, 7, 15))

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["month"] == "2026-07-01"


# --- пустой items ---


@patch("services.cdp_client.requests.get")
def test_empty_items_returns_list(mock_get):
    """200 + {"items": []} — валидный пустой результат, не ошибка."""
    mock_get.return_value = _make_resp(json_data={"items": []})

    result = get_daily_report(date(2026, 7, 1), date(2026, 7, 2))

    assert result == []


# --- 429: лимит запросов чужого сервиса — ждём, а не падаем ---


@patch("services.cdp_client.time.sleep")
@patch("services.cdp_client.requests.get")
def test_rate_limit_429_is_awaited_not_fatal(mock_get, mock_sleep):
    """429 → выдержка и повтор. Иначе длинная пагинация теряет все данные.

    Длинный бэкфилл когорт упирается ровно в это: один 429 посреди
    выгрузки платежей — и выручка всех недель остаётся пустой.
    """
    mock_get.side_effect = [
        _make_resp(status_code=429, text='{"detail":"Rate limit exceeded"}'),
        _make_resp(json_data={"items": []}),
    ]

    payload = cdp_client._request("/revenue", {"page": 1})

    assert payload == {"items": []}
    assert mock_get.call_count == 2
    assert mock_sleep.called, "после 429 обязана быть выдержка"


@patch("services.cdp_client.time.sleep")
@patch("services.cdp_client.requests.get")
def test_rate_limit_gives_up_after_retries(mock_get, mock_sleep):
    """Лимит не проходит после всех попыток → честная ошибка, не пустой ответ."""
    mock_get.return_value = _make_resp(
        status_code=429, text='{"detail":"Rate limit exceeded"}'
    )

    with pytest.raises(CdpError) as exc:
        cdp_client._request("/revenue", {"page": 1})

    assert "429" in str(exc.value)
    # Конкретное число, а не формула от той же константы: иначе тест прошёл бы
    # и при _RATE_LIMIT_RETRIES = 0, то есть при полностью выключенных ретраях.
    assert cdp_client._RATE_LIMIT_RETRIES == 4
    assert mock_get.call_count == 5


@patch("services.cdp_client.time.sleep")
@patch("services.cdp_client.requests.get")
def test_rate_limit_respects_retry_after_header(mock_get, mock_sleep):
    """Заголовок Retry-After сервера уважаем — ждём столько, сколько просят."""
    throttled = _make_resp(status_code=429, text="{}")
    throttled.headers = {"Content-Type": "application/json", "Retry-After": "7"}
    mock_get.side_effect = [throttled, _make_resp(json_data={"ok": True})]

    cdp_client._request("/revenue", {"page": 1})

    mock_sleep.assert_called_once_with(7.0)


@patch("services.cdp_client.time.sleep")
@patch("services.cdp_client.requests.get")
def test_rate_limit_ignores_absurd_retry_after(mock_get, mock_sleep):
    """Мусор и абсурд в Retry-After не подвешивают прогон на сутки."""
    throttled = _make_resp(status_code=429, text="{}")
    throttled.headers = {"Content-Type": "application/json", "Retry-After": "86400"}
    mock_get.side_effect = [throttled, _make_resp(json_data={"ok": True})]

    cdp_client._request("/revenue", {"page": 1})

    waited = mock_sleep.call_args[0][0]
    assert waited <= cdp_client._RATE_LIMIT_MAX_SLEEP_SEC


@patch("services.cdp_client.time.sleep")
@patch("services.cdp_client.requests.get")
def test_other_http_errors_are_not_retried(mock_get, mock_sleep):
    """500 — это отказ, а не «подожди»: повторять нельзя, ошибка сразу."""
    mock_get.return_value = _make_resp(status_code=500, text="boom")

    with pytest.raises(CdpError):
        cdp_client._request("/revenue", {"page": 1})

    assert mock_get.call_count == 1
    assert not mock_sleep.called


@patch("services.cdp_client.time.sleep")
@patch("services.cdp_client.requests.get")
def test_total_rate_limit_sleep_is_bounded(mock_get, mock_sleep):
    """Суммарное ожидание на один вызов ограничено бюджетом.

    Иначе выдержки CDP съедали бы 20-минутный таймаут бюджет-скейлера
    (web/app.py: _SCALER_TIMEOUT_SEC), а брошенный поток продолжал бы писать
    бюджеты уже вне контроля крона.
    """
    throttled = _make_resp(status_code=429, text="{}")
    throttled.headers = {"Content-Type": "application/json", "Retry-After": "30"}
    mock_get.return_value = throttled

    with pytest.raises(CdpError):
        cdp_client._request("/revenue", {"page": 1})

    total_slept = sum(call[0][0] for call in mock_sleep.call_args_list)
    assert total_slept <= cdp_client._RATE_LIMIT_TOTAL_SLEEP_SEC


@patch("services.cdp_client.time.sleep")
@patch("services.cdp_client.requests.get")
def test_rate_limit_is_awaited_in_metadata_path_too(mock_get, mock_sleep):
    """429 переживается и во втором пути запроса — том, которым ходит проверка.

    _request_with_meta обслуживает строгое чтение платежей (approval_source_cdp),
    и без симметричной правки денежные факты падали бы там, где обычный путь
    уже умеет ждать.
    """
    mock_get.side_effect = [
        _make_resp(status_code=429, text='{"detail":"Rate limit exceeded"}'),
        _make_resp(json_data={"items": [], "total": 0}),
    ]

    response = cdp_client._request_with_meta("/revenue", {"page": 1})

    assert response.status_code == 200
    assert mock_get.call_count == 2
    assert mock_sleep.called


# --- строгое чтение платежей: полнота выгрузки, а не привязка к сделке ---


def test_strict_validity_ignores_internal_cashflow():
    """Внутренний кэшфлоу без договора не делает страницу битой.

    Замер показал, что большинство записей на странице — expense/transfer
    с пустым contract_number. Требование договора к ним
    объявляло всю выгрузку неполной, и get_payments_strict возвращал ноль
    записей на ЛЮБОМ окне: проверка денежных фактов не могла подтвердить
    ни одного отчёта.
    """
    transfer = {
        "id": 1001,
        "contract_number": "",
        "direction": "transfer",
        "doc_date": "2026-06-30",
        "amount": 100000.0,
    }
    assert cdp_client._strict_payment_is_valid(transfer) is True


def test_strict_validity_allows_income_without_contract():
    """Приход без номера договора — не битая запись, а непривязываемая.

    В ERP такие встречаются (например, приход 100 000 ¤ без договора). Ронять из-за них
    полноту выгрузки нельзя — привязка это забота вызывающего.
    """
    orphan_income = {
        "id": 1002,
        "contract_number": "",
        "direction": "income",
        "doc_date": "2026-03-03",
        "amount": 100000.0,
    }
    assert cdp_client._strict_payment_is_valid(orphan_income) is True


def test_strict_validity_still_rejects_broken_records():
    """Структурно битые записи по-прежнему валят полноту."""
    base = {
        "id": 1,
        "contract_number": "12345",
        "direction": "income",
        "doc_date": "2026-06-01",
        "amount": 100.0,
    }
    assert cdp_client._strict_payment_is_valid(base) is True
    assert cdp_client._strict_payment_is_valid({**base, "id": None}) is False
    assert cdp_client._strict_payment_is_valid({**base, "direction": "wat"}) is False
    assert cdp_client._strict_payment_is_valid({**base, "doc_date": "нет"}) is False
    assert cdp_client._strict_payment_is_valid({**base, "amount": "abc"}) is False
    assert cdp_client._strict_payment_is_valid({**base, "amount": -5}) is False
    assert cdp_client._strict_payment_is_valid("не словарь") is False


@patch("services.cdp_client.time.sleep")
@patch("services.cdp_client.requests.get")
def test_metadata_path_sleep_is_bounded_too(mock_get, mock_sleep):
    """Потолок ожидания действует и на пути строгого чтения.

    Именно им ходят деньги: get_payments_strict → _request_with_meta. Первая
    версия потолка завела там переменную и забыла её передать — путь спал
    120с при объявленных 45.
    """
    throttled = _make_resp(status_code=429, text="{}")
    throttled.headers = {"Content-Type": "application/json", "Retry-After": "30"}
    mock_get.return_value = throttled

    with pytest.raises(CdpError):
        cdp_client._request_with_meta("/revenue", {"page": 1})

    total_slept = sum(call[0][0] for call in mock_sleep.call_args_list)
    assert total_slept <= cdp_client._RATE_LIMIT_TOTAL_SLEEP_SEC


@patch("services.cdp_client.time.sleep")
@patch("services.cdp_client.requests.get")
def test_both_paths_sleep_the_same(mock_get, mock_sleep):
    """Оба пути запроса ждут одинаково — расхождение уже стоило нам дыры."""
    throttled = _make_resp(status_code=429, text="{}")
    throttled.headers = {"Content-Type": "application/json"}

    slept_by_path = {}
    for name, call in (
        ("plain", lambda: cdp_client._request("/revenue", {"page": 1})),
        ("meta", lambda: cdp_client._request_with_meta("/revenue", {"page": 2})),
    ):
        mock_get.reset_mock()
        mock_sleep.reset_mock()
        mock_get.return_value = throttled
        with pytest.raises(CdpError):
            call()
        slept_by_path[name] = sum(c[0][0] for c in mock_sleep.call_args_list)

    assert slept_by_path["plain"] == slept_by_path["meta"]
    assert slept_by_path["plain"] <= cdp_client._RATE_LIMIT_TOTAL_SLEEP_SEC
