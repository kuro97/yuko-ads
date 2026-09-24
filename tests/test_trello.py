"""Тесты утилит Trello интеграции (чистые функции без API)."""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from integrations.trello import detect_language, get_card_body


# --- detect_language ---

def test_detect_language_default_l1():
    """Без маркера второго языка — L1 адсеты (язык по умолчанию)."""
    assert detect_language("Петров / Тема А", "Текст без маркера") == "L1"


def test_detect_language_l2_tag_in_desc():
    """Явный тег [L2] в описании — L2 адсеты."""
    assert detect_language("Сидорова Ирина", "[L2] Leave your number — we will call you back.") == "L2"


def test_detect_language_l2_marker_in_name():
    """Маркер-токен l2 в названии карточки — L2 адсеты."""
    assert detect_language("Автор / Новая тема / l2", "Обычный текст") == "L2"


def test_detect_language_mixed():
    """Смешанный текст — если есть хоть один тег [L2], то L2."""
    assert detect_language("Тест", "Обычный текст и [L2] вставка") == "L2"


def test_detect_language_empty():
    """Пустые строки — L1 по умолчанию."""
    assert detect_language("", "") == "L1"


# --- get_card_body ---

def test_get_card_body_with_instagram():
    """Тело объявления — текст после ссылки Instagram."""
    desc = "https://instagram.com/acme\nНужна консультация?\nОставьте номер."
    body = get_card_body(desc)
    assert "Нужна консультация?" in body
    assert "Оставьте номер." in body
    assert "instagram" not in body.lower()


def test_get_card_body_no_instagram():
    """Без Instagram ссылки — весь текст."""
    desc = "Просто текст без ссылки"
    body = get_card_body(desc)
    assert body == desc


def test_get_card_body_empty():
    """Пустое описание."""
    assert get_card_body("") == ""


def test_get_card_body_only_instagram():
    """Только Instagram ссылка без текста после."""
    desc = "https://instagram.com/acme"
    body = get_card_body(desc)
    assert body == desc  # нет текста после ссылки — возвращает всё


# --- get_unlaunched_cards: порядок и fail-closed snapshot ---


@patch("integrations.trello.session.get")
def test_get_unlaunched_cards_sorts_by_pos_then_id(mock_get):
    """Верхние карточки Trello идут первыми, равные позиции стабильны по id."""
    from integrations.trello import get_unlaunched_cards

    mock_get.return_value = MagicMock(
        json=lambda: [
            {
                "id": "card-z",
                "name": "Третья",
                "dueComplete": False,
                "closed": False,
                "pos": 20,
                "labels": [],
            },
            {
                "id": "card-b",
                "name": "Вторая",
                "dueComplete": False,
                "closed": False,
                "pos": 10.0,
                "labels": [],
            },
            {
                "id": "card-a",
                "name": "Первая",
                "dueComplete": False,
                "closed": False,
                "pos": 10,
                "labels": [],
            },
            {
                "id": "card-complete",
                "name": "Завершённая",
                "dueComplete": True,
                "closed": False,
                "pos": 1,
                "labels": [],
            },
            {
                "id": "card-closed",
                "name": "Закрытая",
                "dueComplete": False,
                "closed": True,
                "pos": 2,
                "labels": [],
            },
        ],
    )
    mock_get.return_value.raise_for_status = lambda: None

    cards = get_unlaunched_cards("ready-list")

    assert [card["id"] for card in cards] == ["card-a", "card-b", "card-z"]
    assert all(type(card["pos"]) is float for card in cards)
    assert mock_get.call_args.kwargs["params"]["fields"] == (
        "id,name,desc,dueComplete,due,idAttachmentCover,labels,"
        "pos,closed,shortLink"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        ["not-a-card"],
        [{"id": "", "name": "Имя", "dueComplete": False, "closed": False, "pos": 1}],
        [{"id": "card", "name": None, "dueComplete": False, "closed": False, "pos": 1}],
        [{"id": "card", "name": "Имя", "dueComplete": False, "closed": False, "pos": True}],
        [{"id": "card", "name": "Имя", "dueComplete": False, "closed": False, "pos": float("nan")}],
        [{"id": "card", "name": "Имя", "dueComplete": False, "closed": False, "pos": float("inf")}],
        [{"id": "card", "name": "Имя", "dueComplete": 0, "closed": False, "pos": 1}],
        [{"id": "card", "name": "Имя", "dueComplete": False, "closed": 0, "pos": 1}],
    ],
)
@patch("integrations.trello.session.get")
def test_get_unlaunched_cards_rejects_invalid_snapshot(mock_get, payload):
    """Одна битая provider-карточка блокирует снимок целиком."""
    from integrations.trello import TrelloError, get_unlaunched_cards

    mock_get.return_value = MagicMock(json=lambda: payload)
    mock_get.return_value.raise_for_status = lambda: None

    with pytest.raises(TrelloError, match="Trello request failed"):
        get_unlaunched_cards("ready-list")


