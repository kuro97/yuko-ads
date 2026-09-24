"""Read-only Approval Checker и единственный writer его собственного audit."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from services.approval_audit import (
    ApprovalAuditError,
    append_report_check,
    append_event,
    find_operation,
    read_action_history,
)
from services.approval_checker_models import (
    ActionBatchManifest,
    ActionKind,
    ActionManifest,
    ActionResult,
    ActionReview,
    ApprovalDecision,
    CheckAuditEvent,
    CheckerHealth,
    CheckIssue,
    EvidenceRequest,
    OperationRecord,
    ReportCheckRequest,
    ReportCheckAuditEvent,
    ReportCheckResult,
    ReportVerdict,
    SourceSystem,
    SubjectKind,
    TimeWindow,
    canonical_state_sha256,
    manifest_sha256,
)
from services.approval_report import validate_report_coverage
from services.approval_rules import evaluate_action_item, evaluate_report
from services.approval_sources import load_action_item_evidence, load_evidence


_LAST_CHECK_LOCK = threading.Lock()
_LAST_CHECK_AT: datetime | None = None
logger = logging.getLogger(__name__)


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} должен содержать timezone")


def _mark_check(now: datetime) -> None:
    global _LAST_CHECK_AT
    with _LAST_CHECK_LOCK:
        _LAST_CHECK_AT = now


def _last_check_at() -> datetime | None:
    with _LAST_CHECK_LOCK:
        return _LAST_CHECK_AT


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current if current.exists() and current.is_dir() else None


def _audit_is_writable_and_valid(now: datetime) -> bool:
    path = Path(config.REPORT_CHECKER_AUDIT_PATH).expanduser()
    try:
        if path.exists():
            if path.is_symlink() or not path.is_file() or not os.access(path, os.W_OK):
                return False
        else:
            parent = _nearest_existing_parent(path.parent)
            if parent is None or not os.access(parent, os.W_OK):
                return False
        # Пустое/отсутствующее WAL валидно; существующее полностью сканирует audit API.
        read_action_history(
            TimeWindow(
                start=datetime(1970, 1, 1, tzinfo=timezone.utc),
                end=now + timedelta(microseconds=1),
                timezone_name="UTC",
                semantic="checker_health_audit_scan",
            )
        )
        return True
    except (OSError, ValueError, ApprovalAuditError):
        return False


def _pending_marker() -> bool:
    path = Path(config.REPORT_CHECKER_RECONCILIATION_PATH).expanduser()
    if not path.exists():
        return False
    try:
        if path.is_symlink() or not path.is_file():
            return True
        raw = path.read_bytes()
        if not raw:
            return False
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return True
    if isinstance(payload, list):
        return bool(payload)
    if isinstance(payload, dict):
        for key in ("pending", "operations", "items"):
            if key in payload:
                return bool(payload[key])
        return bool(payload)
    return True


def _audit_has_pending(now: datetime) -> bool:
    try:
        history = read_action_history(
            TimeWindow(
                start=datetime(1970, 1, 1, tzinfo=timezone.utc),
                end=now + timedelta(microseconds=1),
                timezone_name="UTC",
                semantic="checker_health_pending_scan",
            )
        )
    except (OSError, ValueError, ApprovalAuditError):
        return True
    return any(item.reconciliation_required for item in history)


def checker_health(now: datetime | None = None) -> CheckerHealth:
    """Проверяет checker fail-closed, не создавая файлов и provider side effects."""

    checked_at = now or datetime.now(timezone.utc)
    _aware(checked_at, "now")
    audit_writable = _audit_is_writable_and_valid(checked_at)
    reconciliation_pending = (
        _pending_marker() or _audit_has_pending(checked_at) if audit_writable else True
    )
    return CheckerHealth(
        enabled=bool(config.REPORT_CHECKER_ENABLED),
        enforce_reports=bool(config.REPORT_CHECKER_ENFORCE_REPORTS),
        enforce_actions=bool(config.REPORT_CHECKER_ENFORCE_ACTIONS),
        audit_writable=audit_writable,
        reconciliation_pending=reconciliation_pending,
        last_check_at=_last_check_at(),
    )


def _report_evidence_request(
    request: ReportCheckRequest, now: datetime
) -> EvidenceRequest:
    subjects = tuple(dict.fromkeys(claim.subject for claim in request.claims))
    windows = tuple(
        dict.fromkeys(
            claim.window for claim in request.claims if claim.window is not None
        )
    )
    account_ids = tuple(
        dict.fromkeys(
            subject.subject_id
            for subject in subjects
            if subject.kind is SubjectKind.ACCOUNT
        )
    )
    adset_ids = tuple(
        dict.fromkeys(
            (
                subject.subject_id
                if subject.kind is SubjectKind.ADSET
                else subject.parent_id
            )
            for subject in subjects
            if subject.kind in {SubjectKind.ADSET, SubjectKind.AD}
            and (subject.kind is SubjectKind.ADSET or subject.parent_id is not None)
        )
    )
    ad_ids = tuple(
        dict.fromkeys(
            subject.subject_id for subject in subjects if subject.kind is SubjectKind.AD
        )
    )
    card_ids = tuple(
        dict.fromkeys(
            subject.subject_id
            for subject in subjects
            if subject.kind in {SubjectKind.CARD, SubjectKind.BRIEF}
        )
    )
    required_sources = tuple(dict.fromkeys(claim.source for claim in request.claims))
    return EvidenceRequest(
        request_id=f"report:{request.correlation_id}",
        purpose="REPORT",
        action_kind=None,
        generated_at=now,
        subjects=subjects,
        claims=request.claims,
        required_sources=required_sources,
        windows=windows,
        account_ids=account_ids,
        adset_ids=adset_ids,
        ad_ids=ad_ids,
        card_ids=card_ids,
        staged_relative_paths=(),
        include_full_inventory=SourceSystem.FACEBOOK in required_sources,
        force_live=False,
        max_age_seconds=config.REPORT_CHECKER_LOCAL_DB_MAX_AGE_SECONDS,
    )


def _report_failure(
    request: ReportCheckRequest,
    now: datetime,
    verdict: ReportVerdict,
    issues: tuple[CheckIssue, ...],
) -> ReportCheckResult:
    return ReportCheckResult(
        check_id=str(uuid.uuid4()),
        verdict=verdict,
        checked_at=now,
        outcomes=(),
        issues=issues,
        manifest_sha256=request.manifest_sha256,
        audit_persisted=False,
    )


def _persist_report_result(
    request: ReportCheckRequest,
    result: ReportCheckResult,
    now: datetime,
    evidence_state_sha256: str | None,
) -> ReportCheckResult:
    """Связывает verdict с evidence без значений, ID субъектов и business text."""

    event = ReportCheckAuditEvent(
        schema_version=1,
        event="report_check",
        event_id=str(uuid.uuid4()),
        written_at=now,
        correlation_id=(
            "sha256:"
            + hashlib.sha256(request.correlation_id.encode("utf-8")).hexdigest()
        ),
        check_id=result.check_id,
        report_template=request.payload.template,
        verdict=result.verdict,
        manifest_sha256=result.manifest_sha256,
        evidence_state_sha256=evidence_state_sha256,
        issue_codes=tuple(sorted({issue.code for issue in result.issues})),
        checked_at=result.checked_at,
    )
    try:
        append_report_check(event)
    except Exception:
        return _report_failure(
            request,
            now,
            ReportVerdict.CHECKER_UNAVAILABLE,
            (
                CheckIssue(
                    code="AUDIT_WRITE_FAILED",
                    message="Verdict отчёта не сохранён; отправка запрещена",
                    blocking=True,
                    source=SourceSystem.CHECKER_AUDIT,
                ),
            ),
        )
    return replace(result, audit_persisted=True)


def check_report(
    request: ReportCheckRequest,
    now: datetime | None = None,
) -> ReportCheckResult:
    """Проверяет coverage до любого source I/O, затем независимо читает evidence."""

    checked_at = now or datetime.now(timezone.utc)
    _aware(checked_at, "now")
    coverage = validate_report_coverage(request)
    if not coverage.complete:
        result = _persist_report_result(
            request,
            _report_failure(
                request,
                checked_at,
                ReportVerdict.BLOCKED,
                coverage.issues,
            ),
            checked_at,
            None,
        )
        _mark_check(checked_at)
        return result

    health = checker_health(checked_at)
    if not health.enabled or not health.audit_writable:
        result = _persist_report_result(
            request,
            _report_failure(
                request,
                checked_at,
                ReportVerdict.CHECKER_UNAVAILABLE,
                (
                    CheckIssue(
                        code="CHECKER_UNAVAILABLE",
                        message="Approval Checker отключён или его audit недоступен",
                        blocking=True,
                        source=SourceSystem.CHECKER_RUNTIME,
                    ),
                ),
            ),
            checked_at,
            None,
        )
        _mark_check(checked_at)
        return result
    evidence_state_sha256: str | None = None
    try:
        evidence = load_evidence(
            _report_evidence_request(request, checked_at), checked_at
        )
        evidence_state_sha256 = canonical_state_sha256(evidence)
        result = evaluate_report(request, evidence, checked_at)
    except Exception:
        result = _report_failure(
            request,
            checked_at,
            ReportVerdict.CHECKER_UNAVAILABLE,
            (
                CheckIssue(
                    code="CHECKER_INTERNAL_ERROR",
                    message="Approval Checker не смог завершить проверку отчёта",
                    blocking=True,
                    source=SourceSystem.CHECKER_RUNTIME,
                ),
            ),
        )
    result = _persist_report_result(
        request,
        result,
        checked_at,
        evidence_state_sha256,
    )
    _mark_check(checked_at)
    return result


def _action_subject_ids(item: ActionManifest) -> tuple[str, ...]:
    if item.kind is ActionKind.LAUNCH:
        return (f"card:{item.trello.card_id}",)  # type: ignore[union-attr]
    if item.kind in {ActionKind.PAUSE, ActionKind.UNPAUSE}:
        return (f"ad:{item.ad_id}",)  # type: ignore[union-attr]
    if item.kind is ActionKind.ASSET_RECOVERY:
        return (f"adset:{item.target_adset_id}",)  # type: ignore[union-attr]
    return (f"adset:{item.adset_id}",)  # type: ignore[union-attr]


def _denied_review(
    batch: ActionBatchManifest,
    item: ActionManifest,
    item_index: int,
    now: datetime,
    operation_id: str,
    issues: tuple[CheckIssue, ...],
) -> ActionReview:
    return ActionReview(
        check_id=str(uuid.uuid4()),
        operation_id=operation_id,
        idempotency_key=batch.idempotency_key,
        batch_manifest_id=batch.batch_manifest_id,
        item_id=item.manifest_id,
        item_index=item_index,
        action_kind=item.kind,
        decision=ApprovalDecision.DENIED,
        checked_at=now,
        expires_at=None,
        batch_manifest_sha256=batch.manifest_sha256,
        item_manifest_sha256=manifest_sha256(item),
        evidence_state_sha256=None,
        subject_ids=_action_subject_ids(item),
        issues=issues,
        audit_persisted=False,
    )


def _sequence_issue(
    batch: ActionBatchManifest,
    item: ActionManifest,
    item_index: int,
    operation: OperationRecord | None,
) -> CheckIssue | None:
    if batch.manifest_sha256 != manifest_sha256(batch):
        return CheckIssue("INVALID_CONTRACT", "Batch manifest hash изменён", True)
    if (
        item_index < 0
        or item_index >= len(batch.actions)
        or batch.actions[item_index] != item
    ):
        return CheckIssue(
            "SEQUENCE_INVALID", "Item не совпадает с позицией batch", True
        )
    if operation is None:
        return CheckIssue(
            "SEQUENCE_INVALID", "Operation ещё не зарезервирована в audit", True
        )
    if (
        operation.batch_manifest_id != batch.batch_manifest_id
        or operation.batch_manifest_sha256 != batch.manifest_sha256
        or operation.idempotency_key != batch.idempotency_key
    ):
        return CheckIssue(
            "SEQUENCE_INVALID", "Reserved operation не совпадает с batch", True
        )
    current = operation.items[item_index]
    if (
        current.item_id != item.manifest_id
        or current.item_manifest_sha256 != manifest_sha256(item)
        or current.action_kind is not item.kind
    ):
        return CheckIssue(
            "SEQUENCE_INVALID", "Reserved item не совпадает с manifest", True
        )
    if current.check_id is not None or current.permit_id is not None:
        return CheckIssue(
            "SEQUENCE_INVALID", "Item уже проверялся или получил permit", True
        )
    if any(
        previous.result is not ActionResult.CONFIRMED
        for previous in operation.items[:item_index]
    ):
        return CheckIssue("SEQUENCE_INVALID", "Предыдущий item ещё не CONFIRMED", True)
    if any(
        later.check_id is not None or later.permit_id is not None
        for later in operation.items[item_index + 1 :]
    ):
        return CheckIssue("SEQUENCE_INVALID", "Более поздний item уже проверен", True)
    return None


def _persist_review(review: ActionReview, now: datetime) -> ActionReview:
    event = CheckAuditEvent(
        schema_version=1,
        event="check",
        event_id=str(uuid.uuid4()),
        written_at=now,
        operation_id=review.operation_id,
        idempotency_key=review.idempotency_key,
        batch_manifest_id=review.batch_manifest_id,
        batch_manifest_sha256=review.batch_manifest_sha256,
        subject_ids=review.subject_ids,
        check_id=review.check_id,
        item_id=review.item_id,
        item_index=review.item_index,
        action_kind=review.action_kind,
        item_manifest_sha256=review.item_manifest_sha256,
        decision=review.decision,
        checked_at=review.checked_at,
        expires_at=review.expires_at,
        final_live_state_sha256=review.evidence_state_sha256,
        issue_codes=tuple(issue.code for issue in review.issues),
    )
    append_event(event)
    return replace(review, audit_persisted=True)


def review_action_item(
    batch: ActionBatchManifest,
    item: ActionManifest,
    item_index: int,
    now: datetime | None = None,
) -> ActionReview:
    """Проверяет один item и fsync-сохраняет verdict; permit здесь не создаётся."""

    checked_at = now or datetime.now(timezone.utc)
    _aware(checked_at, "now")
    try:
        operation = find_operation(batch.idempotency_key)
    except Exception:
        operation = None
    operation_id = (
        operation.operation_id if operation is not None else batch.idempotency_key
    )
    sequence_issue = _sequence_issue(batch, item, item_index, operation)
    if sequence_issue is not None:
        review = _denied_review(
            batch, item, item_index, checked_at, operation_id, (sequence_issue,)
        )
        # Invalid successor нельзя писать: WAL сам запрещает check до CONFIRMED predecessor.
        if operation is not None and sequence_issue.code != "SEQUENCE_INVALID":
            try:
                review = _persist_review(review, checked_at)
            except Exception as exc:
                logger.error(
                    "approval denied-review audit persist failed: %s",
                    type(exc).__name__,
                )
        _mark_check(checked_at)
        return review

    health = checker_health(checked_at)
    health_issues: list[CheckIssue] = []
    if not health.enabled or not health.audit_writable:
        health_issues.append(
            CheckIssue(
                "CHECKER_UNAVAILABLE",
                "Approval Checker отключён или audit недоступен",
                True,
                SourceSystem.CHECKER_RUNTIME,
            )
        )
    if health.reconciliation_pending:
        health_issues.append(
            CheckIssue(
                "PENDING_RECONCILIATION",
                "Есть незавершённое действие, требующее reconciliation",
                True,
                SourceSystem.CHECKER_AUDIT,
            )
        )
    if health_issues:
        review = _denied_review(
            batch,
            item,
            item_index,
            checked_at,
            operation_id,
            tuple(health_issues),
        )
    else:
        try:
            evidence = load_action_item_evidence(batch, item, item_index, checked_at)
            review = replace(
                evaluate_action_item(batch, item, item_index, evidence, checked_at),
                operation_id=operation_id,
            )
        except Exception:
            review = _denied_review(
                batch,
                item,
                item_index,
                checked_at,
                operation_id,
                (
                    CheckIssue(
                        "CHECKER_INTERNAL_ERROR",
                        "Approval Checker не смог завершить проверку действия",
                        True,
                        SourceSystem.CHECKER_RUNTIME,
                    ),
                ),
            )
    try:
        persisted = _persist_review(review, checked_at)
    except Exception:
        persisted = replace(
            review,
            decision=ApprovalDecision.DENIED,
            expires_at=None,
            evidence_state_sha256=None,
            issues=(
                *review.issues,
                CheckIssue(
                    "AUDIT_WRITE_FAILED",
                    "Verdict не удалось fsync-сохранить; действие запрещено",
                    True,
                    SourceSystem.CHECKER_AUDIT,
                ),
            ),
            audit_persisted=False,
        )
    _mark_check(checked_at)
    return persisted
