"""Крон-исполнитель одобренных заданий: очередь, свипер TTL и страж зависаний.

Дыра, которую закрывают эти тесты: APPROVE писал решение и ставил job в
owner_execution_jobs (state=QUEUED), но execute_owner_approved никто регулярно
не звал — нажатие владельца уходило в пустоту. Плюс переход PENDING_OWNER →
EXPIRED не существовал: карточка висела с живыми кнопками после TTL.

Второй слой (секция 8) — сквозной боевой путь целиком: нажатие «Одобрить» →
крон → сборка манифеста по ЖИВОМУ состоянию → настоящий PAUSE-адаптер →
замоканный FB-транспорт → CONFIRMED → «✅ Сработало» ответом на карточку.
Именно его отсутствие позволило уехать в прод сборке манифеста, которая падала
на первом же нажатии владельца.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import config as config_module
from config import OwnerApprovalConfig
from services import (
    action_locks,
    action_adapter_pause,
    approval_source_amo,
    approval_source_cdp,
    approval_source_facebook,
    owner_action_executor,
    owner_action_live_manifest,
)
from services.approval_checker_models import (
    ActionObservation,
    ActionReview,
    EvidenceBundle,
    EvidenceState,
    FactCategory,
    Metric,
    SafetyDecision,
    SourceEvidence,
    SourceFreshness,
    SubjectRef,
    manifest_sha256,
)
from services.approval_checker_models import EvidenceRecord as SourceRecord
from services.adset_pause_guard import LockedPauseValidation, inventory_state_sha256
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
        bot_token="123456789:AAQueueBotTokenForOwnerTests",
        chat_id=CHAT_ID,
        owner_user_id=OWNER_ID,
        callback_secret="callback-7Cq9_Zv2-Nm8!Lp4-Rs6@Wx3-Kd5",
        webhook_secret="webhook-2Jf8_Yt4-Pq6!Bn9-Hm3@Vs7-Lx5",
        db_path=tmp_path / "queue.db",
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


@pytest.fixture
def alerts(monkeypatch) -> list[tuple[str, str]]:
    """Критические алерты никуда не уходят — только записываются."""

    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        owner_action_executor,
        "_send_owner_critical",
        lambda title, detail: recorded.append((title, detail)),
    )
    return recorded


def _rows(settings: OwnerApprovalConfig, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _make_proposal(
    settings: OwnerApprovalConfig,
    *,
    name: str = "a",
    valid_until: datetime | None = None,
    intended_payload: dict | None = None,
    idempotency_key: str | None = None,
    subject_id: str | None = None,
    adset_id: str = "adset-1",
) -> str:
    payload = intended_payload or {"status": "PAUSED", "ad": name}
    evidence_payload = {"effective_status": "ACTIVE"}
    plan = ProposedActionPlan(
        proposal_kind=ProposalKind.PAUSE,
        origin=ProposalOrigin.AUTOPILOT,
        idempotency_key=idempotency_key or f"queue:{name}",
        source_ref=f"autopilot:{name}",
        actor="autopilot",
        summary=f"Выключить {name}",
        targets=(
            ProposedTarget(
                claim_id=f"claim-{name}",
                ordinal=0,
                action_kind="PAUSE_AD",
                account_id="act-1",
                adset_id=adset_id,
                subject_id=subject_id or f"ad-{name}",
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
        valid_until=valid_until or (NOW + timedelta(hours=24)),
        staged_media_root=None,
    )
    return OwnerActionRepository(settings.db_path).propose_action(
        plan,
        now=NOW,
    ).proposal_id


def _deliver_card(
    settings: OwnerApprovalConfig,
    *,
    name: str = "a",
    valid_until: datetime | None = None,
    outbox: OwnerDeliveryOutbox | None = None,
    sender: RecordingSender | None = None,
    **proposal_kwargs,
) -> tuple[str, int, OwnerDeliveryOutbox, RecordingSender]:
    proposal_id = _make_proposal(
        settings,
        name=name,
        valid_until=valid_until,
        **proposal_kwargs,
    )
    used_sender = sender or RecordingSender()
    used_outbox = outbox or OwnerDeliveryOutbox(settings.db_path, settings, used_sender)
    used_outbox.deliver(worker_id="delivery-1", now=NOW)
    message_id = latest_proposal_message_id(settings.db_path, proposal_id)
    assert message_id is not None
    return proposal_id, message_id, used_outbox, used_sender


def _token_proposal(settings: OwnerApprovalConfig, callback_data: str) -> str:
    """callback_data = oa:<kind>:<public_nonce>.<mac> — нонс ведёт к предложению."""

    nonce = str(callback_data).split(":")[-1].split(".")[0]
    rows = _rows(
        settings,
        "SELECT proposal_id FROM owner_callback_tokens WHERE public_nonce = ?",
        (nonce,),
    )
    return "" if not rows else str(rows[0]["proposal_id"])


def _approve_callback_data(
    settings: OwnerApprovalConfig,
    sender: RecordingSender,
    proposal_id: str,
) -> str:
    return next(
        button["callback_data"]
        for item in sender.payloads
        if item.get("inline_keyboard") is not None
        for row in item["inline_keyboard"]
        for button in row
        if button["text"] == "Одобрить"
        and _token_proposal(settings, button["callback_data"]) == proposal_id
    )


def _approve(
    settings: OwnerApprovalConfig,
    *,
    proposal_id: str,
    message_id: int,
    outbox: OwnerDeliveryOutbox,
    sender: RecordingSender,
    update_id: int,
    now: datetime,
) -> None:
    """Реальное нажатие «Одобрить»: карточка → callback → решение → job QUEUED."""

    approve_data = _approve_callback_data(settings, sender, proposal_id)
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(
            [
                {
                    "update_id": update_id,
                    "callback_query": {
                        "id": f"cbq-{update_id}",
                        "from": {"id": OWNER_ID},
                        "data": approve_data,
                        "message": {
                            "message_id": message_id,
                            "chat": {"id": CHAT_ID},
                        },
                    },
                }
            ]
        ),
        delivery_outbox=outbox,
        ack_client=RecordingAck(),
    )
    service.poll(now=now)
    batch = service.process_inbox(worker_id="inbox-1", now=now)
    assert batch.decision_count == 1


def _queued_job(settings: OwnerApprovalConfig, proposal_id: str) -> sqlite3.Row:
    rows = _rows(
        settings,
        "SELECT * FROM owner_execution_jobs WHERE proposal_id = ?",
        (proposal_id,),
    )
    assert len(rows) == 1
    return rows[0]


def _advance_job(settings: OwnerApprovalConfig, proposal_id: str, state: str) -> None:
    """Дублёр исполнения: двигает job так же, как это делает execution boundary."""

    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute(
            "UPDATE owner_execution_jobs SET state = ?, updated_at = ? "
            "WHERE proposal_id = ?",
            (state, NOW.isoformat(), proposal_id),
        )
        connection.commit()
    finally:
        connection.close()


def _run_queue(
    settings: OwnerApprovalConfig,
    tmp_path: Path,
    *,
    now: datetime,
    worker_id: str = "queue-1",
):
    return owner_action_executor.run_owner_execution_queue(
        worker_id=worker_id,
        now=now,
        db_path=settings.db_path,
        state_path=tmp_path / "dispatch.json",
    )


# ---------------------------------------------------------------------------
# 1. Одобренное задание реально исполняется краном.
# ---------------------------------------------------------------------------


def test_queued_job_is_executed_by_cron(settings, alerts, tmp_path, monkeypatch) -> None:
    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9001,
        now=NOW + timedelta(minutes=1),
    )
    assert _queued_job(settings, proposal_id)["state"] == "QUEUED"

    calls: list[str] = []

    def _fake_execute(
        target_id: str,
        *,
        worker_id: str,
        now: datetime,
        deadline_monotonic: float | None = None,
    ):
        calls.append(target_id)
        _advance_job(settings, target_id, "EXECUTED")
        return owner_action_executor.ActionRun(
            proposal_id=target_id,
            decision_id="decision-1",
            job_id="job-1",
            claim_id="claim-a",
            state=LifecycleState.EXECUTED.value,
            reason_code="ALL_CLAIMS_TERMINAL",
            safety_review=None,
            execution=None,
            provider_mutation_count=1,
            reconciliation_required=False,
        )

    monkeypatch.setattr(owner_action_executor, "execute_owner_approved", _fake_execute)

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))

    assert calls == [proposal_id]
    assert (run.claimed, run.executed, run.retried, run.exhausted) == (1, 1, 0, 0)
    assert run.errors == ()
    job = _queued_job(settings, proposal_id)
    assert job["state"] == "EXECUTED"
    # Аренда снята — задание не залипает под протухшим lease.
    assert job["lease_token"] is None and job["lease_until"] is None
    assert alerts == []


def test_execution_result_reaches_owner_card(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    """Нажал → крон исполнил → «✅ Сработало» пришло ответом на карточку."""

    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9011,
        now=NOW + timedelta(minutes=1),
    )

    def _fake_execute(
        target_id: str,
        *,
        worker_id: str,
        now: datetime,
        deadline_monotonic: float | None = None,
    ):
        _advance_job(settings, target_id, "EXECUTED")
        run = owner_action_executor.ActionRun(
            proposal_id=target_id,
            decision_id="decision-1",
            job_id="job-1",
            claim_id="claim-a",
            state=LifecycleState.EXECUTED.value,
            reason_code="ALL_CLAIMS_TERMINAL",
            safety_review=None,
            execution=None,
            provider_mutation_count=1,
            reconciliation_required=False,
        )
        # Настоящий execute_owner_approved зовёт отчёт ровно так же.
        owner_action_executor.report_execution_result(run, now=now)
        return run

    monkeypatch.setattr(owner_action_executor, "execute_owner_approved", _fake_execute)

    _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))
    outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=3))

    replies = [
        payload
        for payload in sender.payloads
        if payload.get("reply_to_message_id") == message_id
    ]
    assert len(replies) == 1
    assert str(replies[0]["text"]).startswith("✅ Сработало")
    trail = _rows(
        settings,
        "SELECT trail_kind, state FROM owner_trail_messages",
    )
    assert [(row["trail_kind"], row["state"]) for row in trail] == [
        ("EXECUTION_RESULT", "SENT")
    ]


def test_second_pass_does_not_execute_twice(settings, alerts, tmp_path, monkeypatch) -> None:
    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9002,
        now=NOW + timedelta(minutes=1),
    )
    calls: list[str] = []

    def _fake_execute(
        target_id: str,
        *,
        worker_id: str,
        now: datetime,
        deadline_monotonic: float | None = None,
    ):
        calls.append(target_id)
        _advance_job(settings, target_id, "EXECUTED")
        return owner_action_executor.ActionRun(
            proposal_id=target_id,
            decision_id="decision-1",
            job_id="job-1",
            claim_id="claim-a",
            state=LifecycleState.EXECUTED.value,
            reason_code="ALL_CLAIMS_TERMINAL",
            safety_review=None,
            execution=None,
            provider_mutation_count=1,
            reconciliation_required=False,
        )

    monkeypatch.setattr(owner_action_executor, "execute_owner_approved", _fake_execute)

    first = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))
    second = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=5))

    assert first.executed == 1
    # Повторный прогон не берёт задание: job больше не QUEUED.
    assert (second.claimed, second.executed) == (0, 0)
    assert calls == [proposal_id]


def test_job_with_terminal_lifecycle_is_not_claimed(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    """Одобрение, которое успели погасить, диспетчер не исполняет."""

    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9004,
        now=NOW + timedelta(minutes=1),
    )
    OwnerActionRepository(settings.db_path).transition_lifecycle(
        proposal_id,
        expected_state=LifecycleState.APPROVED.value,
        expected_version=_rows(
            settings,
            "SELECT version FROM owner_action_lifecycle WHERE proposal_id = ?",
            (proposal_id,),
        )[0]["version"],
        new_state=LifecycleState.EXPIRED.value,
        actor="test",
        reason_code="PROPOSAL_EXPIRED",
        now=NOW + timedelta(minutes=2),
    )
    monkeypatch.setattr(
        owner_action_executor,
        "execute_owner_approved",
        lambda *args, **kwargs: pytest.fail("исполнять погашенное нельзя"),
    )

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=3))

    assert (run.claimed, run.executed) == (0, 0)


def test_success_without_progress_does_not_burn_attempt_cap(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    """Проход без исключения, но и без сдвига, счётчик попыток не тратит."""

    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9005,
        now=NOW + timedelta(minutes=1),
    )

    def _no_progress(
        target_id: str,
        *,
        worker_id: str,
        now: datetime,
        deadline_monotonic: float | None = None,
    ):
        return owner_action_executor.ActionRun(
            proposal_id=target_id,
            decision_id="decision-1",
            job_id="job-1",
            claim_id="claim-a",
            state=LifecycleState.EXECUTION_QUEUED.value,
            reason_code="CLAIM_NOT_READY",
            safety_review=None,
            execution=None,
            provider_mutation_count=0,
            reconciliation_required=False,
        )

    monkeypatch.setattr(owner_action_executor, "execute_owner_approved", _no_progress)

    for index in range(3):
        run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2 + index))
        assert run.retried == 1
        assert run.exhausted == 0

    entry = next(
        iter(
            owner_action_executor._load_dispatch_state(
                tmp_path / "dispatch.json"
            ).values()
        )
    )
    assert entry["attempts"] == 0
    assert entry["state"] == "RETRY"


def test_chronic_transient_failure_backs_off_and_frees_queue_head(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    """Хронически недоступный источник отодвигает задание, а не держит голову.

    Сценарий: несколько PAUSE падают на таймауте AMO, транзиентный
    backoff был фиксированные 60 секунд при интервале крона 180, а attempts у
    транзиента не растёт. Задания оставались eligible на каждом тике и, будучи
    старейшими в ORDER BY created_at LIMIT 10, часами не пускали к
    провайдеру ни один одобренный запуск.
    """

    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9006,
        now=NOW + timedelta(minutes=1),
    )

    def _source_unavailable(
        target_id: str,
        *,
        worker_id: str,
        now: datetime,
        deadline_monotonic: float | None = None,
    ):
        raise RuntimeError("SOURCE_UNAVAILABLE:LIVE_FACTS_INCOMPLETE_AMO:Timeout")

    monkeypatch.setattr(
        owner_action_executor, "execute_owner_approved", _source_unavailable
    )

    ladder = owner_action_executor.OWNER_EXECUTION_TRANSIENT_BACKOFF_LADDER_SECONDS
    observed: list[int] = []
    for index in range(len(ladder)):
        checked_at = NOW + timedelta(minutes=2 + index * 60)
        run = _run_queue(settings, tmp_path, now=checked_at)
        assert run.retried == 1
        # Потолок попыток транзиент по-прежнему не тратит.
        assert run.exhausted == 0
        next_attempt = _rows(
            settings,
            "SELECT next_attempt_at FROM owner_execution_jobs WHERE proposal_id = ?",
            (proposal_id,),
        )[0]["next_attempt_at"]
        delay = datetime.fromisoformat(str(next_attempt).replace("Z", "+00:00"))
        observed.append(int((delay - checked_at).total_seconds()))

    assert observed == list(ladder), (
        "повтор обязан отодвигаться лестницей, иначе задание вечно первое в "
        f"очереди: получили {observed}"
    )

    entry = next(
        iter(
            owner_action_executor._load_dispatch_state(
                tmp_path / "dispatch.json"
            ).values()
        )
    )
    assert entry["attempts"] == 0, "транзиент не должен тратить потолок попыток"
    assert entry["transient_streak"] == len(ladder)

    # Потолок лестницы держится и дальше — задание не уходит в бесконечность.
    checked_at = NOW + timedelta(minutes=2 + len(ladder) * 60)
    _run_queue(settings, tmp_path, now=checked_at)
    next_attempt = _rows(
        settings,
        "SELECT next_attempt_at FROM owner_execution_jobs WHERE proposal_id = ?",
        (proposal_id,),
    )[0]["next_attempt_at"]
    delay = datetime.fromisoformat(str(next_attempt).replace("Z", "+00:00"))
    assert int((delay - checked_at).total_seconds()) == ladder[-1]


def test_transient_streak_resets_after_non_transient_outcome(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    """Разовая недоступность не наказывает задание после её конца."""

    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9007,
        now=NOW + timedelta(minutes=1),
    )

    mode = {"transient": True}

    def _flaky(
        target_id: str,
        *,
        worker_id: str,
        now: datetime,
        deadline_monotonic: float | None = None,
    ):
        if mode["transient"]:
            raise RuntimeError("SOURCE_UNAVAILABLE:LIVE_FACTS_INCOMPLETE_AMO:Timeout")
        raise RuntimeError("BOOM: сборка манифеста упала")

    monkeypatch.setattr(owner_action_executor, "execute_owner_approved", _flaky)

    _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))
    _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=62))
    state_path = tmp_path / "dispatch.json"
    entry = next(iter(owner_action_executor._load_dispatch_state(state_path).values()))
    assert entry["transient_streak"] == 2

    mode["transient"] = False
    _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=122))
    entry = next(iter(owner_action_executor._load_dispatch_state(state_path).values()))
    assert entry["transient_streak"] == 0
    assert entry["attempts"] == 1, "нетранзиентная неудача обязана тратить потолок"


def test_leased_job_is_not_claimed_twice_in_parallel(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    """Аренда закрывает второго воркера, пока первый ещё исполняет."""

    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9003,
        now=NOW + timedelta(minutes=1),
    )
    claimed_first = owner_action_executor._claim_execution_jobs(
        str(settings.db_path),
        worker_id="queue-a",
        now=NOW + timedelta(minutes=2),
        limit=10,
    )
    claimed_second = owner_action_executor._claim_execution_jobs(
        str(settings.db_path),
        worker_id="queue-b",
        now=NOW + timedelta(minutes=3),
        limit=10,
    )

    assert len(claimed_first) == 1
    assert claimed_second == []

    # После истечения аренды задание снова доступно — оно не потеряно.
    claimed_after_lease = owner_action_executor._claim_execution_jobs(
        str(settings.db_path),
        worker_id="queue-c",
        now=NOW
        + timedelta(
            minutes=2,
            seconds=owner_action_executor.OWNER_EXECUTION_LEASE_SECONDS + 1,
        ),
        limit=10,
    )
    assert len(claimed_after_lease) == 1


# ---------------------------------------------------------------------------
# 2. Сбой одного задания не мешает остальным.
# ---------------------------------------------------------------------------


def test_one_failing_job_does_not_block_others(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    sender = RecordingSender()
    outbox = OwnerDeliveryOutbox(settings.db_path, settings, sender)
    bad_id, bad_message, _o1, _s1 = _deliver_card(
        settings, name="bad", outbox=outbox, sender=sender
    )
    good_id, good_message, _o2, _s2 = _deliver_card(
        settings, name="good", outbox=outbox, sender=sender
    )
    for proposal_id, message_id, update_id in (
        (bad_id, bad_message, 9101),
        (good_id, good_message, 9102),
    ):
        _approve(
            settings,
            proposal_id=proposal_id,
            message_id=message_id,
            outbox=outbox,
            sender=sender,
            update_id=update_id,
            now=NOW + timedelta(minutes=1),
        )

    executed: list[str] = []

    def _fake_execute(
        target_id: str,
        *,
        worker_id: str,
        now: datetime,
        deadline_monotonic: float | None = None,
    ):
        if target_id == bad_id:
            raise RuntimeError("facebook недоступен")
        executed.append(target_id)
        _advance_job(settings, target_id, "EXECUTED")
        return owner_action_executor.ActionRun(
            proposal_id=target_id,
            decision_id="decision-1",
            job_id="job-1",
            claim_id="claim-good",
            state=LifecycleState.EXECUTED.value,
            reason_code="ALL_CLAIMS_TERMINAL",
            safety_review=None,
            execution=None,
            provider_mutation_count=1,
            reconciliation_required=False,
        )

    monkeypatch.setattr(owner_action_executor, "execute_owner_approved", _fake_execute)

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))

    assert executed == [good_id]
    assert run.claimed == 2
    assert run.executed == 1
    assert run.retried == 1
    assert run.errors == (f"{bad_id}:RuntimeError",)
    # Упавшее задание не потеряно: осталось QUEUED с отложенным повтором.
    failed_job = _queued_job(settings, bad_id)
    assert failed_job["state"] == "QUEUED"
    assert failed_job["lease_token"] is None
    assert failed_job["next_attempt_at"] is not None


# ---------------------------------------------------------------------------
# 3. Потолок попыток: терминальный FAILED + критический алерт.
# ---------------------------------------------------------------------------


def test_exhausted_attempts_stop_retries_and_alert_owner(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9201,
        now=NOW + timedelta(minutes=1),
    )
    attempts: list[str] = []

    def _always_fails(
        target_id: str,
        *,
        worker_id: str,
        now: datetime,
        deadline_monotonic: float | None = None,
    ):
        attempts.append(target_id)
        raise RuntimeError("permit не выдан")

    monkeypatch.setattr(owner_action_executor, "execute_owner_approved", _always_fails)

    runs = []
    for index in range(owner_action_executor.OWNER_EXECUTION_MAX_ATTEMPTS):
        runs.append(
            _run_queue(
                settings,
                tmp_path,
                # Каждый прогон — позже backoff предыдущего.
                now=NOW + timedelta(minutes=10 + index * 60),
            )
        )
    after_cap = _run_queue(settings, tmp_path, now=NOW + timedelta(hours=12))

    assert len(attempts) == owner_action_executor.OWNER_EXECUTION_MAX_ATTEMPTS
    assert runs[-1].exhausted == 1
    # После потолка задание больше не берётся в работу.
    assert after_cap.claimed == 0
    assert len(attempts) == owner_action_executor.OWNER_EXECUTION_MAX_ATTEMPTS
    titles = [title for title, _detail in alerts]
    assert "Одобренное действие не исполнено" in titles
    # Владелец видит отказ в ветке самой карточки.
    trail = _rows(
        settings,
        "SELECT rendered_text FROM owner_trail_messages "
        "WHERE trail_kind = 'EXECUTION_RESULT'",
    )
    assert len(trail) == 1
    assert str(trail[0]["rendered_text"]).startswith("❌ Не сработало")


def test_chronic_transient_job_yields_queue_head_to_fresh_jobs(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    """Задание с серией отказов источника идёт после свежих, а не впереди них.

    Сценарий: несколько запусков каждый прогон ждут AMO/Trello/CDP, сжигают по бюджету задания в
    голове очереди (ORDER BY created_at), и свежие паузы раннего стопа ждут исполнения часами.
    """

    old_id, old_message, outbox, sender = _deliver_card(settings, name="old")
    _approve(settings, proposal_id=old_id, message_id=old_message, outbox=outbox, sender=sender,
             update_id=9701, now=NOW + timedelta(minutes=1))
    fresh_id, fresh_message, outbox, sender = _deliver_card(settings, name="fresh", outbox=outbox, sender=sender)
    _approve(settings, proposal_id=fresh_id, message_id=fresh_message, outbox=outbox, sender=sender,
             update_id=9702, now=NOW + timedelta(minutes=2))
    old_job = _queued_job(settings, old_id)["job_id"]
    owner_action_executor._save_dispatch_state(
        {old_job: {"attempts": 0, "transient_streak": owner_action_executor.OWNER_EXECUTION_DEPRIORITIZE_STREAK,
                   "state": "RETRY"}},
        tmp_path / "dispatch.json",
    )

    order: list[str] = []

    def _record(target_id: str, *, worker_id: str, now: datetime, deadline_monotonic: float | None = None):
        order.append(target_id)
        raise RuntimeError("SOURCE_UNAVAILABLE:LIVE_FACTS_INCOMPLETE_AMO:Timeout")

    monkeypatch.setattr(owner_action_executor, "execute_owner_approved", _record)
    _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=5))

    assert order == [fresh_id, old_id], f"свежее задание обязано идти первым, получили {order}"


def test_backoff_holds_job_until_next_attempt_at(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9202,
        now=NOW + timedelta(minutes=1),
    )
    monkeypatch.setattr(
        owner_action_executor,
        "execute_owner_approved",
        lambda target_id, *, worker_id, now: (_ for _ in ()).throw(
            RuntimeError("сеть легла")
        ),
    )

    first = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))
    too_early = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=3))

    assert first.retried == 1
    assert too_early.claimed == 0


# ---------------------------------------------------------------------------
# 4. Свипер протухших предложений.
# ---------------------------------------------------------------------------


def test_sweeper_expires_stale_pending_owner(settings, alerts, tmp_path) -> None:
    proposal_id, _message_id, _outbox, _sender = _deliver_card(
        settings,
        valid_until=NOW + timedelta(hours=2),
    )
    lifecycle = _rows(
        settings,
        "SELECT state FROM owner_action_lifecycle WHERE proposal_id = ?",
        (proposal_id,),
    )[0]
    assert lifecycle["state"] == "PENDING_OWNER"

    sweep = owner_action_executor.sweep_expired_proposals(
        now=NOW + timedelta(hours=3),
        db_path=settings.db_path,
    )

    assert sweep.expired == 1
    assert sweep.errors == ()
    after = _rows(
        settings,
        "SELECT state, latest_reason_code FROM owner_action_lifecycle "
        "WHERE proposal_id = ?",
        (proposal_id,),
    )[0]
    assert after["state"] == "EXPIRED"
    assert after["latest_reason_code"] == "PROPOSAL_EXPIRED"
    # Кнопки погашены — позднее нажатие не найдёт живой токен.
    tokens = _rows(
        settings,
        "SELECT revoked_at, revoke_reason FROM owner_callback_tokens "
        "WHERE proposal_id = ?",
        (proposal_id,),
    )
    assert tokens and all(row["revoked_at"] is not None for row in tokens)
    assert all(row["revoke_reason"] == "PROPOSAL_EXPIRED" for row in tokens)
    assert sweep.tokens_revoked == len(tokens)


def test_sweeper_leaves_live_proposal_alone(settings, alerts, tmp_path) -> None:
    proposal_id, _message_id, _outbox, _sender = _deliver_card(
        settings,
        valid_until=NOW + timedelta(hours=48),
    )

    sweep = owner_action_executor.sweep_expired_proposals(
        now=NOW + timedelta(hours=30),
        db_path=settings.db_path,
    )

    assert sweep.expired == 0
    state = _rows(
        settings,
        "SELECT state FROM owner_action_lifecycle WHERE proposal_id = ?",
        (proposal_id,),
    )[0]["state"]
    assert state == "PENDING_OWNER"
    live_tokens = _rows(
        settings,
        "SELECT 1 FROM owner_callback_tokens WHERE proposal_id = ? "
        "AND revoked_at IS NULL",
        (proposal_id,),
    )
    assert live_tokens


def test_sweeper_does_not_touch_approved_proposal(
    settings, alerts, tmp_path
) -> None:
    """Одобренное предложение свипер не гасит: у него другой lifecycle."""

    proposal_id, message_id, outbox, sender = _deliver_card(
        settings,
        valid_until=NOW + timedelta(hours=2),
    )
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9301,
        now=NOW + timedelta(minutes=1),
    )

    sweep = owner_action_executor.sweep_expired_proposals(
        now=NOW + timedelta(hours=3),
        db_path=settings.db_path,
    )

    assert sweep.expired == 0
    state = _rows(
        settings,
        "SELECT state FROM owner_action_lifecycle WHERE proposal_id = ?",
        (proposal_id,),
    )[0]["state"]
    assert state == "APPROVED"


# ---------------------------------------------------------------------------
# 5. Страж «одобрено, но не исполнено».
# ---------------------------------------------------------------------------


def test_stalled_approved_job_raises_critical_alert(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9401,
        now=NOW + timedelta(minutes=1),
    )
    # Задание не берём в работу вовсе — только смотрим сторожем.
    monkeypatch.setattr(
        owner_action_executor,
        "_claim_execution_jobs",
        lambda *args, **kwargs: [],
    )

    fresh = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=10))
    stalled = _run_queue(
        settings,
        tmp_path,
        now=NOW
        + timedelta(minutes=owner_action_executor.OWNER_EXECUTION_STALL_MINUTES + 5),
    )
    repeated = _run_queue(
        settings,
        tmp_path,
        now=NOW
        + timedelta(minutes=owner_action_executor.OWNER_EXECUTION_STALL_MINUTES + 10),
    )

    assert fresh.stall_alerts == 0
    assert stalled.stall_alerts == 1
    # Дедуп: тот же зависший job не спамит владельца каждые три минуты.
    assert repeated.stall_alerts == 0
    title, detail = alerts[0]
    assert title == "Одобрено, но не исполнено"
    assert proposal_id in detail


def test_stall_guard_ignores_finished_job(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9402,
        now=NOW + timedelta(minutes=1),
    )
    _advance_job(settings, proposal_id, "EXECUTED")
    monkeypatch.setattr(
        owner_action_executor,
        "_claim_execution_jobs",
        lambda *args, **kwargs: [],
    )

    run = _run_queue(
        settings,
        tmp_path,
        now=NOW + timedelta(hours=6),
    )

    assert run.stall_alerts == 0
    assert alerts == []


def _force_lifecycle(
    settings: OwnerApprovalConfig,
    proposal_id: str,
    states: tuple[str, ...],
) -> None:
    """Проводит lifecycle по цепочке легальных переходов (версия +1 на шаг)."""

    connection = sqlite3.connect(settings.db_path)
    try:
        for state in states:
            connection.execute(
                "UPDATE owner_action_lifecycle "
                "SET state = ?, version = version + 1, updated_at = ? "
                "WHERE proposal_id = ?",
                (state, NOW.isoformat(), proposal_id),
            )
        connection.commit()
    finally:
        connection.close()


def test_stall_guard_skips_terminal_lifecycle(
    settings, alerts, tmp_path, monkeypatch
) -> None:
    """BLOCKED_STALE закрыт и уже объяснён владельцу — второй раз не шумим."""

    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9403,
        now=NOW + timedelta(minutes=1),
    )
    _force_lifecycle(
        settings,
        proposal_id,
        ("EXECUTION_QUEUED", "LIVE_REVIEW", "BLOCKED_STALE"),
    )
    _advance_job(settings, proposal_id, "REVIEWING")
    monkeypatch.setattr(
        owner_action_executor,
        "_claim_execution_jobs",
        lambda *args, **kwargs: [],
    )

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(hours=6))

    assert run.stall_alerts == 0
    assert alerts == []


# ---------------------------------------------------------------------------
# 6. Бухгалтерия диспетчера переживает перезапуск.
# ---------------------------------------------------------------------------


def test_dispatch_state_survives_restart(settings, alerts, tmp_path, monkeypatch) -> None:
    proposal_id, message_id, outbox, sender = _deliver_card(settings)
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9501,
        now=NOW + timedelta(minutes=1),
    )
    monkeypatch.setattr(
        owner_action_executor,
        "execute_owner_approved",
        lambda target_id, *, worker_id, now: (_ for _ in ()).throw(
            RuntimeError("сеть легла")
        ),
    )

    _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))
    state = owner_action_executor._load_dispatch_state(tmp_path / "dispatch.json")

    assert len(state) == 1
    entry = next(iter(state.values()))
    assert entry["attempts"] == 1
    assert entry["state"] == "RETRY"
    assert entry["proposal_id"] == proposal_id


def test_dispatch_state_is_pruned_by_ttl() -> None:
    """State-файл не растёт вечно: записи старше TTL выметаются."""

    fresh = {"attempts": 1, "state": "RETRY", "updated_at": NOW.isoformat()}
    ancient = {
        "attempts": 3,
        "state": "FAILED",
        "updated_at": (
            NOW
            - timedelta(days=owner_action_executor.OWNER_EXECUTION_STATE_TTL_DAYS + 1)
        ).isoformat(),
    }

    kept = owner_action_executor._prune_dispatch_state(
        {"job-fresh": fresh, "job-ancient": ancient},
        now=NOW,
    )

    assert set(kept) == {"job-fresh"}


def test_corrupt_dispatch_state_does_not_break_run(settings, alerts, tmp_path) -> None:
    broken = tmp_path / "dispatch.json"
    broken.write_text("{не json", encoding="utf-8")

    assert owner_action_executor._load_dispatch_state(broken) == {}


# ---------------------------------------------------------------------------
# 7. Разовая уборка накопленного (scripts/cleanup_stale_proposals.py).
# ---------------------------------------------------------------------------


@pytest.fixture
def cleanup_script(monkeypatch):
    """Скрипт уборки с фиксированным «сейчас» — тесты не зависят от часов."""

    from scripts import cleanup_stale_proposals

    monkeypatch.setattr(
        cleanup_stale_proposals,
        "_utcnow",
        lambda: NOW + timedelta(hours=6),
    )
    return cleanup_stale_proposals


def test_cleanup_script_dry_run_changes_nothing(
    settings, alerts, capsys, cleanup_script
) -> None:
    cleanup_stale_proposals = cleanup_script

    proposal_id = _make_proposal(
        settings,
        name="undelivered",
        valid_until=NOW + timedelta(hours=1),
    )

    code = cleanup_stale_proposals.main(["--db-path", str(settings.db_path)])

    assert code == 0
    assert "Dry-run" in capsys.readouterr().out
    state = _rows(
        settings,
        "SELECT state FROM owner_action_lifecycle WHERE proposal_id = ?",
        (proposal_id,),
    )[0]["state"]
    assert state == "DELIVERY_PENDING"


def test_cleanup_script_apply_requires_confirmation(
    settings, alerts, capsys, cleanup_script
) -> None:
    cleanup_stale_proposals = cleanup_script

    proposal_id = _make_proposal(
        settings,
        name="undelivered",
        valid_until=NOW + timedelta(hours=1),
    )

    code = cleanup_stale_proposals.main(
        ["--db-path", str(settings.db_path), "--apply"]
    )

    assert code == 2
    assert "Отказ" in capsys.readouterr().out
    state = _rows(
        settings,
        "SELECT state FROM owner_action_lifecycle WHERE proposal_id = ?",
        (proposal_id,),
    )[0]["state"]
    assert state == "DELIVERY_PENDING"


def test_cleanup_script_apply_is_idempotent(
    settings, alerts, capsys, cleanup_script
) -> None:
    cleanup_stale_proposals = cleanup_script

    proposal_id = _make_proposal(
        settings,
        name="undelivered",
        valid_until=NOW + timedelta(hours=1),
    )
    argv = [
        "--db-path",
        str(settings.db_path),
        "--apply",
        "--confirm-production",
        cleanup_stale_proposals.CONFIRM_PRODUCTION,
    ]

    first = cleanup_stale_proposals.main(argv)
    first_out = capsys.readouterr().out
    second = cleanup_stale_proposals.main(argv)
    second_out = capsys.readouterr().out

    assert (first, second) == (0, 0)
    assert "Погашено: 1" in first_out
    assert "нечего гасить" in second_out
    state = _rows(
        settings,
        "SELECT state FROM owner_action_lifecycle WHERE proposal_id = ?",
        (proposal_id,),
    )[0]["state"]
    assert state == "EXPIRED"


# ---------------------------------------------------------------------------
# 8. Сквозной боевой путь: нажатие → крон → адаптер → «✅ Сработало».
#
# Сценарий: владелец одобрил несколько карточек, все решения записались, но
# исполнение падало на КАЖДОЙ с ACTION_MANIFEST_INVALID. Причина —
# _fresh_manifest_for_target пыталась «восстановить» манифест из
# intended_payload через manifest_from_payload, а там лежит бизнес-намерение
# продюсера (ad_id/статусы/причина), а не сериализованный манифест. Ни один из
# существовавших тестов этого не ловил: сквозного теста «нажали → реально
# исполнилось» не было вовсе.
# ---------------------------------------------------------------------------

PAUSE_KEY = "6f1c8a2e-9b4d-4c3a-8e57-1d2f3a4b5c6d"
# Настоящий AMO-адаптер, снятый ДО фикстур: фикстура live_pause подменяет
# load_amo_evidence фейком, и внутри теста «оригинала» уже не достать.
_REAL_AMO_EVIDENCE = approval_source_amo.load_amo_evidence
AD_ID = "3001"
SIBLING_ID = "3002"
ADSET_ID = "4001"
LIVE_SHA = "e" * 64
UNRELATED_SHA = "9" * 64


def _pause_intent(**overrides) -> dict:
    """Ровно та форма payload, которую пишет action_producer_gateway.propose_pause."""

    payload = {
        "schema_version": 1,
        "operation": "PAUSE_AD",
        "ad_id": AD_ID,
        "adset_id": ADSET_ID,
        "expected_before_status": "ACTIVE",
        "expected_after_status": "PAUSED",
        "reason_code": "LOW_ROMI",
        "producer_inventory_sha256": "a" * 64,
        "sibling_active_ids": [SIBLING_ID],
    }
    payload.update(overrides)
    return payload


def _inventory_rows(*, target_effective: str = "ACTIVE") -> dict[str, dict[str, str]]:
    return {
        AD_ID: {
            "ad_id": AD_ID,
            "adset_id": ADSET_ID,
            "configured_status": target_effective,
            "effective_status": target_effective,
        },
        SIBLING_ID: {
            "ad_id": SIBLING_ID,
            "adset_id": ADSET_ID,
            "configured_status": "ACTIVE",
            "effective_status": "ACTIVE",
        },
    }


def _live_inventory(*, target_effective: str = "ACTIVE") -> dict:
    rows = _inventory_rows(target_effective=target_effective)
    return {
        "adset_id": ADSET_ID,
        "active_ids": {
            ad_id
            for ad_id, row in rows.items()
            if row["effective_status"] == "ACTIVE"
        },
        "candidate_context": {AD_ID: rows[AD_ID]},
        "inventory_context": rows,
        "state_sha256": inventory_state_sha256(
            rows.values(), expected_adset_id=ADSET_ID
        ),
        "complete": True,
        "pages_read": 1,
    }


def _post_pause_inventory() -> dict:
    """Инвентарь после успешной паузы: цель PAUSED, сосед остался ACTIVE."""

    return _live_inventory(target_effective="PAUSED")


def _source_record(
    subject: SubjectRef,
    metric: Metric,
    value,
    source,
    window,
    currency: str | None,
    now: datetime,
) -> SourceRecord:
    return SourceRecord(
        category=FactCategory.BUSINESS_METRIC,
        subject=subject,
        metric=metric,
        value=value,
        source=source,
        state=EvidenceState.FRESH_COMPLETE,
        observed_at=now,
        window=window,
        currency=currency,
    )


def _source(source, records, now: datetime) -> SourceEvidence:
    return SourceEvidence(
        source=source,
        state=EvidenceState.FRESH_COMPLETE,
        fetched_at=now,
        data_as_of=now,
        from_cache=False,
        complete=True,
        records=tuple(records),
    )


def _fake_source_loaders(monkeypatch, *, leads: int = 5, quals: int = 1) -> None:
    """FB/AMO/CDP отдают ровно те метрики, из которых собираются PAUSE-факты."""

    from services.approval_checker_models import SourceSystem

    def _facebook(request, now, *, force_live):
        subject = request.subjects[0]
        window = request.windows[0]
        return _source(
            SourceSystem.FACEBOOK,
            (
                _source_record(
                    subject, Metric.SPEND, Decimal("12.34"),
                    SourceSystem.FACEBOOK, window, "USD", now,
                ),
                _source_record(
                    subject, Metric.LEADS, leads,
                    SourceSystem.FACEBOOK, window, None, now,
                ),
            ),
            now,
        )

    def _amo(request, now, *, force_live):
        return _source(
            SourceSystem.AMO,
            (
                _source_record(
                    request.subjects[0], Metric.QUALS, quals,
                    SourceSystem.AMO, request.windows[0], None, now,
                ),
            ),
            now,
        )

    def _cdp(request, now, *, force_live):
        return _source(
            SourceSystem.CDP_ERP,
            (
                _source_record(
                    request.subjects[0], Metric.REVENUE, Decimal("0"),
                    SourceSystem.CDP_ERP, request.windows[0], "LCY", now,
                ),
            ),
            now,
        )

    monkeypatch.setattr(approval_source_facebook, "load_facebook_evidence", _facebook)
    monkeypatch.setattr(
        approval_source_facebook, "load_ad_account_timezone", lambda ad_id: "UTC"
    )
    monkeypatch.setattr(approval_source_amo, "load_amo_evidence", _amo)
    monkeypatch.setattr(approval_source_cdp, "load_cdp_evidence", _cdp)


def _live_bundle(now: datetime) -> EvidenceBundle:
    from services.approval_checker_models import SourceSystem

    sources = tuple(
        _source(system, (), now)
        for system in (
            SourceSystem.FACEBOOK,
            SourceSystem.TRELLO,
            SourceSystem.MEDIA_BYTES,
            SourceSystem.AMO,
            SourceSystem.CDP_ERP,
        )
    )
    return EvidenceBundle(
        loaded_at=now,
        sources=sources,
        freshness=tuple(
            SourceFreshness(
                source=item.source,
                fetched_at=now,
                observed_at=now,
                data_as_of=now,
                max_age_seconds=1,
                from_cache=False,
                complete=True,
                fresh=True,
            )
            for item in sources
        ),
        facebook_sha256="1" * 64,
        trello_sha256="2" * 64,
        media_sha256="3" * 64,
        amo_sha256="4" * 64,
        cdp_sha256="5" * 64,
        final_live_state_sha256=LIVE_SHA,
        local_state_sha256=None,
    )


def _safe_review(batch, item, item_index, evidence, now) -> ActionReview:
    return ActionReview(
        check_id="check-e2e",
        operation_id=batch.idempotency_key,
        idempotency_key=batch.idempotency_key,
        batch_manifest_id=batch.batch_manifest_id,
        item_id=item.manifest_id,
        item_index=item_index,
        action_kind=item.kind,
        decision=SafetyDecision.SAFE,
        checked_at=now,
        expires_at=now + timedelta(minutes=2),
        batch_manifest_sha256=batch.manifest_sha256,
        item_manifest_sha256=manifest_sha256(item),
        evidence_state_sha256=evidence.final_live_state_sha256,
        subject_ids=batch.subject_ids,
        issues=(),
        audit_persisted=False,
    )


@pytest.fixture
def live_pause(monkeypatch, tmp_path) -> dict:
    """Живой PAUSE-путь: настоящий адаптер, замоканы только внешние чтения и FB-мутация."""

    inventory = {"value": _live_inventory()}
    mutations: list[dict[str, str]] = []

    monkeypatch.setattr(action_locks, "_lock_root", lambda: tmp_path / "locks")
    # Сборка манифеста: живой инвентарь адсета + живые факты FB/AMO/CDP.
    monkeypatch.setattr(
        owner_action_live_manifest,
        "fetch_pause_inventory",
        lambda ad_ids: {ADSET_ID: inventory["value"]},
    )
    _fake_source_loaders(monkeypatch)

    # Live review исполнителя: пять источников полны, вердикт SAFE.
    monkeypatch.setattr(
        owner_action_executor,
        "load_action_item_evidence",
        lambda batch, item, index, now: _live_bundle(now),
    )
    monkeypatch.setattr(owner_action_executor, "evaluate_action_item", _safe_review)

    # Настоящий PauseActionAdapter: провайдерские чтения замоканы, мутация — нет.
    def _observation(now, target_state):
        return ActionObservation(
            observed_at=now,
            digest=LIVE_SHA,
            target_state=target_state,
            subject_ids=(AD_ID,),
            unrelated_state_digest=UNRELATED_SHA,
        )

    monkeypatch.setattr(
        action_adapter_pause,
        "_validate_pause_locked",
        lambda manifest: LockedPauseValidation(
            action_result=None,
            check_reason=None,
            ad_id=manifest.ad_id,
            adset_id=manifest.adset_id,
            active_other_ids=manifest.sibling_active_ids,
            inventory_sha256=manifest.pre_inventory_sha256,
        ),
    )
    monkeypatch.setattr(
        action_adapter_pause,
        "read_facebook_precondition",
        lambda manifest, now: _observation(now, "ACTIVE|ACTIVE"),
    )
    # _five_source_observation остаётся настоящим: он сам требует пять полных и
    # свежих источников и сам подписывает наблюдение subject_id манифеста.
    monkeypatch.setattr(
        action_adapter_pause,
        "load_action_item_evidence",
        lambda batch, item, index, now: _live_bundle(now),
    )
    monkeypatch.setattr(
        action_adapter_pause,
        "read_facebook_postcondition",
        lambda manifest, created_ids, now: replace(
            _observation(now, "PAUSED|PAUSED"),
            unrelated_state_digest=UNRELATED_SHA,
        ),
    )
    monkeypatch.setattr(
        action_adapter_pause,
        "fetch_pause_inventory",
        lambda ad_ids: {ADSET_ID: _post_pause_inventory()},
    )

    def _set_ad_status(attempt, *, account_id, ad_id, status, payload_sha256):
        mutations.append(
            {
                "ad_id": ad_id,
                "status": status,
                "attempt_id": attempt.attempt_id,
                "payload_sha256": payload_sha256,
            }
        )
        return True

    monkeypatch.setattr(action_adapter_pause, "set_ad_status", _set_ad_status)
    return {"inventory": inventory, "mutations": mutations}


def _approved_pause(
    settings: OwnerApprovalConfig,
    *,
    intended_payload: dict | None = None,
    update_id: int = 9601,
) -> tuple[str, int, OwnerDeliveryOutbox, RecordingSender]:
    proposal_id, message_id, outbox, sender = _deliver_card(
        settings,
        name="live",
        intended_payload=intended_payload or _pause_intent(),
        idempotency_key=PAUSE_KEY,
        subject_id=AD_ID,
        adset_id=ADSET_ID,
    )
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=update_id,
        now=NOW + timedelta(minutes=1),
    )
    return proposal_id, message_id, outbox, sender


def test_approved_pause_runs_end_to_end_and_reports_success(
    settings, alerts, tmp_path, live_pause
) -> None:
    """Нажал «Одобрить» → крон → адаптер → FB → CONFIRMED → «✅ Сработало».

    Без сборки манифеста по живому состоянию этот тест падает на первом же
    проходе крона: run.state = BLOCKED_STALE, причина ACTION_MANIFEST_INVALID.
    """

    proposal_id, message_id, outbox, sender = _approved_pause(settings)
    assert _queued_job(settings, proposal_id)["state"] == "QUEUED"

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))
    outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=3))

    assert (run.claimed, run.executed, run.retried, run.exhausted) == (1, 1, 0, 0)
    assert run.errors == ()
    # Мутация в Facebook действительно случилась — ровно одна и ровно та.
    assert [item["ad_id"] for item in live_pause["mutations"]] == [AD_ID]
    assert live_pause["mutations"][0]["status"] == "PAUSED"
    lifecycle = _rows(
        settings,
        "SELECT state FROM owner_action_lifecycle WHERE proposal_id = ?",
        (proposal_id,),
    )[0]["state"]
    assert lifecycle == LifecycleState.EXECUTED.value
    attempts = _rows(
        settings,
        "SELECT state FROM owner_action_attempts WHERE proposal_id = ?",
        (proposal_id,),
    )
    assert [row["state"] for row in attempts] == ["CONFIRMED"]
    replies = [
        payload
        for payload in sender.payloads
        if payload.get("reply_to_message_id") == message_id
    ]
    assert len(replies) == 1
    assert str(replies[0]["text"]).startswith("✅ Сработало")
    assert alerts == []


def test_pause_manifest_uses_live_inventory_not_producer_snapshot(
    settings, alerts, tmp_path, live_pause
) -> None:
    """Манифест описывает Facebook НА МОМЕНТ исполнения, а не на момент карточки."""

    _approved_pause(settings, update_id=9602)

    _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))

    permit = _rows(settings, "SELECT manifest_json FROM owner_technical_permits")[0]
    import json

    manifest = json.loads(str(permit["manifest_json"]))
    live = _live_inventory()
    assert manifest["kind"] == "PAUSE"
    assert manifest["idempotency_key"] == PAUSE_KEY
    # Digest — живой, а не producer_inventory_sha256 из намерения ("a" * 64).
    assert manifest["pre_inventory_sha256"] == live["state_sha256"]
    assert manifest["pre_inventory_sha256"] != "a" * 64
    assert manifest["sibling_active_ids"] == [SIBLING_ID]
    assert {claim["source"] for claim in manifest["facts"]} == {
        "FACEBOOK",
        "AMO",
        "CDP_ERP",
    }


def test_intent_without_required_field_blocks_with_manifest_invalid(
    settings, alerts, tmp_path, live_pause, monkeypatch
) -> None:
    """Битое намерение — это ACTION_MANIFEST_INVALID, и одобрение не сгорает."""

    intent = _pause_intent()
    del intent["reason_code"]
    proposal_id, _message_id, _outbox, _sender = _approved_pause(
        settings,
        intended_payload=intent,
        update_id=9603,
    )

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))

    assert run.claimed == 1
    assert live_pause["mutations"] == []
    lifecycle = _rows(
        settings,
        "SELECT state, latest_reason_code FROM owner_action_lifecycle "
        "WHERE proposal_id = ?",
        (proposal_id,),
    )[0]
    assert lifecycle["state"] == LifecycleState.EXECUTION_RETRY_WAIT.value
    assert str(lifecycle["latest_reason_code"]).startswith("ACTION_MANIFEST_INVALID")
    assert "REASON_CODE" in str(lifecycle["latest_reason_code"])
    # Задание не залипло в REVIEWING — оно вернулось в очередь повторов.
    job = _queued_job(settings, proposal_id)
    assert job["state"] == "WAITING_RETRY"
    assert job["lease_token"] is None


def test_live_state_drift_blocks_with_its_own_reason_code(
    settings, alerts, tmp_path, live_pause
) -> None:
    """Реклама уже не ACTIVE → BLOCKED_STALE с кодом дрейфа, а не «битый манифест»."""

    proposal_id, _message_id, _outbox, _sender = _approved_pause(
        settings,
        update_id=9604,
    )
    # Владелец одобрял паузу активной рекламы, а её успели выключить без нас.
    live_pause["inventory"]["value"] = _live_inventory(target_effective="PAUSED")

    _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))

    assert live_pause["mutations"] == []
    lifecycle = _rows(
        settings,
        "SELECT state, latest_reason_code FROM owner_action_lifecycle "
        "WHERE proposal_id = ?",
        (proposal_id,),
    )[0]
    assert lifecycle["state"] == LifecycleState.BLOCKED_STALE.value
    assert (
        str(lifecycle["latest_reason_code"])
        == "LIVE_STATE_DRIFT:LIVE_TARGET_STATUS_CHANGED"
    )
    # Код дрейфа обязан отличаться от кода бага сборки.
    assert not str(lifecycle["latest_reason_code"]).startswith(
        "ACTION_MANIFEST_INVALID"
    )
    # Терминальное задание тоже закрыто, а не висит в REVIEWING.
    assert _queued_job(settings, proposal_id)["state"] == "BLOCKED_STALE"


def test_reviewing_job_with_expired_lease_is_recovered_and_executed(
    settings, alerts, tmp_path, live_pause
) -> None:
    """Воркер умер посреди live review → задание не потеряно, а доисполнено."""

    proposal_id, message_id, outbox, sender = _approved_pause(
        settings,
        update_id=9605,
    )
    # Ровно то состояние, в котором застревали задания: job
    # REVIEWING под протухшей арендой, lifecycle всё ещё LIVE_REVIEW.
    _force_lifecycle(settings, proposal_id, ("EXECUTION_QUEUED", "LIVE_REVIEW"))
    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute(
            "UPDATE owner_execution_jobs "
            "SET state = 'REVIEWING', lease_token = ?, lease_until = ?, "
            "    updated_at = ? WHERE proposal_id = ?",
            (
                "dead-worker:1",
                (NOW + timedelta(minutes=2)).isoformat(),
                NOW.isoformat(),
                proposal_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    # Пока аренда жива — задание чужое.
    held = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=1))
    assert held.claimed == 0

    recovered = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=30))
    outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=31))

    assert (recovered.claimed, recovered.executed) == (1, 1)
    assert [item["ad_id"] for item in live_pause["mutations"]] == [AD_ID]
    assert _queued_job(settings, proposal_id)["state"] == "EXECUTED"
    replies = [
        payload
        for payload in sender.payloads
        if payload.get("reply_to_message_id") == message_id
    ]
    assert [str(item["text"]).startswith("✅ Сработало") for item in replies] == [True]


def test_repeated_manifest_failure_stops_and_alerts_owner(
    settings, alerts, tmp_path, live_pause
) -> None:
    """Возврат в повтор не вечен: после потолка — терминальный отказ и алерт."""

    intent = _pause_intent()
    del intent["adset_id"]
    proposal_id, _message_id, _outbox, _sender = _approved_pause(
        settings,
        intended_payload=intent,
        update_id=9606,
    )

    for index in range(owner_action_executor.OWNER_EXECUTION_MAX_ATTEMPTS):
        run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=10 + index * 60))
    after_cap = _run_queue(settings, tmp_path, now=NOW + timedelta(hours=12))

    assert run.exhausted == 1
    assert after_cap.claimed == 0
    assert "Одобренное действие не исполнено" in [title for title, _ in alerts]
    assert live_pause["mutations"] == []


def test_reviewing_job_without_lease_is_claimed_and_executed(
    settings, alerts, tmp_path, live_pause
) -> None:
    """REVIEWING с пустой арендой — не «чужое задание», а брошенное: забираем.

    Именно так висели одобренные задания: аренду прошлый проход
    снял, lifecycle остался LIVE_REVIEW, а провайдерских попыток — ноль.
    """

    proposal_id, message_id, outbox, sender = _approved_pause(
        settings,
        update_id=9607,
    )
    _force_lifecycle(settings, proposal_id, ("EXECUTION_QUEUED", "LIVE_REVIEW"))
    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute(
            "UPDATE owner_execution_jobs "
            "SET state = 'REVIEWING', lease_token = NULL, lease_until = NULL, "
            "    next_attempt_at = NULL, updated_at = ? WHERE proposal_id = ?",
            (NOW.isoformat(), proposal_id),
        )
        connection.commit()
    finally:
        connection.close()
    assert _queued_job(settings, proposal_id)["lease_until"] is None

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))
    outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=3))

    assert (run.claimed, run.executed) == (1, 1)
    assert [item["ad_id"] for item in live_pause["mutations"]] == [AD_ID]
    assert _queued_job(settings, proposal_id)["state"] == "EXECUTED"
    replies = [
        payload
        for payload in sender.payloads
        if payload.get("reply_to_message_id") == message_id
    ]
    assert [str(item["text"]).startswith("✅ Сработало") for item in replies] == [True]


def test_reviewing_job_with_retry_lifecycle_is_realigned_and_executed(
    settings, alerts, tmp_path, live_pause
) -> None:
    """Рассинхрон «job REVIEWING + lifecycle EXECUTION_RETRY_WAIT» не хоронит задание.

    Такие пары остались от кода, который двигал lifecycle без задания: диспетчер
    их забирал, но live review падал на проверке парного состояния (LineageError)
    ещё до создания попытки — три прохода, и владельцу уходило «не сработало».
    """

    proposal_id, _message_id, outbox, sender = _approved_pause(
        settings,
        update_id=9608,
    )
    _force_lifecycle(
        settings,
        proposal_id,
        ("EXECUTION_QUEUED", "LIVE_REVIEW", "EXECUTION_RETRY_WAIT"),
    )
    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute(
            "UPDATE owner_execution_jobs "
            "SET state = 'REVIEWING', lease_token = NULL, lease_until = NULL, "
            "    next_attempt_at = NULL, updated_at = ? WHERE proposal_id = ?",
            (NOW.isoformat(), proposal_id),
        )
        connection.commit()
    finally:
        connection.close()

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))

    assert (run.claimed, run.executed) == (1, 1)
    assert run.errors == ()
    assert [item["ad_id"] for item in live_pause["mutations"]] == [AD_ID]
    assert _queued_job(settings, proposal_id)["state"] == "EXECUTED"
    assert alerts == []


# ---------------------------------------------------------------------------
# 9. Бюджет времени прогона и устойчивость к недоступным источникам.
#
# Инцидент: прогон, начатый в 09:08, шёл ещё в 09:14 — AMO отвечал таймаутами,
# а исполнитель на КАЖДОЕ задание заново тянул 30-дневные факты FB/AMO/CDP.
# APScheduler начал пропускать тики («maximum number of running instances
# reached»), и очередь одобренных владельцем действий встала целиком. Плюс
# недоступность источника уводила предложение в терминальный BLOCKED_STALE —
# минутный сбой AMO сжигал одобрение навсегда.
# ---------------------------------------------------------------------------


class FakeClock:
    """Управляемые монотонные часы бюджета."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_run_budget_returns_untouched_jobs_to_queue(
    settings, alerts, tmp_path, live_pause, monkeypatch
) -> None:
    """Кончился бюджет прогона — незапущенное задание возвращается в очередь."""

    first, _message_id, _outbox, _sender = _approved_pause(settings, update_id=9701)
    second, second_message, second_outbox, second_sender = _deliver_card(
        settings,
        name="second",
        intended_payload=_pause_intent(),
        idempotency_key="1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        subject_id=AD_ID,
        adset_id=ADSET_ID,
    )
    _approve(
        settings,
        proposal_id=second,
        message_id=second_message,
        outbox=second_outbox,
        sender=second_sender,
        update_id=9711,
        now=NOW + timedelta(minutes=1),
    )

    clock = FakeClock()
    monkeypatch.setattr(owner_action_executor, "_monotonic", clock)
    executed_ids: list[str] = []
    real_execute = owner_action_executor.execute_owner_approved

    def _slow_execute(target_id: str, **kwargs):
        executed_ids.append(target_id)
        run = real_execute(target_id, **kwargs)
        # Первое задание отработало, но съело весь бюджет прогона.
        clock.advance(owner_action_executor.OWNER_EXECUTION_RUN_BUDGET_SECONDS)
        return run

    monkeypatch.setattr(owner_action_executor, "execute_owner_approved", _slow_execute)

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))

    assert run.claimed == 2
    # Порядок выборки — по created_at/job_id, поэтому важен факт: одно задание
    # исполнено, второе не начато и возвращено в очередь целым.
    assert len(executed_ids) == 1
    postponed = second if executed_ids[0] == first else first
    assert (run.executed, run.deferred) == (1, 1)
    # Отложенное задание не потеряно: аренды нет, повтор разрешён сразу.
    deferred_job = _queued_job(settings, postponed)
    assert deferred_job["state"] in ("QUEUED", "WAITING_RETRY")
    assert deferred_job["lease_token"] is None and deferred_job["lease_until"] is None
    # Потолок попыток на отложенное задание не тратится.
    assert owner_action_executor._load_dispatch_state(tmp_path / "dispatch.json") == {}

    monkeypatch.setattr(owner_action_executor, "execute_owner_approved", real_execute)
    clock.advance(1000.0)
    second_run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=5))

    assert (second_run.claimed, second_run.executed) == (1, 1)
    assert _queued_job(settings, postponed)["state"] == "EXECUTED"
    assert alerts == []


