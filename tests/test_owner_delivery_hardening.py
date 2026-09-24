"""Живучесть доставки владельцу: порядок, троттлинг, 429, отказы и ack.

Волна разбора жалобы «карточки пустые, кнопки не работают, /digest молчит».
Каждый тест здесь закрывает конкретную дыру, найденную архитектурным ревью.
"""

from __future__ import annotations

import dataclasses
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
    MAX_DELIVERY_GENERATIONS,
    OwnerDeliveryOutbox,
    TelegramRateLimited,
    enqueue_trail_message,
)


NOW = datetime(2026, 7, 27, 4, 0, tzinfo=timezone.utc)
OWNER_ID = 42
CHAT_ID = 777
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
        self.edits.append({"message_id": message_id, "text": text})


class FakePollClient:
    def __init__(self, updates: list[dict[str, object]] | None = None) -> None:
        self.updates = updates or []

    def get_updates(self, *, offset: int) -> list[dict[str, object]]:
        return self.updates


@pytest.fixture
def settings(tmp_path) -> OwnerApprovalConfig:
    value = OwnerApprovalConfig(
        bot_token="123456789:AAHardeningBotTokenForOwnerTst",
        chat_id=CHAT_ID,
        owner_user_id=OWNER_ID,
        callback_secret="callback-7Cq9_Zv2-Nm8!Lp4-Rs6@Wx3-Kd5",
        webhook_secret="webhook-2Jf8_Yt4-Pq6!Bn9-Hm3@Vs7-Lx5",
        db_path=tmp_path / "hardening.db",
    )
    apply_runtime_migrations(str(value.db_path))
    return value


def _make_proposal(
    settings: OwnerApprovalConfig,
    *,
    name: str,
    created_at: datetime = NOW - timedelta(hours=2),
) -> str:
    payload = {"status": "PAUSED", "ad": name}
    evidence_payload = {"effective_status": "ACTIVE"}
    plan = ProposedActionPlan(
        proposal_kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        idempotency_key=f"hardening:pause:{name}",
        source_ref=f"autopilot:{name}",
        actor="autopilot",
        summary=f"⏸ Предлагаю паузу\nCityA · L1 · adset adset-1\nРеклама «{name}»",
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
    return (
        OwnerActionRepository(settings.db_path)
        .propose_action(plan, now=created_at)
        .proposal_id
    )


def _outbox(
    settings: OwnerApprovalConfig,
    sender: object,
    **kwargs: object,
) -> OwnerDeliveryOutbox:
    return OwnerDeliveryOutbox(
        settings.db_path,
        settings,
        sender,  # type: ignore[arg-type]
        digest_hour=kwargs.pop("digest_hour", DIGEST_HOUR),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _rows(settings: OwnerApprovalConfig, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _callback_update(*, update_id: int, callback_data: str, message_id: int) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": OWNER_ID},
            "data": callback_data,
            "message": {"message_id": message_id, "chat": {"id": CHAT_ID}},
        },
    }


def _message_update(*, update_id: int, text: str, message_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": OWNER_ID},
            "chat": {"id": CHAT_ID},
            "text": text,
        },
    }


def _service(
    settings: OwnerApprovalConfig,
    outbox: OwnerDeliveryOutbox,
    updates: list[dict],
) -> tuple[OwnerApprovalTelegram, RecordingAck]:
    ack = RecordingAck()
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(updates),
        delivery_outbox=outbox,
        ack_client=ack,
    )
    return service, ack


# ---------------------------------------------------------------------------
# Порядок пакета и «Отложить»
# ---------------------------------------------------------------------------


def test_digest_header_is_sent_after_the_cards_it_covers(
    settings: OwnerApprovalConfig,
) -> None:
    """Батч-токен одноразовый и требует доставленных карточек.

    Если шапка уходит первой, владелец может нажать «Одобрить все» раньше, чем
    придут карточки: решения не пройдут (нужен PENDING_OWNER и expected_message_id),
    токен сгорит, а uq_owner_digest_item_proposal навсегда запретит этим
    предложениям попасть в другой дайджест.
    """
    for name in ("a", "b"):
        _make_proposal(settings, name=name)
    sender = RecordingSender()

    run = _outbox(settings, sender).deliver(
        worker_id="delivery-1",
        now=NOW.replace(hour=4),
    )

    assert (run.sent_count, run.trail_sent_count) == (2, 1)
    texts = [str(payload["text"]) for payload in sender.payloads]
    assert "Дайджест предложений" in texts[-1]
    assert all("Дайджест предложений" not in text for text in texts[:-1])


