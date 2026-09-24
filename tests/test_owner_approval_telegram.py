"""Trusted poller/webhook ingress и HMAC owner callbacks без реальной сети."""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
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
    canonical_sha256,
)
from services.owner_action_repository import OwnerActionRepository
from services.owner_approval_telegram import (
    OwnerApprovalTelegram,
    OwnerTelegramWebhookUnauthorized,
    handle_telegram_webhook,
    poll_telegram_updates,
)
from services.owner_delivery_outbox import OwnerDeliveryOutbox


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)


class FakePollClient:
    def __init__(self, updates: list[dict[str, object]]) -> None:
        self.updates = updates
        self.offsets: list[int] = []

    def get_updates(self, *, offset: int) -> list[dict[str, object]]:
        self.offsets.append(offset)
        return self.updates


class CapturingSender:
    def __init__(self, message_id: int = 888) -> None:
        self.message_id = message_id
        self.payloads: list[dict[str, object]] = []

    def send_message(self, **payload: object) -> int:
        self.payloads.append(payload)
        return self.message_id


@pytest.fixture
def settings(tmp_path) -> OwnerApprovalConfig:
    value = OwnerApprovalConfig(
        bot_token="123456789:AAOwnerApprovalBotTokenForTests",
        chat_id=777,
        owner_user_id=42,
        callback_secret="callback-7Cq9_Zv2-Nm8!Lp4-Rs6@Wx3-Kd5",
        webhook_secret="webhook-2Jf8_Yt4-Pq6!Bn9-Hm3@Vs7-Lx5",
        db_path=tmp_path / "owner-actions.db",
    )
    apply_runtime_migrations(str(value.db_path))
    return value


def _proposal(settings: OwnerApprovalConfig, suffix: str = "1") -> str:
    target_payload = {"status": "PAUSED"}
    evidence_payload = {"effective_status": "ACTIVE"}
    plan = ProposedActionPlan(
        proposal_kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        idempotency_key=f"telegram:pause:{suffix}",
        source_ref=f"autopilot:{suffix}",
        actor="autopilot",
        summary="Поставить объявление на паузу",
        targets=(
            ProposedTarget(
                claim_id=f"claim-{suffix}",
                ordinal=0,
                action_kind="PAUSE_AD",
                account_id="act-1",
                adset_id="adset-1",
                subject_id=f"ad-{suffix}",
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
                subject_id=f"ad-{suffix}",
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
    return (
        OwnerActionRepository(settings.db_path)
        .propose_action(
            plan,
            now=NOW,
        )
        .proposal_id
    )


def _count(settings: OwnerApprovalConfig, table: str) -> int:
    queries = {
        "telegram_update_inbox": "SELECT COUNT(*) FROM telegram_update_inbox",
        "owner_action_decisions": "SELECT COUNT(*) FROM owner_action_decisions",
        "owner_execution_jobs": "SELECT COUNT(*) FROM owner_execution_jobs",
    }
    if table not in queries:
        raise ValueError("Неизвестная тестовая таблица")
    connection = sqlite3.connect(settings.db_path)
    try:
        return int(connection.execute(queries[table]).fetchone()[0])
    finally:
        connection.close()


def _callback_update(
    *,
    update_id: int,
    callback_data: str,
    owner_user_id: int = 42,
    chat_id: int = 777,
    message_id: int = 888,
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": owner_user_id},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id},
            },
            "data": callback_data,
        },
    }


def test_public_poller_has_no_caller_supplied_update_parameter() -> None:
    assert tuple(inspect.signature(poll_telegram_updates).parameters) == ("now",)
    assert tuple(inspect.signature(handle_telegram_webhook).parameters) == (
        "raw_body",
        "secret_header",
        "remote_addr",
        "now",
    )


def test_poller_persists_updates_and_advances_durable_cursor(
    settings: OwnerApprovalConfig,
) -> None:
    client = FakePollClient(
        [
            {"update_id": 100, "callback_query": {"id": "one"}},
            {"update_id": 101, "callback_query": {"id": "two"}},
        ]
    )
    service = OwnerApprovalTelegram(settings.db_path, settings, client)

    result = service.poll(now=NOW)

    assert client.offsets == [0]
    assert result.fetched_count == 2
    assert result.inserted_count == 2
    assert result.next_update_id == 102
    connection = sqlite3.connect(settings.db_path)
    try:
        assert (
            connection.execute(
                "SELECT next_update_id FROM telegram_poll_cursor"
            ).fetchone()[0]
            == 102
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM telegram_update_inbox").fetchone()[
                0
            ]
            == 2
        )
    finally:
        connection.close()


