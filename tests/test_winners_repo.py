"""Тесты репозитория архива победителей (файловый)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.repositories.winners_repo import (
    get_winners,
    add_winner,
    delete_winner,
    sync_from_learner,
    _read,
    _write,
)

TENANT_ID = "default"


@pytest.fixture(autouse=True)
def clean_winners_file(tmp_path):
    """Изолируем файл победителей через tmp_path."""
    test_file = tmp_path / "winner_archive.json"
    with patch("agent.repositories.winners_repo.WINNERS_FILE", test_file):
        with patch("agent.repositories.winners_repo._next_id", 0):
            yield test_file


def test_get_winners_empty():
    """Изначально список пуст."""
    assert get_winners(TENANT_ID) == []


def test_add_winner():
    """Добавление победителя."""
    add_winner(TENANT_ID, "Петров / Обзор / Топ-1")
    winners = get_winners(TENANT_ID)
    assert len(winners) == 1
    assert winners[0]["ad_name"] == "Петров / Обзор / Топ-1"
    assert winners[0]["source"] == "manual"


def test_add_winner_with_source():
    """Добавление с кастомным source."""
    add_winner(TENANT_ID, "Семёнов / Результат", source="learner_sync")
    winners = get_winners(TENANT_ID)
    assert winners[0]["source"] == "learner_sync"


def test_delete_winner():
    """Удаление победителя."""
    result = add_winner(TENANT_ID, "Удаляемый")
    winners = get_winners(TENANT_ID)
    assert len(winners) == 1

    deleted = delete_winner(TENANT_ID, result["id"])
    assert deleted is True
    assert get_winners(TENANT_ID) == []


def test_delete_winner_not_found():
    """Удаление несуществующего — False."""
    assert delete_winner(TENANT_ID, 99999) is False


def test_sync_from_learner():
    """Синхронизация — победители импортируются."""
    learner_data = {
        "creative_table": [
            {"ad_name": "Победитель-1", "creative_class": "Winner"},
            {"ad_name": "Победитель-2", "creative_class": "Winner"},
            {"ad_name": "Не победитель", "creative_class": "Candidate"},
        ]
    }

    with patch("agent.repositories.winners_repo.LEARNER_FILE") as mock_path:
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps(learner_data)
        result = sync_from_learner(TENANT_ID)

    assert result["imported"] == 2
    assert result["skipped"] == 0
    names = {w["ad_name"] for w in get_winners(TENANT_ID)}
    assert "Победитель-1" in names
    assert "Не победитель" not in names


def test_sync_from_learner_skips_existing():
    """Существующие не дублируются."""
    add_winner(TENANT_ID, "Победитель-1", source="manual")

    learner_data = {
        "creative_table": [
            {"ad_name": "Победитель-1", "creative_class": "Winner"},
            {"ad_name": "Победитель-2", "creative_class": "Winner"},
        ]
    }

    with patch("agent.repositories.winners_repo.LEARNER_FILE") as mock_path:
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps(learner_data)
        result = sync_from_learner(TENANT_ID)

    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert len(get_winners(TENANT_ID)) == 2


def test_sync_from_learner_file_missing():
    """Файл отсутствует — нули."""
    with patch("agent.repositories.winners_repo.LEARNER_FILE") as mock_path:
        mock_path.exists.return_value = False
        result = sync_from_learner(TENANT_ID)

    assert result == {"imported": 0, "skipped": 0}