def test_job_budget_exceeded_returns_job_to_waiting_retry(
    settings, alerts, tmp_path, live_pause, monkeypatch
) -> None:
    """Задание, вышедшее за свой бюджет, уходит в WAITING_RETRY без мутаций."""

    proposal_id, _message_id, _outbox, _sender = _approved_pause(
        settings,
        update_id=9702,
    )
    clock = FakeClock()
    monkeypatch.setattr(owner_action_executor, "_monotonic", clock)
    inventory = live_pause["inventory"]

    def _slow_inventory(ad_ids):
        # Сборка манифеста «зависла» на источнике дольше бюджета задания.
        clock.advance(owner_action_executor.OWNER_EXECUTION_JOB_BUDGET_SECONDS + 1)
        return {ADSET_ID: inventory["value"]}

    monkeypatch.setattr(
        owner_action_live_manifest,
        "fetch_pause_inventory",
        _slow_inventory,
    )

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))

    assert live_pause["mutations"] == []
    lifecycle = _rows(
        settings,
        "SELECT state, latest_reason_code FROM owner_action_lifecycle "
        "WHERE proposal_id = ?",
        (proposal_id,),
    )[0]
    assert lifecycle["state"] == LifecycleState.EXECUTION_RETRY_WAIT.value
    assert str(lifecycle["latest_reason_code"]).startswith("EXECUTION_BUDGET_EXCEEDED")
    job = _queued_job(settings, proposal_id)
    assert job["state"] == "WAITING_RETRY"
    assert job["lease_token"] is None
    assert (run.executed, run.retried, run.exhausted) == (0, 1, 0)
    # Своё же превышение бюджета не тратит потолок попыток владельца.
    state = owner_action_executor._load_dispatch_state(tmp_path / "dispatch.json")
    assert int(state[str(job["job_id"])]["attempts"]) == 0

    # Источник ответил быстро — задание доезжает само.
    monkeypatch.setattr(
        owner_action_live_manifest,
        "fetch_pause_inventory",
        lambda ad_ids: {ADSET_ID: inventory["value"]},
    )
    recovered = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=10))

    assert (recovered.claimed, recovered.executed) == (1, 1)
    assert [item["ad_id"] for item in live_pause["mutations"]] == [AD_ID]
    assert alerts == []