def test_webhook_checks_secret_before_parsing_or_persisting(
    settings: OwnerApprovalConfig,
) -> None:
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient([]),
    )

    with pytest.raises(OwnerTelegramWebhookUnauthorized):
        service.webhook(
            raw_body=b"not-json",
            secret_header="wrong",
            remote_addr="127.0.0.1",
            now=NOW,
        )
    assert _count(settings, "telegram_update_inbox") == 0

    raw_body = b'{ "update_id": 501, "callback_query": {"id": "webhook"} }'
    accepted = service.webhook(
        raw_body=raw_body,
        secret_header=settings.webhook_secret,
        remote_addr="127.0.0.1",
        now=NOW,
    )

    assert accepted.accepted is True
    connection = sqlite3.connect(settings.db_path)
    try:
        row = connection.execute(
            """
            SELECT ingress_kind, raw_update_sha256
            FROM telegram_update_inbox WHERE update_id = 501
            """
        ).fetchone()
    finally:
        connection.close()
    assert row == ("WEBHOOK", hashlib.sha256(raw_body).hexdigest())


def test_hmac_callback_records_exact_approval_and_revokes_siblings(
    settings: OwnerApprovalConfig,
) -> None:
    proposal_id = _proposal(settings)
    sender = CapturingSender()
    outbox = OwnerDeliveryOutbox(settings.db_path, settings, sender)
    outbox.deliver(worker_id="delivery", now=NOW + timedelta(minutes=1))
    callback_data = sender.payloads[0]["inline_keyboard"][0][0]["callback_data"]
    update = _callback_update(update_id=700, callback_data=callback_data)
    poll_client = FakePollClient([update])
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        poll_client,
        delivery_outbox=outbox,
    )

    service.poll(now=NOW + timedelta(minutes=2))
    batch = service.process_inbox(
        worker_id="inbox",
        now=NOW + timedelta(minutes=2),
    )

    assert batch.decision_count == 1
    assert batch.results[0].proposal_id == proposal_id
    assert batch.results[0].state == "APPROVED"
    assert _count(settings, "owner_action_decisions") == 1
    assert _count(settings, "owner_execution_jobs") == 1
    connection = sqlite3.connect(settings.db_path)
    try:
        tokens = connection.execute(
            """
            SELECT decision_kind, consumed_at, revoked_at
            FROM owner_callback_tokens ORDER BY decision_kind
            """
        ).fetchall()
    finally:
        connection.close()
    assert sum(row[1] is not None for row in tokens) == 1
    assert sum(row[2] is not None for row in tokens) == 2


def test_wrong_owner_is_failed_without_decision(
    settings: OwnerApprovalConfig,
) -> None:
    _proposal(settings)
    sender = CapturingSender()
    outbox = OwnerDeliveryOutbox(settings.db_path, settings, sender)
    outbox.deliver(worker_id="delivery", now=NOW + timedelta(minutes=1))
    callback_data = sender.payloads[0]["inline_keyboard"][0][0]["callback_data"]
    update = _callback_update(
        update_id=701,
        callback_data=callback_data,
        owner_user_id=99,
    )
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient([update]),
        delivery_outbox=outbox,
    )

    service.poll(now=NOW + timedelta(minutes=2))
    batch = service.process_inbox(
        worker_id="inbox",
        now=NOW + timedelta(minutes=2),
    )

    assert batch.failed_count == 1
    assert _count(settings, "owner_action_decisions") == 0
    assert _count(settings, "owner_execution_jobs") == 0


