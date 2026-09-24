"""Дневной дайджест предложений и батч-кнопки (волна E, блоки E2 и E3)."""

from __future__ import annotations

import json
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
from services.owner_delivery_outbox import (
    OwnerDeliveryOutbox,
    enqueue_trail_message,
    next_digest_moment,
)


# 04:00 UTC = 09:00 CityA — ровно час дайджеста по умолчанию.
DIGEST_MOMENT = datetime(2026, 7, 27, 4, 0, tzinfo=timezone.utc)
BEFORE_DIGEST = DIGEST_MOMENT - timedelta(hours=2)
DIGEST_HOUR = 9


class RecordingSender:
    def __init__(self, first_message_id: int = 9000) -> None:
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
        self.edits.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text}
        )


class FakePollClient:
    def __init__(self, updates: list[dict[str, object]] | None = None) -> None:
        self.updates = updates or []

    def get_updates(self, *, offset: int) -> list[dict[str, object]]:
        return self.updates


@pytest.fixture
def settings(tmp_path) -> OwnerApprovalConfig:
    value = OwnerApprovalConfig(
        bot_token="123456789:AADigestBotTokenForOwnerTests",
        chat_id=777,
        owner_user_id=42,
        callback_secret="callback-7Cq9_Zv2-Nm8!Lp4-Rs6@Wx3-Kd5",
        webhook_secret="webhook-2Jf8_Yt4-Pq6!Bn9-Hm3@Vs7-Lx5",
        db_path=tmp_path / "digest.db",
    )
    apply_runtime_migrations(str(value.db_path))
    return value


def _make_proposal(
    settings: OwnerApprovalConfig,
    *,
    name: str,
    city: str = "CityA",
    language: str = "L1",
    created_at: datetime = BEFORE_DIGEST,
) -> str:
    payload = {"status": "PAUSED", "ad": name}
    evidence_payload = {"effective_status": "ACTIVE"}
    plan = ProposedActionPlan(
        proposal_kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        idempotency_key=f"digest:pause:{name}",
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
                city=city,
                language=language,
                intended_payload=payload,
                intended_payload_sha256=canonical_sha256(payload),
            ),
        ),
        evidence=(
            EvidenceRecord(
                evidence_kind="PROPOSAL_INVENTORY",
                source_system="FACEBOOK",
                subject_id=f"ad-{name}",
                observed_at=created_at,
                complete=True,
                payload=evidence_payload,
                payload_sha256=canonical_sha256(evidence_payload),
            ),
        ),
        config_version_sha256="c" * 64,
        valid_until=created_at + timedelta(hours=24),
        staged_media_root=None,
    )
    receipt = OwnerActionRepository(settings.db_path).propose_action(
        plan,
        now=created_at,
    )
    return receipt.proposal_id


def _outbox(settings: OwnerApprovalConfig, sender: RecordingSender) -> OwnerDeliveryOutbox:
    return OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        sender,
        digest_hour=DIGEST_HOUR,
    )


