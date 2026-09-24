"""Card-root из staged ad names: режем только asset-суффикс, не имя карточки.

Регрессия: split(" / ", 1) обрубал имя карточки по первому « / »
(«Автор /PRODB формат / Тема А / Подтема 1» → «автор /prodb формат»), и
DUPLICATE_LIVE-гейт ловил ЧУЖУЮ карточку того же автора («… / Тема Б …»)
как дубль корня. Запуск в CityB/CityE/CityD блокировался при чистых
целевых адсетах.
"""
from types import SimpleNamespace

import pytest

from services.action_adapter_launch import _manifest_card_root
from services.launch_checker import LaunchCheckBlocked


def _manifest(*ad_names_by_city: tuple[str, tuple[str, ...]]):
    return SimpleNamespace(
        manifest_id="m-1",
        destinations=tuple(
            SimpleNamespace(
                city=city,
                creatives=tuple(
                    SimpleNamespace(ad_name=name) for name in names
                ),
            )
            for city, names in ad_names_by_city
        ),
    )


def test_card_name_with_inner_slashes_keeps_full_root() -> None:
    item = _manifest(
        (
            "CityE",
            (
                "CityE | Автор  /PRODB формат / Тема А / Подтема 1 / 1 блоггер [PRODB]",
                "CityE | Автор  /PRODB формат / Тема А / Подтема 1 / 2 блоггер [PRODB]",
            ),
        ),
        (
            "CityB",
            (
                "CityB | Автор  /PRODB формат / Тема А / Подтема 1 / 1 блоггер [PRODB]",
            ),
        ),
    )
    assert (
        _manifest_card_root(item)
        == "автор /prodb формат / тема а / подтема 1"
    )


def test_simple_card_name_unchanged() -> None:
    item = _manifest(
        ("CityA", ("CityA | Карточка про задачи / 1", "CityA | Карточка про задачи / 2")),
    )
    assert _manifest_card_root(item) == "карточка про задачи"


def test_sibling_card_of_same_blogger_is_not_same_root() -> None:
    ours = _manifest(
        ("CityE", ("CityE | Автор  /PRODB формат / Тема А / Подтема 1 / 1 блоггер [PRODB]",)),
    )
    theirs = _manifest(
        ("CityE", ("CityE | Автор  /PRODB формат / Тема Б / Подтема 2 / 1 [PRODB]",)),
    )
    assert _manifest_card_root(ours) != _manifest_card_root(theirs)


def test_mixed_roots_blocked() -> None:
    item = _manifest(
        ("CityE", ("CityE | Карточка А / 1", "CityE | Карточка Б / 1")),
    )
    with pytest.raises(LaunchCheckBlocked):
        _manifest_card_root(item)


def test_missing_city_prefix_blocked() -> None:
    item = _manifest(("CityE", ("CityB | Карточка А / 1",)))
    with pytest.raises(LaunchCheckBlocked):
        _manifest_card_root(item)
