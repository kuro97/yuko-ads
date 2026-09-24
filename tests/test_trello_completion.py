"""Потребитель очереди TRELLO_COMPLETE — отметка «запущено» на карточке."""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
import requests

from services.trello_completion import complete_trello_cards
from tests.test_owner_launch_lifecycle import (  # noqa: F401
    NOW,
    launch_repository,
)
from tests.test_owner_launch_lifecycle import _observation


def _verify(repository):
    """Прогоняет сторож до VERIFIED — ровно так строка очереди и появляется."""

    pending = repository.record_observation(
        repository.claim_due(
            worker_id="verify-a",
            now=NOW + timedelta(seconds=9),
            limit=10,
        )[0],
        observations={"ad-1": _observation(effective_status="PENDING_REVIEW")},
        fetch_complete=True,
        now=NOW + timedelta(seconds=10),
    )
    repository.record_observation(
        repository.claim_due(
            worker_id="verify-b",
            now=pending.next_verify_at,
            limit=10,
        )[0],
        observations={"ad-1": _observation(effective_status="ACTIVE")},
        fetch_complete=True,
        now=pending.next_verify_at,
    )


def _log_rows(db_path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT card_id, outcome, reason_code FROM trello_completion_log"
        ).fetchall()
    finally:
        connection.close()


def test_verified_launch_marks_card_done(launch_repository) -> None:
    db_path, repository, _watchdog_id, _proposal_id = launch_repository
    _verify(repository)
    marked: list[str] = []

    run = complete_trello_cards(db_path, mark_card_done=marked.append, now=NOW)

    assert marked == ["card-1"]
    assert run.completed_count == 1
    assert [tuple(row) for row in _log_rows(db_path)] == [
        ("card-1", "COMPLETED", None)
    ]


def test_second_pass_does_not_touch_trello_again(launch_repository) -> None:
    db_path, repository, _watchdog_id, _proposal_id = launch_repository
    _verify(repository)
    marked: list[str] = []
    complete_trello_cards(db_path, mark_card_done=marked.append, now=NOW)

    run = complete_trello_cards(db_path, mark_card_done=marked.append, now=NOW)

    assert marked == ["card-1"]
    assert run.completed_count == 0
    assert len(_log_rows(db_path)) == 1


def test_trello_failure_leaves_card_for_next_tick(launch_repository) -> None:
    db_path, repository, _watchdog_id, _proposal_id = launch_repository
    _verify(repository)
    attempts: list[str] = []

    def failing(card_id: str) -> None:
        attempts.append(card_id)
        raise RuntimeError("Trello 503")

    run = complete_trello_cards(db_path, mark_card_done=failing, now=NOW)

    assert run.failed and run.completed_count == 0
    assert _log_rows(db_path) == []

    recovered: list[str] = []
    second = complete_trello_cards(db_path, mark_card_done=recovered.append, now=NOW)
    assert recovered == ["card-1"]
    assert second.completed_count == 1


def test_launch_without_verification_is_not_marked(launch_repository) -> None:
    db_path, repository, _watchdog_id, _proposal_id = launch_repository
    repository.record_observation(
        repository.claim_due(
            worker_id="verify-a",
            now=NOW + timedelta(seconds=9),
            limit=10,
        )[0],
        observations={"ad-1": _observation(effective_status="PENDING_REVIEW")},
        fetch_complete=True,
        now=NOW + timedelta(seconds=10),
    )
    marked: list[str] = []

    run = complete_trello_cards(db_path, mark_card_done=marked.append, now=NOW)

    assert marked == []
    assert run.completed_count == 0


def test_deleted_card_is_closed_and_stops_blocking_queue(launch_repository) -> None:
    db_path, repository, _watchdog_id, _proposal_id = launch_repository
    _verify(repository)
    calls: list[str] = []

    def gone(card_id: str) -> None:
        calls.append(card_id)
        error = requests.HTTPError("404 Client Error: Not Found")
        error.response = SimpleNamespace(status_code=404)
        raise error

    run = complete_trello_cards(db_path, mark_card_done=gone, now=NOW)

    assert run.skipped and run.completed_count == 0
    assert [tuple(row) for row in _log_rows(db_path)] == [
        ("card-1", "SKIPPED", "CARD_NOT_FOUND")
    ]

    complete_trello_cards(db_path, mark_card_done=gone, now=NOW)
    assert calls == ["card-1"]


def test_trello_secrets_never_reach_the_log(launch_repository, caplog) -> None:
    db_path, repository, _watchdog_id, _proposal_id = launch_repository
    _verify(repository)
    leaky = (
        "503 Server Error for url: "
        "https://api.trello.com/1/cards/card-1?key=LEAKKEY777&token=LEAKTOKEN888"
    )

    def failing(_card_id: str) -> None:
        raise requests.HTTPError(leaky)

    with caplog.at_level(logging.WARNING):
        complete_trello_cards(db_path, mark_card_done=failing, now=NOW)

    logtext = "\n".join(record.getMessage() for record in caplog.records)
    assert "LEAKKEY777" not in logtext and "LEAKTOKEN888" not in logtext
    assert "key=***" in logtext and "token=***" in logtext


def test_naive_now_is_rejected(launch_repository) -> None:
    db_path, _repository, _watchdog_id, _proposal_id = launch_repository
    with pytest.raises(ValueError):
        complete_trello_cards(
            db_path,
            mark_card_done=lambda _card: None,
            now=NOW.replace(tzinfo=None),
        )
