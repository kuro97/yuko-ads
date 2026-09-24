"""manual_launch.find_card видит отмеченные карточки; get_open_cards vs get_unlaunched_cards."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import manual_launch  # noqa: E402
from integrations import trello  # noqa: E402


def _card(card_id: str, name: str, *, due: bool = False, closed: bool = False, pos: float = 1.0) -> dict:
    return {
        "id": card_id, "name": name, "dueComplete": due, "closed": closed, "pos": pos,
        "labels": [{"name": "PRODA", "color": "green"}],
    }


# --- integrations.trello.get_open_cards ------------------------------------------


def _list_response(cards: list[dict]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = cards
    return response


def test_get_open_cards_keeps_completed_and_drops_closed() -> None:
    cards = [
        _card("a", "Без галочки", pos=2.0),
        _card("b", "С галочкой", due=True, pos=1.0),
        _card("c", "Архив", closed=True),
    ]
    with patch.object(trello.session, "get", return_value=_list_response(cards)):
        result = trello.get_open_cards("L1")

    assert [card["id"] for card in result] == ["b", "a"]
    assert result[0]["labels"] == ["PRODA"]


def test_get_unlaunched_cards_still_filters_completed() -> None:
    cards = [_card("a", "Без галочки", pos=2.0), _card("b", "С галочкой", due=True, pos=1.0)]
    with patch.object(trello.session, "get", return_value=_list_response(cards)):
        result = trello.get_unlaunched_cards("L1")

    assert [card["id"] for card in result] == ["a"]


# --- manual_launch.find_card ------------------------------------------------------


@pytest.fixture
def open_cards(monkeypatch):
    cards = [
        _card("id-done", "Креатор А / Тема 1 l2", due=True),
        _card("id-new", "Блогер Б / PRODB / Формат 1"),
        _card("id-new-2", "Блогер В / PRODB / Формат 1"),
    ]
    monkeypatch.setattr(manual_launch, "get_done_list_id", lambda: "L1")
    monkeypatch.setattr(manual_launch, "get_open_cards", lambda list_id: cards)
    return cards


def test_find_card_by_id_sees_completed_card(open_cards) -> None:
    assert manual_launch.find_card("id-done")["name"] == "Креатор А / Тема 1 l2"


def test_find_card_by_name_fragment_sees_completed_card(open_cards) -> None:
    assert manual_launch.find_card("тема 1")["id"] == "id-done"


def test_find_card_ambiguous_fragment_asks_to_refine(open_cards) -> None:
    with pytest.raises(manual_launch.ManualLaunchError, match="несколько карточек"):
        manual_launch.find_card("Формат 1")


def test_find_card_unknown_lists_candidates(open_cards) -> None:
    with pytest.raises(manual_launch.ManualLaunchError, match="не найдена"):
        manual_launch.find_card("нет такой")


# --- manual_launch._mark_card_launched --------------------------------------------


def test_mark_card_launched_skips_already_completed(capsys) -> None:
    with patch.object(trello, "mark_card_done") as mark:
        manual_launch._mark_card_launched({"id": "id-done", "dueComplete": True})

    mark.assert_not_called()
    assert "уже отмечена" in capsys.readouterr().out


def test_mark_card_launched_marks_and_reports(capsys) -> None:
    with patch.object(trello, "mark_card_done") as mark:
        manual_launch._mark_card_launched({"id": "id-new", "dueComplete": False})

    mark.assert_called_once_with("id-new")
    assert "отмечена запущенной" in capsys.readouterr().out


def test_mark_card_launched_redacts_trello_secrets_on_failure(capsys) -> None:
    error = requests.HTTPError(
        "401 Client Error: Unauthorized for url: "
        "https://api.trello.com/1/cards/id-new?key=KEY_SECRET&token=TOKEN_SECRET&dueComplete=true"
    )
    with patch.object(trello, "mark_card_done", side_effect=error):
        manual_launch._mark_card_launched({"id": "id-new", "dueComplete": False})

    out = capsys.readouterr().out
    assert "Не удалось отметить карточку" in out
    assert "KEY_SECRET" not in out and "TOKEN_SECRET" not in out
    assert "key=***" in out and "token=***" in out