def test_amo_timeout_does_not_cancel_approved_action(
    settings, alerts, tmp_path, live_pause, monkeypatch
) -> None:
    """AMO отвалился — одобрение живо: повтор, а не терминальный BLOCKED_STALE."""

    import requests

    proposal_id, message_id, outbox, sender = _approved_pause(
        settings,
        update_id=9703,
    )

    def _amo_down(request, now, *, force_live):
        raise requests.exceptions.ReadTimeout("AMO GET leads таймаут")

    monkeypatch.setattr(approval_source_amo, "load_amo_evidence", _amo_down)

    # Даже нескольких проходов подряд недостаточно, чтобы сжечь одобрение.
    for index in range(owner_action_executor.OWNER_EXECUTION_MAX_ATTEMPTS + 1):
        run = _run_queue(
            settings,
            tmp_path,
            now=NOW + timedelta(minutes=10 + index * 60),
        )
        assert run.exhausted == 0

    assert live_pause["mutations"] == []
    lifecycle = _rows(
        settings,
        "SELECT state, latest_reason_code FROM owner_action_lifecycle "
        "WHERE proposal_id = ?",
        (proposal_id,),
    )[0]
    assert lifecycle["state"] == LifecycleState.EXECUTION_RETRY_WAIT.value
    assert str(lifecycle["latest_reason_code"]).startswith("SOURCE_UNAVAILABLE")
    assert _queued_job(settings, proposal_id)["state"] == "WAITING_RETRY"
    assert "Одобренное действие не исполнено" not in [title for title, _ in alerts]

    # AMO ожил — одобренная пауза доезжает без нового нажатия владельца.
    _fake_source_loaders(monkeypatch)
    recovered = _run_queue(settings, tmp_path, now=NOW + timedelta(hours=5))
    outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(hours=5, minutes=1))

    assert (recovered.claimed, recovered.executed) == (1, 1)
    assert [item["ad_id"] for item in live_pause["mutations"]] == [AD_ID]
    replies = [
        payload
        for payload in sender.payloads
        if payload.get("reply_to_message_id") == message_id
    ]
    assert [str(item["text"]).startswith("✅ Сработало") for item in replies] == [True]