@patch("integrations.trello.session.get")
def test_get_unlaunched_cards_does_not_return_partial_snapshot(mock_get):
    """Валидная первая карточка не возвращается, если следующая карточка битая."""
    from integrations.trello import TrelloError, get_unlaunched_cards

    mock_get.return_value = MagicMock(
        json=lambda: [
            {
                "id": "valid",
                "name": "Валидная",
                "dueComplete": False,
                "closed": False,
                "pos": 1,
                "labels": [],
            },
            {
                "id": "broken",
                "name": "Битая",
                "dueComplete": False,
                "closed": False,
                "pos": "top",
                "labels": [],
            },
        ]
    )
    mock_get.return_value.raise_for_status = lambda: None

    with pytest.raises(TrelloError):
        get_unlaunched_cards("ready-list")


# --- publish_hypotheses ---


@patch("integrations.trello.session.post")
@patch("integrations.trello.session.get")
def test_publish_hypotheses_creates_cards(mock_get, mock_post):
    """Публикация гипотез — создаёт карточки в Trello."""
    from integrations.trello import publish_hypotheses

    # Мок: колонка "Гипотезы" уже существует
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [{"name": "Готово", "id": "list1"}, {"name": "Гипотезы", "id": "list_hypo"}],
    )
    mock_get.return_value.raise_for_status = lambda: None

    # Мок: создание карточки
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"id": "card1", "url": "https://trello.com/c/card1"},
    )
    mock_post.return_value.raise_for_status = lambda: None

    hypotheses = [
        {"title": "Масштабировать Winner", "description": "ROMI 300%", "type": "scale", "priority": "high"},
        {"title": "Отключить Dead", "description": "0 лидов", "type": "reduce", "priority": "medium"},
    ]
    result = publish_hypotheses(hypotheses)
    assert len(result) == 2
    assert result[0]["title"] == "Масштабировать Winner"
    assert mock_post.call_count == 2


@patch("integrations.trello.session.post")
@patch("integrations.trello.session.get")
def test_publish_hypotheses_creates_list_if_missing(mock_get, mock_post):
    """Если колонки 'Гипотезы' нет — создаёт её."""
    from integrations.trello import get_or_create_list

    # Мок: колонки "Гипотезы" нет
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [{"name": "Готово", "id": "list1"}],
    )
    mock_get.return_value.raise_for_status = lambda: None

    # Мок: создание колонки
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"id": "new_list"},
    )
    mock_post.return_value.raise_for_status = lambda: None

    result = get_or_create_list("Гипотезы")
    assert result == "new_list"
    assert mock_post.called


def test_publish_hypotheses_empty():
    """Пустой список — ничего не публикуется, нет запросов."""
    from integrations.trello import publish_hypotheses
    result = publish_hypotheses([])
    assert result == []


# --- create_card ---


