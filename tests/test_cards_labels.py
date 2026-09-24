"""Тесты определения продукта/меток по карточкам Trello."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


# --- Тесты _detect_product ---

def test_detect_product_proda_only():
    """Только PRODA → 'PRODA'."""
    from web.app import _detect_product
    assert _detect_product(["FB", "PRODA"]) == "PRODA"


def test_detect_product_prodb_only():
    """PRODB + другие → 'PRODB'."""
    from web.app import _detect_product
    assert _detect_product(["PRODB", "СТАТИКА"]) == "PRODB"


def test_detect_product_proda_and_prodb():
    """PRODA и PRODB вместе → 'PRODA+PRODB'."""
    from web.app import _detect_product
    assert _detect_product(["PRODA", "PRODB"]) == "PRODA+PRODB"


def test_detect_product_no_match():
    """Только несущественные метки → None."""
    from web.app import _detect_product
    assert _detect_product(["FB"]) is None


def test_detect_product_empty():
    """Пустой список → None."""
    from web.app import _detect_product
    assert _detect_product([]) is None


def test_detect_product_start():
    """Старт → 'Старт'."""
    from web.app import _detect_product
    assert _detect_product(["Старт", "ВИДЕО"]) == "Старт"


def test_detect_product_order_stable():
    """Порядок в объединении всегда PRODA перед PRODB."""
    from web.app import _detect_product
    # Даже если PRODB идёт раньше в списке
    assert _detect_product(["PRODB", "PRODA"]) == "PRODA+PRODB"


# --- Тест что get_unlaunched_cards прокидывает labels ---

@patch("integrations.trello.session.get")
def test_get_unlaunched_cards_includes_labels(mock_get):
    """get_unlaunched_cards возвращает поле labels с именами меток."""
    from integrations.trello import get_unlaunched_cards

    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [
            {
                "id": "card1",
                "name": "Тест карточка",
                "desc": "",
                "dueComplete": False,
                "closed": False,
                "pos": 1,
                "due": None,
                "idAttachmentCover": None,
                "labels": [
                    {"id": "lbl1", "name": "PRODA", "color": "blue"},
                    {"id": "lbl2", "name": "СТАТИКА", "color": "green"},
                ],
            }
        ],
    )
    mock_get.return_value.raise_for_status = lambda: None

    cards = get_unlaunched_cards("list123")
    assert len(cards) == 1
    assert "labels" in cards[0]
    assert "PRODA" in cards[0]["labels"]
    assert "СТАТИКА" in cards[0]["labels"]


@patch("integrations.trello.session.get")
def test_get_unlaunched_cards_empty_labels(mock_get):
    """Карточка без меток — labels пустой список."""
    from integrations.trello import get_unlaunched_cards

    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [
            {
                "id": "card2",
                "name": "Без меток",
                "desc": "",
                "dueComplete": False,
                "closed": False,
                "pos": 1,
                "due": None,
                "idAttachmentCover": None,
                "labels": [],
            }
        ],
    )
    mock_get.return_value.raise_for_status = lambda: None

    cards = get_unlaunched_cards("list456")
    assert cards[0]["labels"] == []


@patch("integrations.trello.session.get")
def test_get_unlaunched_cards_filters_completed(mock_get):
    """Карточки с dueComplete=True отфильтровываются."""
    from integrations.trello import get_unlaunched_cards

    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [
            {
                "id": "c1",
                "name": "Запущена",
                "dueComplete": True,
                "closed": False,
                "pos": 1,
                "labels": [],
            },
            {
                "id": "c2",
                "name": "Новая",
                "dueComplete": False,
                "closed": False,
                "pos": 2,
                "labels": [{"name": "PRODB", "color": "orange"}],
            },
        ],
    )
    mock_get.return_value.raise_for_status = lambda: None

    cards = get_unlaunched_cards("list789")
    assert len(cards) == 1
    assert cards[0]["id"] == "c2"
    assert "PRODB" in cards[0]["labels"]
