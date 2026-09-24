"""Свободный фидбек владельца реплаем (волна E, блок E4)."""

from __future__ import annotations

import sqlite3
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
from services.owner_approval_telegram import OwnerApprovalTelegram
from services.owner_delivery_outbox import OwnerDeliveryOutbox
from services.owner_training_export import export_owner_training_dataset


NOW = datetime(2026, 7, 27, 4, 0, tzinfo=timezone.utc)
OWNER_ID = 42
CHAT_ID = 777


class RecordingSender:
    def __init__(self, first_message_id: int = 7000) -> None:
        self.next_message_id = first_message_id
        self.payloads: list[dict[str, object]] = []

    def send_message(self, **payload: object) -> int:
        self.payloads.append(payload)
        self.next_message_id += 1
        return self.next_message_id


class RecordingAck:
    def __init__(self) -> None:
        self.answers: list[dict[str, object]] = []
        self.edits: list[dict[str, object]] = []

    def answer_callback(self, *, callback_query_id: str, text: str) -> None:
        self.answers.append({"callback_query_id": callback_query_id, "text": text})

    def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append({"message_id": message_id, "text": text})


class FakePollClient:
    def __init__(self, updates: list[dict[str, object]]) -> None:
        self.updates = updates

    def get_updates(self, *, offset: int) -> list[dict[str, object]]:
        return self.updates


@pytest.fixture
def settings(tmp_path) -> OwnerApprovalConfig:
    value = OwnerApprovalConfig(
        bot_token="123456789:AAFeedbackBotTokenForOwnerTests",
        chat_id=CHAT_ID,
        owner_user_id=OWNER_ID,
        callback_secret="callback-7Cq9_Zv2-Nm8!Lp4-Rs6@Wx3-Kd5",
        webhook_secret="webhook-2Jf8_Yt4-Pq6!Bn9-Hm3@Vs7-Lx5",
        db_path=tmp_path / "feedback.db",
    )
    apply_runtime_migrations(str(value.db_path))
    return value


def _make_proposal(settings: OwnerApprovalConfig, *, name: str = "a") -> str:
    payload = {"status": "PAUSED", "ad": name}
    evidence_payload = {"effective_status": "ACTIVE"}
    plan = ProposedActionPlan(
        proposal_kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        idempotency_key=f"feedback:pause:{name}",
        source_ref=f"autopilot:{name}",
        actor="autopilot",
        summary=f"Поставить на паузу {name}",
        targets=(
            ProposedTarget(
                claim_id=f"claim-{name}",
                ordinal=0,
                action_kind="PAUSE_AD",
                account_id="act-1",
                adset_id="adset-1",
                subject_id=f"ad-{name}",
                city="CityA",
                language="L1",
                intended_payload=payload,
                intended_payload_sha256=canonical_sha256(payload),
            ),
        ),
        evidence=(
            EvidenceRecord(
                evidence_kind="PROPOSAL_INVENTORY",
                source_system="FACEBOOK",
                subject_id=f"ad-{name}",
                observed_at=NOW,
                complete=True,
                payload=evidence_payload,
                payload_sha256=canonical_sha256(evidence_payload),
            ),
        ),
        config_version_sha256="c" * 64,
        valid_until=NOW + timedelta(hours=24),
        staged_media_root=None,
    )
    return OwnerActionRepository(settings.db_path).propose_action(
        plan,
        now=NOW,
    ).proposal_id