def test_amo_reads_inside_job_run_under_short_budget(
    settings, alerts, tmp_path, live_pause, monkeypatch
) -> None:
    """Внутри задания AMO читается с коротким таймаутом и без трёх попыток."""

    from integrations import amo

    _approved_pause(settings, update_id=9705)
    seen: list[object] = []
    original = approval_source_amo.load_amo_evidence

    def _spy(request, now, *, force_live):
        seen.append(amo._REQUEST_BUDGET.get())
        return original(request, now, force_live=force_live)

    monkeypatch.setattr(approval_source_amo, "load_amo_evidence", _spy)

    _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))

    assert seen, "AMO-чтение внутри задания не случилось — тест бесполезен"
    budget = seen[0]
    assert budget is not None
    assert budget.read_seconds == owner_action_executor.OWNER_EXECUTION_AMO_READ_SECONDS
    assert budget.attempts == owner_action_executor.OWNER_EXECUTION_AMO_ATTEMPTS
    assert budget.deadline_monotonic is not None
    # Бюджет живёт только внутри задания и не протекает на другие кроны.
    assert amo._REQUEST_BUDGET.get() is None


def test_incomplete_amo_source_is_source_unavailable_not_manifest_bug(
    settings, alerts, tmp_path, live_pause, monkeypatch
) -> None:
    """Неполный ответ AMO — «источник недоступен», с указанием какого именно."""

    from services.approval_checker_models import SourceSystem

    proposal_id, _message_id, _outbox, _sender = _approved_pause(
        settings,
        update_id=9704,
    )

    def _amo_incomplete(request, now, *, force_live):
        return SourceEvidence(
            source=SourceSystem.AMO,
            state=EvidenceState.INCOMPLETE,
            fetched_at=now,
            data_as_of=None,
            from_cache=False,
            complete=False,
            records=(),
            error_code="AMO_PAGE_LIMIT_EXCEEDED",
        )

    monkeypatch.setattr(approval_source_amo, "load_amo_evidence", _amo_incomplete)

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))

    assert live_pause["mutations"] == []
    lifecycle = _rows(
        settings,
        "SELECT state, latest_reason_code FROM owner_action_lifecycle "
        "WHERE proposal_id = ?",
        (proposal_id,),
    )[0]
    assert lifecycle["state"] == LifecycleState.EXECUTION_RETRY_WAIT.value
    # Причина называет и источник, и его собственный диагноз: голый
    # LIVE_FACTS_INCOMPLETE_AMO в сотнях событий инцидента не говорил ничего.
    assert str(lifecycle["latest_reason_code"]) == (
        "SOURCE_UNAVAILABLE:LIVE_FACTS_INCOMPLETE_AMO:AMO_PAGE_LIMIT_EXCEEDED"
    )
    assert run.exhausted == 0
    assert alerts == []