def _rows(settings: OwnerApprovalConfig, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def test_next_digest_moment_holds_until_configured_hour() -> None:
    assert next_digest_moment(BEFORE_DIGEST, DIGEST_HOUR) == DIGEST_MOMENT
    # Ровно в час дайджеста ждать не нужно.
    assert next_digest_moment(DIGEST_MOMENT, DIGEST_HOUR) == DIGEST_MOMENT
    # После часа — следующий день.
    assert next_digest_moment(
        DIGEST_MOMENT + timedelta(minutes=1), DIGEST_HOUR
    ) == DIGEST_MOMENT + timedelta(days=1)


def test_proposals_accumulate_silently_and_leave_in_one_batch(
    settings: OwnerApprovalConfig,
) -> None:
    for name in ("a", "b", "c"):
        _make_proposal(settings, name=name)
    sender = RecordingSender()
    outbox = _outbox(settings, sender)

    quiet = outbox.deliver(worker_id="delivery-1", now=BEFORE_DIGEST)

    assert (quiet.sent_count, quiet.trail_sent_count, quiet.digest_id) == (0, 0, None)
    assert sender.payloads == []
    assert all(
        row["state"] == "DELIVERY_PENDING"
        for row in _rows(settings, "SELECT state FROM owner_action_lifecycle")
    )

    batch = outbox.deliver(worker_id="delivery-1", now=DIGEST_MOMENT)

    assert batch.sent_count == 3
    assert batch.trail_sent_count == 1
    assert batch.digest_id is not None
    # Шапка уходит ПОСЛЕ карточек пакета: её кнопка «Одобрить все» берёт только
    # позиции в PENDING_OWNER и требует доставленной карточки, а батч-токен
    # одноразовый. Нажатие до доставки карточек сжигало токен впустую, и
    # uq_owner_digest_item_proposal навсегда запрещал этим предложениям попасть
    # в другой дайджест.
    header = sender.payloads[-1]
    assert "Дайджест предложений" in str(header["text"])
    assert "Всего: 3" in str(header["text"])
    labels = [button[0]["text"] for button in header["inline_keyboard"]]
    assert labels == [
        "✅ Одобрить все (3)",
        "❌ Отклонить все (3)",
        "🔎 Решать по одной",
    ]
    assert len(sender.payloads) == 4
    digest = _rows(settings, "SELECT * FROM owner_digest_runs")[0]
    assert (digest["digest_trigger"], digest["item_count"]) == ("SCHEDULE", 3)
    assert len(_rows(settings, "SELECT * FROM owner_digest_items")) == 3
    assert all(
        row["state"] == "PENDING_OWNER"
        for row in _rows(settings, "SELECT state FROM owner_action_lifecycle")
    )


def test_digest_is_deduplicated_within_one_day(
    settings: OwnerApprovalConfig,
) -> None:
    _make_proposal(settings, name="a")
    sender = RecordingSender()
    outbox = _outbox(settings, sender)

    first = outbox.deliver(worker_id="delivery-1", now=DIGEST_MOMENT)
    _make_proposal(settings, name="b", created_at=DIGEST_MOMENT)
    second = outbox.deliver(
        worker_id="delivery-1",
        now=DIGEST_MOMENT + timedelta(minutes=15),
    )

    assert first.digest_id is not None
    assert second.digest_id is None
    assert len(_rows(settings, "SELECT * FROM owner_digest_runs")) == 1
    # Второе предложение молча ждёт следующего дайджеста.
    assert second.sent_count == 0
    assert len(_rows(settings, "SELECT * FROM owner_digest_items")) == 1


def test_digest_groups_by_city_language_and_marks_stale_evidence(
    settings: OwnerApprovalConfig,
) -> None:
    _make_proposal(settings, name="a", city="CityA", language="L1")
    _make_proposal(settings, name="b", city="CityA", language="L1")
    _make_proposal(settings, name="c", city="CityB", language="L2")
    # Это предложение к часу дайджеста уже протухло по свежести проверки.
    _make_proposal(
        settings,
        name="old",
        city="CityC",
        language="L1",
        created_at=DIGEST_MOMENT - timedelta(hours=20),
    )
    sender = RecordingSender()

    _outbox(settings, sender).deliver(worker_id="delivery-1", now=DIGEST_MOMENT)

    header = str(sender.payloads[-1]["text"])
    assert "• CityA · L1 · PAUSE — 2" in header
    assert "• CityB · L2 · PAUSE — 1" in header
    assert "• CityC · L1 · PAUSE — 1" in header
    assert "1 требуют свежей проверки" in header
    stale = _rows(
        settings,
        "SELECT proposal_id FROM owner_digest_items WHERE evidence_stale = 1",
    )
    assert len(stale) == 1


def test_critical_trail_message_bypasses_digest_queue(
    settings: OwnerApprovalConfig,
) -> None:
    """Критика идёт мимо очереди дайджеста — в тот же тик, без пакета."""

    proposal_id = _make_proposal(settings, name="a")
    enqueue_trail_message(
        settings.db_path,
        trail_kind="VERIFICATION_VERDICT",
        dedupe_key="verify:critical",
        rendered_text="⚠️ Расхождение: заявлено PAUSE, в FB — ACTIVE",
        proposal_id=proposal_id,
        now=BEFORE_DIGEST,
    )
    sender = RecordingSender()

    run = _outbox(settings, sender).deliver(worker_id="delivery-1", now=BEFORE_DIGEST)

    assert run.trail_sent_count == 1
    assert run.sent_count == 0
    assert run.digest_id is None
    assert "Расхождение" in str(sender.payloads[0]["text"])


def test_manual_digest_releases_waiting_cards_now(
    settings: OwnerApprovalConfig,
) -> None:
    _make_proposal(settings, name="a")
    _make_proposal(settings, name="b")
    sender = RecordingSender()
    outbox = _outbox(settings, sender)
    outbox.deliver(worker_id="delivery-1", now=BEFORE_DIGEST)
    assert sender.payloads == []

    released = outbox.release_pending_for_digest(now=BEFORE_DIGEST)
    manual = outbox.collect_digest(now=BEFORE_DIGEST, trigger="MANUAL")
    run = outbox.deliver(worker_id="delivery-1", now=BEFORE_DIGEST)

    assert released == 2
    assert manual is not None and manual.trigger == "MANUAL"
    assert run.trail_sent_count == 1
    assert run.sent_count == 2


# ---------------------------------------------------------------------------
# E3: батч-кнопки
# ---------------------------------------------------------------------------


def _batch_callback_update(
    *,
    update_id: int,
    callback_data: str,
    message_id: int,
    user_id: int = 42,
    chat_id: int = 777,
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cbq-{update_id}",
            "from": {"id": user_id},
            "data": callback_data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
        },
    }