@patch("integrations.trello.session.post")
def test_create_card_long_desc_uses_data_not_params(mock_post):
    """Длинный desc (>2000 символов, типичный сценарий рекламного текста)
    должен уходить в тело запроса (data=), а не в query string (params=),
    иначе Trello отвечает 414 Request-URI Too Large."""
    from integrations.trello import create_card

    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"id": "card123", "url": "https://trello.com/c/card123"},
    )
    mock_post.return_value.raise_for_status = lambda: None

    long_desc = "Текст рекламного сценария на кириллице. " * 100  # > 2000 символов
    result = create_card("list_id_1", "Тестовая карточка", long_desc)

    assert result["id"] == "card123"
    assert mock_post.called
    _, kwargs = mock_post.call_args
    assert "data" in kwargs
    assert kwargs["data"]["desc"] == long_desc
    assert kwargs["data"]["idList"] == "list_id_1"
    assert kwargs["data"]["name"] == "Тестовая карточка"
    # desc НЕ должен попадать в params (query string) — иначе 414
    assert "params" not in kwargs or "desc" not in (kwargs.get("params") or {})


@patch("integrations.trello.session.post")
def test_create_card_returns_response_json(mock_post):
    """create_card возвращает JSON ответа Trello без изменений."""
    from integrations.trello import create_card

    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"id": "card456", "url": "https://trello.com/c/card456", "name": "Карточка"},
    )
    mock_post.return_value.raise_for_status = lambda: None

    result = create_card("list_id_2", "Карточка", "короткое описание")

    assert result == {"id": "card456", "url": "https://trello.com/c/card456", "name": "Карточка"}


@patch("integrations.trello.session.post")
def test_create_card_no_product_no_idlabels(mock_post):
    """product не передан (дефолт None) — idLabels в запросе нет, доп. GET
    к меткам доски НЕ делается (проверяем на mock_post, GET не мокан вовсе)."""
    from integrations.trello import create_card

    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"id": "card1", "url": "https://trello.com/c/card1"},
    )
    mock_post.return_value.raise_for_status = lambda: None

    create_card("list_id_1", "Карточка без продукта", "desc")

    _, kwargs = mock_post.call_args
    assert "idLabels" not in kwargs["data"]


# --- create_card с продуктовой Trello-меткой (ARCH-product-tags.md) ---


@pytest.fixture(autouse=True)
def _clear_product_label_cache():
    """Сбрасывает кеш меток продукта между тестами — иначе тест N+1 может
    получить закешированный id из мока теста N (см. _product_label_cache)."""
    import integrations.trello as trello_module
    trello_module._product_label_cache.clear()
    yield
    trello_module._product_label_cache.clear()


@patch("integrations.trello.session.post")
@patch("integrations.trello.session.get")
def test_create_card_with_product_attaches_existing_label(mock_get, mock_post):
    """product задан, метка уже есть на доске — идёт в idLabels ОДНИМ запросом
    создания карточки (без отдельного прохода на добавление метки)."""
    from integrations.trello import create_card

    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [{"id": "label_prodb", "name": "PRODB"}, {"id": "label_proda", "name": "PRODA"}],
    )
    mock_get.return_value.raise_for_status = lambda: None
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"id": "card1", "url": "https://trello.com/c/card1"},
    )
    mock_post.return_value.raise_for_status = lambda: None

    create_card("list_id_1", "CityA / Пакет 5", "desc", product="PRODB")

    # Ровно один POST — карточка создана СРАЗУ с меткой, не отдельным вызовом
    assert mock_post.call_count == 1
    _, kwargs = mock_post.call_args
    assert kwargs["data"]["idLabels"] == "label_prodb"


