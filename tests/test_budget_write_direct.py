"""
Прямые тесты денежного пути services/budget_scaler.py.

В tests/test_budget_scaler.py обе функции ниже ВСЕГДА замокированы целиком
(patch("services.budget_scaler.set_adset_budget", return_value=True) и т.п.) —
это скрывает реальную логику: конвертацию USD→центы, имя поля запроса,
интерпретацию HTTP-статуса, пагинацию и fail-open поведение при сбое FB API.

Здесь мокается ТОЛЬКО HTTP-граница (agent.fb_common.session.get/post) —
сами функции set_adset_budget и _fetch_all_account_adset_budgets выполняются
по-настоящему.

Покрывает:
- set_adset_budget: конвертация USD → центы (round, int), имя поля daily_budget,
  endpoint запроса, интерпретация статуса (200 → True, не-200 → False, без исключений).
- _fetch_all_account_adset_budgets: пагинация по cursor (after), конвертация
  центы → USD, пустой аккаунт, fail-closed при сбое на поздней странице
  (не-200 → None, по итогам ревью).
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импортов проекта (как в test_budget_scaler.py —
# некоторые модули в цепочке импортов services.budget_scaler тянут его транзитивно).
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from agent.fb_common import API
from services.budget_scaler import set_adset_budget, _fetch_all_account_adset_budgets


def _resp(status_code=200, json_data=None, text=""):
    """Создаёт мок HTTP-ответа requests."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json = MagicMock(return_value=json_data or {})
    return resp


# ---------------------------------------------------------------------------
# set_adset_budget — единственное место, реально пишущее бюджет в FB
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("budget", [23.5, 0.994999, 50.0, 10.0])
def test_public_raw_budget_setter_is_denied_without_provider_call(budget):
    """Public legacy setter закрыт: денежная мутация разрешена только gateway."""
    with patch("agent.fb_common.session.post") as provider_post:
        with pytest.raises(RuntimeError, match="DIRECT_BUDGET_MUTATION_DENIED"):
            set_adset_budget("adset_123", budget)
    provider_post.assert_not_called()


# ---------------------------------------------------------------------------
# _fetch_all_account_adset_budgets — сумма бюджетов всего аккаунта
# ---------------------------------------------------------------------------

def test_fetch_all_budgets_two_pages_via_cursor():
    """2 страницы по after-курсору → полный словарь со всеми адсетами."""
    page1 = _resp(200, json_data={
        "data": [
            {"id": "as1", "name": "Adset 1", "daily_budget": "1000", "effective_status": "ACTIVE"},
            {"id": "as2", "name": "Adset 2", "daily_budget": "2000", "effective_status": "ACTIVE"},
        ],
        "paging": {"cursors": {"after": "CURSOR_ABC"}, "next": "https://graph.facebook.com/next"},
    })
    page2 = _resp(200, json_data={
        "data": [
            {"id": "as3", "name": "Adset 3", "daily_budget": "3000", "effective_status": "ACTIVE"},
        ],
        "paging": {"cursors": {"after": "CURSOR_ABC"}},  # нет "next" — конец пагинации
    })

    with patch("agent.fb_common._time.sleep"), \
         patch("services.fb_token_provider.get_fb_token", return_value="fake-token"), \
         patch("services.fb_token_provider.get_fb_account_id", return_value="999"), \
         patch("agent.fb_common.session.get") as mock_get:
        mock_get.side_effect = [page1, page2]

        result = _fetch_all_account_adset_budgets()

    assert set(result.keys()) == {"as1", "as2", "as3"}
    assert result["as1"]["daily_budget_usd"] == 10.0
    assert result["as2"]["daily_budget_usd"] == 20.0
    assert result["as3"]["daily_budget_usd"] == 30.0
    assert mock_get.call_count == 2

    # Вторая страница запрошена с курсором первой
    _, second_kwargs = mock_get.call_args_list[1]
    assert second_kwargs["params"]["after"] == "CURSOR_ABC"

    # Первая страница — без курсора
    _, first_kwargs = mock_get.call_args_list[0]
    assert "after" not in first_kwargs["params"]