def test_manual_digest_does_not_cancel_owner_postponement(
    settings: OwnerApprovalConfig,
) -> None:
    """«Отложить» — решение владельца, /digest не имеет права его отменять."""
    postponed_id = _make_proposal(settings, name="postponed")
    sender = RecordingSender()
    outbox = _outbox(settings, sender)
    outbox.deliver(worker_id="delivery-1", now=NOW)

    postpone_data = sender.payloads[0]["inline_keyboard"][0][2]["callback_data"]
    service, _ack = _service(
        settings,
        outbox,
        [
            _callback_update(
                update_id=901,
                callback_data=postpone_data,
                message_id=9001,
            )
        ],
    )
    service.poll(now=NOW + timedelta(minutes=1))
    service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    postponed_until = _rows(
        settings,
        "SELECT next_attempt_at FROM telegram_delivery_outbox "
        "WHERE proposal_id = ? AND state = 'PENDING'",
        (postponed_id,),
    )[0]["next_attempt_at"]

    # Свежее предложение и ручной /digest в тот же момент.
    _make_proposal(settings, name="fresh", created_at=NOW + timedelta(minutes=2))
    released = outbox.release_pending_for_digest(now=NOW + timedelta(minutes=3))

    still_postponed = _rows(
        settings,
        "SELECT next_attempt_at FROM telegram_delivery_outbox "
        "WHERE proposal_id = ? AND state = 'PENDING'",
        (postponed_id,),
    )[0]["next_attempt_at"]
    assert still_postponed == postponed_until
    # Отложенная карточка в выпуск не попала.
    assert released == 0


# ---------------------------------------------------------------------------
# Троттлинг, 429 и потолок поколений
# ---------------------------------------------------------------------------


def test_sends_are_throttled_between_messages(settings: OwnerApprovalConfig) -> None:
    for name in ("a", "b", "c"):
        _make_proposal(settings, name=name)
    sender = RecordingSender()
    pauses: list[float] = []
    outbox = _outbox(
        settings,
        sender,
        send_interval_seconds=1.0,
        sleep=pauses.append,
    )

    outbox.deliver(worker_id="delivery-1", now=NOW)

    # 3 карточки + шапка = 4 сообщения, пауза перед каждым кроме первого.
    assert len(sender.payloads) == 4
    assert pauses == [1.0, 1.0, 1.0]


def test_rate_limit_requeues_without_new_generation_or_revoked_buttons(
    settings: OwnerApprovalConfig,
) -> None:
    """429 — это «не показано», а не «неизвестно»: кнопки живы, копий нет."""
    _make_proposal(settings, name="a")
    _make_proposal(settings, name="b")

    class RateLimitedSender:
        def __init__(self) -> None:
            self.calls = 0

        def send_message(self, **_payload: object) -> int:
            self.calls += 1
            raise TelegramRateLimited(17)

    sender = RateLimitedSender()

    run = _outbox(settings, sender).deliver(worker_id="delivery-1", now=NOW)

    assert run.rate_limited_count == 1
    # Прогон прерван на первой же карточке — шторма запросов нет.
    assert sender.calls == 1
    assert run.sent_count == 0
    assert run.failed_visible_count == 0
    deliveries = _rows(
        settings,
        "SELECT generation, state, next_attempt_at, last_error_code "
        "FROM telegram_delivery_outbox WHERE purpose = 'OWNER_PROPOSAL'",
    )
    assert {int(row["generation"]) for row in deliveries} == {1}
    assert {str(row["state"]) for row in deliveries} == {"PENDING"}
    throttled = [row for row in deliveries if row["last_error_code"]][0]
    assert str(throttled["last_error_code"]) == "TELEGRAM_RATE_LIMITED"
    assert str(throttled["next_attempt_at"]).startswith("2026-07-27T04:00:17")
    # Кнопки не отзывались.
    assert (
        _rows(
            settings,
            "SELECT COUNT(*) AS n FROM owner_callback_tokens WHERE revoked_at IS NOT NULL",
        )[0]["n"]
        == 0
    )


