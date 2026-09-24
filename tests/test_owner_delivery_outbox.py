"""Durable generations, leases и message binding owner outbox."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from config import OwnerApprovalConfig
from services.database_migrations import apply_runtime_migrations
from services.owner_action_models import (
    EvidenceRecord,
    ProposalKind,
    ProposalOrigin,
    ProposedActionPlan,
    ProposedTarget,
    SystemRedeliveryReason,
    canonical_sha256,
)
from services.owner_action_repository import (
    OwnerActionLifecycleConflict,
    OwnerActionRepository,
)
from services.owner_delivery_outbox import (
    OwnerDeliveryOutbox,
    TelegramDeliveryRejected,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)


class FakeSender:
    def __init__(
        self, *, message_id: int = 9001, error: Exception | None = None
    ) -> None:
        self.message_id = message_id
        self.error = error
        self.calls: list[dict[str, object]] = []

    def send_message(self, **payload: object) -> int:
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return self.message_id


@pytest.fixture
def settings(tmp_path) -> OwnerApprovalConfig:
    return OwnerApprovalConfig(
        bot_token="123456789:AAOwnerApprovalBotTokenForTests",
        chat_id=777,
        owner_user_id=42,
        callback_secret="callback-7Cq9_Zv2-Nm8!Lp4-Rs6@Wx3-Kd5",
        webhook_secret="webhook-2Jf8_Yt4-Pq6!Bn9-Hm3@Vs7-Lx5",
        db_path=tmp_path / "owner-actions.db",
    )


@pytest.fixture
def proposal_id(settings: OwnerApprovalConfig) -> str:
    apply_runtime_migrations(str(settings.db_path))
    target_payload = {"status": "PAUSED"}
    evidence_payload = {"effective_status": "ACTIVE"}
    plan = ProposedActionPlan(
        proposal_kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        idempotency_key="outbox:pause:1",
        source_ref="autopilot:test",
        actor="autopilot",
        summary="Поставить объявление на паузу",
        targets=(
            ProposedTarget(
                claim_id="claim-1",
                ordinal=0,
                action_kind="PAUSE_AD",
                account_id="act-1",
                adset_id="adset-1",
                subject_id="ad-1",
                city="CityA",
                language="L1",
                intended_payload=target_payload,
                intended_payload_sha256=canonical_sha256(target_payload),
            ),
        ),
        evidence=(
            EvidenceRecord(
                evidence_kind="PROPOSAL_INVENTORY",
                source_system="FACEBOOK",
                subject_id="ad-1",
                observed_at=NOW,
                complete=True,
                payload=evidence_payload,
                payload_sha256=canonical_sha256(evidence_payload),
            ),
        ),
        config_version_sha256="c" * 64,
        valid_until=NOW + timedelta(hours=4),
        staged_media_root=None,
    )
    receipt = OwnerActionRepository(settings.db_path).propose_action(plan, now=NOW)
    return receipt.proposal_id


def _rows(settings: OwnerApprovalConfig, sql: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def test_delivery_creates_96_bit_nonces_and_binds_message_once(
    settings: OwnerApprovalConfig,
    proposal_id: str,
) -> None:
    sender = FakeSender(message_id=8123)
    outbox = OwnerDeliveryOutbox(settings.db_path, settings, sender)

    run = outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=1))

    assert run.sent_count == 1
    assert len(sender.calls) == 1
    keyboard = sender.calls[0]["inline_keyboard"]
    callback_values = [button["callback_data"] for button in keyboard[0]]
    assert all(
        value.startswith(("oa:a:", "oa:r:", "oa:p:")) for value in callback_values
    )
    assert all(len(value.rsplit(".", 1)[1]) == 22 for value in callback_values)
    tokens = _rows(
        settings,
        """
        SELECT public_nonce, expected_message_id, bound_at
        FROM owner_callback_tokens
        ORDER BY decision_kind
        """,
    )
    assert len(tokens) == 3
    assert all(len(row["public_nonce"]) == 16 for row in tokens)
    assert all(row["expected_message_id"] == 8123 for row in tokens)
    assert all(row["bound_at"] is not None for row in tokens)
    lifecycle = _rows(
        settings,
        "SELECT state, delivery_generation FROM owner_action_lifecycle",
    )[0]
    assert dict(lifecycle) == {"state": "PENDING_OWNER", "delivery_generation": 1}

    with pytest.raises(sqlite3.IntegrityError, match="one-time"):
        connection = sqlite3.connect(settings.db_path)
        try:
            connection.execute(
                """
                UPDATE owner_callback_tokens
                SET expected_message_id = 9999, bound_at = ?
                WHERE public_nonce = ?
                """,
                ((NOW + timedelta(minutes=2)).isoformat(), tokens[0]["public_nonce"]),
            )
        finally:
            connection.close()


def test_unknown_send_result_revokes_generation_and_delivers_replacement(
    settings: OwnerApprovalConfig,
    proposal_id: str,
) -> None:
    failing = OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        FakeSender(error=ConnectionError("connection lost after send")),
    )
    first = failing.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=1))

    assert first.failed_visible_count == 1
    generations = _rows(
        settings,
        """
        SELECT generation, state FROM telegram_delivery_outbox ORDER BY generation
        """,
    )
    assert [tuple(row) for row in generations] == [
        (1, "FAILED_VISIBLE"),
        (2, "PENDING"),
    ]
    old_tokens = _rows(
        settings,
        """
        SELECT revoked_at, expected_message_id
        FROM owner_callback_tokens WHERE delivery_generation = 1
        """,
    )
    assert all(row["revoked_at"] is not None for row in old_tokens)
    assert all(row["expected_message_id"] is None for row in old_tokens)

    succeeding = OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        FakeSender(message_id=9902),
    )
    second = succeeding.deliver(
        worker_id="delivery-2",
        now=NOW + timedelta(minutes=2),
    )

    assert second.sent_count == 1
    lifecycle = _rows(
        settings,
        "SELECT state, delivery_generation FROM owner_action_lifecycle",
    )[0]
    assert dict(lifecycle) == {"state": "PENDING_OWNER", "delivery_generation": 2}


def test_explicit_telegram_rejection_retries_same_unbound_generation(
    settings: OwnerApprovalConfig,
    proposal_id: str,
) -> None:
    outbox = OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        FakeSender(error=TelegramDeliveryRejected("known rejection")),
    )

    run = outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=1))

    assert run.retry_count == 1
    delivery = _rows(
        settings,
        """
        SELECT generation, state, next_attempt_at
        FROM telegram_delivery_outbox
        """,
    )[0]
    assert delivery["generation"] == 1
    assert delivery["state"] == "PENDING"
    assert delivery["next_attempt_at"] is not None
    tokens = _rows(
        settings,
        "SELECT revoked_at, expected_message_id FROM owner_callback_tokens",
    )
    assert all(row["revoked_at"] is None for row in tokens)
    assert all(row["expected_message_id"] is None for row in tokens)


def test_pending_owner_redelivery_prepares_then_binds_new_generation(
    settings: OwnerApprovalConfig,
    proposal_id: str,
) -> None:
    first_sender = FakeSender(message_id=701)
    outbox = OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        first_sender,
    )
    outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=1))

    prepared = outbox.prepare_system_redelivery(
        proposal_id,
        expected_lifecycle_version=2,
        reason=SystemRedeliveryReason.REDELIVERY,
        actor="manual-redelivery",
        now=NOW + timedelta(minutes=2),
    )

    assert prepared.generation == 2
    assert len(first_sender.calls) == 1
    lifecycle = _rows(
        settings,
        """
        SELECT state, delivery_generation, latest_reason_code
        FROM owner_action_lifecycle
        """,
    )[0]
    assert tuple(lifecycle) == ("DELIVERY_PENDING", 2, "REDELIVERY")
    old_tokens = _rows(
        settings,
        """
        SELECT revoked_at, revoke_reason FROM owner_callback_tokens
        WHERE delivery_generation = 1
        """,
    )
    assert all(row["revoked_at"] is not None for row in old_tokens)
    assert all(row["revoke_reason"] == "REDELIVERY" for row in old_tokens)

    second_sender = FakeSender(message_id=702)
    second_outbox = OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        second_sender,
    )
    run = second_outbox.deliver(
        worker_id="delivery-2",
        now=NOW + timedelta(minutes=3),
    )

    assert run.sent_count == 1
    final = _rows(
        settings,
        """
        SELECT state, delivery_generation FROM owner_action_lifecycle
        """,
    )[0]
    assert tuple(final) == ("PENDING_OWNER", 2)


def test_callback_secret_rotation_is_automatic_and_uses_new_hmac(
    settings: OwnerApprovalConfig,
    proposal_id: str,
) -> None:
    first_sender = FakeSender(message_id=801)
    OwnerDeliveryOutbox(settings.db_path, settings, first_sender).deliver(
        worker_id="delivery-1",
        now=NOW + timedelta(minutes=1),
    )
    rotated_settings = replace(
        settings,
        callback_secret="rotated-4Np8_Yx2-Qm7!Dv5-Hk9@Ls3-Wc6",
    )
    second_sender = FakeSender(message_id=802)
    rotated_outbox = OwnerDeliveryOutbox(
        settings.db_path,
        rotated_settings,
        second_sender,
    )

    run = rotated_outbox.deliver(
        worker_id="rotation-worker",
        now=NOW + timedelta(minutes=2),
    )

    assert run.sent_count == 1
    assert run.replacement_count == 1
    generations = _rows(
        settings,
        """
        SELECT generation, state FROM telegram_delivery_outbox
        ORDER BY generation
        """,
    )
    assert [tuple(row) for row in generations] == [(1, "SENT"), (2, "SENT")]
    old_tokens = _rows(
        settings,
        """
        SELECT revoked_at, revoke_reason FROM owner_callback_tokens
        WHERE delivery_generation = 1
        """,
    )
    assert all(row["revoked_at"] is not None for row in old_tokens)
    assert all(row["revoke_reason"] == "CALLBACK_SECRET_ROTATION" for row in old_tokens)
    assert (
        first_sender.calls[0]["inline_keyboard"][0][0]["callback_data"]
        != second_sender.calls[0]["inline_keyboard"][0][0]["callback_data"]
    )


def test_concurrent_system_redelivery_has_one_cas_winner(
    settings: OwnerApprovalConfig,
    proposal_id: str,
) -> None:
    OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        FakeSender(message_id=901),
    ).deliver(worker_id="initial", now=NOW + timedelta(minutes=1))
    barrier = threading.Barrier(2)

    def prepare(worker_id: str) -> str:
        outbox = OwnerDeliveryOutbox(
            settings.db_path,
            settings,
            FakeSender(message_id=902),
        )
        barrier.wait()
        try:
            outbox.prepare_system_redelivery(
                proposal_id,
                expected_lifecycle_version=2,
                reason=SystemRedeliveryReason.REDELIVERY,
                actor=worker_id,
                now=NOW + timedelta(minutes=2),
            )
        except OwnerActionLifecycleConflict:
            return "CONFLICT"
        return "PREPARED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(prepare, ("worker-a", "worker-b")))

    assert sorted(results) == ["CONFLICT", "PREPARED"]
    generation_two = _rows(
        settings,
        """
        SELECT COUNT(*) AS count FROM telegram_delivery_outbox
        WHERE generation = 2
        """,
    )[0]
    assert generation_two["count"] == 1


def test_crash_after_send_before_bind_recovers_with_next_generation(
    settings: OwnerApprovalConfig,
    proposal_id: str,
) -> None:
    OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        FakeSender(message_id=1001),
    ).deliver(worker_id="initial", now=NOW + timedelta(minutes=1))
    crashed_sender = FakeSender(message_id=1002)
    crashed_outbox = OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        crashed_sender,
    )
    crashed_outbox.prepare_system_redelivery(
        proposal_id,
        expected_lifecycle_version=2,
        reason=SystemRedeliveryReason.REDELIVERY,
        now=NOW + timedelta(minutes=2),
    )
    claimed = crashed_outbox._claim(  # noqa: SLF001
        worker_id="crashed-worker",
        now=NOW + timedelta(minutes=2),
        limit=1,
    )
    assert len(claimed) == 1
    crashed_sender.send_message(
        chat_id=settings.chat_id,
        text=str(claimed[0]["rendered_text"]),
        inline_keyboard=crashed_outbox._keyboard(claimed[0]),  # noqa: SLF001
    )

    recovery_sender = FakeSender(message_id=1003)
    recovery = OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        recovery_sender,
    ).deliver(
        worker_id="recovery-worker",
        now=NOW + timedelta(minutes=3, seconds=1),
    )

    assert recovery.sent_count == 1
    generations = _rows(
        settings,
        """
        SELECT generation, state, telegram_message_id
        FROM telegram_delivery_outbox ORDER BY generation
        """,
    )
    assert [tuple(row) for row in generations] == [
        (1, "SENT", 1001),
        (2, "FAILED_VISIBLE", None),
        (3, "SENT", 1003),
    ]
    generation_two_tokens = _rows(
        settings,
        """
        SELECT expected_message_id, revoked_at FROM owner_callback_tokens
        WHERE delivery_generation = 2
        """,
    )
    assert all(row["expected_message_id"] is None for row in generation_two_tokens)
    assert all(row["revoked_at"] is not None for row in generation_two_tokens)


def test_bind_failure_rolls_back_all_binding_and_replaces_generation(
    settings: OwnerApprovalConfig,
    proposal_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = FakeSender(message_id=1101)
    outbox = OwnerDeliveryOutbox(settings.db_path, settings, sender)

    def fail_event(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected crash before bind commit")

    monkeypatch.setattr(outbox, "_append_event", fail_event)
    run = outbox.deliver(worker_id="delivery", now=NOW + timedelta(minutes=1))

    assert run.failed_visible_count == 1
    first_generation = _rows(
        settings,
        """
        SELECT state, telegram_message_id, sent_at
        FROM telegram_delivery_outbox WHERE generation = 1
        """,
    )[0]
    assert tuple(first_generation) == ("FAILED_VISIBLE", None, None)
    first_tokens = _rows(
        settings,
        """
        SELECT expected_message_id, bound_at, revoked_at
        FROM owner_callback_tokens WHERE delivery_generation = 1
        """,
    )
    assert all(row["expected_message_id"] is None for row in first_tokens)
    assert all(row["bound_at"] is None for row in first_tokens)
    assert all(row["revoked_at"] is not None for row in first_tokens)