@patch("integrations.trello.session.post")
@patch("integrations.trello.session.get")
def test_create_card_with_product_creates_missing_label(mock_get, mock_post):
    """Метки продукта на доске ещё нет — создаётся (POST /boards/.../labels),
    затем используется в idLabels."""
    from integrations.trello import create_card

    mock_get.return_value = MagicMock(status_code=200, json=lambda: [])
    mock_get.return_value.raise_for_status = lambda: None

    def _post_side_effect(url, **kwargs):
        if url.endswith("/labels"):
            resp = MagicMock(status_code=200, json=lambda: {"id": "label_new_proda"})
        else:
            resp = MagicMock(status_code=200, json=lambda: {"id": "card1", "url": "https://trello.com/c/card1"})
        resp.raise_for_status = lambda: None
        return resp

    mock_post.side_effect = _post_side_effect

    result = create_card("list_id_1", "Тема А / Подтема 1", "desc", product="PRODA")

    assert result["id"] == "card1"
    # Второй POST (создание карточки) должен содержать свежесозданную метку
    card_call = [c for c in mock_post.call_args_list if c.args[0].endswith("/cards")][0]
    assert card_call.kwargs["data"]["idLabels"] == "label_new_proda"


def test_product_label_color_follows_product_registry():
    """Цвет метки продукта — по порядку реестра PRODUCTS, дефолтный и чужой — чёрный."""
    from integrations.trello import _product_label_color
    from services.product_tags import DEFAULT_PRODUCT, PRODUCTS

    ordered = [name for name in PRODUCTS if name != DEFAULT_PRODUCT]
    assert _product_label_color(ordered[0]) == "purple"
    assert _product_label_color(ordered[1]) == "sky"
    assert _product_label_color(DEFAULT_PRODUCT) == "black"
    assert _product_label_color("НЕТ_ТАКОГО_ПРОДУКТА") == "black"


@patch("integrations.trello.session.post")
@patch("integrations.trello.session.get")
def test_create_card_product_label_failure_is_fail_safe(mock_get, mock_post):
    """Trello-запрос за метками упал — карточка ВСЁ РАВНО создаётся (без
    метки), create_card не бросает исключение (fail-safe, §7 задачи)."""
    from integrations.trello import create_card

    mock_get.side_effect = RuntimeError("Trello недоступен")
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"id": "card1", "url": "https://trello.com/c/card1"},
    )
    mock_post.return_value.raise_for_status = lambda: None

    result = create_card("list_id_1", "Карточка", "desc", product="PRODA")

    assert result["id"] == "card1"
    assert mock_post.called
    _, kwargs = mock_post.call_args
    assert "idLabels" not in kwargs["data"]


@patch("integrations.trello.session.post")
@patch("integrations.trello.session.get")
def test_create_card_product_label_cached_across_calls(mock_get, mock_post):
    """Метка продукта запрашивается у Trello один раз за процесс — второй
    create_card с тем же продуктом НЕ делает повторный GET (не умножаем
    Trello-запросы)."""
    from integrations.trello import create_card

    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [{"id": "label_prodb", "name": "PRODB"}],
    )
    mock_get.return_value.raise_for_status = lambda: None
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"id": "card1", "url": "https://trello.com/c/card1"},
    )
    mock_post.return_value.raise_for_status = lambda: None

    create_card("list_id_1", "Карточка 1", "desc", product="PRODB")
    create_card("list_id_1", "Карточка 2", "desc", product="PRODB")

    assert mock_get.call_count == 1
    assert mock_post.call_count == 2


# --- Read-only recovery audit ---


def _trello_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _update_card_action(
    action_id: str,
    date: str,
    *,
    old_due_complete=None,
    due_complete=None,
) -> dict:
    data = {"old": {}, "card": {"id": f"card-{action_id}"}}
    if old_due_complete is not None:
        data["old"]["dueComplete"] = old_due_complete
    if due_complete is not None:
        data["card"]["dueComplete"] = due_complete
    return {"id": action_id, "date": date, "type": "updateCard", "data": data}