def _sent_digest(
    settings: OwnerApprovalConfig,
    sender: RecordingSender,
) -> tuple[str, int, dict[str, str]]:
    """Отправляет дайджест и возвращает (digest_id, header_message_id, callbacks)."""

    outbox = _outbox(settings, sender)
    run = outbox.deliver(worker_id="delivery-1", now=DIGEST_MOMENT)
    assert run.digest_id is not None
    # Шапка уходит последней в пакете — карточки к этому моменту уже доставлены.
    header = sender.payloads[-1]
    callbacks = {
        button[0]["text"]: button[0]["callback_data"]
        for button in header["inline_keyboard"]
    }
    header_message_id = _rows(
        settings,
        "SELECT telegram_message_id FROM owner_trail_messages "
        "WHERE trail_kind='DIGEST_HEADER'",
    )[0]["telegram_message_id"]
    return run.digest_id, int(header_message_id), callbacks


def test_approve_all_runs_independent_decisions_and_reports_partial_outcome(
    settings: OwnerApprovalConfig,
) -> None:
    for name in ("a", "b", "blocked"):
        _make_proposal(settings, name=name)
    sender = RecordingSender()
    digest_id, header_message_id, callbacks = _sent_digest(settings, sender)
    approve_all = next(
        value for label, value in callbacks.items() if label.startswith("✅")
    )
    # Состояние третьего предложения изменилось после дайджеста: истёк срок.
    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute(
            """
            UPDATE owner_action_lifecycle
            SET state = 'EXPIRED', version = version + 1, updated_at = ?
            WHERE proposal_id = (
                SELECT proposal_id FROM owner_action_proposal_targets
                WHERE claim_id = 'claim-blocked'
            )
            """,
            (DIGEST_MOMENT.isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()

    ack = RecordingAck()
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(
            [
                _batch_callback_update(
                    update_id=9101,
                    callback_data=approve_all,
                    message_id=header_message_id,
                )
            ]
        ),
        delivery_outbox=_outbox(settings, sender),
        ack_client=ack,
    )
    service.poll(now=DIGEST_MOMENT + timedelta(minutes=1))
    batch = service.process_inbox(
        worker_id="inbox-1",
        now=DIGEST_MOMENT + timedelta(minutes=1),
    )

    assert batch.failed_count == 0
    report = batch.batch_reports[0]
    assert (report.total, report.approved, report.rejected, report.blocked) == (
        3,
        2,
        0,
        1,
    )
    assert len(report.blocked_reasons) == 1
    assert "EXPIRED" in report.blocked_reasons[0]
    decisions = _rows(
        settings,
        "SELECT decision_kind, telegram_update_id FROM owner_action_decisions",
    )
    assert [row["decision_kind"] for row in decisions] == ["APPROVE", "APPROVE"]
    # N независимых решений — N производных trusted ingress записей.
    assert len({row["telegram_update_id"] for row in decisions}) == 2
    assert all(int(row["telegram_update_id"]) < 0 for row in decisions)
    assert len(_rows(settings, "SELECT * FROM owner_execution_jobs")) == 2
    # Ответ на нажатие уходит ДО записи N решений — иначе Telegram отклоняет его
    # как устаревший и владелец не видит ничего. Итог батча приходит следом
    # отдельным сообщением, а не в toast.
    assert ack.answers[0]["text"] == "⏳ Принято, обрабатываю"
    assert len(ack.answers) == 1
    trail = _rows(
        settings,
        "SELECT rendered_text, digest_id FROM owner_trail_messages "
        "WHERE trail_kind='BATCH_REPORT'",
    )
    assert len(trail) == 1
    assert "одобрено 2" in str(trail[0]["rendered_text"])
    assert "заблокировано 1" in str(trail[0]["rendered_text"])
    assert trail[0]["digest_id"] == digest_id
    assert "Причины блокировок" in trail[0]["rendered_text"]


def test_batch_token_is_single_use(settings: OwnerApprovalConfig) -> None:
    _make_proposal(settings, name="a")
    sender = RecordingSender()
    _, header_message_id, callbacks = _sent_digest(settings, sender)
    reject_all = next(
        value for label, value in callbacks.items() if label.startswith("❌")
    )
    ack = RecordingAck()
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(
            [
                _batch_callback_update(
                    update_id=9201,
                    callback_data=reject_all,
                    message_id=header_message_id,
                ),
                _batch_callback_update(
                    update_id=9202,
                    callback_data=reject_all,
                    message_id=header_message_id,
                ),
            ]
        ),
        delivery_outbox=_outbox(settings, sender),
        ack_client=ack,
    )
    service.poll(now=DIGEST_MOMENT + timedelta(minutes=1))
    batch = service.process_inbox(
        worker_id="inbox-1",
        now=DIGEST_MOMENT + timedelta(minutes=1),
    )

    assert len(batch.batch_reports) == 1
    assert batch.batch_reports[0].rejected == 1
    assert batch.failed_count == 1
    failures = _rows(
        settings,
        "SELECT last_error_code FROM telegram_update_inbox WHERE state='FAILED'",
    )
    assert "DIGEST_TOKEN" in str(failures[0]["last_error_code"])