def _amo_lead_row(lead_id: int, ad_id: str, *, qualified: bool) -> dict[str, object]:
    fields: list[dict[str, object]] = [
        {"field_id": 902422, "field_name": "fb_ad_id", "values": [{"value": ad_id}]}
    ]
    if qualified:
        fields.append(
            {"field_name": "Квалификация пройдена", "values": [{"value": "ДА"}]}
        )
    created = int((NOW - timedelta(days=1)).timestamp())
    return {
        "id": lead_id,
        "created_at": created,
        "updated_at": created,
        "custom_fields_values": fields,
    }


def _amo_pages_fake(rows: list[dict[str, object]], calls: list) -> object:
    """Фейк AMO как ЖИВОЙ аккаунт acme: ``query`` — да, фильтр по полю — 400.

    Прошлая версия «уважала» фильтр ``filter[custom_fields_values][902422][]``,
    которого у аккаунта нет: живой AMO отвечает 400 «Invalid filter for current
    account». Зелёный тест с щедрым фейком и был дырой,
    через которую залипание уехало в прод.
    """

    def _page(params):
        items = list(params.items()) if isinstance(params, dict) else list(params)
        calls.append(items)
        if any(key.startswith("filter[custom_fields_values]") for key, _ in items):
            raise Exception(
                "AMO API ошибка (400): Invalid filter for current account"
            )
        page = next(int(value) for key, value in items if key == "page")
        term = next((str(value) for key, value in items if key == "query"), None)
        if page > 1:
            return []
        return [
            row
            for row in rows
            if term is None
            or any(
                term in str(value.get("value") or "")
                for field in row.get("custom_fields_values") or []
                for value in field.get("values") or []
                if isinstance(value, dict)
            )
        ]

    return _page