def _rows(settings: OwnerApprovalConfig, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _message_update(
    *,
    update_id: int,
    text: str,
    message_id: int,
    reply_to_message_id: int | None,
    user_id: int = OWNER_ID,
    chat_id: int = CHAT_ID,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": message_id,
        "from": {"id": user_id},
        "chat": {"id": chat_id},
        "text": text,
    }
    if reply_to_message_id is not None:
        message["reply_to_message"] = {"message_id": reply_to_message_id}
    return {"update_id": update_id, "message": message}


def _delivered(
    settings: OwnerApprovalConfig,
    *,
    name: str = "a",
) -> tuple[str, int, OwnerDeliveryOutbox, RecordingSender]:
    """Создаёт предложение и доставляет карточку; возвращает message_id карточки."""

    proposal_id = _make_proposal(settings, name=name)
    sender = RecordingSender()
    outbox = OwnerDeliveryOutbox(settings.db_path, settings, sender)
    outbox.deliver(worker_id="delivery-1", now=NOW)
    card_message_id = int(
        _rows(
            settings,
            "SELECT telegram_message_id FROM telegram_delivery_outbox "
            "WHERE proposal_id = ? AND state = 'SENT'",
            (proposal_id,),
        )[0]["telegram_message_id"]
    )
    return proposal_id, card_message_id, outbox, sender


def _service(
    settings: OwnerApprovalConfig,
    outbox: OwnerDeliveryOutbox,
    updates: list[dict[str, object]],
) -> tuple[OwnerApprovalTelegram, RecordingAck]:
    ack = RecordingAck()
    return (
        OwnerApprovalTelegram(
            settings.db_path,
            settings,
            FakePollClient(updates),
            delivery_outbox=outbox,
            ack_client=ack,
        ),
        ack,
    )


@pytest.mark.parametrize(
    ("text", "days"),
    [
        ("ещё день", 1),
        ("еще 1 день", 1),
        ("дай день", 1),
        ("завтра", 1),
        ("через 3 дня", 3),
        ("5 дней", 5),
        ("2 дн", 2),
        ("отклони это", None),
        ("подумаю", None),
        ("через 90 дней", None),
    ],
)
def test_parse_postpone_days(text: str, days: int | None) -> None:
    assert OwnerApprovalTelegram.parse_postpone_days(text) == days


def test_reply_one_more_day_postpones_with_confirmation(
    settings: OwnerApprovalConfig,
) -> None:
    proposal_id, card_message_id, outbox, sender = _delivered(settings)
    service, _ack = _service(
        settings,
        outbox,
        [
            _message_update(
                update_id=5001,
                text="ещё день, посмотрим на выходные",
                message_id=6001,
                reply_to_message_id=card_message_id,
            )
        ],
    )

    service.poll(now=NOW + timedelta(minutes=1))
    batch = service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    assert batch.failed_count == 0
    record = batch.feedback[0]
    assert record.parsed_action == "POSTPONE"
    assert record.proposal_id == proposal_id
    decision = _rows(settings, "SELECT decision_kind, reason_text FROM owner_action_decisions")[0]
    assert decision["decision_kind"] == "POSTPONE"
    assert "ещё день" in str(decision["reason_text"])
    lifecycle = _rows(settings, "SELECT state FROM owner_action_lifecycle")[0]
    assert lifecycle["state"] == "DELIVERY_PENDING"
    redelivery = _rows(
        settings,
        "SELECT generation, next_attempt_at FROM telegram_delivery_outbox "
        "WHERE proposal_id = ? ORDER BY generation DESC",
        (proposal_id,),
    )[0]
    assert int(redelivery["generation"]) == 2
    assert str(redelivery["next_attempt_at"]).startswith("2026-07-28")
    feedback = _rows(settings, "SELECT * FROM owner_feedback")[0]
    assert feedback["parsed_action"] == "POSTPONE"
    assert feedback["parsed_until"] is not None
    assert feedback["text"] == "ещё день, посмотрим на выходные"
    ack_message = _rows(
        settings,
        "SELECT rendered_text, reply_to_message_id FROM owner_trail_messages "
        "WHERE trail_kind='FEEDBACK_ACK'",
    )[0]
    assert "отложено до 2026-07-28" in str(ack_message["rendered_text"])
    assert "комментарий записан" in str(ack_message["rendered_text"])
    assert int(ack_message["reply_to_message_id"]) == 6001


def test_unknown_reply_text_is_stored_as_comment(
    settings: OwnerApprovalConfig,
) -> None:
    proposal_id, card_message_id, outbox, _sender = _delivered(settings)
    service, _ack = _service(
        settings,
        outbox,
        [
            _message_update(
                update_id=5101,
                text="этот креатив мне никогда не нравился, но пусть покрутится",
                message_id=6101,
                reply_to_message_id=card_message_id,
            )
        ],
    )

    service.poll(now=NOW + timedelta(minutes=1))
    batch = service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    assert batch.feedback[0].parsed_action == "COMMENT"
    feedback = _rows(settings, "SELECT * FROM owner_feedback")[0]
    assert feedback["parsed_action"] == "COMMENT"
    assert feedback["parsed_until"] is None
    assert feedback["proposal_id"] == proposal_id
    assert "никогда не нравился" in str(feedback["text"])
    # Решение НЕ записано: незнакомый текст ничего не решает.
    assert _rows(settings, "SELECT * FROM owner_action_decisions") == []
    ack_message = _rows(
        settings,
        "SELECT rendered_text FROM owner_trail_messages WHERE trail_kind='FEEDBACK_ACK'",
    )[0]
    assert "Комментарий записан к предложению" in str(ack_message["rendered_text"])
    assert proposal_id in str(ack_message["rendered_text"])


def test_reply_from_foreign_user_is_ignored_silently(
    settings: OwnerApprovalConfig,
) -> None:
    _proposal_id, card_message_id, outbox, _sender = _delivered(settings)
    service, ack = _service(
        settings,
        outbox,
        [
            _message_update(
                update_id=5201,
                text="ещё день",
                message_id=6201,
                reply_to_message_id=card_message_id,
                user_id=999_999,
            )
        ],
    )

    service.poll(now=NOW + timedelta(minutes=1))
    batch = service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    assert batch.feedback == ()
    assert batch.failed_count == 0
    assert _rows(settings, "SELECT * FROM owner_feedback") == []
    assert _rows(settings, "SELECT * FROM owner_action_decisions") == []
    assert _rows(settings, "SELECT * FROM owner_trail_messages") == []
    assert ack.answers == []
    inbox = _rows(settings, "SELECT state FROM telegram_update_inbox")[0]
    assert inbox["state"] == "PROCESSED"


def test_reply_without_target_is_stored_without_proposal(
    settings: OwnerApprovalConfig,
) -> None:
    _proposal_id, _card_message_id, outbox, _sender = _delivered(settings)
    service, _ack = _service(
        settings,
        outbox,
        [
            _message_update(
                update_id=5301,
                text="ещё день",
                message_id=6301,
                reply_to_message_id=None,
            )
        ],
    )

    service.poll(now=NOW + timedelta(minutes=1))
    batch = service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    record = batch.feedback[0]
    assert (record.parsed_action, record.proposal_id) == ("COMMENT", None)
    assert _rows(settings, "SELECT * FROM owner_action_decisions") == []


def test_digest_command_triggers_manual_batch(settings: OwnerApprovalConfig) -> None:
    _make_proposal(settings, name="a")
    _make_proposal(settings, name="b")
    sender = RecordingSender()
    outbox = OwnerDeliveryOutbox(settings.db_path, settings, sender, digest_hour=23)
    outbox.deliver(worker_id="delivery-1", now=NOW)
    assert sender.payloads == []
    service, _ack = _service(
        settings,
        outbox,
        [
            _message_update(
                update_id=5401,
                text="/digest",
                message_id=6401,
                reply_to_message_id=None,
            )
        ],
    )

    service.poll(now=NOW + timedelta(minutes=1))
    batch = service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    assert batch.feedback[0].parsed_action == "DIGEST_NOW"
    digest = _rows(settings, "SELECT digest_trigger, item_count FROM owner_digest_runs")[0]
    assert (digest["digest_trigger"], digest["item_count"]) == ("MANUAL", 2)
    # Пакет уходит В ЭТОМ ЖЕ вызове: раньше _deliver_manual_digest не звал
    # deliver(), и владелец не видел ничего до следующего тика крона (15 минут).
    texts = [str(payload["text"]) for payload in sender.payloads]
    assert sum(1 for text in texts if "Поставить на паузу" in text) == 2
    assert any("Дайджест предложений" in text for text in texts)
    # И подтверждение владельцу тоже уходит сразу, а не ложится в очередь.
    assert any("Отправил" in text for text in texts)

    # Следующий тик крона ничего не дублирует.
    run = outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=2))
    assert (run.sent_count, run.trail_sent_count) == (0, 0)


