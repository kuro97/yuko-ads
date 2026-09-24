from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest

import services.approval_telegram as telegram
from services.approval_checker_models import (
    ActionKind,
    ActionResult,
    ActionRun,
    ApprovalDecision,
    DeliveryKind,
    FactFreeTemplate,
    OperationItemRecord,
    OperationRecord,
    OperationState,
    RenderedReport,
    ReportCheckAuditEvent,
    ReportTemplate,
    ReportVerdict,
    TelegramButton,
    TransportErrorCode,
    canonical_json,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)
MANIFEST_SHA = "a" * 64
OPERATION_ID = "11111111-1111-4111-8111-111111111111"
IDEMPOTENCY_KEY = "22222222-2222-4222-8222-222222222222"
BATCH_ID = "33333333-3333-4333-8333-333333333333"


def _report(
    verdict: ReportVerdict = ReportVerdict.VERIFIED,
    *,
    text: str = "✅ Вечерний отчёт\n\nИтог\nОплаты: 1",
) -> RenderedReport:
    field_ids = () if verdict in {ReportVerdict.BLOCKED, ReportVerdict.CHECKER_UNAVAILABLE} else ("payments",)
    return RenderedReport(
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        rendered_field_ids=field_ids,
        manifest_sha256=MANIFEST_SHA,
        verdict=verdict,
        check_id="check-report",
    )


def _report_proof(report: RenderedReport) -> ReportCheckAuditEvent:
    return ReportCheckAuditEvent(
        schema_version=1,
        event="report_check",
        event_id="event-report",
        written_at=NOW,
        correlation_id="report-run",
        check_id=report.check_id,
        report_template=ReportTemplate.EVENING,
        verdict=report.verdict,
        manifest_sha256=report.manifest_sha256,
        evidence_state_sha256=None,
        issue_codes=(),
        checked_at=NOW,
    )


def _operation(
    state: OperationState,
    result: ActionResult | None,
    *,
    reconciliation_required: bool = False,
) -> OperationRecord:
    item = OperationItemRecord(
        item_id="item-0",
        item_index=0,
        action_kind=ActionKind.PAUSE,
        item_manifest_sha256="b" * 64,
        subject_ids=("ad:1",),
        check_id="check-action",
        decision=(
            ApprovalDecision.DENIED
            if state is OperationState.DENIED
            else ApprovalDecision.APPROVED
        ),
        evidence_state_sha256="c" * 64,
        shadow_evaluation=None,
        permit_id=None,
        attempt_id=None if state is OperationState.DENIED else "attempt-1",
        result=result,
        exact_created_ids=(),
        attempted_at=None if state is OperationState.DENIED else NOW,
        completed_at=NOW if result is not None else None,
        reconciliation_required=reconciliation_required,
    )
    return OperationRecord(
        operation_id=OPERATION_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        batch_manifest_id=BATCH_ID,
        batch_manifest_sha256=MANIFEST_SHA,
        correlation_id="action-run",
        subject_ids=("ad:1",),
        state=state,
        items=(item,),
        reserved_at=NOW,
        updated_at=NOW,
        reconciliation_required=reconciliation_required,
    )


def _run(
    state: OperationState,
    result: ActionResult | None,
    *,
    reconciliation_required: bool = False,
) -> ActionRun:
    return ActionRun(
        operation_id=OPERATION_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        batch_manifest_id=BATCH_ID,
        batch_manifest_sha256=MANIFEST_SHA,
        state=state,
        reviews=(),
        executions=(),
        result=result,
        shadow_evaluation=None,
        shadow_items=(),
        dry_run=False,
        provider_mutation_count=0 if state is OperationState.DENIED else 1,
        first_unprocessed_index=None,
        stop_reason_code=None,
        reconciliation_required=reconciliation_required,
    )


def _capture_delivery(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[object]]:
    sent_texts: list[str] = []
    events: list[object] = []
    monkeypatch.setattr(
        telegram,
        "send_telegram",
        lambda text, channel="ads": sent_texts.append(text) is None,
    )
    monkeypatch.setattr(
        telegram,
        "append_delivery_event",
        lambda event: events.append(event) or event.event_id,
    )
    return sent_texts, events


def test_verified_report_requires_matching_persisted_check_and_audits_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report()
    monkeypatch.setattr(telegram, "find_report_check", lambda check_id: _report_proof(report))
    sent_texts, events = _capture_delivery(monkeypatch)

    delivery = telegram.send_checked_report(report)

    assert delivery.sent is True
    assert delivery.audit_persisted is True
    assert delivery.report_verdict is ReportVerdict.VERIFIED
    assert len(sent_texts) == 1
    assert sent_texts[0].startswith("✅ VERIFIED · check check-re")
    assert "Оплаты: 1" in sent_texts[0]
    assert len(events) == 1
    assert events[0].delivery_kind is DeliveryKind.REPORT
    assert events[0].reference_id == report.check_id
    assert events[0].sent is True
    assert events[0].transport_error_code is None
    assert events[0].payload_sha256 == hashlib.sha256(
        sent_texts[0].encode("utf-8")
    ).hexdigest()
    assert events[0].buttons_sha256 is None


