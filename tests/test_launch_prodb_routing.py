"""PRODB-карточки (leadgen_prodb) льются в PRODB-адсеты (тип PRODB), а не в PRODA по языку.

Контракт: PRODB-адсеты «Owner | PRODB | MQL | Geo <Город> | ver1» (по PRODB-адсету
на город) живут в cabinet_b и приходят из discovery как leadgen[city]["PRODB"]. Ветка
leadgen_prodb в integrations.facebook._resolve_launch_adsets обязана брать именно
их; язык карточки (adset_type) выбирает только лид-форму и текст.

До фикса ветка брала adsets[adset_type] — PRODB-креативы уезжали в PRODA-адсеты.
"""

from __future__ import annotations

import pytest

from integrations import facebook


_INVENTORY = {
    "CityA": {"L2": "1", "L1": "2", "PRODB": "9"},
    "CityB": {"L2": "3", "L1": "4"},
    "Онлайн": {"L1": "5"},
}


@pytest.fixture
def inventory(monkeypatch):
    """discovery отдаёт фиксированный leadgen-словарь; MQL не участвует."""
    monkeypatch.setattr("agent.adset_discovery.get_adsets_dict", lambda: dict(_INVENTORY))
    monkeypatch.setattr("agent.adset_discovery.get_mql_adsets_dict", lambda: {})
    return _INVENTORY


def test_prodb_card_targets_only_prodb_adsets(inventory, caplog):
    with caplog.at_level("WARNING", logger="integrations.facebook"):
        targets = facebook._resolve_launch_adsets("leadgen_prodb", "L1", None)

    # Только PRODB-адсет CityA; L1-адсет "2" по языку не берётся, Онлайн отсечён.
    assert targets == [("CityA", "9")]
    # Город без живого PRODB-адсета пропущен с предупреждением.
    assert "CityB" in caplog.text
    assert "PRODB_ADSET_NOT_DISCOVERED" in caplog.text


def test_prodb_card_language_does_not_change_target(inventory):
    """Язык карточки для PRODB выбирает лид-форму, а не адсет."""
    assert facebook._resolve_launch_adsets("leadgen_prodb", "L2", None) == [("CityA", "9")]
    assert facebook._resolve_launch_adsets("leadgen_prodb", "L1", None) == [("CityA", "9")]


def test_prodb_card_for_city_without_prodb_adset_fails_closed(inventory):
    """CityB без PRODB-адсета: запуск в её L1-адсет по языку недопустим — отказ."""
    with pytest.raises(ValueError, match="не найдены среди доступных адсетов"):
        facebook._resolve_launch_adsets("leadgen_prodb", "L1", ["CityB"])


def test_regular_leadgen_still_uses_card_language(inventory):
    assert facebook._resolve_launch_adsets("leadgen", "L1", None) == [
        ("CityA", "2"),
        ("CityB", "4"),
    ]
    assert facebook._resolve_launch_adsets("leadgen", "L2", None) == [
        ("CityA", "1"),
        ("CityB", "3"),
    ]


def test_no_prodb_adsets_anywhere_raises(monkeypatch):
    monkeypatch.setattr(
        "agent.adset_discovery.get_adsets_dict",
        lambda: {"CityA": {"L2": "1", "L1": "2"}, "CityB": {"L2": "3", "L1": "4"}},
    )
    monkeypatch.setattr("agent.adset_discovery.get_mql_adsets_dict", lambda: {})

    with pytest.raises(ValueError, match="Ни одного PRODB-адсета"):
        facebook._resolve_launch_adsets("leadgen_prodb", "L1", None)