@patch("integrations.trello.session.request")
def test_board_update_actions_reads_all_pages_and_deduplicates(mock_request):
    from integrations.trello import get_board_update_card_actions

    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")
    completed_3 = _update_card_action(
        "a3",
        "2026-07-03T10:00:00+05:00",
        old_due_complete=False,
        due_complete=True,
    )
    completed_2 = _update_card_action(
        "a2",
        "2026-07-02T10:00:00+05:00",
        old_due_complete=False,
        due_complete=True,
    )
    completed_1 = _update_card_action(
        "a1",
        "2026-07-01T10:00:00+05:00",
        old_due_complete=False,
        due_complete=True,
    )
    mock_request.side_effect = [
        _trello_response([completed_3, completed_2]),
        _trello_response([completed_2, completed_1]),
        _trello_response([]),
    ]

    actions = get_board_update_card_actions("board-1", since, page_size=2)

    assert [action["id"] for action in actions] == ["a3", "a2", "a1"]
    assert "page" not in mock_request.call_args_list[0].kwargs["params"]
    assert "before" not in mock_request.call_args_list[0].kwargs["params"]
    assert mock_request.call_args_list[1].kwargs["params"]["before"] == "a2"
    assert mock_request.call_args_list[2].kwargs["params"]["before"] == "a1"
    assert all(
        call.kwargs["params"]["filter"] == "updateCard"
        for call in mock_request.call_args_list
    )
    assert all(
        call.kwargs["params"]["since"] == since.isoformat()
        for call in mock_request.call_args_list
    )


@patch("integrations.trello.session.request")
def test_board_update_actions_reads_more_than_one_thousand(mock_request):
    from integrations.trello import get_board_update_card_actions

    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")
    first_page = [
        _update_card_action(
            f"action-{index}",
            "2026-07-02T10:00:00+05:00",
            old_due_complete=False,
            due_complete=True,
        )
        for index in range(1000)
    ]
    last_action = _update_card_action(
        "action-1000",
        "2026-07-01T10:00:00+05:00",
        old_due_complete=False,
        due_complete=True,
    )
    mock_request.side_effect = [
        _trello_response(first_page),
        _trello_response([last_action]),
        _trello_response([]),
    ]

    actions = get_board_update_card_actions("board-1", since)

    assert len(actions) == 1001
    assert actions[-1]["id"] == "action-1000"
    assert mock_request.call_count == 3


@patch("integrations.trello.session.request")
def test_board_update_actions_repeated_page_fails_closed(mock_request):
    from integrations.trello import TrelloError, get_board_update_card_actions

    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")
    action = _update_card_action(
        "same-action",
        "2026-07-02T10:00:00+05:00",
        old_due_complete=False,
        due_complete=True,
    )
    mock_request.side_effect = [
        _trello_response([action]),
        _trello_response([action]),
    ]

    with pytest.raises(TrelloError, match="Trello request failed"):
        get_board_update_card_actions("board-1", since)


@patch("integrations.trello.session.request")
def test_board_update_actions_conflicting_duplicate_fails_closed(mock_request):
    from integrations.trello import TrelloError, get_board_update_card_actions

    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")
    original = _update_card_action(
        "same-action",
        "2026-07-02T10:00:00+05:00",
        old_due_complete=False,
        due_complete=True,
    )
    conflicting = _update_card_action(
        "same-action",
        "2026-07-03T10:00:00+05:00",
        old_due_complete=False,
        due_complete=True,
    )
    mock_request.side_effect = [
        _trello_response([original]),
        _trello_response([conflicting]),
    ]

    with pytest.raises(TrelloError, match="Trello request failed"):
        get_board_update_card_actions("board-1", since)


@patch("integrations.trello._MAX_ACTION_PAGES", 2)
@patch("integrations.trello.session.request")
def test_board_update_actions_page_limit_fails_closed(mock_request):
    from integrations.trello import TrelloError, get_board_update_card_actions

    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")
    mock_request.side_effect = [
        _trello_response([
            _update_card_action(
                f"action-{page}",
                "2026-07-02T10:00:00+05:00",
                old_due_complete=False,
                due_complete=True,
            )
        ])
        for page in range(2)
    ]

    with pytest.raises(TrelloError, match="Trello request failed"):
        get_board_update_card_actions("board-1", since)

    assert mock_request.call_count == 2