def test_one_by_one_button_confirms_cards_are_already_there(
    settings: OwnerApprovalConfig,
) -> None:
    _make_proposal(settings, name="a")
    _make_proposal(settings, name="b")
    sender = RecordingSender()
    _, header_message_id, callbacks = _sent_digest(settings, sender)
    one_by_one = next(
        value for label, value in callbacks.items() if label.startswith("🔎")
    )
    ack = RecordingAck()
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(
            [
                _batch_callback_update(
                    update_id=9301,
                    callback_data=one_by_one,
                    message_id=header_message_id,
                )
            ]
        ),
        delivery_outbox=_outbox(settings, sender),
        ack_client=ack,
    )
    service.poll(now=DIGEST_MOMENT + timedelta(minutes=1))
    batch = service.process_inbox(
        worker_id="inbox-1",
        now=DIGEST_MOMENT + timedelta(minutes=1),
    )

    report = batch.batch_reports[0]
    assert (report.batch_kind, report.total, report.approved, report.rejected) == (
        "ONE_BY_ONE",
        2,
        0,
        0,
    )
    assert _rows(settings, "SELECT * FROM owner_action_decisions") == []
    assert "Решаем по одной" in ack.answers[0]["text"]


def test_foreign_user_batch_press_is_rejected_without_decision(
    settings: OwnerApprovalConfig,
) -> None:
    _make_proposal(settings, name="a")
    sender = RecordingSender()
    _, header_message_id, callbacks = _sent_digest(settings, sender)
    approve_all = next(
        value for label, value in callbacks.items() if label.startswith("✅")
    )
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(
            [
                _batch_callback_update(
                    update_id=9401,
                    callback_data=approve_all,
                    message_id=header_message_id,
                    user_id=999_999,
                )
            ]
        ),
        delivery_outbox=_outbox(settings, sender),
        ack_client=RecordingAck(),
    )
    service.poll(now=DIGEST_MOMENT + timedelta(minutes=1))
    batch = service.process_inbox(
        worker_id="inbox-1",
        now=DIGEST_MOMENT + timedelta(minutes=1),
    )

    assert batch.batch_reports == ()
    assert batch.failed_count == 1
    assert _rows(settings, "SELECT * FROM owner_action_decisions") == []
    token = _rows(
        settings,
        "SELECT consumed_at FROM owner_digest_batch_tokens WHERE batch_kind='APPROVE_ALL'",
    )[0]
    assert token["consumed_at"] is None


def test_single_card_press_acks_and_edits_message(
    settings: OwnerApprovalConfig,
) -> None:
    """E5а: на нажатие приходит мгновенный ack и правка карточки."""

    _make_proposal(settings, name="a")
    sender = RecordingSender()
    outbox = _outbox(settings, sender)
    outbox.deliver(worker_id="delivery-1", now=DIGEST_MOMENT)
    # Карточки уходят первыми, шапка дайджеста — последней.
    card = sender.payloads[0]
    approve = next(
        button["callback_data"]
        for button in card["inline_keyboard"][0]
        if button["text"] == "Одобрить"
    )
    card_message_id = _rows(
        settings,
        "SELECT telegram_message_id FROM telegram_delivery_outbox "
        "WHERE purpose='OWNER_PROPOSAL' AND state='SENT'",
    )[0]["telegram_message_id"]
    ack = RecordingAck()
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(
            [
                _batch_callback_update(
                    update_id=9501,
                    callback_data=approve,
                    message_id=int(card_message_id),
                )
            ]
        ),
        delivery_outbox=outbox,
        ack_client=ack,
    )
    service.poll(now=DIGEST_MOMENT + timedelta(minutes=1))
    batch = service.process_inbox(
        worker_id="inbox-1",
        now=DIGEST_MOMENT + timedelta(minutes=1),
    )

    assert batch.decision_count == 1
    assert ack.answers[0]["text"] == "⏳ Принято, исполняю"
    assert ack.edits[0]["message_id"] == int(card_message_id)
    assert "⏳ Принято, исполняю" in str(ack.edits[0]["text"])