def test_postpone_legally_creates_and_binds_new_generation(
    settings: OwnerApprovalConfig,
) -> None:
    proposal_id = _proposal(settings)
    first_sender = CapturingSender(message_id=801)
    first_outbox = OwnerDeliveryOutbox(settings.db_path, settings, first_sender)
    first_outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=1))
    postpone_data = first_sender.payloads[0]["inline_keyboard"][0][2]["callback_data"]
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(
            [
                _callback_update(
                    update_id=702, callback_data=postpone_data, message_id=801
                )
            ]
        ),
        delivery_outbox=first_outbox,
    )
    service.poll(now=NOW + timedelta(minutes=2))
    batch = service.process_inbox(
        worker_id="inbox",
        now=NOW + timedelta(minutes=2),
    )

    assert batch.results[0].state == "POSTPONED"
    connection = sqlite3.connect(settings.db_path)
    try:
        lifecycle = connection.execute(
            """
            SELECT state, delivery_generation
            FROM owner_action_lifecycle WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
        deliveries = connection.execute(
            """
            SELECT generation, state FROM telegram_delivery_outbox
            WHERE proposal_id = ? ORDER BY generation
            """,
            (proposal_id,),
        ).fetchall()
        generation_one = connection.execute(
            """
            SELECT consumed_at, revoked_at FROM owner_callback_tokens
            WHERE proposal_id = ? AND delivery_generation = 1
            """,
            (proposal_id,),
        ).fetchall()
    finally:
        connection.close()
    assert lifecycle == ("DELIVERY_PENDING", 2)
    assert deliveries == [(1, "SENT"), (2, "PENDING")]
    assert sum(row[0] is not None for row in generation_one) == 1
    assert sum(row[1] is not None for row in generation_one) == 2
    assert _count(settings, "owner_execution_jobs") == 0

    second_sender = CapturingSender(message_id=802)
    second_outbox = OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        second_sender,
    )
    delivered = second_outbox.deliver(
        worker_id="delivery-2",
        now=NOW + timedelta(minutes=63),
    )

    assert delivered.sent_count == 1
    connection = sqlite3.connect(settings.db_path)
    try:
        final = connection.execute(
            """
            SELECT state, delivery_generation
            FROM owner_action_lifecycle WHERE proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
    finally:
        connection.close()
    assert final == ("PENDING_OWNER", 2)


def test_rotated_old_callback_is_rejected_and_new_generation_can_approve(
    settings: OwnerApprovalConfig,
) -> None:
    proposal_id = _proposal(settings)
    old_sender = CapturingSender(message_id=901)
    OwnerDeliveryOutbox(settings.db_path, settings, old_sender).deliver(
        worker_id="delivery-old",
        now=NOW + timedelta(minutes=1),
    )
    old_callback = old_sender.payloads[0]["inline_keyboard"][0][0]["callback_data"]
    rotated_settings = replace(
        settings,
        callback_secret="rotated-4Np8_Yx2-Qm7!Dv5-Hk9@Ls3-Wc6",
    )
    new_sender = CapturingSender(message_id=902)
    rotated_outbox = OwnerDeliveryOutbox(
        settings.db_path,
        rotated_settings,
        new_sender,
    )
    rotated_outbox.deliver(
        worker_id="rotation-worker",
        now=NOW + timedelta(minutes=2),
    )
    new_callback = new_sender.payloads[0]["inline_keyboard"][0][0]["callback_data"]
    service = OwnerApprovalTelegram(
        settings.db_path,
        rotated_settings,
        FakePollClient(
            [
                _callback_update(
                    update_id=703,
                    callback_data=old_callback,
                    message_id=901,
                ),
                _callback_update(
                    update_id=704,
                    callback_data=new_callback,
                    message_id=902,
                ),
            ]
        ),
        delivery_outbox=rotated_outbox,
    )

    service.poll(now=NOW + timedelta(minutes=3))
    batch = service.process_inbox(
        worker_id="inbox",
        now=NOW + timedelta(minutes=3),
    )

    assert batch.failed_count == 1
    assert batch.decision_count == 1
    assert batch.results[0].proposal_id == proposal_id
    assert batch.results[0].state == "APPROVED"
    connection = sqlite3.connect(settings.db_path)
    try:
        inbox = connection.execute(
            """
            SELECT update_id, state, last_error_code
            FROM telegram_update_inbox
            WHERE update_id IN (703, 704)
            ORDER BY update_id
            """
        ).fetchall()
    finally:
        connection.close()
    assert inbox[0][0:2] == (703, "FAILED")
    assert "TOKEN_MAC_MISMATCH" in inbox[0][2]
    assert inbox[1] == (704, "PROCESSED", None)
