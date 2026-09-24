"""Регрессии T7: Telegram не обходит персональное owner approval."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import telegram_bot, telegram_console
from services.approval_checker_models import ActionOrigin
from services.database_migrations import apply_runtime_migrations
from services.owner_action_repository import OwnerActionRepository


OWNER_ID = "111"


def _message(text: str, message_id: int = 7) -> dict:
    return {
        "message_id": message_id,
        "from": {"id": int(OWNER_ID)},
        "chat": {"id": int(OWNER_ID)},
        "date": int(datetime.now(timezone.utc).timestamp()),
        "text": text,
    }


def _callback(data: str) -> dict:
    return {
        "id": "callback-1",
        "from": {"id": int(OWNER_ID)},
        "message": {"chat": {"id": int(OWNER_ID)}},
        "data": data,
    }


@pytest.fixture
def repository(tmp_path):
    db_path = tmp_path / "owner-actions.db"
    apply_runtime_migrations(str(db_path))
    return OwnerActionRepository(db_path)


@pytest.fixture
def owner_console(monkeypatch, repository):
    monkeypatch.setattr("config.TELEGRAM_CHAT_ID", OWNER_ID)
    monkeypatch.setattr(telegram_console, "_owner_repository", lambda: repository)
    delivery = Mock()
    monkeypatch.setattr(telegram_console, "_deliver_owner_proposals", delivery)
    return repository, delivery


def _counts(repository: OwnerActionRepository) -> tuple[int, int]:
    connection = sqlite3.connect(repository._db_path)  # noqa: SLF001 - тест repository
    try:
        proposals = connection.execute(
            "SELECT COUNT(*) FROM owner_action_proposals"
        ).fetchone()[0]
        decisions = connection.execute(
            "SELECT COUNT(*) FROM owner_action_decisions"
        ).fetchone()[0]
        return proposals, decisions
    finally:
        connection.close()


@pytest.mark.parametrize(
    "callback_data",
    [
        "undo:12345",
        "apply:12345",
        "applyrun:run-1",
        "apfb:up:12345:p",
        "pause:12345",
        "stop_launch:card-1",
        "oa:a:nonce.signature",
    ],
)
def test_legacy_and_oa_callbacks_are_stale_only(
    monkeypatch,
    callback_data: str,
) -> None:
    monkeypatch.setattr("config.TELEGRAM_CHAT_ID", OWNER_ID)
    answer = Mock()
    monkeypatch.setattr(telegram_bot, "_answer_callback", answer)
    provider = Mock()
    monkeypatch.setattr(
        "services.action_producer_gateway.execute_unpause",
        provider,
    )
    monkeypatch.setattr("services.autopilot.approve_pause", provider)

    telegram_bot._handle_callback(_callback(callback_data))

    provider.assert_not_called()
    answer.assert_called_once()
    assert "устарела" in answer.call_args.args[1].lower()


def test_undo_pause_callback_creates_proposal_instead_of_stale(monkeypatch) -> None:
    """`undo_pause:` больше не тупик: клик рождает UNPAUSE-предложение владельцу.

    Кнопка «↩️ Вернуть» осталась в отчётах автопилота, поэтому ответ «кнопка
    устарела» был для владельца ложным тупиком. Мутации в этом пути по-прежнему
    нет — только producer, исполнение ждёт одобрения.
    """
    monkeypatch.setattr("config.TELEGRAM_CHAT_ID", OWNER_ID)
    answer = Mock()
    monkeypatch.setattr(telegram_bot, "_answer_callback", answer)
    propose = Mock(return_value=SimpleNamespace(action="PROPOSAL_CREATED", receipt=None))
    monkeypatch.setattr(
        "services.action_producer_gateway.propose_unpause",
        propose,
    )

    telegram_bot._handle_callback(_callback("undo_pause:12345"))

    propose.assert_called_once()
    assert propose.call_args.args == ("12345",)
    assert propose.call_args.kwargs["scope"] == "telegram-undo-pause:12345"
    assert propose.call_args.kwargs["origin"] is ActionOrigin.TELEGRAM
    answer.assert_called_once()
    assert "устарела" not in answer.call_args.args[1].lower()


@pytest.mark.parametrize(
    ("command", "expected_kind"),
    [
        ("/pause 123456789", "PAUSE"),
        ("/unpause 123456789", "UNPAUSE"),
    ],
)
def test_status_commands_persist_proposal_not_decision(
    owner_console,
    command: str,
    expected_kind: str,
) -> None:
    repository, delivery = owner_console

    telegram_console.handle_message(_message(command))

    assert _counts(repository) == (1, 0)
    proposal = repository.list_proposals(state=None, cursor=None).items[0]
    assert proposal.proposal_kind.value == expected_kind
    assert proposal.origin.value == "TELEGRAM_COMMAND"
    delivery.assert_called_once()


def test_scale_command_uses_dry_run_and_persists_proposal(
    monkeypatch,
    owner_console,
) -> None:
    repository, delivery = owner_console
    run = Mock(
        return_value={
            "recommendations": [
                {
                    "adset_id": "987654321",
                    "adset_name": "CityA L1",
                    "current_budget_usd": 20,
                    "new_budget_usd": 24,
                }
            ]
        }
    )
    monkeypatch.setattr("services.budget_scaler.run_budget_scaling", run)
    monkeypatch.setattr(
        "services.budget_scaler.get_scale_config",
        lambda: {"max_scales_per_run": 2},
    )

    telegram_console._run_scale_async(source_ref="telegram-message:111:8")

    run.assert_called_once_with(mode="dry_run", max_scales=2)
    assert _counts(repository) == (1, 0)
    assert repository.list_proposals(
        state=None, cursor=None
    ).items[0].proposal_kind.value == "SCALE"
    delivery.assert_called_once()


def test_launch_command_uses_dry_run_and_persists_proposal(
    monkeypatch,
    owner_console,
) -> None:
    repository, delivery = owner_console
    run = Mock(
        return_value={
            "recommendations": [
                {"card_id": "card-1", "card_name": "Новый креатив"}
            ]
        }
    )
    monkeypatch.setattr("services.auto_launch.run_auto_launch", run)
    monkeypatch.setattr(
        "services.auto_launch._get_autopilot_config",
        lambda: {"max_launches_per_day": 1},
    )

    telegram_console._run_launch_async(
        source_ref="telegram-message:111:9",
    )

    run.assert_called_once_with(mode="dry_run", max_launches=1)
    assert _counts(repository) == (1, 0)
    assert repository.list_proposals(
        state=None, cursor=None
    ).items[0].proposal_kind.value == "LAUNCH"
    delivery.assert_called_once()


def test_same_message_is_idempotent(owner_console) -> None:
    repository, delivery = owner_console
    message = _message("/pause 123456789", message_id=10)

    telegram_console.handle_message(message)
    telegram_console.handle_message(message)

    assert _counts(repository) == (1, 0)
    assert delivery.call_count == 2