@patch("integrations.trello.session.request")
def test_board_update_actions_out_of_order_pages_fail_closed(mock_request):
    from integrations.trello import TrelloError, get_board_update_card_actions

    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")
    older = _update_card_action(
        "older",
        "2026-06-30T23:59:59+05:00",
        old_due_complete=False,
        due_complete=True,
    )
    newer = _update_card_action(
        "newer",
        "2026-07-02T10:00:00+05:00",
        old_due_complete=False,
        due_complete=True,
    )
    mock_request.side_effect = [
        _trello_response([older]),
        _trello_response([newer]),
        _trello_response([]),
    ]

    with pytest.raises(TrelloError, match="Trello request failed"):
        get_board_update_card_actions("board-1", since)

    assert mock_request.call_count == 2


@patch("integrations.trello.session.request")
def test_board_update_actions_before_interval_is_inclusive(mock_request):
    from integrations.trello import get_board_update_card_actions

    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")
    before = datetime.fromisoformat("2026-07-02T00:00:00+05:00")
    mock_request.side_effect = [
        _trello_response([
            _update_card_action(
                "after-before",
                "2026-07-02T00:00:00.000002+05:00",
                old_due_complete=False,
                due_complete=True,
            ),
            _update_card_action(
                "before-boundary",
                before.isoformat(),
                old_due_complete=False,
                due_complete=True,
            ),
            _update_card_action(
                "since-boundary",
                since.isoformat(),
                old_due_complete=False,
                due_complete=True,
            ),
        ]),
        _trello_response([]),
    ]

    actions = get_board_update_card_actions("board-1", since, before=before)

    assert [action["id"] for action in actions] == [
        "before-boundary",
        "since-boundary",
    ]
    assert mock_request.call_args_list[0].kwargs["params"]["before"] == (
        "2026-07-02T00:00:00.000001+05:00"
    )


@patch("integrations.trello.session.request")
def test_board_update_actions_rejects_invalid_before_without_request(mock_request):
    from integrations.trello import get_board_update_card_actions

    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")

    with pytest.raises(ValueError, match="aware datetime"):
        get_board_update_card_actions(
            "board-1",
            since,
            before=datetime.fromisoformat("2026-07-02T00:00:00"),
        )
    with pytest.raises(ValueError, match="не раньше since"):
        get_board_update_card_actions(
            "board-1",
            since,
            before=datetime.fromisoformat("2026-06-30T23:59:59+05:00"),
        )

    mock_request.assert_not_called()


@patch("integrations.trello.session.request")
def test_board_update_actions_since_boundary_is_inclusive(mock_request):
    from integrations.trello import get_board_update_card_actions

    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")
    boundary = _update_card_action(
        "boundary",
        "2026-06-30T19:00:00Z",
        old_due_complete=False,
        due_complete=True,
    )
    older = _update_card_action(
        "older",
        "2026-06-30T18:59:59Z",
        old_due_complete=False,
        due_complete=True,
    )
    mock_request.side_effect = [
        _trello_response([boundary, older]),
        _trello_response([]),
    ]

    actions = get_board_update_card_actions("board-1", since)

    assert [action["id"] for action in actions] == ["boundary"]
    assert mock_request.call_count == 2


@patch("integrations.trello.session.request")
def test_due_complete_transition_is_filtered_locally(mock_request):
    from integrations.trello import get_board_update_card_actions

    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")
    date = "2026-07-02T10:00:00+05:00"
    mock_request.side_effect = [
        _trello_response([
            _update_card_action(
                "completion",
                date,
                old_due_complete=False,
                due_complete=True,
            ),
            _update_card_action("name-change", date),
            _update_card_action(
                "unchecked",
                date,
                old_due_complete=True,
                due_complete=False,
            ),
        ]),
        _trello_response([]),
    ]

    actions = get_board_update_card_actions("board-1", since)

    assert [action["id"] for action in actions] == ["completion"]
    assert mock_request.call_args_list[0].kwargs["params"] == {
        "filter": "updateCard",
        "since": since.isoformat(),
        "limit": 1000,
    }