def test_approved_pause_builds_manifest_without_scanning_the_funnel(
    settings, alerts, tmp_path, live_pause, monkeypatch
) -> None:
    """Живой AMO внутри исполнения: точечный ``query``, ни одного оконного.

    Два инцидента, оба — «одобрено, но owner_action_attempts пуст». Сначала
    сборка манифеста вычитывала весь поток воронки за 30 дней (14k+ лидов,
    242с) и не влезала ни в какой бюджет. Потом точечный путь собрали на
    фильтре по кастомному полю, который аккаунту недоступен (400 «Invalid
    filter for current account») — 211 ретраев LIVE_FACTS_INCOMPLETE_AMO за
    6,5 часов, пока предложения не умерли от дрейфа. Оконное чтение здесь
    запрещено насмерть, а фейк AMO отвергает фильтр как живой API.
    """

    calls: list = []
    rows = [
        _amo_lead_row(1, AD_ID, qualified=True),
        _amo_lead_row(2, AD_ID, qualified=False),
        _amo_lead_row(3, "9999", qualified=True),
    ]

    def _forbidden_window(_window):
        raise AssertionError("оконное чтение AMO на пути исполнения запрещено")

    monkeypatch.setattr(approval_source_amo, "_load_window_leads", _forbidden_window)
    monkeypatch.setattr(
        approval_source_amo, "_raw_leads_page", _amo_pages_fake(rows, calls)
    )
    # Возвращаем НАСТОЯЩИЙ загрузчик поверх фикстурного фейка: именно он и
    # падал на проде, значит именно он должен участвовать в тесте.
    monkeypatch.setattr(
        approval_source_amo, "load_amo_evidence", _REAL_AMO_EVIDENCE
    )

    proposal_id, message_id, outbox, sender = _approved_pause(
        settings,
        update_id=9707,
    )
    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))
    outbox.deliver(worker_id="delivery-1", now=NOW + timedelta(minutes=3))

    assert (run.claimed, run.executed, run.retried, run.exhausted) == (1, 1, 0, 0)
    assert [item["ad_id"] for item in live_pause["mutations"]] == [AD_ID]
    assert len(calls) == 1, f"AMO должен читаться точечно один раз, было {len(calls)}"
    assert next(value for key, value in calls[0] if key == "query") == AD_ID
    assert not any(
        key.startswith("filter[custom_fields_values]") for key, _ in calls[0]
    ), "фильтр по кастомному полю мёртв для этого аккаунта (400)"
    lifecycle = _rows(
        settings,
        "SELECT state FROM owner_action_lifecycle WHERE proposal_id = ?",
        (proposal_id,),
    )[0]
    assert lifecycle["state"] == LifecycleState.EXECUTED.value
    # Стержень инцидента: за всю историю боевой БД owner_action_attempts был пуст.
    attempts = _rows(
        settings,
        "SELECT state FROM owner_action_attempts WHERE proposal_id = ?",
        (proposal_id,),
    )
    assert [row["state"] for row in attempts] == ["CONFIRMED"]
    replies = [
        payload
        for payload in sender.payloads
        if payload.get("reply_to_message_id") == message_id
    ]
    assert [str(item["text"]).startswith("✅ Сработало") for item in replies] == [True]