def test_unknown_send_result_stops_spawning_generations_at_the_cap(
    settings: OwnerApprovalConfig,
) -> None:
    _make_proposal(settings, name="a")

    class BrokenSender:
        def send_message(self, **_payload: object) -> int:
            raise RuntimeError("соединение оборвалось")

    outbox = _outbox(settings, BrokenSender())
    for minute in range(MAX_DELIVERY_GENERATIONS + 3):
        outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=minute))

    generations = [
        int(row["generation"])
        for row in _rows(
            settings,
            "SELECT generation FROM telegram_delivery_outbox "
            "WHERE purpose = 'OWNER_PROPOSAL' ORDER BY generation",
        )
    ]
    assert generations == list(range(1, MAX_DELIVERY_GENERATIONS + 1))


# ---------------------------------------------------------------------------
# Ротация callback secret не должна быть ядовитой таблеткой
# ---------------------------------------------------------------------------


def test_broken_rotation_of_one_proposal_does_not_stop_the_others(
    settings: OwnerApprovalConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Один битый proposal раньше навсегда останавливал доставку всей очереди."""
    first = _make_proposal(settings, name="a")
    _make_proposal(settings, name="b")
    _outbox(settings, RecordingSender()).deliver(worker_id="delivery-1", now=NOW)

    rotated = dataclasses.replace(
        settings,
        callback_secret="callback-ROTATED_9Fx2-Bn7!Qk4-Zt8@Ws1",
    )
    sender = RecordingSender(first_message_id=9500)
    outbox = OwnerDeliveryOutbox(
        rotated.db_path,
        rotated,
        sender,
        digest_hour=DIGEST_HOUR,
    )
    original = OwnerDeliveryOutbox.prepare_system_redelivery

    def _flaky(self, proposal_id, **kwargs):
        if proposal_id == first:
            raise RuntimeError("битый proposal")
        return original(self, proposal_id, **kwargs)

    monkeypatch.setattr(OwnerDeliveryOutbox, "prepare_system_redelivery", _flaky)

    run = outbox.deliver(worker_id="delivery-2", now=NOW + timedelta(minutes=5))

    assert run.rotation_failed_count == 1
    # Второе предложение всё равно получило свежее поколение и ушло владельцу.
    assert run.sent_count == 1
    assert run.replacement_count == 1


# ---------------------------------------------------------------------------
# След решения: подтверждение обязано дойти
# ---------------------------------------------------------------------------


def test_trail_falls_back_to_plain_message_when_reply_target_is_gone(
    settings: OwnerApprovalConfig,
) -> None:
    """Два из трёх подтверждений в проде висели в FAILED_VISIBLE из-за реплая."""
    enqueue_trail_message(
        settings.db_path,
        trail_kind="FEEDBACK_ACK",
        dedupe_key="feedback-ack:1",
        rendered_text="📋 Собираю дайджест сейчас.",
        reply_to_message_id=555,
        now=NOW,
    )

    class ReplyHostileSender(RecordingSender):
        def send_message(self, **payload: object) -> int:
            if "reply_to_message_id" in payload:
                raise RuntimeError("message to reply not found")
            return super().send_message(**payload)

    sender = ReplyHostileSender()

    run = _outbox(settings, sender).deliver(worker_id="delivery-1", now=NOW)

    assert (run.trail_sent_count, run.trail_failed_count) == (1, 0)
    assert len(sender.payloads) == 1
    assert "reply_to_message_id" not in sender.payloads[0]
    trail = _rows(settings, "SELECT state, last_error_code FROM owner_trail_messages")[0]
    assert (str(trail["state"]), trail["last_error_code"]) == ("SENT", None)


def test_trail_failure_is_counted_and_not_silent(
    settings: OwnerApprovalConfig,
) -> None:
    enqueue_trail_message(
        settings.db_path,
        trail_kind="FEEDBACK_ACK",
        dedupe_key="feedback-ack:2",
        rendered_text="💬 Комментарий записан",
        now=NOW,
    )

    class DeadSender:
        def send_message(self, **_payload: object) -> int:
            raise RuntimeError("Telegram недоступен")

    run = _outbox(settings, DeadSender()).deliver(worker_id="delivery-1", now=NOW)

    assert (run.trail_sent_count, run.trail_failed_count) == (0, 1)
    trail = _rows(settings, "SELECT state, last_error_code FROM owner_trail_messages")[0]
    assert str(trail["state"]) == "FAILED_VISIBLE"
    assert str(trail["last_error_code"]) == "RuntimeError"


# ---------------------------------------------------------------------------
# /digest и ответ на отказ нажатия
# ---------------------------------------------------------------------------


def test_manual_digest_sends_the_batch_in_the_same_call(
    settings: OwnerApprovalConfig,
) -> None:
    """Раньше пакет ждал следующего тика крона — до 15 минут молчания."""
    _make_proposal(settings, name="a")
    _make_proposal(settings, name="b")
    sender = RecordingSender()
    # digest_hour далеко впереди: без /digest карточки молча ждали бы.
    outbox = _outbox(settings, sender, digest_hour=23)
    outbox.deliver(worker_id="delivery-1", now=NOW)
    assert sender.payloads == []

    service, _ack = _service(
        settings,
        outbox,
        [_message_update(update_id=1001, text="/digest", message_id=2001)],
    )
    service.poll(now=NOW + timedelta(minutes=1))
    service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    texts = [str(payload["text"]) for payload in sender.payloads]
    assert sum(1 for text in texts if "Предлагаю паузу" in text) == 2
    assert any("Дайджест предложений" in text for text in texts)
    assert any("Отправил" in text for text in texts)


def test_manual_digest_on_empty_queue_answers_instead_of_staying_silent(
    settings: OwnerApprovalConfig,
) -> None:
    sender = RecordingSender()
    outbox = _outbox(settings, sender)
    service, _ack = _service(
        settings,
        outbox,
        [_message_update(update_id=1101, text="/digest", message_id=2101)],
    )

    service.poll(now=NOW + timedelta(minutes=1))
    service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    texts = [str(payload["text"]) for payload in sender.payloads]
    assert texts == ["📭 Предложений нет — очередь пуста."]


def test_refused_button_press_gets_a_human_answer(
    settings: OwnerApprovalConfig,
) -> None:
    """Молчание на нажатие и есть жалоба «кнопки не работают»."""
    sender = RecordingSender()
    outbox = _outbox(settings, sender)
    service, ack = _service(
        settings,
        outbox,
        [
            _callback_update(
                update_id=1201,
                # Формат валиден, токена такого нет — карточка устарела.
                callback_data="oa:a:AAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBBBBBBBB",
                message_id=2201,
            )
        ],
    )

    service.poll(now=NOW + timedelta(minutes=1))
    batch = service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    assert batch.failed_count == 1
    assert len(ack.answers) == 1
    assert "устарела" in str(ack.answers[0]["text"])


@pytest.mark.parametrize(
    "error_code,marker",
    [
        ("PROPOSAL_EXPIRED", "устарело"),
        ("TOKEN_MAC_MISMATCH", "устарела"),
        ("OWNER_CHAT_MISMATCH", "не из твоего чата"),
        ("SOMETHING_NEW", "Не смог принять нажатие"),
    ],
)
def test_refusal_ack_texts_are_human(error_code: str, marker: str) -> None:
    assert marker in OwnerApprovalTelegram._refusal_ack_text(error_code)


# ---------------------------------------------------------------------------
# Единый поллер: команды пульта и решения владельца из одного инбокса
# ---------------------------------------------------------------------------


def test_console_command_reaches_the_console_from_the_owner_inbox(
    settings: OwnerApprovalConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/status доходит до пульта, хотя апдейт забрал owner-ingress."""
    from services import telegram_console

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        telegram_console,
        "_dispatch",
        lambda command, args="", **_kwargs: dispatched.append((command, args)),
    )
    sender = RecordingSender()
    outbox = _outbox(settings, sender)
    service, _ack = _service(
        settings,
        outbox,
        [_message_update(update_id=1301, text="/status", message_id=2301)],
    )

    service.poll(now=NOW)
    batch = service.process_inbox(worker_id="inbox-1", now=NOW)

    assert dispatched == [("status", "")]
    assert batch.failed_count == 0
    # Команда пульта остаётся в памяти фидбека как обычный текст владельца.
    assert batch.feedback[0].parsed_action == "COMMENT"