def test_repeated_update_does_not_break_inbox(
    settings: OwnerApprovalConfig,
) -> None:
    """Повторная выдача апдейта не роняет инбокс.

    Регрессия: Telegram переотдавал апдейт, пока offset не подтверждён, а
    голый INSERT в owner_feedback падал на UNIQUE constraint и ронял всю
    пачку — offset снова не подтверждался. Инбокс крутился в этом кругу и не
    читал ни одной новой команды владельца.
    """

    _proposal_id, _message_id, outbox, _sender = _delivered(settings)
    service, _ack = _service(settings, outbox, [])

    common = {
        "update_id": 5801,
        "proposal_id": None,
        "digest_id": None,
        "message_id": 6801,
        "reply_to_message_id": None,
        "owner_user_id": OWNER_ID,
        "chat_id": CHAT_ID,
        "text": "дороговато",
        "parsed_action": "COMMENT",
        "parsed_until": None,
    }
    first_id = service._persist_feedback(now=NOW + timedelta(minutes=1), **common)
    # Тот же апдейт во второй раз: раньше здесь падал UNIQUE constraint и
    # ронял всю пачку инбокса вместе с ещё не прочитанными командами.
    second_id = service._persist_feedback(now=NOW + timedelta(minutes=2), **common)

    assert first_id == second_id
    assert len(_rows(settings, "SELECT * FROM owner_feedback")) == 1