def test_execution_run_shares_one_live_read_scope(
    settings, alerts, tmp_path, live_pause, monkeypatch
) -> None:
    """Все живые чтения задания идут внутри одной области переиспользования."""

    from services import live_read_scope

    fixture_loader = approval_source_amo.load_amo_evidence
    seen: list[bool] = []

    def _spy(request, now, *, force_live):
        seen.append(live_read_scope.scope_is_open())
        return fixture_loader(request, now, force_live=force_live)

    monkeypatch.setattr(approval_source_amo, "load_amo_evidence", _spy)

    _approved_pause(settings, update_id=9708)
    run = _run_queue(settings, tmp_path, now=NOW + timedelta(minutes=2))

    assert (run.claimed, run.executed) == (1, 1)
    assert seen and all(seen), "AMO-чтение вне области переиспользования"
    # Область закрывается вместе с прогоном и не протекает на другие кроны.
    assert live_read_scope.scope_is_open() is False


def test_unique_uuid_key_is_not_reused_across_proposals() -> None:
    """Страховка от копипасты: PAUSE_KEY обязан быть валидным UUID."""

    assert uuid.UUID(PAUSE_KEY).version == 4


def test_budgets_fit_the_measured_cost_of_a_live_job() -> None:
    """Бюджеты обязаны покрывать реальную стоимость задания и не наезжать на крон.

    Замер на боевом задании (одобренная пауза): живой сбор фактов идёт
    трижды за задание, Facebook читается заново каждый раз — 15.5с + 15.5с +
    20.1с, плюс CDP ~8с и AMO ~0.7с на первом проходе. Прежние 45 секунд не
    покрывали этого никогда: дедлайн истекал, задание уходило в повтор, и ни
    одна одобренная пауза не исполнялась.

    Второй инвариант: прогон обязан завершаться до истечения аренды задания
    (OWNER_EXECUTION_LEASE_SECONDS). Пропуск тиков крона при живом прогоне —
    штатный режим (max_instances=1), а вот прогон, переживший собственную
    аренду, столкнулся бы с реаниматором лизов на том же задании.
    """

    # Замер после фикса дневного окна: полный холодный сбор
    # доказательств 110с (FB 30.3 + AMO 1.3 + CDP 77.8) плюс два повторных
    # чтения Facebook по ~30с — задание стоит ~170с. Прежний инвариант
    # «прогон короче тика крона» делал такой бюджет невозможным, очередь
    # стояла целиком; теперь допускаем пропуск тиков (max_instances=1 у
    # APScheduler делает это безопасным), но прогон ОБЯЗАН завершаться до
    # истечения аренды задания — иначе живой прогон и реаниматор лизов
    # начнут воевать за одно задание.
    measured_live_pass_seconds = 110 + 30 + 30

    assert (
        owner_action_executor.OWNER_EXECUTION_JOB_BUDGET_SECONDS
        >= measured_live_pass_seconds
    )
    assert (
        owner_action_executor.OWNER_EXECUTION_JOB_BUDGET_SECONDS
        <= owner_action_executor.OWNER_EXECUTION_RUN_BUDGET_SECONDS
    )
    assert (
        owner_action_executor.OWNER_EXECUTION_RUN_BUDGET_SECONDS
        < owner_action_executor.OWNER_EXECUTION_LEASE_SECONDS
    )
    assert (
        owner_action_executor.OWNER_EXECUTION_MIN_SLICE_SECONDS
        < owner_action_executor.OWNER_EXECUTION_JOB_BUDGET_SECONDS
    )


# ---------------------------------------------------------------------------
# 9. Протухание после одобрения: терминальное закрытие одним сообщением,
#    свипер одобренных, свипер недорешённых состояний, дедуп producer'ов.
# ---------------------------------------------------------------------------


def test_expired_approved_job_closes_terminally_with_single_message(
    settings, alerts, tmp_path
) -> None:
    """Протухшее одобренное умирает с первого клейма: без ретраев и без ❌×2."""

    valid_until = NOW + timedelta(hours=1)
    proposal_id, message_id, outbox, sender = _deliver_card(
        settings, valid_until=valid_until
    )
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9701,
        now=NOW + timedelta(minutes=30),
    )
    job_id = str(_queued_job(settings, proposal_id)["job_id"])

    # Спустя оба дедлайна (valid_until и одобрение + 6ч).
    run = _run_queue(settings, tmp_path, now=NOW + timedelta(hours=8))

    assert (run.claimed, run.expired, run.exhausted, run.retried) == (1, 1, 0, 0)
    # Штатное закрытие — не сбой прогона: cron-failure не поднимается.
    assert run.errors == ()
    lifecycle = _rows(
        settings,
        "SELECT state, latest_reason_code FROM owner_action_lifecycle "
        "WHERE proposal_id = ?",
        (proposal_id,),
    )[0]
    assert lifecycle["state"] == "EXPIRED"
    assert lifecycle["latest_reason_code"] == "PROPOSAL_EXPIRED"
    job = _queued_job(settings, proposal_id)
    assert job["state"] == "BLOCKED_STALE"
    assert job["lease_token"] is None and job["lease_until"] is None
    # Один честный след — и ноль критических алертов.
    trails = _rows(
        settings,
        "SELECT rendered_text FROM owner_trail_messages WHERE dedupe_key = ?",
        (f"exec-dispatch-failed:{proposal_id}:{job_id}",),
    )
    assert len(trails) == 1
    assert "устарела" in str(trails[0]["rendered_text"])
    assert alerts == []
    # Повторный тик задание больше не видит.
    run_next = _run_queue(settings, tmp_path, now=NOW + timedelta(hours=8, minutes=3))
    assert run_next.claimed == 0


