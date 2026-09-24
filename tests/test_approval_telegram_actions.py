"""T23: Telegram callbacks меняют локальное состояние только после CONFIRMED."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import Mock

from services import telegram_bot


ROOT = Path(__file__).resolve().parents[1]


def test_telegram_actions_have_no_raw_pause_or_unpause_calls() -> None:
    tree = ast.parse((ROOT / "services/telegram_bot.py").read_text(encoding="utf-8"))
    forbidden = {"pause_ad", "unpause_ad", "safe_pause_or_enqueue_replacement"}
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert imported.isdisjoint(forbidden)
    assert called.isdisjoint(forbidden)
    assert "execute_unpause" not in imported


def test_undo_unknown_has_zero_success_side_effects(monkeypatch) -> None:
    execute = Mock()
    save = Mock()
    override = Mock()
    answer = Mock()
    send = Mock()
    monkeypatch.setattr("services.action_producer_gateway.execute_unpause", execute)
    monkeypatch.setattr("agent.repositories.decisions_repo.save_decision", save)
    monkeypatch.setattr("services.autopilot.add_manual_override", override)
    monkeypatch.setattr(telegram_bot, "_answer_callback", answer)
    monkeypatch.setattr("services.notifications.send_telegram", send)

    telegram_bot._execute_undo("12345", "callback-1")

    save.assert_not_called()
    override.assert_not_called()
    send.assert_not_called()
    execute.assert_not_called()
    assert "устарела" in answer.call_args.args[1].lower()


def test_undo_double_click_has_zero_local_effects(monkeypatch) -> None:
    execute = Mock()
    save = Mock(side_effect=[True, False])
    override = Mock()
    answer = Mock()
    send = Mock()
    monkeypatch.setattr("services.action_producer_gateway.execute_unpause", execute)
    monkeypatch.setattr("agent.repositories.decisions_repo.save_decision", save)
    monkeypatch.setattr("services.autopilot.add_manual_override", override)
    monkeypatch.setattr(telegram_bot, "_answer_callback", answer)
    monkeypatch.setattr("services.notifications.send_telegram", send)

    telegram_bot._execute_undo("12345", "callback-1")
    telegram_bot._execute_undo("12345", "callback-1")

    execute.assert_not_called()
    save.assert_not_called()
    override.assert_not_called()
    send.assert_not_called()
    assert answer.call_count == 2


def test_applyrun_stops_after_first_non_confirmed(monkeypatch) -> None:
    ads = [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]
    approve = Mock(return_value=(False, "UNKNOWN"))
    pop = Mock()
    monkeypatch.setattr("services.autopilot.get_pending_approval", lambda _: {"ads": ads})
    monkeypatch.setattr("services.autopilot.approve_pause", approve)
    monkeypatch.setattr("services.autopilot.pop_applied", pop)
    monkeypatch.setattr(telegram_bot, "_answer_callback", Mock())
    monkeypatch.setattr("services.notifications.send_telegram", Mock())

    telegram_bot._execute_applyrun("run-1", "callback-1")

    approve.assert_not_called()
    pop.assert_not_called()
