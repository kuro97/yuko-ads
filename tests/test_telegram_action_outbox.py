"""Crash/retry граница durable Telegram action outbox."""

import sqlite3

from services import telegram_bot


def test_claim_failure_does_not_escape_confirmed_callback(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.database.claim_action_outbox",
        lambda _effect_id: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    sent = []
    monkeypatch.setattr(
        "services.notifications.send_telegram",
        lambda text: sent.append(text),
    )

    result = telegram_bot._deliver_action_notification(
        "11111111-1111-4111-8111-111111111111:decision:UNPAUSED"
    )

    assert result == "failed"
    assert sent == []