def test_sweep_expired_approved_jobs_respects_approval_grace(
    settings, alerts, tmp_path
) -> None:
    """Свипер одобренных гасит по дедлайну исполнения, а не по valid_until."""

    valid_until = NOW + timedelta(hours=1)
    stale_id, stale_message, stale_outbox, stale_sender = _deliver_card(
        settings, name="stale", valid_until=valid_until
    )
    _approve(
        settings,
        proposal_id=stale_id,
        message_id=stale_message,
        outbox=stale_outbox,
        sender=stale_sender,
        update_id=9702,
        now=NOW + timedelta(minutes=30),
    )
    fresh_id, fresh_message, fresh_outbox, fresh_sender = _deliver_card(
        settings, name="fresh", valid_until=valid_until
    )
    _approve(
        settings,
        proposal_id=fresh_id,
        message_id=fresh_message,
        outbox=fresh_outbox,
        sender=fresh_sender,
        update_id=9703,
        now=NOW + timedelta(minutes=59),
    )

    # stale: одобрение+6ч = 6ч30м — уже позади; fresh: 6ч59м — ещё впереди.
    run = owner_action_executor.sweep_expired_approved_jobs(
        now=NOW + timedelta(hours=6, minutes=45),
        db_path=settings.db_path,
    )

    assert run.expired == 1
    states = {
        str(row["proposal_id"]): str(row["state"])
        for row in _rows(
            settings,
            "SELECT proposal_id, state FROM owner_action_lifecycle "
            "WHERE proposal_id IN (?, ?)",
            (stale_id, fresh_id),
        )
    }
    assert states[stale_id] == "EXPIRED"
    assert states[fresh_id] == "APPROVED"
    # Закрытие оставило один честный след по протухшему.
    stale_job = str(_queued_job(settings, stale_id)["job_id"])
    trails = _rows(
        settings,
        "SELECT rendered_text FROM owner_trail_messages WHERE dedupe_key = ?",
        (f"exec-dispatch-failed:{stale_id}:{stale_job}",),
    )
    assert len(trails) == 1
    assert alerts == []


def test_sweeper_defaults_cover_delivery_pending_and_postponed(settings) -> None:
    """Недоставленные и отложенные висяки гасятся регулярным свипером."""

    valid_until = NOW + timedelta(hours=1)
    # DELIVERY_PENDING: предложение создано, но до Telegram не доехало.
    undelivered_id = _make_proposal(settings, name="dp", valid_until=valid_until)
    # POSTPONED: карточка доставлена и отложена владельцем.
    postponed_id, _, _, _ = _deliver_card(
        settings, name="pp", valid_until=valid_until
    )
    repository = OwnerActionRepository(settings.db_path)
    view = repository.get_proposal(postponed_id)
    repository.transition_lifecycle(
        postponed_id,
        expected_state="PENDING_OWNER",
        expected_version=view.lifecycle_version,
        new_state="POSTPONED",
        actor="test",
        reason_code="OWNER_POSTPONED",
        now=NOW + timedelta(minutes=5),
    )

    run = owner_action_executor.sweep_expired_proposals(
        now=NOW + timedelta(hours=2),
        db_path=settings.db_path,
    )

    assert run.expired == 2
    states = {
        str(row["proposal_id"]): str(row["state"])
        for row in _rows(
            settings,
            "SELECT proposal_id, state FROM owner_action_lifecycle "
            "WHERE proposal_id IN (?, ?)",
            (undelivered_id, postponed_id),
        )
    }
    assert states == {undelivered_id: "EXPIRED", postponed_id: "EXPIRED"}
    # Кнопки отложенной карточки отозваны — нажатие честно не сработает.
    tokens = _rows(
        settings,
        "SELECT revoked_at, revoke_reason FROM owner_callback_tokens "
        "WHERE proposal_id = ? AND consumed_at IS NULL",
        (postponed_id,),
    )
    assert tokens and all(
        row["revoked_at"] is not None
        and row["revoke_reason"] == "PROPOSAL_EXPIRED"
        for row in tokens
    )


def test_find_live_proposal_ignores_expired(settings) -> None:
    """Протухший висяк не блокирует новое предложение по тому же объекту."""

    _make_proposal(
        settings,
        name="dup",
        valid_until=NOW + timedelta(hours=1),
        subject_id="ad-dup",
    )
    repository = OwnerActionRepository(settings.db_path)
    assert (
        repository.find_live_proposal_for_subject(
            subject_id="ad-dup",
            action_kind="PAUSE_AD",
            now=NOW + timedelta(minutes=30),
        )
        is not None
    )
    assert (
        repository.find_live_proposal_for_subject(
            subject_id="ad-dup",
            action_kind="PAUSE_AD",
            now=NOW + timedelta(hours=2),
        )
        is None
    )


def test_press_on_expired_card_gets_honest_refusal(settings) -> None:
    """Клик по протухшей карточке: честный тост вместо «Принято» + durable след."""

    valid_until = NOW + timedelta(hours=1)
    proposal_id, message_id, outbox, sender = _deliver_card(
        settings, valid_until=valid_until
    )
    approve_data = _approve_callback_data(settings, sender, proposal_id)
    ack = RecordingAck()
    service = OwnerApprovalTelegram(
        settings.db_path,
        settings,
        FakePollClient(
            [
                {
                    "update_id": 9801,
                    "callback_query": {
                        "id": "cbq-9801",
                        "from": {"id": OWNER_ID},
                        "data": approve_data,
                        "message": {
                            "message_id": message_id,
                            "chat": {"id": CHAT_ID},
                        },
                    },
                }
            ]
        ),
        delivery_outbox=outbox,
        ack_client=ack,
    )
    late = NOW + timedelta(hours=2)
    service.poll(now=late)
    batch = service.process_inbox(worker_id="inbox-1", now=late)

    assert batch.decision_count == 0
    assert batch.failed_count == 1
    # Ровно один ответ на query — и он честный, а не «Принято».
    assert [item["text"] for item in ack.answers] == [
        "⌛ Предложение устарело — пришлю свежее"
    ]
    # Отказ задублирован durable следом на карточку, с дедупом по причине.
    trails = _rows(
        settings,
        "SELECT dedupe_key, rendered_text FROM owner_trail_messages "
        "WHERE proposal_id = ?",
        (proposal_id,),
    )
    refusals = [
        row
        for row in trails
        if str(row["dedupe_key"]).startswith(f"press-refusal:{proposal_id}:")
    ]
    assert len(refusals) == 1
    assert "устарело" in str(refusals[0]["rendered_text"])
    # Решение не записано, задание не создано.
    assert (
        _rows(
            settings,
            "SELECT * FROM owner_execution_jobs WHERE proposal_id = ?",
            (proposal_id,),
        )
        == []
    )


def test_expired_live_review_orphan_is_closed_not_looped(
    settings, alerts, tmp_path
) -> None:
    """Сирота LIVE_REVIEW (умерший воркер) закрывается, а не клеймится вечно."""

    valid_until = NOW + timedelta(hours=1)
    proposal_id, message_id, outbox, sender = _deliver_card(
        settings, valid_until=valid_until
    )
    _approve(
        settings,
        proposal_id=proposal_id,
        message_id=message_id,
        outbox=outbox,
        sender=sender,
        update_id=9704,
        now=NOW + timedelta(minutes=30),
    )
    # Воркер умер посреди live review: доводим lifecycle до LIVE_REVIEW тем же
    # легальным путём, что и настоящий исполнитель, и «умираем» — lease нет.
    repository = OwnerActionRepository(settings.db_path)
    view = repository.get_proposal(proposal_id)
    repository.queue_execution(
        proposal_id=proposal_id,
        decision_id=view.active_decision_id,
        job_id=view.active_job_id,
        expected_lifecycle_version=view.lifecycle_version,
        actor="test",
        now=NOW + timedelta(minutes=31),
    )
    view = repository.get_proposal(proposal_id)
    repository.begin_claim_live_review(
        proposal_id=proposal_id,
        decision_id=view.active_decision_id,
        job_id=view.active_job_id,
        claim_id=view.targets[0].claim_id,
        expected_lifecycle_state="EXECUTION_QUEUED",
        expected_lifecycle_version=view.lifecycle_version,
        actor="test",
        now=NOW + timedelta(minutes=32),
    )

    run = _run_queue(settings, tmp_path, now=NOW + timedelta(hours=8))

    assert (run.claimed, run.expired) == (1, 1)
    assert run.errors == ()
    lifecycle = _rows(
        settings,
        "SELECT state FROM owner_action_lifecycle WHERE proposal_id = ?",
        (proposal_id,),
    )[0]
    assert lifecycle["state"] == "EXPIRED"
    # Следующий тик сироту больше не видит — цикла нет.
    run_next = _run_queue(settings, tmp_path, now=NOW + timedelta(hours=8, minutes=3))
    assert run_next.claimed == 0
    assert alerts == []


def test_failed_pause_alert_becomes_already_done_when_target_paused(monkeypatch):
    """«❌ Не сработало» по паузе сверяется с кабинетом: цель уже выключена → ✅.

    Сценарий: одобренные паузы исполнялись напрямую в обход застрявшей очереди,
    и хвостовые задания бомбили владельца ложными провалами.
    """
    from types import SimpleNamespace

    monkeypatch.setattr(
        owner_action_executor,
        "_pause_goal_already_reached",
        lambda proposal: True,
    )
    proposal = SimpleNamespace(
        proposal_kind=SimpleNamespace(value="PAUSE"),
        targets=(SimpleNamespace(intended_payload={"ad_id": "123"}),),
    )
    monkeypatch.setattr(
        owner_action_executor,
        "_repository",
        lambda: SimpleNamespace(get_proposal=lambda pid: proposal),
    )
    sent = {}

    def _capture(db_path, **kwargs):
        sent.update(kwargs)

    import config as _config
    monkeypatch.setattr(
        _config, "load_owner_approval_config",
        lambda: SimpleNamespace(db_path=":memory:"),
    )
    import services.owner_delivery_outbox as outbox

    monkeypatch.setattr(outbox, "enqueue_trail_message", _capture)
    monkeypatch.setattr(outbox, "latest_proposal_message_id", lambda db, pid: None)

    run = SimpleNamespace(
        proposal_id="p1", job_id="j1",
        state=owner_action_executor.LifecycleState.BLOCKED_STALE.value,
        reason_code="LIVE_REVIEW_FAILED",
        reconciliation_required=False, claim_id="c1",
        provider_mutation_count=0,
    )
    owner_action_executor.report_execution_result(run)

    assert sent["rendered_text"].startswith("✅ Уже выключено")


def test_failed_pause_alert_stays_honest_when_target_still_active(monkeypatch):
    """Цель ещё крутится — ❌ уходит как есть, сверка ничего не прячет."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        owner_action_executor,
        "_pause_goal_already_reached",
        lambda proposal: False,
    )
    proposal = SimpleNamespace(
        proposal_kind=SimpleNamespace(value="PAUSE"),
        targets=(SimpleNamespace(intended_payload={"ad_id": "123"}),),
    )
    monkeypatch.setattr(
        owner_action_executor,
        "_repository",
        lambda: SimpleNamespace(get_proposal=lambda pid: proposal),
    )
    sent = {}

    import config as _config
    monkeypatch.setattr(
        _config, "load_owner_approval_config",
        lambda: SimpleNamespace(db_path=":memory:"),
    )
    import services.owner_delivery_outbox as outbox

    monkeypatch.setattr(outbox, "enqueue_trail_message",
                        lambda db_path, **kw: sent.update(kw))
    monkeypatch.setattr(outbox, "latest_proposal_message_id", lambda db, pid: None)

    run = SimpleNamespace(
        proposal_id="p1", job_id="j1",
        state=owner_action_executor.LifecycleState.BLOCKED_STALE.value,
        reason_code="LIVE_REVIEW_FAILED",
        reconciliation_required=False, claim_id="c1",
        provider_mutation_count=0,
    )
    owner_action_executor.report_execution_result(run)

    assert sent["rendered_text"].startswith("❌ Не сработало")