def test_digest_header_binds_all_batch_tokens_once(
    settings: OwnerApprovalConfig,
) -> None:
    _make_proposal(settings, name="a")
    sender = RecordingSender()
    _sent_digest(settings, sender)

    tokens = _rows(
        settings,
        "SELECT batch_kind, expected_message_id, bound_at FROM owner_digest_batch_tokens",
    )
    assert len(tokens) == 3
    assert all(row["expected_message_id"] is not None for row in tokens)
    assert all(row["bound_at"] is not None for row in tokens)
    connection = sqlite3.connect(settings.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="one-time"):
            connection.execute(
                "UPDATE owner_digest_batch_tokens SET expected_message_id = 1, "
                "bound_at = ? WHERE batch_kind = 'APPROVE_ALL'",
                (DIGEST_MOMENT.isoformat(),),
            )
    finally:
        connection.close()


def test_digest_button_spec_is_persisted_for_replay(
    settings: OwnerApprovalConfig,
) -> None:
    _make_proposal(settings, name="a")
    sender = RecordingSender()
    _sent_digest(settings, sender)

    header = _rows(
        settings,
        "SELECT button_spec_json, rendered_text_sha256 FROM owner_trail_messages "
        "WHERE trail_kind='DIGEST_HEADER'",
    )[0]
    buttons = json.loads(str(header["button_spec_json"]))
    assert [row[0]["callback_data"].split(":")[0] for row in buttons] == [
        "od",
        "od",
        "od",
    ]
    assert len(str(header["rendered_text_sha256"])) == 64


# ---------------------------------------------------------------------------
# Настройка часа дайджеста (approval.digest_hour)
# ---------------------------------------------------------------------------


def test_digest_hour_setting_is_validated_by_range() -> None:
    from fastapi import HTTPException

    from web.settings_validation import validate_settings_update

    updated = validate_settings_update({"approval": {"digest_hour": 7}}, {})
    assert updated["approval"] == {"digest_hour": 7}

    for invalid in (-1, 24, 99):
        with pytest.raises(HTTPException) as error:
            validate_settings_update({"approval": {"digest_hour": invalid}}, {})
        assert error.value.status_code == 400
        assert "от 0 до 23" in str(error.value.detail)

    for wrong_type in ("9", 9.0, True, None):
        with pytest.raises(HTTPException) as error:
            validate_settings_update({"approval": {"digest_hour": wrong_type}}, {})
        assert "должен быть int" in str(error.value.detail)


def test_unknown_approval_keys_are_rejected() -> None:
    from fastapi import HTTPException

    from web.settings_validation import validate_settings_update

    with pytest.raises(HTTPException, match="Неизвестные поля approval"):
        validate_settings_update({"approval": {"digest_minute": 30}}, {})
    with pytest.raises(HTTPException, match="approval должен быть объектом"):
        validate_settings_update({"approval": 9}, {})


def test_load_digest_hour_falls_back_to_default(monkeypatch) -> None:
    from services import owner_delivery_outbox

    monkeypatch.setattr(
        "agent.scheduler.load_settings",
        lambda: {"approval": {"digest_hour": 6}},
    )
    assert owner_delivery_outbox.load_digest_hour() == 6

    monkeypatch.setattr("agent.scheduler.load_settings", lambda: {})
    assert owner_delivery_outbox.load_digest_hour() == (
        owner_delivery_outbox.DIGEST_DEFAULT_HOUR
    )

    monkeypatch.setattr(
        "agent.scheduler.load_settings",
        lambda: {"approval": {"digest_hour": "утром"}},
    )
    assert owner_delivery_outbox.load_digest_hour() == (
        owner_delivery_outbox.DIGEST_DEFAULT_HOUR
    )


def test_outbox_rejects_invalid_digest_hour(settings: OwnerApprovalConfig) -> None:
    with pytest.raises(ValueError, match="digest_hour"):
        OwnerDeliveryOutbox(settings.db_path, settings, RecordingSender(), digest_hour=24)
