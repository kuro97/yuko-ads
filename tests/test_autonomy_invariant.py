"""Сторож-инвариант: PAUSE-карточка у владельца при полной автономии = алерт."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services.autonomy_invariant import check_and_alert, find_pause_card_leaks
from tests.test_action_verifier import _build_executed, _connect, db_path  # noqa: F401

NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)


def _send_card(path, proposal_id, *, sent_at, kind_row_exists=True):
    connection = _connect(path)
    try:
        connection.execute(
            """
            INSERT INTO telegram_delivery_outbox (
                delivery_id, purpose, proposal_id, generation, dedupe_key,
                rendered_text, rendered_text_sha256, button_spec_json,
                button_spec_sha256, state, telegram_chat_id,
                telegram_message_id, created_at, sent_at
            ) VALUES (?, 'OWNER_PROPOSAL', ?, 2, ?, 'txt', ?, '[]', ?, 'SENT',
                      777, 9001, ?, ?)
            """,
            (
                f"delivery-inv-{proposal_id[:8]}",
                proposal_id,
                f"inv:{proposal_id}",
                "a" * 64,
                "b" * 64,
                sent_at.isoformat(),
                sent_at.isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_fresh_pause_card_detected_and_alerted(db_path):
    proposal_id, _ = _build_executed(db_path, kind="PAUSE", name="inv1")
    _send_card(db_path, proposal_id, sent_at=NOW - timedelta(minutes=5))
    with patch(
        "services.autonomous_pause.is_full_pause_autonomy_enabled", return_value=True
    ), patch("services.notifications.send_critical_alert") as alert:
        leaked = check_and_alert(db_path, now=NOW)
    assert leaked == 1
    alert.assert_called_once()


def test_old_card_outside_window_silent(db_path):
    proposal_id, _ = _build_executed(db_path, kind="PAUSE", name="inv2")
    _send_card(db_path, proposal_id, sent_at=NOW - timedelta(hours=3))
    assert find_pause_card_leaks(db_path, now=NOW) == []


def test_disabled_full_autonomy_silent(db_path):
    proposal_id, _ = _build_executed(db_path, kind="PAUSE", name="inv3")
    _send_card(db_path, proposal_id, sent_at=NOW - timedelta(minutes=5))
    with patch(
        "services.autonomous_pause.is_full_pause_autonomy_enabled", return_value=False
    ), patch("services.notifications.send_critical_alert") as alert:
        assert check_and_alert(db_path, now=NOW) == 0
    alert.assert_not_called()


def test_scale_card_is_not_a_leak(db_path):
    proposal_id, _ = _build_executed(db_path, kind="SCALE", name="inv4")
    _send_card(db_path, proposal_id, sent_at=NOW - timedelta(minutes=5))
    assert find_pause_card_leaks(db_path, now=NOW) == []