def test_report_without_matching_wal_proof_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report()
    proof = replace(_report_proof(report), manifest_sha256="f" * 64)
    monkeypatch.setattr(telegram, "find_report_check", lambda check_id: proof)
    sent_texts, events = _capture_delivery(monkeypatch)

    delivery = telegram.send_checked_report(report)

    assert delivery.sent is False
    assert delivery.audit_persisted is False
    assert sent_texts == []
    assert events == []


def test_limited_report_keeps_safe_fields_but_rejects_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report(
        ReportVerdict.VERIFIED_WITH_LIMITATIONS,
        text="⚠️ Отчёт\n\nИтог\nЛиды: 4",
    )
    monkeypatch.setattr(telegram, "find_report_check", lambda check_id: _report_proof(report))
    sent_texts, events = _capture_delivery(monkeypatch)
    button = TelegramButton(
        text="Пауза",
        callback_data="pause:ad-1",
        action_kind=ActionKind.PAUSE,
        subject_id="ad-1",
        check_id=report.check_id,
        permit_seed_id=None,
    )

    delivery = telegram.send_checked_report(report, buttons=((button,),))

    assert delivery.sent is False
    assert sent_texts == []
    assert events == []


def test_verified_report_sends_typed_buttons_and_audits_exact_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report()
    monkeypatch.setattr(telegram, "find_report_check", lambda check_id: _report_proof(report))
    _, events = _capture_delivery(monkeypatch)
    button_calls: list[tuple[str, list[list[tuple[str, str]]]]] = []
    monkeypatch.setattr(
        "services.telegram_bot.send_with_buttons",
        lambda text, buttons: button_calls.append((text, buttons)) is None,
    )
    button = TelegramButton(
        text="Остановить",
        callback_data="pause:permit-1",
        action_kind=ActionKind.PAUSE,
        subject_id="ad-1",
        check_id=report.check_id,
        permit_seed_id="permit-1",
    )
    buttons = ((button,),)

    delivery = telegram.send_checked_report(report, buttons=buttons)

    assert delivery.sent is True
    assert delivery.audit_persisted is True
    assert len(button_calls) == 1
    assert button_calls[0][1] == [[("Остановить", "pause:permit-1")]]
    assert events[0].buttons_sha256 == hashlib.sha256(
        canonical_json(buttons)
    ).hexdigest()
    assert events[0].payload_sha256 == hashlib.sha256(
        button_calls[0][0].encode("utf-8")
    ).hexdigest()


def test_blocked_report_uses_fixed_notice_without_original_business_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report(
        ReportVerdict.BLOCKED,
        text="Секретный ad 123: $999, оплата 1",
    )
    monkeypatch.setattr(telegram, "find_report_check", lambda check_id: _report_proof(report))
    sent_texts, events = _capture_delivery(monkeypatch)

    delivery = telegram.send_checked_report(report)

    assert delivery.sent is True
    assert delivery.audit_persisted is True
    assert sent_texts == ["⛔ Отчёт не отправлен: проверка данных не пройдена."]
    assert "123" not in sent_texts[0]
    assert "999" not in sent_texts[0]
    assert events[0].report_verdict is ReportVerdict.BLOCKED


def test_transport_failure_is_audited_once_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report()
    monkeypatch.setattr(telegram, "find_report_check", lambda check_id: _report_proof(report))
    calls: list[str] = []
    events: list[object] = []
    monkeypatch.setattr(
        telegram,
        "send_telegram",
        lambda text, channel="ads": calls.append(text) and True,
    )
    monkeypatch.setattr(
        telegram,
        "append_delivery_event",
        lambda event: events.append(event) or event.event_id,
    )

    delivery = telegram.send_checked_report(report)

    assert delivery.sent is False
    assert delivery.audit_persisted is True
    assert len(calls) == 1
    assert len(events) == 1
    assert events[0].sent is False
    assert events[0].transport_error_code is TransportErrorCode.UNKNOWN


def test_post_send_audit_failure_keeps_real_sent_state_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report()
    monkeypatch.setattr(telegram, "find_report_check", lambda check_id: _report_proof(report))
    calls: list[str] = []
    monkeypatch.setattr(
        telegram,
        "send_telegram",
        lambda text, channel="ads": calls.append(text) is None,
    )

    def fail_audit(event: object) -> str:
        raise OSError("disk unavailable")

    monkeypatch.setattr(telegram, "append_delivery_event", fail_audit)

    delivery = telegram.send_checked_report(report)

    assert delivery.sent is True
    assert delivery.audit_persisted is False
    assert delivery.fallback_sent is False
    assert len(calls) == 1


