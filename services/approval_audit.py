"""Crash-safe append-only журнал утверждений и выполненных действий.

Журнал является единственным источником истины для idempotency и одноразовых
permit. Каждая операция перечитывает и проверяет весь JSONL под одним flock,
поэтому память процесса не участвует в решении о возможности действия.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping

import fcntl

from services.approval_checker_models import (
    ActionAttemptAuditEvent,
    ActionBatchManifest,
    ActionHistoryRecord,
    ActionKind,
    ActionManifest,
    AssetRecoveryManifest,
    ActionPermit,
    ActionResult,
    ActionResultAuditEvent,
    ActionReview,
    SafetyDecision,
    AuditEvent,
    DeliveryAuditEvent,
    DeliveryChannel,
    DeliveryKind,
    LaunchManifest,
    OperationAttemptAttestation,
    OperationItemRecord,
    OperationRecord,
    OperationReservedAuditEvent,
    OperationState,
    PauseManifest,
    PermitIssuedAuditEvent,
    PermitRevokedAuditEvent,
    ReportCheckAuditEvent,
    ReportTemplate,
    ReportVerdict,
    ScaleManifest,
    TimeWindow,
    TransportErrorCode,
    UnpauseManifest,
    canonical_json,
    manifest_sha256,
)

_SCHEMA_VERSION = 1
_THREAD_LOCK = threading.RLock()
_SHA256_LENGTH = 64
_MACHINE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_KNOWN_EVENTS = {
    "delivery",
    "operation_reserved",
    "report_check",
    "check",
    "permit_issued",
    "permit_revoked",
    "action_attempt",
    "action_result",
    "reconciliation",
}
_COMMON_KEYS = {"schema_version", "event", "event_id", "written_at"}
_OPERATION_KEYS = {
    "operation_id",
    "idempotency_key",
    "batch_manifest_id",
    "batch_manifest_sha256",
    "subject_ids",
}
_EVENT_KEYS = {
    "delivery": _COMMON_KEYS
    | {
        "delivery_id",
        "delivery_kind",
        "reference_id",
        "payload_sha256",
        "buttons_sha256",
        "manifest_sha256",
        "report_verdict",
        "action_result",
        "channel",
        "sent",
        "fallback_sent",
        "transport_error_code",
        "delivered_at",
    },
    "operation_reserved": _COMMON_KEYS
    | _OPERATION_KEYS
    | {"correlation_id", "item_ids", "item_metadata"},
    "report_check": _COMMON_KEYS
    | {
        "correlation_id",
        "check_id",
        "report_template",
        "verdict",
        "manifest_sha256",
        "evidence_state_sha256",
        "issue_codes",
        "checked_at",
    },
    "check": _COMMON_KEYS
    | _OPERATION_KEYS
    | {
        "check_id",
        "item_id",
        "item_index",
        "action_kind",
        "item_manifest_sha256",
        "decision",
        "checked_at",
        "expires_at",
        "final_live_state_sha256",
        "issue_codes",
    },
    "permit_issued": _COMMON_KEYS
    | _OPERATION_KEYS
    | {
        "check_id",
        "permit_id",
        "item_id",
        "item_index",
        "action_kind",
        "item_manifest_sha256",
        "final_live_state_sha256",
        "issued_at",
        "expires_at",
    },
    "permit_revoked": _COMMON_KEYS
    | _OPERATION_KEYS
    | {
        "check_id",
        "permit_id",
        "item_id",
        "item_index",
        "action_kind",
        "item_manifest_sha256",
        "evidence_state_sha256",
        "revoked_at",
        "reason_code",
    },
    "action_attempt": _COMMON_KEYS
    | _OPERATION_KEYS
    | {
        "permit_id",
        "attempt_id",
        "item_id",
        "item_index",
        "action_kind",
        "item_manifest_sha256",
        "precondition_sha256",
        "attempted_at",
    },
    "action_result": _COMMON_KEYS
    | _OPERATION_KEYS
    | {
        "permit_id",
        "attempt_id",
        "item_id",
        "item_index",
        "action_kind",
        "result",
        "exact_created_ids",
        "postcondition_sha256",
        "reason_code",
        "remote_may_have_changed",
        "reconciliation_required",
        "completed_at",
    },
    "reconciliation": _COMMON_KEYS
    | _OPERATION_KEYS
    | {
        "item_id",
        "item_index",
        "prior_attempt_id",
        "final_result",
        "exact_created_ids",
        "postcondition_sha256",
        "reconciled_at",
    },
}


class ApprovalAuditError(RuntimeError):
    """Базовая fail-closed ошибка approval WAL."""

    code = "CHECKER_UNAVAILABLE"


class ApprovalAuditCorruptionError(ApprovalAuditError):
    """WAL оборван, повреждён или содержит невозможную цепочку."""


class ApprovalAuditConflictError(ApprovalAuditError):
    """Idempotency key уже связан с другим immutable manifest."""

    code = "IDEMPOTENCY_CONFLICT"


class ApprovalAuditSequenceError(ApprovalAuditError):
    """Нарушен порядок item check -> permit -> attempt -> result."""

    code = "SEQUENCE_INVALID"


class ApprovalPermitInvalidError(ApprovalAuditError):
    """Permit истёк, изменён или уже использован."""

    code = "PERMIT_INVALID"


@dataclass
class _ItemState:
    item_id: str
    item_index: int
    action_kind: ActionKind
    item_manifest_sha256: str
    subject_ids: tuple[str, ...]
    action_manifest_json: str
    action_manifest_payload_sha256: str
    check: dict[str, object] | None = None
    permit: dict[str, object] | None = None
    permit_revocation: dict[str, object] | None = None
    attempt: dict[str, object] | None = None
    result: dict[str, object] | None = None
    reconciliation: dict[str, object] | None = None


@dataclass
class _OperationState:
    operation_id: str
    idempotency_key: str
    batch_manifest_id: str
    batch_manifest_sha256: str
    correlation_id: str
    subject_ids: tuple[str, ...]
    reserved_at: datetime
    updated_at: datetime
    items: list[_ItemState]


@dataclass
class _WalState:
    operations: dict[str, _OperationState]
    by_idempotency: dict[str, str]
    by_batch_id: dict[str, str]
    by_batch_sha256: dict[str, str]
    event_ids: set[str]
    check_ids: set[str]
    permit_ids: set[str]
    attempt_ids: set[str]
    report_checks: dict[str, dict[str, object]]
    deliveries: dict[str, dict[str, object]]
    events_by_id: dict[str, dict[str, object]]


def _default_path() -> Path:
    # Импорт ленивый: contracts и unit-тесты не должны зависеть от загрузки config.
    from config import REPORT_CHECKER_AUDIT_PATH

    return Path(REPORT_CHECKER_AUDIT_PATH)


def _path(path: Path | None) -> Path:
    return Path(path) if path is not None else _default_path()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} должен содержать timezone")


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ApprovalAuditCorruptionError(f"{field_name}: ожидалась ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApprovalAuditCorruptionError(f"{field_name}: неверная ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApprovalAuditCorruptionError(f"{field_name}: timezone обязателен")
    return parsed


def _require_uuid(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ApprovalAuditCorruptionError(f"{field_name}: ожидался UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ApprovalAuditCorruptionError(f"{field_name}: неверный UUID") from exc
    if str(parsed) != value.lower():
        raise ApprovalAuditCorruptionError(f"{field_name}: UUID не canonical")
    return value


def _require_sha256(value: object, field_name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ApprovalAuditCorruptionError(f"{field_name}: неверный lowercase SHA-256")
    return value


def _strings(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ApprovalAuditCorruptionError(f"{field_name}: ожидался список непустых IDs")
    return tuple(value)


def _machine_code(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _MACHINE_CODE_RE.fullmatch(value) is None:
        raise ApprovalAuditCorruptionError(f"{field_name}: ожидался machine-readable code")
    return value


def _enum(enum_type: type[Enum], value: object, field_name: str) -> Enum:
    try:
        return enum_type(value)
    except (ValueError, TypeError) as exc:
        raise ApprovalAuditCorruptionError(f"{field_name}: неизвестное значение") from exc


def _empty_state() -> _WalState:
    return _WalState({}, {}, {}, {}, set(), set(), set(), set(), {}, {}, {})


@contextmanager
def _locked_wal(path: Path) -> Iterator[int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_APPEND | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    with _THREAD_LOCK:
        fd = os.open(path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _append_payload(fd: int, payload: Mapping[str, object]) -> None:
    line = canonical_json(payload) + b"\n"
    written = 0
    while written < len(line):
        count = os.write(fd, line[written:])
        if count <= 0:
            raise OSError("approval WAL: os.write не записал данные")
        written += count
    os.fsync(fd)


def _decode_lines(raw: bytes) -> list[dict[str, object]]:
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise ApprovalAuditCorruptionError("approval WAL содержит оборванную последнюю строку")
    events: list[dict[str, object]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        try:
            decoded = line.decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApprovalAuditCorruptionError(
                f"approval WAL строка {line_number} повреждена"
            ) from exc
        if not isinstance(payload, dict):
            raise ApprovalAuditCorruptionError(
                f"approval WAL строка {line_number}: ожидался JSON object"
            )
        events.append(payload)
    return events


def _subject_for_action(action: ActionManifest) -> tuple[str, ...]:
    if isinstance(action, LaunchManifest):
        return (f"card:{action.trello.card_id}",)
    if isinstance(action, (PauseManifest, UnpauseManifest)):
        return (f"ad:{action.ad_id}",)
    if isinstance(action, AssetRecoveryManifest):
        return (f"adset:{action.target_adset_id}",)
    if isinstance(action, ScaleManifest):
        return (f"adset:{action.adset_id}",)
    raise TypeError(f"Неподдерживаемый action manifest: {type(action).__name__}")


def _validate_common(payload: dict[str, object], state: _WalState) -> tuple[str, datetime]:
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ApprovalAuditCorruptionError("approval WAL: неизвестная schema_version")
    event = payload.get("event")
    if event not in _KNOWN_EVENTS:
        raise ApprovalAuditCorruptionError("approval WAL: неизвестный event")
    if set(payload) != _EVENT_KEYS[event]:  # type: ignore[index]
        raise ApprovalAuditCorruptionError(
            "approval WAL: набор полей события не совпадает со схемой"
        )
    event_id = (
        _machine_code(payload.get("event_id"), "event_id")
        if event in {"report_check", "delivery"}
        else _require_uuid(payload.get("event_id"), "event_id")
    )
    if event_id in state.event_ids:
        raise ApprovalAuditCorruptionError("approval WAL: duplicate event_id")
    written_at = _parse_datetime(payload.get("written_at"), "written_at")
    return event_id, written_at


def _require_operation(payload: dict[str, object], state: _WalState) -> _OperationState:
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or operation_id not in state.operations:
        raise ApprovalAuditSequenceError("event ссылается на неизвестную operation")
    operation = state.operations[operation_id]
    expected = (
        ("idempotency_key", operation.idempotency_key),
        ("batch_manifest_id", operation.batch_manifest_id),
        ("batch_manifest_sha256", operation.batch_manifest_sha256),
    )
    for field_name, value in expected:
        if payload.get(field_name) != value:
            raise ApprovalAuditSequenceError(f"{field_name} не совпадает с operation")
    return operation


def _apply_report_check(
    payload: dict[str, object], state: _WalState, written_at: datetime
) -> None:
    _machine_code(payload.get("correlation_id"), "correlation_id")
    check_id = _machine_code(payload.get("check_id"), "check_id")
    if check_id in state.report_checks:
        raise ApprovalAuditCorruptionError("approval WAL: duplicate report check_id")
    _enum(ReportTemplate, payload.get("report_template"), "report_template")
    _enum(ReportVerdict, payload.get("verdict"), "verdict")
    _require_sha256(payload.get("manifest_sha256"), "manifest_sha256")
    _require_sha256(
        payload.get("evidence_state_sha256"),
        "evidence_state_sha256",
        optional=True,
    )
    issue_codes = _strings(payload.get("issue_codes"), "issue_codes")
    if issue_codes != tuple(sorted(set(issue_codes))):
        raise ApprovalAuditCorruptionError(
            "report_check.issue_codes должны быть sorted и unique"
        )
    for issue_code in issue_codes:
        _machine_code(issue_code, "issue_code")
    checked_at = _parse_datetime(payload.get("checked_at"), "checked_at")
    if written_at < checked_at:
        raise ApprovalAuditSequenceError("report_check записан до checked_at")
    state.report_checks[check_id] = payload


def _operation_action_result(operation: _OperationState) -> ActionResult | None:
    operation_record = _operation_record(operation)
    mapping = {
        OperationState.CONFIRMED: ActionResult.CONFIRMED,
        OperationState.PARTIAL: ActionResult.PARTIAL,
        OperationState.FAILED: ActionResult.FAILED,
        OperationState.UNKNOWN: ActionResult.UNKNOWN,
    }
    return mapping.get(operation_record.state)


def _apply_delivery(
    payload: dict[str, object], state: _WalState, written_at: datetime
) -> None:
    delivery_id = _machine_code(payload.get("delivery_id"), "delivery_id")
    if delivery_id in state.deliveries:
        raise ApprovalAuditCorruptionError("approval WAL: duplicate delivery_id")
    delivery_kind = _enum(DeliveryKind, payload.get("delivery_kind"), "delivery_kind")
    reference_id = _machine_code(payload.get("reference_id"), "reference_id")
    _require_sha256(payload.get("payload_sha256"), "payload_sha256")
    buttons_sha256 = _require_sha256(
        payload.get("buttons_sha256"), "buttons_sha256", optional=True
    )
    manifest_sha256_value = _require_sha256(
        payload.get("manifest_sha256"), "manifest_sha256", optional=True
    )
    report_verdict_raw = payload.get("report_verdict")
    report_verdict = (
        None
        if report_verdict_raw is None
        else _enum(ReportVerdict, report_verdict_raw, "report_verdict")
    )
    action_result_raw = payload.get("action_result")
    action_result = (
        None
        if action_result_raw is None
        else _enum(ActionResult, action_result_raw, "action_result")
    )
    _enum(DeliveryChannel, payload.get("channel"), "channel")
    sent = payload.get("sent")
    fallback_sent = payload.get("fallback_sent")
    if not isinstance(sent, bool) or not isinstance(fallback_sent, bool):
        raise ApprovalAuditCorruptionError("delivery sent flags должны быть bool")
    if sent and fallback_sent:
        raise ApprovalAuditSequenceError(
            "primary и fallback delivery не могут быть успешны одновременно"
        )
    transport_error_raw = payload.get("transport_error_code")
    if transport_error_raw is not None:
        _enum(TransportErrorCode, transport_error_raw, "transport_error_code")
    if not sent and not fallback_sent and transport_error_raw is None:
        raise ApprovalAuditSequenceError(
            "неуспешная delivery требует sanitized transport error code"
        )
    if sent and transport_error_raw is not None:
        raise ApprovalAuditSequenceError(
            "успешная primary delivery не должна иметь transport error"
        )
    delivered_at = _parse_datetime(payload.get("delivered_at"), "delivered_at")
    if written_at < delivered_at:
        raise ApprovalAuditSequenceError("delivery записана до delivered_at")

    if delivery_kind is DeliveryKind.REPORT:
        if report_verdict is None or action_result is not None or manifest_sha256_value is None:
            raise ApprovalAuditSequenceError(
                "REPORT delivery требует только report verdict и manifest"
            )
        report_check = state.report_checks.get(reference_id)
        if report_check is None:
            raise ApprovalAuditSequenceError(
                "REPORT delivery не имеет persisted report check proof"
            )
        if (
            report_check.get("manifest_sha256") != manifest_sha256_value
            or report_check.get("verdict") != report_verdict.value
        ):
            raise ApprovalAuditSequenceError(
                "REPORT delivery не совпадает с persisted report check"
            )
    elif delivery_kind is DeliveryKind.ACTION:
        if action_result is None or report_verdict is not None or manifest_sha256_value is None:
            raise ApprovalAuditSequenceError(
                "ACTION delivery требует только action result и manifest"
            )
        operation_id = state.by_idempotency.get(reference_id, reference_id)
        operation = state.operations.get(operation_id)
        if operation is None:
            raise ApprovalAuditSequenceError(
                "ACTION delivery не имеет persisted operation proof"
            )
        if (
            operation.batch_manifest_sha256 != manifest_sha256_value
            or _operation_action_result(operation) is not action_result
        ):
            raise ApprovalAuditSequenceError(
                "ACTION delivery не совпадает с persisted operation result"
            )
        if buttons_sha256 is not None:
            raise ApprovalAuditSequenceError(
                "ACTION delivery не может содержать buttons digest"
            )
    elif (
        report_verdict is not None
        or action_result is not None
        or manifest_sha256_value is not None
        or buttons_sha256 is not None
    ):
        raise ApprovalAuditSequenceError(
            "FACT_FREE delivery не может нести business proof"
        )
    state.deliveries[delivery_id] = payload


def _require_item(payload: dict[str, object], operation: _OperationState) -> _ItemState:
    item_index = payload.get("item_index")
    if isinstance(item_index, bool) or not isinstance(item_index, int):
        raise ApprovalAuditSequenceError("item_index должен быть int")
    if item_index < 0 or item_index >= len(operation.items):
        raise ApprovalAuditSequenceError("item_index вне batch")
    item = operation.items[item_index]
    expected = (
        ("item_id", item.item_id),
        ("action_kind", item.action_kind.value),
    )
    for field_name, value in expected:
        if payload.get(field_name) != value:
            raise ApprovalAuditSequenceError(f"{field_name} не совпадает с reserved item")
    if (
        "item_manifest_sha256" in payload
        and payload.get("item_manifest_sha256") != item.item_manifest_sha256
    ):
        raise ApprovalAuditSequenceError("item_manifest_sha256 не совпадает с reserved item")
    if _strings(payload.get("subject_ids"), "subject_ids") != item.subject_ids:
        raise ApprovalAuditSequenceError("subject_ids не совпадают с reserved item")
    return item


def _apply_reserved(payload: dict[str, object], state: _WalState, written_at: datetime) -> None:
    operation_id = _require_uuid(payload.get("operation_id"), "operation_id")
    idempotency_key = _require_uuid(payload.get("idempotency_key"), "idempotency_key")
    batch_id = payload.get("batch_manifest_id")
    correlation_id = payload.get("correlation_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise ApprovalAuditCorruptionError("batch_manifest_id пуст")
    if not isinstance(correlation_id, str) or not correlation_id:
        raise ApprovalAuditCorruptionError("correlation_id пуст")
    batch_sha = _require_sha256(payload.get("batch_manifest_sha256"), "batch_manifest_sha256")
    subject_ids = _strings(payload.get("subject_ids"), "subject_ids")
    item_ids = _strings(payload.get("item_ids"), "item_ids")
    if len(item_ids) != len(set(item_ids)):
        raise ApprovalAuditCorruptionError("operation_reserved содержит duplicate item_id")
    metadata = payload.get("item_metadata")
    if not isinstance(metadata, list) or len(metadata) != len(item_ids):
        raise ApprovalAuditCorruptionError("operation_reserved.item_metadata неполон")
    if operation_id in state.operations:
        raise ApprovalAuditCorruptionError("duplicate operation_id")
    for index_name, index, key in (
        ("idempotency_key", state.by_idempotency, idempotency_key),
        ("batch_manifest_id", state.by_batch_id, batch_id),
        ("batch_manifest_sha256", state.by_batch_sha256, batch_sha),
    ):
        if key in index:
            raise ApprovalAuditCorruptionError(f"duplicate {index_name}")
    items: list[_ItemState] = []
    for item_index, raw_item in enumerate(metadata):
        if not isinstance(raw_item, dict):
            raise ApprovalAuditCorruptionError("item_metadata должен содержать objects")
        if raw_item.get("item_id") != item_ids[item_index] or raw_item.get("item_index") != item_index:
            raise ApprovalAuditCorruptionError("item_metadata нарушает порядок batch")
        action_kind = _enum(ActionKind, raw_item.get("action_kind"), "action_kind")
        item_sha = _require_sha256(raw_item.get("item_manifest_sha256"), "item_manifest_sha256")
        item_subjects = _strings(raw_item.get("subject_ids"), "item subject_ids")
        action_manifest_payload = raw_item.get("action_manifest_payload")
        if not isinstance(action_manifest_payload, dict):
            raise ApprovalAuditCorruptionError(
                "item_metadata.action_manifest_payload должен быть object"
            )
        action_manifest_json = canonical_json(action_manifest_payload).decode("utf-8")
        action_manifest_payload_sha256 = _require_sha256(
            raw_item.get("action_manifest_payload_sha256"),
            "action_manifest_payload_sha256",
        )
        if hashlib.sha256(action_manifest_json.encode("utf-8")).hexdigest() != action_manifest_payload_sha256:
            raise ApprovalAuditCorruptionError("item manifest payload digest неверен")
        if action_manifest_payload_sha256 != item_sha:
            raise ApprovalAuditCorruptionError(
                "item manifest payload не совпадает с item_manifest_sha256"
            )
        items.append(
            _ItemState(
                item_id=item_ids[item_index],
                item_index=item_index,
                action_kind=action_kind,  # type: ignore[arg-type]
                item_manifest_sha256=item_sha,  # type: ignore[arg-type]
                subject_ids=item_subjects,
                action_manifest_json=action_manifest_json,
                action_manifest_payload_sha256=action_manifest_payload_sha256,
            )
        )
    operation = _OperationState(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        batch_manifest_id=batch_id,
        batch_manifest_sha256=batch_sha,  # type: ignore[arg-type]
        correlation_id=correlation_id,
        subject_ids=subject_ids,
        reserved_at=written_at,
        updated_at=written_at,
        items=items,
    )
    state.operations[operation_id] = operation
    state.by_idempotency[idempotency_key] = operation_id
    state.by_batch_id[batch_id] = operation_id
    state.by_batch_sha256[batch_sha] = operation_id  # type: ignore[index]


def _apply_check(
    payload: dict[str, object], operation: _OperationState, state: _WalState
) -> None:
    item = _require_item(payload, operation)
    if item.check is not None:
        raise ApprovalAuditSequenceError("для item уже существует check")
    if any(previous.result is None or _effective_result(previous) is not ActionResult.CONFIRMED for previous in operation.items[: item.item_index]):
        raise ApprovalAuditSequenceError("предыдущий item ещё не CONFIRMED")
    if any(later.check is not None for later in operation.items[item.item_index + 1 :]):
        raise ApprovalAuditSequenceError("более поздний item уже проверен")
    check_id = _require_uuid(payload.get("check_id"), "check_id")
    if check_id in state.check_ids:
        raise ApprovalAuditCorruptionError("duplicate check_id")
    decision = _enum(SafetyDecision, payload.get("decision"), "decision")
    checked_at = _parse_datetime(payload.get("checked_at"), "checked_at")
    if checked_at < operation.reserved_at:
        raise ApprovalAuditSequenceError("check создан до reservation")
    expires_at_raw = payload.get("expires_at")
    expires_at = None if expires_at_raw is None else _parse_datetime(expires_at_raw, "expires_at")
    final_sha = _require_sha256(
        payload.get("final_live_state_sha256"),
        "final_live_state_sha256",
        optional=True,
    )
    issue_codes = _strings(payload.get("issue_codes"), "issue_codes")
    for issue_code in issue_codes:
        _machine_code(issue_code, "issue_code")
    if decision is SafetyDecision.SAFE:
        if expires_at is None or expires_at <= checked_at or final_sha is None or issue_codes:
            raise ApprovalAuditSequenceError("SAFE check требует expiry и live digest")
    # Старый append-only WAL мог хранить строку APPROVED. В памяти она всегда
    # нормализуется в технический SAFE, сам файл при этом не переписывается.
    payload = dict(payload)
    payload["decision"] = decision.value
    item.check = payload
    state.check_ids.add(check_id)


def _apply_permit(
    payload: dict[str, object], operation: _OperationState, state: _WalState
) -> None:
    item = _require_item(payload, operation)
    if item.check is None:
        raise ApprovalAuditSequenceError("permit без check")
    if item.permit is not None:
        raise ApprovalAuditSequenceError("для item уже существует permit")
    if item.check.get("decision") != SafetyDecision.SAFE.value:
        raise ApprovalAuditSequenceError("permit разрешён только после SAFE")
    if payload.get("check_id") != item.check.get("check_id"):
        raise ApprovalAuditSequenceError("permit ссылается на другой check")
    if payload.get("final_live_state_sha256") != item.check.get("final_live_state_sha256"):
        raise ApprovalAuditSequenceError("permit изменил live digest")
    permit_id = _require_uuid(payload.get("permit_id"), "permit_id")
    if permit_id in state.permit_ids:
        raise ApprovalAuditCorruptionError("duplicate permit_id")
    issued_at = _parse_datetime(payload.get("issued_at"), "issued_at")
    expires_at = _parse_datetime(payload.get("expires_at"), "expires_at")
    checked_at = _parse_datetime(item.check.get("checked_at"), "checked_at")
    check_expires_at = _parse_datetime(item.check.get("expires_at"), "expires_at")
    if issued_at < checked_at or expires_at != check_expires_at or expires_at <= issued_at:
        raise ApprovalAuditSequenceError("permit уже истёк при выдаче")
    if any(_effective_result(previous) is not ActionResult.CONFIRMED for previous in operation.items[: item.item_index]):
        raise ApprovalAuditSequenceError("permit выдан до CONFIRMED predecessor")
    if any(later.check is not None or later.permit is not None for later in operation.items[item.item_index + 1 :]):
        raise ApprovalAuditSequenceError("permit выдан после ранней проверки successor")
    item.permit = payload
    state.permit_ids.add(permit_id)


def _apply_permit_revocation(
    payload: dict[str, object], operation: _OperationState
) -> None:
    item = _require_item(payload, operation)
    if item.permit is None:
        raise ApprovalAuditSequenceError("permit_revoked без выданного permit")
    if item.attempt is not None:
        raise ApprovalPermitInvalidError("использованный permit нельзя отозвать")
    if item.permit_revocation is not None:
        raise ApprovalPermitInvalidError("permit уже отозван")
    permit = item.permit
    expected = (
        ("check_id", permit.get("check_id")),
        ("permit_id", permit.get("permit_id")),
        ("item_id", permit.get("item_id")),
        ("item_index", permit.get("item_index")),
        ("action_kind", permit.get("action_kind")),
        ("item_manifest_sha256", permit.get("item_manifest_sha256")),
        ("evidence_state_sha256", permit.get("final_live_state_sha256")),
    )
    for field_name, expected_value in expected:
        if payload.get(field_name) != expected_value:
            raise ApprovalPermitInvalidError(
                f"permit_revoked.{field_name} не совпадает с issued permit"
            )
    revoked_at = _parse_datetime(payload.get("revoked_at"), "revoked_at")
    issued_at = _parse_datetime(permit.get("issued_at"), "issued_at")
    if revoked_at < issued_at:
        raise ApprovalAuditSequenceError("permit отозван до его выдачи")
    _machine_code(payload.get("reason_code"), "reason_code")
    item.permit_revocation = payload


def _apply_attempt(
    payload: dict[str, object], operation: _OperationState, state: _WalState
) -> None:
    item = _require_item(payload, operation)
    if item.permit is None:
        raise ApprovalAuditSequenceError("attempt без permit")
    if item.permit_revocation is not None:
        raise ApprovalPermitInvalidError("отозванный permit нельзя использовать")
    if item.attempt is not None:
        raise ApprovalPermitInvalidError("permit уже использован")
    if payload.get("permit_id") != item.permit.get("permit_id"):
        raise ApprovalPermitInvalidError("attempt ссылается на другой permit")
    attempt_id = _require_uuid(payload.get("attempt_id"), "attempt_id")
    if attempt_id in state.attempt_ids:
        raise ApprovalAuditCorruptionError("duplicate attempt_id")
    _require_sha256(payload.get("precondition_sha256"), "precondition_sha256")
    attempted_at = _parse_datetime(payload.get("attempted_at"), "attempted_at")
    expires_at = _parse_datetime(item.permit.get("expires_at"), "expires_at")
    issued_at = _parse_datetime(item.permit.get("issued_at"), "issued_at")
    if attempted_at < issued_at or attempted_at >= expires_at:
        raise ApprovalPermitInvalidError("permit использован вне срока")
    if payload.get("precondition_sha256") != item.permit.get("final_live_state_sha256"):
        raise ApprovalPermitInvalidError("precondition не совпадает с approved live state")
    item.attempt = payload
    state.attempt_ids.add(attempt_id)


def _apply_result(payload: dict[str, object], operation: _OperationState) -> None:
    item = _require_item(payload, operation)
    if item.attempt is None:
        raise ApprovalAuditSequenceError("result без attempt")
    if item.result is not None:
        raise ApprovalAuditSequenceError("для attempt уже существует result")
    if payload.get("permit_id") != item.attempt.get("permit_id"):
        raise ApprovalAuditSequenceError("result ссылается на другой permit")
    if payload.get("attempt_id") != item.attempt.get("attempt_id"):
        raise ApprovalAuditSequenceError("result ссылается на другой attempt")
    result = _enum(ActionResult, payload.get("result"), "result")
    _strings(payload.get("exact_created_ids"), "exact_created_ids")
    postcondition_sha256 = _require_sha256(
        payload.get("postcondition_sha256"), "postcondition_sha256", optional=True
    )
    completed_at = _parse_datetime(payload.get("completed_at"), "completed_at")
    attempted_at = _parse_datetime(item.attempt.get("attempted_at"), "attempted_at")
    if completed_at < attempted_at:
        raise ApprovalAuditSequenceError("result завершён до attempt")
    _machine_code(payload.get("reason_code"), "reason_code")
    if not isinstance(payload.get("remote_may_have_changed"), bool) or not isinstance(
        payload.get("reconciliation_required"), bool
    ):
        raise ApprovalAuditCorruptionError("result boolean flags повреждены")
    if result is ActionResult.CONFIRMED and (
        postcondition_sha256 is None or payload["reconciliation_required"]
    ):
        raise ApprovalAuditSequenceError(
            "CONFIRMED result требует postcondition и не требует reconciliation"
        )
    if result in {ActionResult.UNKNOWN, ActionResult.PARTIAL} and not payload[
        "reconciliation_required"
    ]:
        raise ApprovalAuditSequenceError(
            "UNKNOWN/PARTIAL result должен требовать reconciliation"
        )
    item.result = payload


def _apply_reconciliation(payload: dict[str, object], operation: _OperationState) -> None:
    item_index = payload.get("item_index")
    if isinstance(item_index, bool) or not isinstance(item_index, int) or not (0 <= item_index < len(operation.items)):
        raise ApprovalAuditSequenceError("reconciliation item_index вне batch")
    item = operation.items[item_index]
    if payload.get("item_id") != item.item_id or item.attempt is None:
        raise ApprovalAuditSequenceError("reconciliation не совпадает с attempted item")
    if _strings(payload.get("subject_ids"), "subject_ids") != item.subject_ids:
        raise ApprovalAuditSequenceError("reconciliation subject_ids не совпадают с item")
    if payload.get("prior_attempt_id") != item.attempt.get("attempt_id"):
        raise ApprovalAuditSequenceError("reconciliation ссылается на другой attempt")
    if item.reconciliation is not None:
        raise ApprovalAuditSequenceError("attempt уже reconciled")
    if item.result is not None and item.result.get("result") not in {
        ActionResult.UNKNOWN.value,
        ActionResult.PARTIAL.value,
    }:
        raise ApprovalAuditSequenceError("terminal result нельзя переписать reconciliation")
    _enum(ActionResult, payload.get("final_result"), "final_result")
    _strings(payload.get("exact_created_ids"), "exact_created_ids")
    _require_sha256(payload.get("postcondition_sha256"), "postcondition_sha256")
    reconciled_at = _parse_datetime(payload.get("reconciled_at"), "reconciled_at")
    attempted_at = _parse_datetime(item.attempt.get("attempted_at"), "attempted_at")
    if reconciled_at < attempted_at:
        raise ApprovalAuditSequenceError("reconciliation завершён до attempt")
    item.reconciliation = payload


def _apply_payload(payload: dict[str, object], state: _WalState) -> None:
    event_id, written_at = _validate_common(payload, state)
    event = payload["event"]
    if event == "operation_reserved":
        _apply_reserved(payload, state, written_at)
    elif event == "report_check":
        _apply_report_check(payload, state, written_at)
    elif event == "delivery":
        _apply_delivery(payload, state, written_at)
    else:
        operation = _require_operation(payload, state)
        if event == "check":
            _apply_check(payload, operation, state)
        elif event == "permit_issued":
            _apply_permit(payload, operation, state)
        elif event == "permit_revoked":
            _apply_permit_revocation(payload, operation)
        elif event == "action_attempt":
            _apply_attempt(payload, operation, state)
        elif event == "action_result":
            _apply_result(payload, operation)
        elif event == "reconciliation":
            _apply_reconciliation(payload, operation)
        operation.updated_at = max(operation.updated_at, written_at)
    state.event_ids.add(event_id)
    state.events_by_id[event_id] = payload


def _scan(fd: int) -> _WalState:
    state = _empty_state()
    for payload in _decode_lines(_read_all(fd)):
        try:
            _apply_payload(payload, state)
        except ApprovalAuditCorruptionError:
            raise
        except ApprovalAuditError as exc:
            raise ApprovalAuditCorruptionError(
                f"approval WAL содержит невозможную цепочку: {exc}"
            ) from exc
    return state


def _event_payload(event: AuditEvent) -> dict[str, object]:
    payload = json.loads(canonical_json(asdict(event)).decode("utf-8"))
    if not isinstance(payload, dict):  # pragma: no cover - dataclass всегда object
        raise TypeError("AuditEvent должен сериализоваться в object")
    return payload


def _effective_result(item: _ItemState) -> ActionResult | None:
    if item.reconciliation is not None:
        return ActionResult(item.reconciliation["final_result"])
    if item.result is not None:
        return ActionResult(item.result["result"])
    if item.attempt is not None:
        return ActionResult.UNKNOWN
    return None


def _operation_record(operation: _OperationState) -> OperationRecord:
    records: list[OperationItemRecord] = []
    for item in operation.items:
        check = item.check
        attempt = item.attempt
        result_payload = item.reconciliation or item.result
        result = _effective_result(item)
        reconciliation_required = item.attempt is not None and (
            result is ActionResult.UNKNOWN
            or (
                item.result is not None
                and bool(item.result.get("reconciliation_required"))
                and item.reconciliation is None
            )
        )
        records.append(
            OperationItemRecord(
                item_id=item.item_id,
                item_index=item.item_index,
                action_kind=item.action_kind,
                item_manifest_sha256=item.item_manifest_sha256,
                subject_ids=item.subject_ids,
                check_id=None if check is None else str(check["check_id"]),
                decision=None if check is None else SafetyDecision(check["decision"]),
                evidence_state_sha256=None if check is None else check.get("final_live_state_sha256"),  # type: ignore[arg-type]
                shadow_evaluation=None,
                permit_id=None if item.permit is None else str(item.permit["permit_id"]),
                attempt_id=None if attempt is None else str(attempt["attempt_id"]),
                result=result,
                exact_created_ids=()
                if result_payload is None
                else tuple(result_payload.get("exact_created_ids", [])),  # type: ignore[arg-type]
                attempted_at=None
                if attempt is None
                else _parse_datetime(attempt["attempted_at"], "attempted_at"),
                completed_at=None
                if result_payload is None
                else _parse_datetime(
                    result_payload.get("reconciled_at", result_payload.get("completed_at")),
                    "completed_at",
                ),
                reconciliation_required=reconciliation_required,
                permit_revoked_at=None
                if item.permit_revocation is None
                else _parse_datetime(
                    item.permit_revocation["revoked_at"], "revoked_at"
                ),
                permit_revocation_reason_code=None
                if item.permit_revocation is None
                else str(item.permit_revocation["reason_code"]),
                action_manifest_json=item.action_manifest_json,
                action_manifest_payload_sha256=item.action_manifest_payload_sha256,
            )
        )
    results = [_effective_result(item) for item in operation.items]
    decisions = [None if item.check is None else item.check.get("decision") for item in operation.items]
    if any(item.permit_revocation is not None for item in operation.items):
        state = OperationState.DENIED
    elif any(decision == SafetyDecision.DENIED.value for decision in decisions):
        state = OperationState.DENIED
    elif any(item.attempt is not None and _effective_result(item) is ActionResult.UNKNOWN for item in operation.items):
        state = OperationState.PARTIAL if ActionResult.CONFIRMED in results else OperationState.UNKNOWN
    elif results and all(result is ActionResult.CONFIRMED for result in results):
        state = OperationState.CONFIRMED
    elif any(result is ActionResult.CONFIRMED for result in results):
        state = OperationState.PARTIAL
    elif any(result is ActionResult.PARTIAL for result in results):
        state = OperationState.PARTIAL
    elif any(result is ActionResult.FAILED for result in results):
        state = OperationState.FAILED
    elif any(item.attempt is not None for item in operation.items):
        state = OperationState.EXECUTING
    elif any(item.permit is not None or decision == SafetyDecision.SAFE.value for item, decision in zip(operation.items, decisions, strict=True)):
        state = OperationState.APPROVED
    else:
        state = OperationState.RESERVED
    return OperationRecord(
        operation_id=operation.operation_id,
        idempotency_key=operation.idempotency_key,
        batch_manifest_id=operation.batch_manifest_id,
        batch_manifest_sha256=operation.batch_manifest_sha256,
        correlation_id=operation.correlation_id,
        subject_ids=operation.subject_ids,
        state=state,
        items=tuple(records),
        reserved_at=operation.reserved_at,
        updated_at=operation.updated_at,
        reconciliation_required=any(item.reconciliation_required for item in records),
    )


def append_report_check(
    event: ReportCheckAuditEvent,
    path: Path | None = None,
) -> str:
    """Idempotent append report verdict без изменения action operation state."""

    payload = _event_payload(event)
    target = _path(path)
    with _locked_wal(target) as fd:
        state = _scan(fd)
        existing_event = state.events_by_id.get(event.event_id)
        if existing_event is not None:
            if existing_event == payload:
                return event.event_id
            raise ApprovalAuditConflictError(
                "report audit event_id уже связан с другим verdict"
            )
        existing_check = state.report_checks.get(event.check_id)
        if existing_check is not None:
            if existing_check == payload:
                return event.event_id
            raise ApprovalAuditConflictError(
                "report check_id уже связан с другим verdict"
            )
        _apply_payload(payload, state)
        _append_payload(fd, payload)
        return event.event_id


def _report_check_from_payload(
    payload: Mapping[str, object],
) -> ReportCheckAuditEvent:
    return ReportCheckAuditEvent(
        schema_version=_SCHEMA_VERSION,
        event="report_check",
        event_id=str(payload["event_id"]),
        written_at=_parse_datetime(payload["written_at"], "written_at"),
        correlation_id=str(payload["correlation_id"]),
        check_id=str(payload["check_id"]),
        report_template=ReportTemplate(payload["report_template"]),
        verdict=ReportVerdict(payload["verdict"]),
        manifest_sha256=str(payload["manifest_sha256"]),
        evidence_state_sha256=None
        if payload["evidence_state_sha256"] is None
        else str(payload["evidence_state_sha256"]),
        issue_codes=tuple(payload["issue_codes"]),  # type: ignore[arg-type]
        checked_at=_parse_datetime(payload["checked_at"], "checked_at"),
    )


def find_report_check(
    check_id: str,
    path: Path | None = None,
) -> ReportCheckAuditEvent | None:
    """Возвращает только redacted typed proof сохранённой проверки отчёта."""

    _machine_code(check_id, "check_id")
    target = _path(path)
    if not target.exists():
        return None
    with _locked_wal(target) as fd:
        state = _scan(fd)
        payload = state.report_checks.get(check_id)
        return None if payload is None else _report_check_from_payload(payload)


def append_delivery_event(
    event: DeliveryAuditEvent,
    path: Path | None = None,
) -> str:
    """Idempotent append redacted delivery result под тем же WAL lock."""

    payload = _event_payload(event)
    target = _path(path)
    with _locked_wal(target) as fd:
        state = _scan(fd)
        existing_event = state.events_by_id.get(event.event_id)
        if existing_event is not None:
            if existing_event == payload:
                return event.event_id
            raise ApprovalAuditConflictError(
                "delivery event_id уже связан с другим result"
            )
        existing_delivery = state.deliveries.get(event.delivery_id)
        if existing_delivery is not None:
            if existing_delivery == payload:
                return event.event_id
            raise ApprovalAuditConflictError(
                "delivery_id уже связан с другим result"
            )
        _apply_payload(payload, state)
        _append_payload(fd, payload)
        return event.event_id


def append_event(event: AuditEvent, path: Path | None = None) -> str:
    """Атомарно проверяет цепочку, дописывает событие и fsync-ит WAL."""

    if isinstance(event, OperationReservedAuditEvent):
        raise ApprovalAuditSequenceError("operation_reserved записывается только reserve_operation")
    if isinstance(event, ReportCheckAuditEvent):
        return append_report_check(event, path)
    if isinstance(event, DeliveryAuditEvent):
        return append_delivery_event(event, path)
    payload = _event_payload(event)
    target = _path(path)
    with _locked_wal(target) as fd:
        state = _scan(fd)
        _apply_payload(payload, state)
        _append_payload(fd, payload)
    return event.event_id


def reserve_operation(
    manifest: ActionBatchManifest,
    now: datetime,
    path: Path | None = None,
) -> OperationRecord:
    """Резервирует immutable batch либо возвращает его idempotent replay."""

    _require_aware(now, "now")
    actual_sha = manifest_sha256(manifest)
    if manifest.manifest_sha256 != actual_sha:
        raise ApprovalAuditConflictError("batch manifest_sha256 не совпадает с содержимым")
    target = _path(path)
    with _locked_wal(target) as fd:
        state = _scan(fd)
        existing_id = state.by_idempotency.get(manifest.idempotency_key)
        if existing_id is not None:
            existing = state.operations[existing_id]
            if existing.batch_manifest_sha256 != manifest.manifest_sha256:
                raise ApprovalAuditConflictError("idempotency key уже связан с другим manifest")
            return _operation_record(existing)
        for index, key in (
            (state.by_batch_id, manifest.batch_manifest_id),
            (state.by_batch_sha256, manifest.manifest_sha256),
        ):
            if key in index:
                raise ApprovalAuditConflictError("batch уже зарезервирован с другим idempotency key")
        operation_id = str(uuid.uuid4())
        reserved = OperationReservedAuditEvent(
            schema_version=_SCHEMA_VERSION,
            event="operation_reserved",
            event_id=str(uuid.uuid4()),
            written_at=now,
            operation_id=operation_id,
            idempotency_key=manifest.idempotency_key,
            batch_manifest_id=manifest.batch_manifest_id,
            batch_manifest_sha256=manifest.manifest_sha256,
            correlation_id=manifest.correlation_id,
            subject_ids=manifest.subject_ids,
            item_ids=tuple(action.manifest_id for action in manifest.actions),
        )
        payload = _event_payload(reserved)
        payload["item_metadata"] = [
            {
                "item_id": action.manifest_id,
                "item_index": item_index,
                "action_kind": action.kind.value,
                "item_manifest_sha256": manifest_sha256(action),
                "subject_ids": list(_subject_for_action(action)),
                "action_manifest_payload": json.loads(
                    canonical_json(action).decode("utf-8")
                ),
                "action_manifest_payload_sha256": hashlib.sha256(
                    canonical_json(action)
                ).hexdigest(),
            }
            for item_index, action in enumerate(manifest.actions)
        ]
        _apply_payload(payload, state)
        _append_payload(fd, payload)
        return _operation_record(state.operations[operation_id])


def issue_item_permit(
    review: ActionReview,
    batch: ActionBatchManifest,
    item: ActionManifest,
    now: datetime,
    path: Path | None = None,
) -> ActionPermit:
    """Выдаёт единственный permit только для уже fsync-проверенного item."""

    try:
        import config

        enforcement_enabled = config.REPORT_CHECKER_ENFORCE_ACTIONS is True
    except (ImportError, AttributeError):
        enforcement_enabled = False
    if not enforcement_enabled:
        raise ApprovalPermitInvalidError(
            "выдача permit запрещена: action enforcement выключен"
        )
    _require_aware(now, "now")
    if review.decision is not SafetyDecision.SAFE:
        raise ApprovalPermitInvalidError("DENIED review не может получить permit")
    if review.issues:
        raise ApprovalPermitInvalidError("review с issues не может получить permit")
    if review.expires_at is None or review.evidence_state_sha256 is None:
        raise ApprovalPermitInvalidError("review не содержит expiry/live digest")
    if now < review.checked_at or now >= review.expires_at:
        raise ApprovalPermitInvalidError("review истёк или ещё не начался")
    if batch.manifest_sha256 != manifest_sha256(batch):
        raise ApprovalPermitInvalidError("batch manifest изменён после review")
    if not (0 <= review.item_index < len(batch.actions)) or batch.actions[review.item_index] != item:
        raise ApprovalPermitInvalidError("review item не совпадает с batch")
    target = _path(path)
    with _locked_wal(target) as fd:
        state = _scan(fd)
        operation = state.operations.get(review.operation_id)
        if operation is None:
            raise ApprovalPermitInvalidError("operation не найдена")
        expected_operation_fields = (
            operation.idempotency_key == review.idempotency_key == batch.idempotency_key,
            operation.batch_manifest_id == review.batch_manifest_id == batch.batch_manifest_id,
            operation.batch_manifest_sha256 == review.batch_manifest_sha256 == batch.manifest_sha256,
        )
        if not all(expected_operation_fields):
            raise ApprovalPermitInvalidError("review/batch не совпадают с reserved operation")
        state_item = operation.items[review.item_index]
        if (
            state_item.item_id != review.item_id
            or state_item.action_kind is not review.action_kind
            or state_item.item_manifest_sha256 != review.item_manifest_sha256
            or state_item.subject_ids != review.subject_ids
        ):
            raise ApprovalPermitInvalidError("review не совпадает с reserved item")
        check = state_item.check
        if check is None:
            raise ApprovalPermitInvalidError("review ещё не сохранён в WAL")
        if (
            check.get("check_id") != review.check_id
            or check.get("decision") != SafetyDecision.SAFE.value
            or check.get("final_live_state_sha256") != review.evidence_state_sha256
            or _parse_datetime(check.get("checked_at"), "checked_at") != review.checked_at
            or _parse_datetime(check.get("expires_at"), "expires_at") != review.expires_at
        ):
            raise ApprovalPermitInvalidError("сохранённый check не совпадает с review")
        if state_item.permit is not None:
            raise ApprovalPermitInvalidError("для item уже выдавался permit")
        if any(_effective_result(previous) is not ActionResult.CONFIRMED for previous in operation.items[: review.item_index]):
            raise ApprovalAuditSequenceError("предыдущий item не CONFIRMED")
        if any(later.check is not None or later.permit is not None for later in operation.items[review.item_index + 1 :]):
            raise ApprovalAuditSequenceError("более поздний item уже имеет check/permit")
        permit = ActionPermit(
            permit_id=str(uuid.uuid4()),
            check_id=review.check_id,
            operation_id=review.operation_id,
            idempotency_key=review.idempotency_key,
            batch_manifest_id=review.batch_manifest_id,
            batch_manifest_sha256=review.batch_manifest_sha256,
            item_id=review.item_id,
            item_index=review.item_index,
            action_kind=review.action_kind,
            item_manifest_sha256=review.item_manifest_sha256,
            subject_ids=review.subject_ids,
            evidence_state_sha256=review.evidence_state_sha256,
            issued_at=now,
            expires_at=review.expires_at,
        )
        event = PermitIssuedAuditEvent(
            schema_version=_SCHEMA_VERSION,
            event="permit_issued",
            event_id=str(uuid.uuid4()),
            written_at=now,
            operation_id=permit.operation_id,
            idempotency_key=permit.idempotency_key,
            batch_manifest_id=permit.batch_manifest_id,
            batch_manifest_sha256=permit.batch_manifest_sha256,
            subject_ids=permit.subject_ids,
            check_id=permit.check_id,
            permit_id=permit.permit_id,
            item_id=permit.item_id,
            item_index=permit.item_index,
            action_kind=permit.action_kind,
            item_manifest_sha256=permit.item_manifest_sha256,
            final_live_state_sha256=permit.evidence_state_sha256,
            issued_at=permit.issued_at,
            expires_at=permit.expires_at,
        )
        payload = _event_payload(event)
        _apply_payload(payload, state)
        _append_payload(fd, payload)
        return permit


def revoke_permit(
    permit: ActionPermit,
    reason_code: str,
    now: datetime,
    path: Path | None = None,
) -> OperationRecord:
    """Навсегда отзывает ещё не использованный permit под full WAL rescan.

    Повтор с той же причиной idempotent и не создаёт вторую строку. Другая
    причина для уже отозванного permit считается конфликтом и блокируется.
    """

    _require_aware(now, "now")
    if _MACHINE_CODE_RE.fullmatch(reason_code) is None:
        raise ApprovalPermitInvalidError("reason_code должен быть machine-readable code")
    target = _path(path)
    with _locked_wal(target) as fd:
        state = _scan(fd)
        operation = state.operations.get(permit.operation_id)
        if operation is None or not (0 <= permit.item_index < len(operation.items)):
            raise ApprovalPermitInvalidError("permit operation/item не найдены")
        item = operation.items[permit.item_index]
        persisted = item.permit
        if persisted is None:
            raise ApprovalPermitInvalidError("permit не был выдан")
        expected = {
            "permit_id": permit.permit_id,
            "check_id": permit.check_id,
            "item_id": permit.item_id,
            "item_index": permit.item_index,
            "action_kind": permit.action_kind.value,
            "item_manifest_sha256": permit.item_manifest_sha256,
            "final_live_state_sha256": permit.evidence_state_sha256,
            "issued_at": permit.issued_at.isoformat(),
            "expires_at": permit.expires_at.isoformat(),
        }
        if any(persisted.get(key) != value for key, value in expected.items()):
            raise ApprovalPermitInvalidError("permit изменён или относится к другому item")
        if (
            operation.idempotency_key != permit.idempotency_key
            or operation.batch_manifest_id != permit.batch_manifest_id
            or operation.batch_manifest_sha256 != permit.batch_manifest_sha256
            or item.subject_ids != permit.subject_ids
        ):
            raise ApprovalPermitInvalidError("permit не совпадает с immutable operation")
        if item.attempt is not None:
            raise ApprovalPermitInvalidError("использованный permit нельзя отозвать")
        if item.permit_revocation is not None:
            if item.permit_revocation.get("reason_code") != reason_code:
                raise ApprovalPermitInvalidError(
                    "permit уже отозван с другой причиной"
                )
            return _operation_record(operation)
        event = PermitRevokedAuditEvent(
            schema_version=_SCHEMA_VERSION,
            event="permit_revoked",
            event_id=str(uuid.uuid4()),
            written_at=now,
            operation_id=permit.operation_id,
            idempotency_key=permit.idempotency_key,
            batch_manifest_id=permit.batch_manifest_id,
            batch_manifest_sha256=permit.batch_manifest_sha256,
            subject_ids=permit.subject_ids,
            check_id=permit.check_id,
            permit_id=permit.permit_id,
            item_id=permit.item_id,
            item_index=permit.item_index,
            action_kind=permit.action_kind,
            item_manifest_sha256=permit.item_manifest_sha256,
            evidence_state_sha256=permit.evidence_state_sha256,
            revoked_at=now,
            reason_code=reason_code,
        )
        payload = _event_payload(event)
        _apply_payload(payload, state)
        _append_payload(fd, payload)
        return _operation_record(operation)


def consume_permit(
    permit: ActionPermit,
    manifest: ActionBatchManifest,
    precondition_sha256: str,
    now: datetime,
    path: Path | None = None,
) -> OperationAttemptAttestation:
    """Одноразово fsync-фиксирует attempt перед внешней mutation."""

    _require_aware(now, "now")
    if precondition_sha256 != permit.evidence_state_sha256:
        raise ApprovalPermitInvalidError("fresh precondition отличается от approved state")
    if manifest.manifest_sha256 != manifest_sha256(manifest):
        raise ApprovalPermitInvalidError("batch manifest изменён")
    target = _path(path)
    with _locked_wal(target) as fd:
        state = _scan(fd)
        operation = state.operations.get(permit.operation_id)
        if operation is None or not (0 <= permit.item_index < len(operation.items)):
            raise ApprovalPermitInvalidError("permit operation/item не найдены")
        if (
            permit.idempotency_key != manifest.idempotency_key
            or permit.idempotency_key != operation.idempotency_key
            or permit.batch_manifest_id != manifest.batch_manifest_id
            or permit.batch_manifest_sha256 != manifest.manifest_sha256
            or operation.batch_manifest_sha256 != manifest.manifest_sha256
        ):
            raise ApprovalPermitInvalidError("permit не совпадает с immutable batch")
        item = operation.items[permit.item_index]
        if (
            item.permit is None
            or item.permit_revocation is not None
            or item.attempt is not None
        ):
            raise ApprovalPermitInvalidError(
                "permit отсутствует, отозван или уже использован"
            )
        persisted = item.permit
        expected = {
            "permit_id": permit.permit_id,
            "check_id": permit.check_id,
            "item_id": permit.item_id,
            "item_index": permit.item_index,
            "action_kind": permit.action_kind.value,
            "item_manifest_sha256": permit.item_manifest_sha256,
            "final_live_state_sha256": permit.evidence_state_sha256,
            "issued_at": permit.issued_at.isoformat(),
            "expires_at": permit.expires_at.isoformat(),
        }
        if any(persisted.get(key) != value for key, value in expected.items()):
            raise ApprovalPermitInvalidError("permit изменён или относится к другому item")
        if item.subject_ids != permit.subject_ids:
            raise ApprovalPermitInvalidError("permit subject_ids изменены")
        if now < permit.issued_at or now >= permit.expires_at:
            raise ApprovalPermitInvalidError("permit истёк или ещё не активен")
        attempt_id = str(uuid.uuid4())
        event = ActionAttemptAuditEvent(
            schema_version=_SCHEMA_VERSION,
            event="action_attempt",
            event_id=str(uuid.uuid4()),
            written_at=now,
            operation_id=permit.operation_id,
            idempotency_key=permit.idempotency_key,
            batch_manifest_id=permit.batch_manifest_id,
            batch_manifest_sha256=permit.batch_manifest_sha256,
            subject_ids=permit.subject_ids,
            permit_id=permit.permit_id,
            attempt_id=attempt_id,
            item_id=permit.item_id,
            item_index=permit.item_index,
            action_kind=permit.action_kind,
            item_manifest_sha256=permit.item_manifest_sha256,
            precondition_sha256=precondition_sha256,
            attempted_at=now,
        )
        payload = _event_payload(event)
        _apply_payload(payload, state)
        _append_payload(fd, payload)
        return OperationAttemptAttestation(
            operation_id=permit.operation_id,
            idempotency_key=permit.idempotency_key,
            batch_manifest_id=permit.batch_manifest_id,
            batch_manifest_sha256=permit.batch_manifest_sha256,
            permit_id=permit.permit_id,
            attempt_id=attempt_id,
            item_id=permit.item_id,
            item_index=permit.item_index,
            action_kind=permit.action_kind,
            item_manifest_sha256=permit.item_manifest_sha256,
            precondition_sha256=precondition_sha256,
            subject_ids=permit.subject_ids,
            attempted_at=now,
        )


def verify_attempt_attestation(
    attestation: OperationAttemptAttestation,
    path: Path | None = None,
) -> None:
    """Проверяет attestation только по полному persisted WAL rescan."""

    if not isinstance(attestation, OperationAttemptAttestation):
        raise ApprovalPermitInvalidError("attempt attestation имеет неверный тип")
    target = _path(path)
    with _locked_wal(target) as fd:
        state = _scan(fd)
        operation = state.operations.get(attestation.operation_id)
        if operation is None or not 0 <= attestation.item_index < len(operation.items):
            raise ApprovalPermitInvalidError("attempt operation/item не найдены")
        item = operation.items[attestation.item_index]
        attempt = item.attempt
        if attempt is None or item.result is not None or item.reconciliation is not None:
            raise ApprovalPermitInvalidError("attempt отсутствует или уже завершён")
        expected = {
            "operation_id": attestation.operation_id,
            "idempotency_key": attestation.idempotency_key,
            "batch_manifest_id": attestation.batch_manifest_id,
            "batch_manifest_sha256": attestation.batch_manifest_sha256,
            "permit_id": attestation.permit_id,
            "attempt_id": attestation.attempt_id,
            "item_id": attestation.item_id,
            "item_index": attestation.item_index,
            "action_kind": attestation.action_kind.value,
            "item_manifest_sha256": attestation.item_manifest_sha256,
            "precondition_sha256": attestation.precondition_sha256,
            "subject_ids": list(attestation.subject_ids),
            "attempted_at": attestation.attempted_at.isoformat(),
        }
        if any(attempt.get(key) != value for key, value in expected.items()):
            raise ApprovalPermitInvalidError("attempt attestation не совпадает с WAL")


def append_item_result(
    event: ActionResultAuditEvent,
    path: Path | None = None,
) -> OperationRecord:
    """Атомарно сохраняет результат exact attempt и возвращает recovery state."""

    payload = _event_payload(event)
    target = _path(path)
    with _locked_wal(target) as fd:
        state = _scan(fd)
        _apply_payload(payload, state)
        _append_payload(fd, payload)
        return _operation_record(state.operations[event.operation_id])


def find_operation(
    idempotency_key: str,
    path: Path | None = None,
) -> OperationRecord | None:
    """Восстанавливает operation только из полностью проверенного WAL."""

    try:
        canonical_key = str(uuid.UUID(idempotency_key))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("idempotency_key должен быть UUID") from exc
    if canonical_key != idempotency_key.lower():
        raise ValueError("idempotency_key должен быть canonical UUID")
    target = _path(path)
    if not target.exists():
        return None
    with _locked_wal(target) as fd:
        state = _scan(fd)
        operation_id = state.by_idempotency.get(idempotency_key)
        return None if operation_id is None else _operation_record(state.operations[operation_id])


def find_operation_by_id(
    operation_id: str,
    path: Path | None = None,
) -> OperationRecord | None:
    """Ищет operation по exact internal ID через strict locked full WAL rescan."""

    try:
        canonical_id = str(uuid.UUID(operation_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("operation_id должен быть UUID") from exc
    if canonical_id != operation_id:
        raise ValueError("operation_id должен быть canonical UUID")
    target = _path(path)
    if not target.exists():
        return None
    with _locked_wal(target) as fd:
        state = _scan(fd)
        operation = state.operations.get(operation_id)
        return None if operation is None else _operation_record(operation)


def read_action_history(
    window: TimeWindow,
    path: Path | None = None,
) -> tuple[ActionHistoryRecord, ...]:
    """Читает per-item историю; незавершённый attempt возвращается как UNKNOWN."""

    target = _path(path)
    if not target.exists():
        return ()
    with _locked_wal(target) as fd:
        state = _scan(fd)
        history: list[ActionHistoryRecord] = []
        for operation in state.operations.values():
            for item in operation.items:
                if item.attempt is None:
                    continue
                attempted_at = _parse_datetime(item.attempt["attempted_at"], "attempted_at")
                if not (window.start <= attempted_at < window.end):
                    continue
                result_payload = item.reconciliation or item.result
                completed_at = None
                exact_ids: tuple[str, ...] = ()
                if result_payload is not None:
                    completed_at = _parse_datetime(
                        result_payload.get("reconciled_at", result_payload.get("completed_at")),
                        "completed_at",
                    )
                    exact_ids = tuple(result_payload.get("exact_created_ids", []))  # type: ignore[arg-type]
                result = _effective_result(item)
                if result is None:  # pragma: no cover - item.attempt гарантирует UNKNOWN
                    result = ActionResult.UNKNOWN
                history.append(
                    ActionHistoryRecord(
                        operation_id=operation.operation_id,
                        batch_manifest_id=operation.batch_manifest_id,
                        item_id=item.item_id,
                        item_index=item.item_index,
                        action_kind=item.action_kind,
                        subject_ids=item.subject_ids,
                        attempt_id=str(item.attempt["attempt_id"]),
                        result=result,
                        exact_created_ids=exact_ids,
                        attempted_at=attempted_at,
                        completed_at=completed_at,
                        reconciliation_required=(
                            result is ActionResult.UNKNOWN
                            or (
                                item.result is not None
                                and bool(item.result.get("reconciliation_required"))
                                and item.reconciliation is None
                            )
                        ),
                    )
                )
        return tuple(sorted(history, key=lambda record: (record.attempted_at, record.operation_id, record.item_index)))