def test_approve_all_command_approves_every_pending_card(
    settings: OwnerApprovalConfig,
) -> None:
    """`/approve_all` закрывает карточки, пришедшие поштучно, одной командой.

    Сценарий: десятки запусков доставились вне дайджеста (их первая отправка
    сорвалась, замена уходит немедленно), батч-кнопка живёт только на шапке
    дайджеста — одобрить пачкой было нечем, а руками это десятки нажатий.
    """

    _first_id, _first_message, outbox, sender = _delivered(settings, name="a")
    second_id = _make_proposal(settings, name="b")
    outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(seconds=30))
    pending = _rows(
        settings,
        "SELECT proposal_id FROM owner_action_lifecycle WHERE state = 'PENDING_OWNER'",
    )
    assert len(pending) == 2, "обе карточки должны ждать решения владельца"

    service, _ack = _service(
        settings,
        outbox,
        [
            _message_update(
                update_id=5601,
                text="/approve_all",
                message_id=6601,
                reply_to_message_id=None,
            )
        ],
    )
    service.poll(now=NOW + timedelta(minutes=1))
    batch = service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    # Справочник parsed_action ограничен CHECK-ом миграции 023, поэтому команда
    # пишется как COMMENT; отличима она по сохранённому тексту.
    assert batch.feedback[0].parsed_action == "COMMENT"
    assert (
        _rows(settings, "SELECT text FROM owner_feedback")[0]["text"] == "/approve_all"
    )

    decisions = _rows(
        settings,
        "SELECT proposal_id, decision_kind, owner_user_id FROM owner_action_decisions",
    )
    assert len(decisions) == 2
    assert {str(row["decision_kind"]) for row in decisions} == {"APPROVE"}
    # Родословная остаётся владельческой: это его решение, не системное.
    assert {int(row["owner_user_id"]) for row in decisions} == {OWNER_ID}
    assert second_id in {str(row["proposal_id"]) for row in decisions}

    states = {
        str(row["state"])
        for row in _rows(settings, "SELECT state FROM owner_action_lifecycle")
    }
    assert states == {"APPROVED"}
    assert any("Одобрил 2 из 2" in str(p["text"]) for p in sender.payloads)


def test_approve_all_command_is_idempotent_on_repeat(
    settings: OwnerApprovalConfig,
) -> None:
    """Повтор команды не создаёт вторых решений по уже одобренным карточкам."""

    _proposal_id, _message_id, outbox, sender = _delivered(settings, name="a")
    service, _ack = _service(
        settings,
        outbox,
        [
            _message_update(
                update_id=5701,
                text="/approve_all",
                message_id=6701,
                reply_to_message_id=None,
            ),
            _message_update(
                update_id=5702,
                text="/approve_all",
                message_id=6702,
                reply_to_message_id=None,
            ),
        ],
    )
    service.poll(now=NOW + timedelta(minutes=1))
    service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))
    service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=2))

    assert len(_rows(settings, "SELECT * FROM owner_action_decisions")) == 1
    assert any("Нечего одобрять" in str(p["text"]) for p in sender.payloads)


def test_feedback_is_included_in_training_export(
    settings: OwnerApprovalConfig,
) -> None:
    import io
    import json

    proposal_id, card_message_id, outbox, _sender = _delivered(settings)
    service, _ack = _service(
        settings,
        outbox,
        [
            _message_update(
                update_id=5501,
                text="дорого, но лидов много — оставь",
                message_id=6501,
                reply_to_message_id=card_message_id,
            )
        ],
    )
    service.poll(now=NOW + timedelta(minutes=1))
    service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    sink = io.StringIO()
    summary = export_owner_training_dataset(
        date_from=NOW - timedelta(days=1),
        date_to=NOW + timedelta(days=1),
        sink=sink,
        db_path=settings.db_path,
    )

    documents = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert summary.owner_feedback_count == 1
    document = next(
        item for item in documents if item["proposal"]["proposal_id"] == proposal_id
    )
    feedback = document["owner_feedback"]
    assert len(feedback) == 1
    assert feedback[0]["text"] == "дорого, но лидов много — оставь"
    assert feedback[0]["parsed_action"] == "COMMENT"
    assert feedback[0]["reply_to_message_id"] == card_message_id
    assert document["action_verifications"] == []


def test_feedback_rows_are_immutable(settings: OwnerApprovalConfig) -> None:
    _proposal_id, card_message_id, outbox, _sender = _delivered(settings)
    service, _ack = _service(
        settings,
        outbox,
        [
            _message_update(
                update_id=5601,
                text="комментарий",
                message_id=6601,
                reply_to_message_id=card_message_id,
            )
        ],
    )
    service.poll(now=NOW + timedelta(minutes=1))
    service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    connection = sqlite3.connect(settings.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE owner_feedback SET text = 'подделка'")
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute("DELETE FROM owner_feedback")
    finally:
        connection.close()