def test_button_transport_failure_does_not_fallback_to_plain_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report()
    monkeypatch.setattr(telegram, "find_report_check", lambda check_id: _report_proof(report))
    plain_calls, events = _capture_delivery(monkeypatch)
    button_calls: list[object] = []
    monkeypatch.setattr(
        "services.telegram_bot.send_with_buttons",
        lambda text, buttons: button_calls.append((text, buttons)) and True,
    )
    button = TelegramButton(
        text="Остановить",
        callback_data="pause:permit-1",
        action_kind=ActionKind.PAUSE,
        subject_id="ad-1",
        check_id=report.check_id,
        permit_seed_id="permit-1",
    )

    delivery = telegram.send_checked_report(report, buttons=((button,),))

    assert delivery.sent is False
    assert delivery.fallback_sent is False
    assert delivery.audit_persisted is True
    assert len(button_calls) == 1
    assert plain_calls == []
    assert events[0].sent is False


@pytest.mark.parametrize(
    ("state", "result", "expected_text"),
    [
        (OperationState.CONFIRMED, ActionResult.CONFIRMED, "выполнено и подтверждено"),
        (OperationState.PARTIAL, ActionResult.PARTIAL, "Результат частичный"),
        (OperationState.FAILED, ActionResult.FAILED, "Действие не выполнено"),
        (OperationState.UNKNOWN, ActionResult.UNKNOWN, "Результат уточняется"),
    ],
)
def test_action_text_matches_terminal_wal_and_success_only_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    state: OperationState,
    result: ActionResult,
    expected_text: str,
) -> None:
    run = _run(state, result, reconciliation_required=result is ActionResult.UNKNOWN)
    persisted = _operation(
        state,
        result,
        reconciliation_required=result is ActionResult.UNKNOWN,
    )
    monkeypatch.setattr(telegram, "find_operation", lambda key: persisted)
    sent_texts, events = _capture_delivery(monkeypatch)

    delivery = telegram.send_action_outcome(run)

    assert delivery.sent is True
    assert delivery.audit_persisted is True
    assert expected_text in sent_texts[0]
    if result is not ActionResult.CONFIRMED:
        assert "выполнено и подтверждено" not in sent_texts[0]
    assert events[0].delivery_kind is DeliveryKind.ACTION
    assert events[0].action_result is result


def test_denied_action_is_safe_fact_free_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(OperationState.DENIED, None)
    monkeypatch.setattr(
        telegram,
        "find_operation",
        lambda key: _operation(OperationState.DENIED, None),
    )
    sent_texts, events = _capture_delivery(monkeypatch)

    delivery = telegram.send_action_outcome(run)

    assert delivery.sent is True
    assert sent_texts == [
        "🛡 Действие не выполнено. Проверка не подтвердила данные. "
        "Реклама не изменена."
    ]
    assert events[0].delivery_kind is DeliveryKind.FACT_FREE
    assert events[0].manifest_sha256 is None


def test_forged_action_result_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(OperationState.CONFIRMED, ActionResult.CONFIRMED)
    monkeypatch.setattr(
        telegram,
        "find_operation",
        lambda key: _operation(OperationState.UNKNOWN, ActionResult.UNKNOWN),
    )
    sent_texts, events = _capture_delivery(monkeypatch)

    delivery = telegram.send_action_outcome(run)

    assert delivery.sent is False
    assert delivery.audit_persisted is False
    assert sent_texts == []
    assert events == []


def test_fact_free_uses_closed_template_and_sanitized_error_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_texts, events = _capture_delivery(monkeypatch)

    delivery = telegram.send_fact_free(
        FactFreeTemplate.PROVIDER_UNAVAILABLE,
        channel="health",
        error_type="requests.Timeout",
    )

    assert delivery.sent is True
    assert delivery.audit_persisted is True
    assert sent_texts == [
        "⚠️ Источник данных недоступен. Тип ошибки: requests.Timeout."
    ]
    assert events[0].delivery_kind is DeliveryKind.FACT_FREE
    assert events[0].manifest_sha256 is None

    with pytest.raises(ValueError, match="sanitized"):
        telegram.send_fact_free(
            FactFreeTemplate.PROVIDER_UNAVAILABLE,
            channel="health",
            error_type="Timeout: account 123",
        )
    with pytest.raises(TypeError, match="FactFreeTemplate"):
        telegram.send_fact_free("PROVIDER_UNAVAILABLE", channel="health")  # type: ignore[arg-type]


def test_public_facade_rejects_arbitrary_text() -> None:
    with pytest.raises(TypeError, match="RenderedReport"):
        telegram.send_checked_report("Оплата 1")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ActionRun"):
        telegram.send_action_outcome("PAUSED")  # type: ignore[arg-type]
