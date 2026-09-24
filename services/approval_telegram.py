"""Проверенная доставка отчётов и результатов действий в Telegram.

Модуль не принимает произвольный текст от production-кода. Перед отправкой
он независимо сверяет typed объект с append-only approval WAL, делает ровно
одну попытку транспорта и сохраняет redacted результат доставки.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import uuid
from datetime import datetime, timezone

from services.approval_audit import (
    ApprovalAuditError,
    append_delivery_event,
    find_operation,
    find_report_check,
)
from services.approval_checker_models import (
    ActionResult,
    ActionRun,
    CheckedDelivery,
    DeliveryAuditEvent,
    DeliveryChannel,
    DeliveryKind,
    FactFreeTemplate,
    OperationRecord,
    OperationState,
    RenderedReport,
    ReportVerdict,
    TelegramButton,
    TransportErrorCode,
    canonical_json,
)
from services.notifications import send_telegram


logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z0-9_.]{1,80}$")
_CHANNELS = {item.value: item for item in DeliveryChannel}

_FACT_FREE_TEXT: dict[FactFreeTemplate, str] = {
    FactFreeTemplate.ACTION_CHECK_STARTED: "🔎 Проверяющий агент проверяет действие.",
    FactFreeTemplate.REPORT_BUILD_STARTED: "🔎 Проверяющий агент проверяет отчёт.",
    FactFreeTemplate.PROVIDER_UNAVAILABLE: "⚠️ Источник данных недоступен.",
    FactFreeTemplate.PROVIDER_AUTH_FAILED: (
        "⚠️ Не удалось подтвердить доступ к источнику данных."
    ),
    FactFreeTemplate.CRON_CRASHED: "⚠️ Фоновая проверка завершилась с ошибкой.",
    FactFreeTemplate.BACKUP_FAILED: (
        "⚠️ Резервное копирование завершилось с ошибкой."
    ),
    FactFreeTemplate.CHECKER_INTERNAL_ERROR: (
        "⚠️ Проверяющий агент временно недоступен."
    ),
    FactFreeTemplate.TRANSPORT_FAILED: "⚠️ Не удалось отправить уведомление.",
}

_ERROR_TEMPLATES = {
    FactFreeTemplate.PROVIDER_UNAVAILABLE,
    FactFreeTemplate.PROVIDER_AUTH_FAILED,
    FactFreeTemplate.CRON_CRASHED,
    FactFreeTemplate.BACKUP_FAILED,
    FactFreeTemplate.CHECKER_INTERNAL_ERROR,
    FactFreeTemplate.TRANSPORT_FAILED,
}

_TERMINAL_RESULTS = {
    OperationState.CONFIRMED: ActionResult.CONFIRMED,
    OperationState.PARTIAL: ActionResult.PARTIAL,
    OperationState.FAILED: ActionResult.FAILED,
    OperationState.UNKNOWN: ActionResult.UNKNOWN,
}


def _delivery_channel(channel: str) -> DeliveryChannel:
    if not isinstance(channel, str) or channel not in _CHANNELS:
        raise ValueError("channel должен быть ads или health")
    return _CHANNELS[channel]


def _valid_sha256(value: str) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _valid_rendered_report(report: RenderedReport) -> bool:
    if not isinstance(report.verdict, ReportVerdict):
        return False
    if not report.check_id or not _valid_sha256(report.manifest_sha256):
        return False
    if not isinstance(report.text, str) or not report.text:
        return False
    if hashlib.sha256(report.text.encode("utf-8")).hexdigest() != report.text_sha256:
        return False
    if not isinstance(report.rendered_field_ids, tuple) or any(
        not isinstance(field_id, str) or not field_id
        for field_id in report.rendered_field_ids
    ):
        return False
    if len(report.rendered_field_ids) != len(set(report.rendered_field_ids)):
        return False
    if report.verdict in {
        ReportVerdict.VERIFIED,
        ReportVerdict.VERIFIED_WITH_LIMITATIONS,
    }:
        return bool(report.rendered_field_ids)
    return not report.rendered_field_ids


def _report_has_persisted_proof(report: RenderedReport) -> bool:
    if not _valid_rendered_report(report):
        return False
    try:
        persisted = find_report_check(report.check_id)
    except (ApprovalAuditError, OSError, TypeError, ValueError) as exc:
        logger.warning(
            "Approval WAL недоступен перед Telegram report: %s",
            type(exc).__name__,
        )
        return False
    return bool(
        persisted is not None
        and persisted.check_id == report.check_id
        and persisted.manifest_sha256 == report.manifest_sha256
        and persisted.verdict is report.verdict
    )


def _report_text(report: RenderedReport) -> str:
    check_label = html.escape(report.check_id[:8])
    if report.verdict is ReportVerdict.VERIFIED:
        return f"✅ VERIFIED · check {check_label}\n{html.escape(report.text)}"
    if report.verdict is ReportVerdict.VERIFIED_WITH_LIMITATIONS:
        return (
            "⚠️ ПРОВЕРЕНО С ОГРАНИЧЕНИЯМИ · "
            f"check {check_label}\n{html.escape(report.text)}"
        )
    if report.verdict is ReportVerdict.CHECKER_UNAVAILABLE:
        return "⚠️ Отчёт не отправлен: проверяющий агент недоступен."
    return "⛔ Отчёт не отправлен: проверка данных не пройдена."


def _validate_buttons(
    report: RenderedReport,
    buttons: tuple[tuple[TelegramButton, ...], ...],
) -> bool:
    if not isinstance(buttons, tuple) or any(not isinstance(row, tuple) for row in buttons):
        raise TypeError("buttons должен быть tuple рядов TelegramButton")
    if any(not row for row in buttons):
        raise ValueError("Ряд Telegram-кнопок не может быть пустым")
    flattened = tuple(button for row in buttons for button in row)
    if any(not isinstance(button, TelegramButton) for button in flattened):
        raise TypeError("buttons должен содержать только TelegramButton")
    if flattened and report.verdict is not ReportVerdict.VERIFIED:
        return False
    if any(button.check_id != report.check_id for button in flattened):
        return False
    for button in flattened:
        if (
            not isinstance(button.text, str)
            or not button.text
            or not isinstance(button.callback_data, str)
            or not button.callback_data
        ):
            return False
        if button.action_kind is not None and (
            not button.subject_id or not button.permit_seed_id
        ):
            return False
    return True


def _buttons_sha256(
    buttons: tuple[tuple[TelegramButton, ...], ...],
) -> str | None:
    if not buttons:
        return None
    return hashlib.sha256(canonical_json(buttons)).hexdigest()


def _transport_buttons(
    buttons: tuple[tuple[TelegramButton, ...], ...],
) -> list[list[tuple[str, str]]]:
    return [
        [(button.text, button.callback_data) for button in row]
        for row in buttons
    ]


def _persist_delivery(event: DeliveryAuditEvent) -> bool:
    try:
        append_delivery_event(event)
        return True
    except (ApprovalAuditError, OSError, TypeError, ValueError) as exc:
        logger.warning("Не удалось записать delivery audit: %s", type(exc).__name__)
        return False


def _send_once(
    text: str,
    channel: DeliveryChannel,
    buttons: tuple[tuple[TelegramButton, ...], ...] = (),
) -> tuple[bool, TransportErrorCode | None]:
    try:
        if buttons:
            if channel is not DeliveryChannel.ADS:
                return False, TransportErrorCode.UNKNOWN
            # Ленивый импорт не создаёт cycle при загрузке Telegram callback handlers.
            from services.telegram_bot import send_with_buttons

            sent = send_with_buttons(text, _transport_buttons(buttons)) is True
        else:
            sent = send_telegram(text, channel=channel.value) is True
    except Exception as exc:  # transport boundary должен оставаться non-throwing
        logger.warning("Telegram transport завершился ошибкой: %s", type(exc).__name__)
        return False, TransportErrorCode.UNKNOWN
    return (True, None) if sent else (False, TransportErrorCode.UNKNOWN)


def _delivery_result(
    *,
    sent: bool,
    audit_persisted: bool,
    check_id: str,
    report_verdict: ReportVerdict | None,
    action_result: ActionResult | None,
) -> CheckedDelivery:
    # sent отражает реальный transport-вызов. После него нельзя возвращать
    # ложный False и провоцировать повтор только из-за сбоя audit append.
    if not audit_persisted:
        logger.warning("Telegram delivery не подтверждена approval WAL")
    return CheckedDelivery(
        sent=sent,
        fallback_sent=False,
        check_id=check_id,
        report_verdict=report_verdict,
        action_result=action_result,
        audit_persisted=audit_persisted,
    )


def send_checked_report(
    report: RenderedReport,
    *,
    channel: str = "ads",
    buttons: tuple[tuple[TelegramButton, ...], ...] = (),
) -> CheckedDelivery:
    """Отправляет только report, подтверждённый persisted check в WAL."""

    if not isinstance(report, RenderedReport):
        raise TypeError("send_checked_report принимает только RenderedReport")
    delivery_channel = _delivery_channel(channel)
    if buttons and delivery_channel is not DeliveryChannel.ADS:
        return CheckedDelivery(False, False, report.check_id, report.verdict, None, False)
    if not _validate_buttons(report, buttons) or not _report_has_persisted_proof(report):
        return CheckedDelivery(False, False, report.check_id, report.verdict, None, False)

    delivered_text = _report_text(report)
    sent, transport_error = _send_once(delivered_text, delivery_channel, buttons)
    now = datetime.now(timezone.utc)
    event = DeliveryAuditEvent(
        schema_version=1,
        event="delivery",
        event_id=str(uuid.uuid4()),
        written_at=now,
        delivery_id=str(uuid.uuid4()),
        delivery_kind=DeliveryKind.REPORT,
        reference_id=report.check_id,
        payload_sha256=hashlib.sha256(delivered_text.encode("utf-8")).hexdigest(),
        buttons_sha256=_buttons_sha256(buttons),
        manifest_sha256=report.manifest_sha256,
        report_verdict=report.verdict,
        action_result=None,
        channel=delivery_channel,
        sent=sent,
        fallback_sent=False,
        transport_error_code=transport_error,
        delivered_at=now,
    )
    audit_persisted = _persist_delivery(event)
    return _delivery_result(
        sent=sent,
        audit_persisted=audit_persisted,
        check_id=report.check_id,
        report_verdict=report.verdict,
        action_result=None,
    )


def _persisted_action(run: ActionRun) -> OperationRecord | None:
    try:
        persisted = find_operation(run.idempotency_key)
    except (ApprovalAuditError, OSError, TypeError, ValueError) as exc:
        logger.warning(
            "Approval WAL недоступен перед Telegram action: %s",
            type(exc).__name__,
        )
        return None
    if persisted is None:
        return None
    persisted_result = _TERMINAL_RESULTS.get(persisted.state)
    if persisted.state is OperationState.DENIED:
        persisted_result = None
    if (
        persisted.operation_id != run.operation_id
        or persisted.idempotency_key != run.idempotency_key
        or persisted.batch_manifest_id != run.batch_manifest_id
        or persisted.batch_manifest_sha256 != run.batch_manifest_sha256
        or persisted.state is not run.state
        or persisted_result is not run.result
        or persisted.reconciliation_required != run.reconciliation_required
    ):
        return None
    if persisted.state not in {*_TERMINAL_RESULTS, OperationState.DENIED}:
        return None
    return persisted


def _action_check_id(operation: OperationRecord) -> str:
    for item in reversed(operation.items):
        if item.check_id:
            return item.check_id
    return operation.operation_id


def _action_text(result: ActionResult | None) -> str:
    if result is ActionResult.CONFIRMED:
        return "✅ Действие выполнено и подтверждено повторной проверкой."
    if result is ActionResult.PARTIAL:
        return "⚠️ Результат частичный; повторно не запускаю."
    if result is ActionResult.UNKNOWN:
        return "⚠️ Результат уточняется; повторно не запускаю."
    if result is ActionResult.FAILED:
        return "🛡 Действие не выполнено. Реклама не изменена."
    return (
        "🛡 Действие не выполнено. Проверка не подтвердила данные. "
        "Реклама не изменена."
    )


def send_action_outcome(
    run: ActionRun,
    *,
    channel: str = "ads",
) -> CheckedDelivery:
    """Доставляет только terminal action outcome, совпавший с WAL."""

    if not isinstance(run, ActionRun):
        raise TypeError("send_action_outcome принимает только ActionRun")
    delivery_channel = _delivery_channel(channel)
    persisted = _persisted_action(run)
    if persisted is None:
        return CheckedDelivery(False, False, run.operation_id, None, run.result, False)

    check_id = _action_check_id(persisted)
    delivered_text = _action_text(run.result)
    sent, transport_error = _send_once(delivered_text, delivery_channel)
    now = datetime.now(timezone.utc)
    is_denied = run.state is OperationState.DENIED
    event = DeliveryAuditEvent(
        schema_version=1,
        event="delivery",
        event_id=str(uuid.uuid4()),
        written_at=now,
        delivery_id=str(uuid.uuid4()),
        delivery_kind=DeliveryKind.FACT_FREE if is_denied else DeliveryKind.ACTION,
        reference_id=run.operation_id,
        payload_sha256=hashlib.sha256(delivered_text.encode("utf-8")).hexdigest(),
        buttons_sha256=None,
        manifest_sha256=None if is_denied else run.batch_manifest_sha256,
        report_verdict=None,
        action_result=None if is_denied else run.result,
        channel=delivery_channel,
        sent=sent,
        fallback_sent=False,
        transport_error_code=transport_error,
        delivered_at=now,
    )
    audit_persisted = _persist_delivery(event)
    return _delivery_result(
        sent=sent,
        audit_persisted=audit_persisted,
        check_id=check_id,
        report_verdict=None,
        action_result=run.result,
    )


def send_fact_free(
    template: FactFreeTemplate,
    *,
    channel: str,
    error_type: str | None = None,
) -> CheckedDelivery:
    """Отправляет сообщение только из закрытого словаря без business facts."""

    if not isinstance(template, FactFreeTemplate):
        raise TypeError("send_fact_free принимает только FactFreeTemplate")
    delivery_channel = _delivery_channel(channel)
    if error_type is not None:
        if template not in _ERROR_TEMPLATES:
            raise ValueError("error_type допустим только для error template")
        if _ERROR_TYPE_RE.fullmatch(error_type) is None:
            raise ValueError("error_type должен быть sanitized class name")

    text = _FACT_FREE_TEXT[template]
    if error_type is not None:
        text = f"{text} Тип ошибки: {html.escape(error_type)}."
    sent, transport_error = _send_once(text, delivery_channel)
    now = datetime.now(timezone.utc)
    event = DeliveryAuditEvent(
        schema_version=1,
        event="delivery",
        event_id=str(uuid.uuid4()),
        written_at=now,
        delivery_id=str(uuid.uuid4()),
        delivery_kind=DeliveryKind.FACT_FREE,
        reference_id=template.value,
        payload_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        buttons_sha256=None,
        manifest_sha256=None,
        report_verdict=None,
        action_result=None,
        channel=delivery_channel,
        sent=sent,
        fallback_sent=False,
        transport_error_code=transport_error,
        delivered_at=now,
    )
    audit_persisted = _persist_delivery(event)
    return _delivery_result(
        sent=sent,
        audit_persisted=audit_persisted,
        check_id=template.value,
        report_verdict=None,
        action_result=None,
    )


def deliver_owner_proposals(
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 20,
):
    """Доставляет proposal только через durable T3 owner outbox."""

    from services.owner_delivery_outbox import deliver_owner_outbox

    return deliver_owner_outbox(worker_id=worker_id, now=now, limit=limit)


def process_owner_decisions(
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 50,
):
    """Получает и применяет только callbacks из trusted T3 ingress."""

    from services.owner_approval_telegram import (
        poll_telegram_updates,
        process_trusted_telegram_inbox,
    )

    poll_telegram_updates(now=now)
    return process_trusted_telegram_inbox(
        worker_id=worker_id,
        now=now,
        limit=limit,
    )
