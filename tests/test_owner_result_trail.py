"""Полный видимый след решения «нажал → исполнено → проверено» (волна E, E5)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import config as config_module
from config import OwnerApprovalConfig
from services import owner_action_executor
from services.database_migrations import apply_runtime_migrations
from services.owner_action_models import (
    EvidenceRecord,
    LifecycleState,
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
    latest_proposal_message_id,
)


NOW = datetime(2026, 7, 27, 4, 0, tzinfo=timezone.utc)
OWNER_ID = 42
CHAT_ID = 777


class RecordingSender:
    def __init__(self, first_message_id: int = 4000) -> None:
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
        self.answers.append({"text": text})

    def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append({"message_id": message_id, "text": text})


class FakePollClient:
    def __init__(self, updates: list[dict[str, object]]) -> None:
        self.updates = updates

    def get_updates(self, *, offset: int) -> list[dict[str, object]]:
        return self.updates


@pytest.fixture
def settings(tmp_path, monkeypatch) -> OwnerApprovalConfig:
    value = OwnerApprovalConfig(
        bot_token="123456789:AATrailBotTokenForOwnerTests",
        chat_id=CHAT_ID,
        owner_user_id=OWNER_ID,
        callback_secret="callback-7Cq9_Zv2-Nm8!Lp4-Rs6@Wx3-Kd5",
        webhook_secret="webhook-2Jf8_Yt4-Pq6!Bn9-Hm3@Vs7-Lx5",
        db_path=tmp_path / "trail.db",
    )
    apply_runtime_migrations(str(value.db_path))
    monkeypatch.setattr(
        config_module,
        "load_owner_approval_config",
        lambda source=None: value,
    )
    monkeypatch.setattr(
        owner_action_executor,
        "_repository",
        lambda: OwnerActionRepository(value.db_path),
    )
    return value


def _make_proposal(
    settings: OwnerApprovalConfig,
    *,
    kind: ProposalKind = ProposalKind.PAUSE,
    name: str = "a",
) -> str:
    action_kind = {
        ProposalKind.PAUSE: "PAUSE_AD",
        ProposalKind.LAUNCH: "CREATE_AD",
    }[kind]
    payload = {"status": "PAUSED", "ad": name}
    evidence_payload = {"effective_status": "ACTIVE"}
    plan = ProposedActionPlan(
        proposal_kind=kind,
        origin=ProposalOrigin.AUTOPILOT,
        idempotency_key=f"trail:{kind.value}:{name}",
        source_ref=f"autopilot:{name}",
        actor="autopilot",
        summary=f"Действие {kind.value} по {name}",
        targets=(
            ProposedTarget(
                claim_id=f"claim-{name}",
                ordinal=0,
                action_kind=action_kind,
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


def _deliver_card(
    settings: OwnerApprovalConfig,
    *,
    kind: ProposalKind = ProposalKind.PAUSE,
    name: str = "a",
) -> tuple[str, int, OwnerDeliveryOutbox, RecordingSender]:
    proposal_id = _make_proposal(settings, kind=kind, name=name)
    sender = RecordingSender()
    outbox = OwnerDeliveryOutbox(settings.db_path, settings, sender)
    outbox.deliver(worker_id="delivery-1", now=NOW)
    message_id = latest_proposal_message_id(settings.db_path, proposal_id)
    assert message_id is not None
    return proposal_id, message_id, outbox, sender


def _run(
    proposal_id: str,
    *,
    state: str,
    reason_code: str,
    reconciliation_required: bool = False,
    mutations: int = 1,
) -> owner_action_executor.ActionRun:
    return owner_action_executor.ActionRun(
        proposal_id=proposal_id,
        decision_id="decision-1",
        job_id="job-1",
        claim_id="claim-a",
        state=state,
        reason_code=reason_code,
        safety_review=None,
        execution=None,
        provider_mutation_count=mutations,
        reconciliation_required=reconciliation_required,
    )


def test_successful_execution_replies_to_card(settings: OwnerApprovalConfig) -> None:
    proposal_id, card_message_id, _outbox, _sender = _deliver_card(settings)

    owner_action_executor.report_execution_result(
        _run(
            proposal_id,
            state=LifecycleState.EXECUTED.value,
            reason_code="ALL_CLAIMS_TERMINAL",
        ),
        now=NOW + timedelta(minutes=2),
    )

    trail = _rows(
        settings,
        "SELECT * FROM owner_trail_messages WHERE trail_kind='EXECUTION_RESULT'",
    )
    assert len(trail) == 1
    assert str(trail[0]["rendered_text"]).startswith("✅ Сработало: PAUSE")
    assert int(trail[0]["reply_to_message_id"]) == card_message_id
    assert trail[0]["proposal_id"] == proposal_id


def test_failed_execution_reports_reason(settings: OwnerApprovalConfig) -> None:
    proposal_id, card_message_id, _outbox, _sender = _deliver_card(settings)

    owner_action_executor.report_execution_result(
        _run(
            proposal_id,
            state=LifecycleState.BLOCKED_STALE.value,
            reason_code="LIVE_EVIDENCE_DRIFT",
            mutations=0,
        ),
        now=NOW + timedelta(minutes=2),
    )

    trail = _rows(
        settings,
        "SELECT rendered_text, reply_to_message_id FROM owner_trail_messages "
        "WHERE trail_kind='EXECUTION_RESULT'",
    )[0]
    assert str(trail["rendered_text"]).startswith("❌ Не сработало: PAUSE")
    assert "LIVE_EVIDENCE_DRIFT" in str(trail["rendered_text"])
    assert int(trail["reply_to_message_id"]) == card_message_id


def test_launch_success_does_not_claim_started_before_active(
    settings: OwnerApprovalConfig,
) -> None:
    proposal_id, _card_message_id, _outbox, _sender = _deliver_card(
        settings,
        kind=ProposalKind.LAUNCH,
        name="launch",
    )

    owner_action_executor.report_execution_result(
        _run(
            proposal_id,
            state=LifecycleState.EXECUTED.value,
            reason_code="ALL_CLAIMS_TERMINAL",
        ),
        now=NOW + timedelta(minutes=2),
    )

    text = str(
        _rows(
            settings,
            "SELECT rendered_text FROM owner_trail_messages "
            "WHERE trail_kind='EXECUTION_RESULT'",
        )[0]["rendered_text"]
    )
    assert text.startswith("🕒 Принято Facebook")
    assert "Сработало" not in text
    assert "Запущено" not in text


def test_execution_result_is_deduplicated(settings: OwnerApprovalConfig) -> None:
    proposal_id, _card_message_id, _outbox, _sender = _deliver_card(settings)
    run = _run(
        proposal_id,
        state=LifecycleState.EXECUTED.value,
        reason_code="ALL_CLAIMS_TERMINAL",
    )

    owner_action_executor.report_execution_result(run, now=NOW + timedelta(minutes=2))
    owner_action_executor.report_execution_result(run, now=NOW + timedelta(minutes=3))

    assert len(
        _rows(
            settings,
            "SELECT * FROM owner_trail_messages WHERE trail_kind='EXECUTION_RESULT'",
        )
    ) == 1


def test_reconciliation_required_is_reported_as_failure(
    settings: OwnerApprovalConfig,
) -> None:
    proposal_id, _card_message_id, _outbox, _sender = _deliver_card(settings)

    owner_action_executor.report_execution_result(
        _run(
            proposal_id,
            state=LifecycleState.EXECUTED.value,
            reason_code="POST_ATTEMPT_UNKNOWN",
            reconciliation_required=True,
        ),
        now=NOW + timedelta(minutes=2),
    )

    text = str(
        _rows(
            settings,
            "SELECT rendered_text FROM owner_trail_messages "
            "WHERE trail_kind='EXECUTION_RESULT'",
        )[0]["rendered_text"]
    )
    assert text.startswith("❌ Не сработало")
    assert "POST_ATTEMPT_UNKNOWN" in text


def test_full_trail_lands_in_one_thread(settings: OwnerApprovalConfig) -> None:
    """Ack на карточке + результат + вердикт — три шага одного решения."""

    proposal_id, card_message_id, outbox, sender = _deliver_card(settings)
    approve = next(
        button["callback_data"]
        for button in sender.payloads[0]["inline_keyboard"][0]
        if button["text"] == "Одобрить"
    )
    ack = RecordingAck()
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(
            [
                {
                    "update_id": 8001,
                    "callback_query": {
                        "id": "cbq-8001",
                        "from": {"id": OWNER_ID},
                        "data": approve,
                        "message": {
                            "message_id": card_message_id,
                            "chat": {"id": CHAT_ID},
                        },
                    },
                }
            ]
        ),
        delivery_outbox=outbox,
        ack_client=ack,
    )

    # 1. Нажатие: мгновенный ack и правка карточки.
    service.poll(now=NOW + timedelta(minutes=1))
    batch = service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))
    assert batch.decision_count == 1
    assert ack.answers[0]["text"] == "⏳ Принято, исполняю"
    assert ack.edits[0]["message_id"] == card_message_id

    # 2. Завершение исполнения.
    owner_action_executor.report_execution_result(
        _run(
            proposal_id,
            state=LifecycleState.EXECUTED.value,
            reason_code="ALL_CLAIMS_TERMINAL",
        ),
        now=NOW + timedelta(minutes=2),
    )

    # 3. Вердикт независимого верификатора.
    enqueue_trail_message(
        settings.db_path,
        trail_kind="VERIFICATION_VERDICT",
        dedupe_key=f"verify:{proposal_id}:claim-a:1",
        rendered_text="🔎 Проверено в FB: реально PAUSED",
        proposal_id=proposal_id,
        reply_to_message_id=card_message_id,
        now=NOW + timedelta(minutes=20),
    )

    run = outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=21))

    assert run.trail_sent_count == 2
    replies = [
        payload
        for payload in sender.payloads
        if payload.get("reply_to_message_id") == card_message_id
    ]
    assert len(replies) == 2
    assert str(replies[0]["text"]).startswith("✅ Сработало")
    assert str(replies[1]["text"]).startswith("🔎 Проверено в FB")
    thread = _rows(
        settings,
        "SELECT trail_kind, state, reply_to_message_id FROM owner_trail_messages "
        "ORDER BY created_at",
    )
    assert [row["trail_kind"] for row in thread] == [
        "EXECUTION_RESULT",
        "VERIFICATION_VERDICT",
    ]
    assert all(row["state"] == "SENT" for row in thread)
    assert all(int(row["reply_to_message_id"]) == card_message_id for row in thread)


def test_ack_answer_goes_out_before_decision_is_recorded(
    settings: OwnerApprovalConfig,
) -> None:
    """Ответ на нажатие уходит ДО записи решения и до постановки задания.

    Инцидент: ack шёл последним шагом обработки, а сама обработка ехала кроном
    доставки раз в 15 минут — Telegram отклонял ответ как устаревший
    («Ack callback не ушёл: HTTPError»), и владелец не видел ни «⏳ Принято», ни
    причины отказа. Ответ обязан идти первым: он не денежная операция, его
    просрочка необратима, а потеря — нет.
    """

    proposal_id, card_message_id, outbox, sender = _deliver_card(settings)
    approve = next(
        button["callback_data"]
        for button in sender.payloads[0]["inline_keyboard"][0]
        if button["text"] == "Одобрить"
    )

    observed: dict[str, int] = {}

    class ProbingAck(RecordingAck):
        def answer_callback(self, *, callback_query_id: str, text: str) -> None:
            # Снимок БД ровно в момент ответа: решения ещё быть не должно.
            observed["decisions_at_ack"] = len(
                _rows(settings, "SELECT decision_id FROM owner_action_decisions")
            )
            observed["jobs_at_ack"] = len(
                _rows(settings, "SELECT job_id FROM owner_execution_jobs")
            )
            super().answer_callback(callback_query_id=callback_query_id, text=text)

    ack = ProbingAck()
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(
            [
                {
                    "update_id": 8101,
                    "callback_query": {
                        "id": "cbq-8101",
                        "from": {"id": OWNER_ID},
                        "data": approve,
                        "message": {
                            "message_id": card_message_id,
                            "chat": {"id": CHAT_ID},
                        },
                    },
                }
            ]
        ),
        delivery_outbox=outbox,
        ack_client=ack,
    )
    service.poll(now=NOW + timedelta(minutes=1))
    batch = service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    assert batch.decision_count == 1
    assert observed == {"decisions_at_ack": 0, "jobs_at_ack": 0}
    # Ответ на один callback ровно один — Telegram второй не принимает.
    assert [item["text"] for item in ack.answers] == ["⏳ Принято, исполняю"]
    # Решение и задание всё равно записаны, а карточка помечена «Принято».
    assert len(_rows(settings, "SELECT job_id FROM owner_execution_jobs")) == 1
    assert ack.edits[0]["message_id"] == card_message_id
    assert "⏳ Принято, исполняю" in str(ack.edits[0]["text"])


def test_ack_network_failure_does_not_cancel_decision(
    settings: OwnerApprovalConfig,
) -> None:
    """Недоступный Telegram не отменяет уже нажатую кнопку владельца."""

    proposal_id, card_message_id, outbox, sender = _deliver_card(settings)
    approve = next(
        button["callback_data"]
        for button in sender.payloads[0]["inline_keyboard"][0]
        if button["text"] == "Одобрить"
    )

    class BrokenAck(RecordingAck):
        def answer_callback(self, *, callback_query_id: str, text: str) -> None:
            raise ConnectionError("query is too old")

        def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
            raise ConnectionError("telegram недоступен")

    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(
            [
                {
                    "update_id": 8102,
                    "callback_query": {
                        "id": "cbq-8102",
                        "from": {"id": OWNER_ID},
                        "data": approve,
                        "message": {
                            "message_id": card_message_id,
                            "chat": {"id": CHAT_ID},
                        },
                    },
                }
            ]
        ),
        delivery_outbox=outbox,
        ack_client=BrokenAck(),
    )
    service.poll(now=NOW + timedelta(minutes=1))
    batch = service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    assert batch.decision_count == 1
    assert batch.failed_count == 0
    decisions = _rows(
        settings,
        "SELECT decision_kind FROM owner_action_decisions",
    )
    assert [row["decision_kind"] for row in decisions] == ["APPROVE"]
    job = _rows(settings, "SELECT state FROM owner_execution_jobs")
    assert [row["state"] for row in job] == ["QUEUED"]
    inbox = _rows(
        settings,
        "SELECT state FROM telegram_update_inbox WHERE update_id = 8102",
    )
    assert [row["state"] for row in inbox] == ["PROCESSED"]


def test_ack_answer_timeout_is_short(
    settings: OwnerApprovalConfig,
    monkeypatch,
) -> None:
    """Ответ на нажатие идёт с коротким таймаутом: просрочка хуже потери."""

    import requests

    from services.owner_approval_telegram import (
        ACK_ANSWER_TIMEOUT_SECONDS,
        RequestsTelegramAckClient,
    )

    calls: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    def _fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(requests, "post", _fake_post)
    client = RequestsTelegramAckClient(settings.bot_token)
    client.answer_callback(callback_query_id="cbq-1", text="⏳ Принято")

    assert calls[0]["timeout"] == ACK_ANSWER_TIMEOUT_SECONDS
    assert 3 <= ACK_ANSWER_TIMEOUT_SECONDS <= 5
    assert "answerCallbackQuery" in str(calls[0]["url"])


def test_trail_send_failure_is_visible_and_retriable(
    settings: OwnerApprovalConfig,
) -> None:
    proposal_id, card_message_id, _outbox, _sender = _deliver_card(settings)
    enqueue_trail_message(
        settings.db_path,
        trail_kind="EXECUTION_RESULT",
        dedupe_key=f"exec-result:{proposal_id}:job-1:EXECUTED",
        rendered_text="✅ Сработало: PAUSE",
        proposal_id=proposal_id,
        reply_to_message_id=card_message_id,
        now=NOW + timedelta(minutes=2),
    )

    class FailingSender(RecordingSender):
        def send_message(self, **payload: object) -> int:
            raise ConnectionError("telegram недоступен")

    failing = OwnerDeliveryOutbox(settings.db_path, settings, FailingSender())
    failing.deliver(worker_id="delivery-2", now=NOW + timedelta(minutes=3))

    trail = _rows(
        settings,
        "SELECT state, last_error_code FROM owner_trail_messages "
        "WHERE trail_kind='EXECUTION_RESULT'",
    )[0]
    assert trail["state"] == "FAILED_VISIBLE"
    assert trail["last_error_code"] == "ConnectionError"