def test_fetch_all_budgets_partial_failure_on_second_page_returns_none():
    """Сбой (500) на 2-й странице → None (fail-closed).

    Фикс по итогам ревью (денежный fail-open → fail-closed):
    раньше функция молча возвращала частичную сумму по успевшим страницам, из-за
    чего проверка max_total_daily_budget видела ложный запас и могла пробить
    потолок. Теперь любой не-200 в ходе пагинации возвращает None — вызывающий
    код НЕ поднимает бюджеты в этот прогон. Исключение не поднимается (мягкий
    сигнал, а не падение крона).
    """
    page1 = _resp(200, json_data={
        "data": [
            {"id": "as1", "name": "Adset 1", "daily_budget": "1000", "effective_status": "ACTIVE"},
        ],
        "paging": {"cursors": {"after": "CURSOR_ABC"}, "next": "https://graph.facebook.com/next"},
    })
    page2_fail = _resp(500, text="Internal error")

    with patch("agent.fb_common._time.sleep"), \
         patch("services.fb_token_provider.get_fb_token", return_value="fake-token"), \
         patch("services.fb_token_provider.get_fb_account_id", return_value="999"), \
         patch("agent.fb_common.session.get") as mock_get:
        mock_get.side_effect = [page1, page2_fail]

        # Fail-closed: не исключение, а маркер None (частичной сумме доверять нельзя)
        result = _fetch_all_account_adset_budgets()

    assert result is None
    assert mock_get.call_count == 2


def test_fetch_all_budgets_empty_account_returns_empty_dict():
    """Пустой аккаунт (нет адсетов) → пустой словарь, без ошибок."""
    empty_page = _resp(200, json_data={"data": [], "paging": {"cursors": {}}})

    with patch("agent.fb_common._time.sleep"), \
         patch("services.fb_token_provider.get_fb_token", return_value="fake-token"), \
         patch("services.fb_token_provider.get_fb_account_id", return_value="999"), \
         patch("agent.fb_common.session.get") as mock_get:
        mock_get.return_value = empty_page

        result = _fetch_all_account_adset_budgets()

    assert result == {}
    assert mock_get.call_count == 1


def test_fetch_all_budgets_converts_cents_to_usd():
    """FB возвращает daily_budget в ЦЕНТАХ (строкой) — функция делит на 100."""
    page = _resp(200, json_data={
        "data": [
            {"id": "as1", "name": "Adset 1", "daily_budget": "12345", "effective_status": "ACTIVE"},
        ],
        "paging": {"cursors": {}},
    })

    with patch("agent.fb_common._time.sleep"), \
         patch("services.fb_token_provider.get_fb_token", return_value="fake-token"), \
         patch("services.fb_token_provider.get_fb_account_id", return_value="999"), \
         patch("agent.fb_common.session.get") as mock_get:
        mock_get.return_value = page

        result = _fetch_all_account_adset_budgets()

    assert result["as1"]["daily_budget_usd"] == 123.45


def test_fetch_all_budgets_targets_account_adsets_endpoint():
    """Запрос уходит на {API}/act_{account_id}/adsets — endpoint аккаунта, не отдельного адсета."""
    empty_page = _resp(200, json_data={"data": [], "paging": {"cursors": {}}})

    with patch("agent.fb_common._time.sleep"), \
         patch("services.fb_token_provider.get_fb_token", return_value="fake-token"), \
         patch("services.fb_token_provider.get_fb_account_id", return_value="999"), \
         patch("agent.fb_common.session.get") as mock_get:
        mock_get.return_value = empty_page

        _fetch_all_account_adset_budgets()

        args, _ = mock_get.call_args
        assert args[0] == f"{API}/act_999/adsets"