@pytest.mark.parametrize(
    "action",
    [
        {
            "id": 123,
            "date": "2026-07-02T10:00:00+05:00",
            "type": "updateCard",
            "data": {"card": {"id": "card-1"}},
        },
        {
            "id": "action-1",
            "date": "2026-07-02T10:00:00+05:00",
            "type": "createCard",
            "data": {"card": {"id": "card-1"}},
        },
        {
            "id": "action-1",
            "date": "2026-07-02T10:00:00+05:00",
            "type": "updateCard",
            "data": {"card": {"id": ""}},
        },
        {
            "id": "action-1",
            "date": "2026-07-02T10:00:00",
            "type": "updateCard",
            "data": {"card": {"id": "card-1"}},
        },
    ],
    ids=["non-string-id", "wrong-type", "missing-card-id", "naive-date"],
)
@patch("integrations.trello.session.request")
def test_board_update_actions_rejects_malformed_identity(mock_request, action):
    from integrations.trello import TrelloError, get_board_update_card_actions

    mock_request.return_value = _trello_response([action])
    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")

    with pytest.raises(TrelloError, match="Trello request failed"):
        get_board_update_card_actions("board-1", since)


@patch("integrations.trello.session.post")
@patch("integrations.trello.session.put")
@patch("integrations.trello.session.request")
def test_exact_card_fetch_is_read_only(mock_request, mock_put, mock_post):
    from integrations.trello import get_card

    card = {
        "id": "card-123",
        "name": "Exact card",
        "desc": "Описание",
        "dueComplete": True,
        "idList": "list-1",
        "labels": [],
        "closed": False,
    }
    mock_request.return_value = _trello_response(card)

    result = get_card("card-123")

    assert result == card
    mock_request.assert_called_once()
    method, url = mock_request.call_args.args[:2]
    assert method == "GET"
    assert url.endswith("/cards/card-123")
    mock_put.assert_not_called()
    mock_post.assert_not_called()


@pytest.mark.parametrize(
    "card",
    [
        {
            "id": 123,
            "name": "Exact card",
            "desc": "Описание",
            "dueComplete": True,
            "idList": "list-1",
            "labels": [],
            "closed": False,
        },
        {
            "id": "other-card",
            "name": "Exact card",
            "desc": "Описание",
            "dueComplete": True,
            "idList": "list-1",
            "labels": [],
            "closed": False,
        },
        {
            "id": "card-123",
            "name": "Exact card",
            "desc": "Описание",
            "dueComplete": "true",
            "idList": "list-1",
            "labels": [],
            "closed": False,
        },
    ],
    ids=["non-string-id", "wrong-id", "invalid-fields"],
)
@patch("integrations.trello.session.request")
def test_exact_card_fetch_rejects_malformed_identity(mock_request, card):
    from integrations.trello import TrelloError, get_card

    mock_request.return_value = _trello_response(card)

    with pytest.raises(TrelloError, match="Trello request failed"):
        get_card("card-123")


@patch("integrations.trello.session.request")
def test_recovery_reads_redact_trello_secrets(mock_request, caplog):
    from integrations.trello import TrelloError, get_board_update_card_actions

    mock_request.side_effect = requests.HTTPError(
        "403 for https://api.trello.com/1/boards/x/actions?key=live-key&token=live-token"
    )
    since = datetime.fromisoformat("2026-07-01T00:00:00+05:00")

    with pytest.raises(TrelloError) as error:
        get_board_update_card_actions("board-1", since)

    assert "live-key" not in str(error.value)
    assert "live-token" not in str(error.value)
    assert "live-key" not in caplog.text
    assert "live-token" not in caplog.text
    assert "key=***" in caplog.text
    assert "token=***" in caplog.text