def test_two_consumers_do_not_steal_updates_from_each_other(
    settings: OwnerApprovalConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Одна выборка getUpdates обслуживает и /digest, и команду пульта.

    Пока веток getUpdates было две, offset одной подтверждал и удалял апдейты
    для другой: /digest мог уйти в консоль (которая его не знала) и пропасть.
    """
    from services import telegram_console

    _make_proposal(settings, name="a")
    dispatched: list[str] = []
    monkeypatch.setattr(
        telegram_console,
        "_dispatch",
        lambda command, args="", **_kwargs: dispatched.append(command),
    )
    sender = RecordingSender()
    outbox = _outbox(settings, sender, digest_hour=23)
    outbox.deliver(worker_id="delivery-1", now=NOW)
    service, _ack = _service(
        settings,
        outbox,
        [
            _message_update(update_id=1401, text="/digest", message_id=2401),
            _message_update(update_id=1402, text="/ads", message_id=2402),
        ],
    )

    service.poll(now=NOW + timedelta(minutes=1))
    batch = service.process_inbox(worker_id="inbox-1", now=NOW + timedelta(minutes=1))

    # /digest обслужил owner-обработчик (пульту его не отдали).
    assert dispatched == ["ads"]
    assert [record.parsed_action for record in batch.feedback] == [
        "DIGEST_NOW",
        "COMMENT",
    ]
    texts = [str(payload["text"]) for payload in sender.payloads]
    assert any("Предлагаю паузу" in text for text in texts)
    # Оба апдейта обработаны ровно по разу.
    assert _rows(
        settings,
        "SELECT state FROM telegram_update_inbox ORDER BY update_id",
    ) and [
        str(row["state"])
        for row in _rows(
            settings, "SELECT state FROM telegram_update_inbox ORDER BY update_id"
        )
    ] == ["PROCESSED", "PROCESSED"]


def test_unknown_command_is_answered_instead_of_ignored(
    settings: OwnerApprovalConfig,
) -> None:
    sender = RecordingSender()
    outbox = _outbox(settings, sender)
    service, _ack = _service(
        settings,
        outbox,
        [_message_update(update_id=1501, text="/чтототакое", message_id=2501)],
    )

    service.poll(now=NOW)
    service.process_inbox(worker_id="inbox-1", now=NOW)

    texts = [str(payload["text"]) for payload in sender.payloads]
    assert texts == ["🤷 Не знаю команду /чтототакое. Что умею — /help"]


def test_console_failure_does_not_break_the_inbox_batch(
    settings: OwnerApprovalConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ошибка одной ветки обработки не съедает апдейты другой."""
    from services import telegram_console

    def _boom(*_args, **_kwargs):
        raise RuntimeError("пульт упал")

    monkeypatch.setattr(telegram_console, "_dispatch", _boom)
    sender = RecordingSender()
    outbox = _outbox(settings, sender)
    service, _ack = _service(
        settings,
        outbox,
        [
            _message_update(update_id=1601, text="/status", message_id=2601),
            _message_update(update_id=1602, text="просто комментарий", message_id=2602),
        ],
    )

    service.poll(now=NOW)
    batch = service.process_inbox(worker_id="inbox-1", now=NOW)

    assert batch.failed_count == 0
    assert len(batch.feedback) == 2
    assert [
        str(row["state"])
        for row in _rows(
            settings, "SELECT state FROM telegram_update_inbox ORDER BY update_id"
        )
    ] == ["PROCESSED", "PROCESSED"]


def test_digest_header_keyboard_is_json_serialisable_after_reorder(
    settings: OwnerApprovalConfig,
) -> None:
    """Перенос шапки в конец не должен ломать привязку батч-токенов."""
    _make_proposal(settings, name="a")
    sender = RecordingSender()

    run = _outbox(settings, sender).deliver(worker_id="delivery-1", now=NOW)

    header = _rows(
        settings,
        "SELECT button_spec_json, telegram_message_id FROM owner_trail_messages "
        "WHERE trail_kind = 'DIGEST_HEADER'",
    )[0]
    assert len(json.loads(str(header["button_spec_json"]))) == 3
    bound = _rows(
        settings,
        "SELECT expected_message_id FROM owner_digest_batch_tokens WHERE digest_id = ?",
        (run.digest_id,),
    )
    assert {row["expected_message_id"] for row in bound} == {
        int(header["telegram_message_id"])
    }
