"""Исполнение ровно одной owner-approved попытки по сохранённой lineage."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from services.action_gateway import _open_owner_execution_session
from services.action_manifests import (
    build_action_batch,
    manifest_from_payload,
)
from services.approval_checker_models import (
    ActionExecution,
    ActionManifest,
    ActionResult,
    ActionReview,
    AssetRecoveryManifest,
    EvidenceBundle,
    LaunchManifest,
    PauseManifest,
    SafetyDecision,
    ScaleManifest,
    UnpauseManifest,
    canonical_json,
    manifest_sha256,
)
from services.approval_rules import evaluate_action_item
from services.approval_sources import load_action_item_evidence
from services.live_read_scope import live_read_scope
from services.owner_action_live_manifest import (
    LiveManifestError,
    action_origin_for,
    build_live_manifest,
    is_serialized_manifest,
)
from services.owner_action_models import (
    ActionAttemptAttestation,
    ClaimProgress,
    LifecycleState,
    PermitRetirementReason,
    ProposalKind,
    ProposedTarget,
    ProposalView,
    TechnicalPermit,
    canonical_sha256,
)
from services.owner_action_repository import (
    OwnerActionLifecycleConflict,
    OwnerActionPermitUnavailable,
    OwnerActionRepository,
    OwnerActionRepositoryError,
    _default_repository,
)


logger = logging.getLogger(__name__)

_MANIFEST_TYPE_BY_PROPOSAL = {
    ProposalKind.LAUNCH: LaunchManifest,
    ProposalKind.PAUSE: PauseManifest,
    ProposalKind.UNPAUSE: UnpauseManifest,
    ProposalKind.SCALE: ScaleManifest,
    ProposalKind.ASSET_RECOVERY: AssetRecoveryManifest,
}

# Виды, для которых есть фабрика манифеста по живому состоянию.
_LIVE_BUILDABLE_KINDS = frozenset(
    {ProposalKind.PAUSE, ProposalKind.UNPAUSE, ProposalKind.SCALE}
)

# Причины, после которых одобрение НЕ сгорает. Задание возвращается в повтор
# (EXECUTION_RETRY_WAIT) и доезжает следующим тиком:
#
# * ACTION_MANIFEST_INVALID — баг сборки манифеста или битое намерение продюсера;
#   исправленный деплой доводит задание сам.
# * SOURCE_UNAVAILABLE — FB/AMO/CDP не ответили или ответили неполно. Раньше это
#   уводило предложение в терминальный BLOCKED_STALE: AMO отвалился на минуту —
#   одобрение владельца сгорело навсегда. Чужой downtime не отменяет решение.
# * EXECUTION_BUDGET_EXCEEDED — прогон вышел за отведённое время и вернул задание
#   в очередь ДО выдачи permit. Ничего не мутировано, повтор безопасен.
#
# Всё остальное (дрейф живого состояния, отказ live review) по-прежнему закрывает
# предложение терминальным BLOCKED_STALE.
_RETRYABLE_BLOCK_REASONS = frozenset(
    {
        "ACTION_MANIFEST_INVALID",
        "SOURCE_UNAVAILABLE",
        "EXECUTION_BUDGET_EXCEEDED",
        # Живых слотов адсета не хватает прямо сейчас (клинер выключен, соседний
        # запуск занял слот). Это «подождём», а не «нельзя»: терминальный отказ
        # сжигал одобрение навсегда и отравлял карточку. Конечность ожидания
        # гарантирует дедлайн исполнения — после него честное terminal-закрытие.
        "LAUNCH_CAPACITY_WAIT",
    }
)
# Совместимость: код причины «баг сборки» используется и в тестах, и в логах.
_RETRYABLE_BLOCK_REASON = "ACTION_MANIFEST_INVALID"
# Недоступность источника — не наша вина и не вина владельца: причина отдельная,
# чтобы диспетчер не тратил на неё потолок попыток.
_SOURCE_UNAVAILABLE_REASON = "SOURCE_UNAVAILABLE"
_BUDGET_EXCEEDED_REASON = "EXECUTION_BUDGET_EXCEEDED"
# Нехватка живых слотов адсета: возвратный исход live review (не терминальный).
_CAPACITY_WAIT_REASON = "LAUNCH_CAPACITY_WAIT"
# Коды issue live review, при которых отказ означает «повторить позже», а не
# «одобрение недействительно». Ровно один код: нехватка живых слотов. Любая
# примесь другого кода — обычный терминальный отказ.
_RETRYABLE_REVIEW_ISSUE_CODES = frozenset({"LAUNCH_CAPACITY_INSUFFICIENT"})
# Транспортные сбои чтения источников: сеть, таймаут, обрыв, недоступный файл.
_TRANSPORT_ERROR_NAMES = (
    "Timeout",
    "ConnectTimeout",
    "ReadTimeout",
    "ConnectionError",
    "ConnectionResetError",
    "SSLError",
    "ProxyError",
    "ChunkedEncodingError",
    "RequestException",
    "HTTPError",
    "TimeoutError",
    "OSError",
    "socket.timeout",
)


# Сколько времени у конвейера есть на исполнение ПОСЛЕ одобрения владельца.
#
# valid_until карточки гейтит НАЖАТИЕ (repository._record_owner_decision) и
# ДОСТАВКУ (outbox) — но не смысл уже принятого решения: безопасность мутации
# держат live review + precondition digest, а не возраст карточки. Поэтому
# дедлайн исполнения — max(valid_until, одобрение + этот запас): одобренное
# при живой карточке задание не умирает из-за медленной очереди, доставки или
# транзиентных ретраев. 6 часов — потолок «конвейер обязан успеть»; дальше
# намерение считается устаревшим и закрывается честным PROPOSAL_EXPIRED.
OWNER_EXECUTION_APPROVAL_TTL = timedelta(hours=6)


class OwnerActionExecutionError(RuntimeError):
    """Fail-closed ошибка owner executor до provider mutation."""


@dataclass(frozen=True, slots=True)
class ActionRun:
    proposal_id: str
    decision_id: str
    job_id: str
    claim_id: str | None
    state: str
    reason_code: str
    safety_review: ActionReview | None
    execution: ActionExecution | None
    provider_mutation_count: int
    reconciliation_required: bool
    safety_reviews: tuple[ActionReview, ...] = ()
    executions: tuple[ActionExecution, ...] = ()


def _repository() -> OwnerActionRepository:
    return _default_repository()


def _clock(explicit: datetime | None) -> datetime:
    current = explicit or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    return current.astimezone(timezone.utc)


def _monotonic() -> float:
    """Монотонные секунды бюджета. Отдельной функцией — её подменяют тесты.

    Бюджет считается по РЕАЛЬНОМУ времени, а не по переданному ``now``: ``now``
    в этом контуре детерминированный (им подписываются манифесты и события), а
    защищаться надо именно от настоящего зависшего провайдера.
    """

    return time.monotonic()


def _budget_expired(deadline_monotonic: float | None) -> bool:
    return deadline_monotonic is not None and _monotonic() >= deadline_monotonic


def _execution_deadline(proposal: ProposalView) -> datetime:
    """Момент, после которого одобренное задание исполнять уже нельзя."""

    deadline = proposal.valid_until
    if proposal.approved_at is not None:
        deadline = max(deadline, proposal.approved_at + OWNER_EXECUTION_APPROVAL_TTL)
    return deadline


def _require_exact_lineage(proposal: ProposalView) -> tuple[str, str]:
    decision_id = proposal.active_decision_id
    job_id = proposal.active_job_id
    if not decision_id or not job_id:
        raise OwnerActionExecutionError("OWNER_APPROVAL_LINEAGE_MISSING")
    return decision_id, job_id


def _fresh_manifest_for_target(
    proposal: ProposalView,
    target: ProposedTarget,
    *,
    now: datetime,
) -> ActionManifest:
    """Свежий технический manifest: одобренное намерение + ЖИВОЕ состояние.

    ``intended_payload`` — это НЕ манифест (кроме LAUNCH, где продюсер кладёт
    сериализованный manifest целиком). Для PAUSE/UNPAUSE/SCALE там лежит
    бизнес-намерение (``ad_id``, ожидаемые статусы, причина, бюджет), и manifest
    из него именно СОБИРАЕТСЯ по живому инвентарю и живым фактам FB/AMO/CDP —
    «восстановить» его из намерения нельзя, там нет ни digest инвентаря, ни
    статусов соседей, ни доказательств.
    """

    if target not in proposal.targets:
        raise OwnerActionExecutionError("TARGET_NOT_IN_PROPOSAL")
    if canonical_sha256(target.intended_payload) != target.intended_payload_sha256:
        raise OwnerActionExecutionError("TARGET_PAYLOAD_HASH_DRIFT")
    expected_type = _MANIFEST_TYPE_BY_PROPOSAL[proposal.proposal_kind]
    payload = dict(target.intended_payload)
    if is_serialized_manifest(payload):
        try:
            manifest = manifest_from_payload(payload, expected_type)
        except (KeyError, TypeError, ValueError) as exc:
            raise OwnerActionExecutionError("ACTION_MANIFEST_INVALID") from exc
    elif proposal.proposal_kind not in _LIVE_BUILDABLE_KINDS:
        # ASSET_RECOVERY фабрики манифеста не имеет: исполнять нечего, но это
        # не «битый манифест» — код обязан быть отличимым.
        raise OwnerActionExecutionError("ACTION_KIND_NOT_EXECUTABLE")
    else:
        try:
            manifest = build_live_manifest(
                proposal.proposal_kind,
                payload,
                origin=action_origin_for(proposal.plan.origin),
                idempotency_key=proposal.plan.idempotency_key,
                now=now,
            )
        except LiveManifestError as exc:
            # Три разных исхода и три разных кода: дрейф живого состояния —
            # честная терминальная остановка, недоступность источника — повтор без
            # траты потолка попыток, всё прочее — баг сборки (тоже повтор).
            if exc.stale:
                prefix = "LIVE_STATE_DRIFT"
            elif exc.unavailable:
                prefix = _SOURCE_UNAVAILABLE_REASON
            else:
                prefix = _RETRYABLE_BLOCK_REASON
            raise OwnerActionExecutionError(f"{prefix}:{exc.code}") from exc
        if type(manifest) is not expected_type:  # pragma: no cover - карта закрыта
            raise OwnerActionExecutionError("ACTION_MANIFEST_INVALID:TYPE_MISMATCH")
    if manifest.idempotency_key != proposal.plan.idempotency_key:
        raise OwnerActionExecutionError("ACTION_MANIFEST_LINEAGE_DRIFT")
    # Proposal фиксирует параметры, а новый технический manifest получает новый TTL.
    return replace(manifest, prepared_at=now)


def _build_fresh_action_manifest_for_target(
    proposal: ProposalView,
    target: ProposedTarget,
    live: EvidenceBundle,
    *,
    now: datetime,
    prebuilt: ActionManifest | None = None,
) -> ActionManifest:
    """Проверяет живую сводку и отдаёт short-lived manifest этого прохода.

    ``prebuilt`` передаётся исполнителем: манифест уже собран по живому
    состоянию до открытия provider-сессии, и второй проход по FB/AMO/CDP дал бы
    не «проверку дрейфа», а лишний live-запрос и лишний шанс разъехаться.
    Дрейф ловят проверки после: факты манифеста сверяются с живой сводкой в
    evaluate_action_item, а precondition digest — с evidence_state_sha256.
    """

    checked_at = _clock(now)
    if live.loaded_at.tzinfo is None or live.loaded_at.utcoffset() is None:
        raise OwnerActionExecutionError("LIVE_EVIDENCE_TIME_INVALID")
    if not live.sources:
        raise OwnerActionExecutionError(
            f"{_SOURCE_UNAVAILABLE_REASON}:LIVE_EVIDENCE_EMPTY"
        )
    unavailable = tuple(
        source.source.value
        for source in live.sources
        if not source.complete or source.from_cache
    )
    if unavailable:
        # Мутировать без полной живой сводки нельзя — это граница безопасности и
        # она остаётся закрытой. Но недоступный источник больше НЕ сжигает
        # одобрение: причина отдельная и возвращает задание в повтор.
        raise OwnerActionExecutionError(
            f"{_SOURCE_UNAVAILABLE_REASON}:LIVE_EVIDENCE_INCOMPLETE_{'_'.join(unavailable)}"
        )
    if prebuilt is not None:
        return replace(prebuilt, prepared_at=checked_at)
    return _fresh_manifest_for_target(proposal, target, now=checked_at)


def build_fresh_action_manifest(
    proposal: ProposalView,
    live: EvidenceBundle,
    *,
    now: datetime,
) -> ActionManifest:
    """Публичный single-target contract; multi-target выбирает claim executor."""

    if len(proposal.targets) != 1:
        raise OwnerActionExecutionError("CLAIM_ID_REQUIRED_FOR_MULTI_TARGET")
    return _build_fresh_action_manifest_for_target(
        proposal,
        proposal.targets[0],
        live,
        now=now,
    )


def _permit_manifest_document(
    proposal: ProposalView,
    target: ProposedTarget,
    *,
    now: datetime,
    expected_manifest_sha256: str,
    manifest: ActionManifest | None = None,
) -> Mapping[str, object]:
    if manifest is None:
        manifest = _fresh_manifest_for_target(proposal, target, now=now)
    actual_sha256 = manifest_sha256(manifest)
    if actual_sha256 != expected_manifest_sha256:
        raise OwnerActionExecutionError("TECHNICAL_MANIFEST_HASH_DRIFT")
    # Decode canonical bytes to a plain document accepted by the repository.
    decoded = json.loads(canonical_json(manifest).decode("utf-8"))
    if not isinstance(decoded, dict):  # pragma: no cover - dataclass invariant
        raise OwnerActionExecutionError("TECHNICAL_MANIFEST_INVALID")
    return decoded


def issue_owner_technical_permit(
    *,
    proposal_id: str,
    decision_id: str,
    job_id: str,
    claim_id: str,
    manifest_sha256: str,
    live_evidence_sha256: str,
    now: datetime,
    manifest: ActionManifest | None = None,
) -> TechnicalPermit:
    """Выдаёт one-use permit только по exact persisted APPROVE lineage.

    ``manifest`` — уже собранный по живому состоянию манифест этого прохода.
    Без него пришлось бы собирать его заново (лишний поход в FB/AMO/CDP), и
    любое движение живых цифр между сборками выглядело бы как hash drift.
    """

    issued_at = _clock(now)
    repository = _repository()
    proposal = repository.get_proposal(proposal_id)
    if proposal is None:
        raise OwnerActionExecutionError("PROPOSAL_NOT_FOUND")
    if (proposal.active_decision_id, proposal.active_job_id) != (
        decision_id,
        job_id,
    ):
        raise OwnerActionExecutionError("OWNER_APPROVAL_LINEAGE_MISMATCH")
    target = next(
        (item for item in proposal.targets if item.claim_id == claim_id),
        None,
    )
    if target is None:
        raise OwnerActionExecutionError("CLAIM_NOT_FOUND")
    document = _permit_manifest_document(
        proposal,
        target,
        now=issued_at,
        expected_manifest_sha256=manifest_sha256,
        manifest=manifest,
    )
    permit_document_sha256 = canonical_sha256(document)
    ttl = timedelta(
        seconds=300 if proposal.proposal_kind is ProposalKind.LAUNCH else 120
    )
    resource_id = (
        target.subject_id
        if target.action_kind in {"PAUSE_AD", "UNPAUSE_AD"}
        else (target.adset_id or target.subject_id)
    )
    return repository.issue_technical_permit(
        proposal_id=proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id=claim_id,
        operation_kind=target.action_kind,
        account_id=target.account_id,
        resource_id=resource_id,
        exact_payload_sha256=target.intended_payload_sha256,
        manifest=document,
        manifest_sha256=permit_document_sha256,
        live_evidence_sha256=live_evidence_sha256,
        expires_at=issued_at + ttl,
        actor="owner_action_executor",
        expected_lifecycle_version=proposal.lifecycle_version,
        now=issued_at,
    )


def consume_owner_technical_permit(
    *,
    permit_secret: str,
    exact_payload_sha256: str,
    now: datetime,
) -> ActionAttemptAttestation:
    """Фиксирует CONSUMED + attempt + ATTEMPT_STARTED до provider I/O."""

    return _repository().consume_technical_permit(
        permit_secret=permit_secret,
        exact_payload_sha256=exact_payload_sha256,
        actor="owner_action_executor",
        now=_clock(now),
    )


def _stop_state_for(reason_code: str) -> str:
    """Терминальный BLOCKED_STALE или возвращаемый в повтор EXECUTION_RETRY_WAIT."""

    if reason_code.split(":", 1)[0] in _RETRYABLE_BLOCK_REASONS:
        return LifecycleState.EXECUTION_RETRY_WAIT.value
    return LifecycleState.BLOCKED_STALE.value


def _review_denial_reason(review: ActionReview) -> str:
    """Код причины для проваленного live review.

    Отказ, который целиком объясняется нехваткой живых слотов адсета
    (и только ею), — это «повторить позже»: слот освободит чистка или
    закончившийся сосед. Любой другой отказ — прежний терминальный
    LIVE_REVIEW_DENIED. Отсутствие evidence-снимка тоже терминально:
    без него доверять составу issues нельзя.
    """

    codes = {issue.code for issue in review.issues}
    if (
        review.evidence_state_sha256 is not None
        and codes
        and codes <= _RETRYABLE_REVIEW_ISSUE_CODES
    ):
        return _CAPACITY_WAIT_REASON
    return "LIVE_REVIEW_DENIED"


def is_transient_reason(reason_code: str) -> bool:
    """True — задание упёрлось во внешнюю недоступность или в свой бюджет времени.

    Такой исход не тратит потолок попыток диспетчера: он не говорит «действие
    исполнить нельзя», он говорит «сейчас не получилось». За тем, что задание не
    висит так вечно, следит страж «одобрено, но не исполнено 30+ минут», а
    конечность гарантирует дедлайн исполнения (_execution_deadline:
    max(valid_until, одобрение + OWNER_EXECUTION_APPROVAL_TTL)) — после него
    диспетчер закрывает задание терминально одним честным сообщением.
    """

    head = reason_code.split(":", 1)[0]
    return head in {
        _SOURCE_UNAVAILABLE_REASON,
        _BUDGET_EXCEEDED_REASON,
        # Ожидание слота может длиться днями — потолок попыток диспетчера оно
        # не тратит, конечность обеспечивает дедлайн исполнения.
        _CAPACITY_WAIT_REASON,
    }


def _transport_failure_code(exc: BaseException) -> str | None:
    """Код причины для сбоя чтения источника; None — это не транспорт."""

    for error in (exc, exc.__cause__, exc.__context__):
        if error is None:
            continue
        name = type(error).__name__
        if name in _TRANSPORT_ERROR_NAMES or isinstance(error, (OSError, TimeoutError)):
            return f"{_SOURCE_UNAVAILABLE_REASON}:{name.upper()}"
    return None


def _blocked_run(
    proposal: ProposalView,
    *,
    reason_code: str,
    claim_id: str | None = None,
    review: ActionReview | None = None,
    reviews: tuple[ActionReview, ...] = (),
    executions: tuple[ActionExecution, ...] = (),
    state: str | None = None,
) -> ActionRun:
    decision_id, job_id = _require_exact_lineage(proposal)
    return ActionRun(
        proposal_id=proposal.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id=claim_id,
        state=state or LifecycleState.BLOCKED_STALE.value,
        reason_code=reason_code,
        safety_review=review,
        execution=executions[-1] if executions else None,
        provider_mutation_count=len(executions),
        reconciliation_required=False,
        safety_reviews=reviews,
        executions=executions,
    )


def _persist_blocked(
    repository: OwnerActionRepository,
    proposal: ProposalView,
    *,
    reason_code: str,
    now: datetime,
) -> tuple[ProposalView, str]:
    """Закрывает или возвращает в повтор проваленный live review.

    Раньше любой провал уводил предложение в терминальный BLOCKED_STALE, а job
    оставался в REVIEWING — то есть одобрение владельца сгорало навсегда даже
    из-за нашей же ошибки сборки манифеста. Теперь ошибка сборки уходит в
    EXECUTION_RETRY_WAIT (задание доедет после починки), а дрейф живого
    состояния по-прежнему закрывает предложение честно и терминально.
    """

    new_state = _stop_state_for(reason_code)
    repository.transition_lifecycle(
        proposal.proposal_id,
        expected_state=LifecycleState.LIVE_REVIEW.value,
        expected_version=proposal.lifecycle_version,
        new_state=new_state,
        actor="owner_action_executor",
        reason_code=reason_code,
        now=now,
    )
    refreshed = repository.get_proposal(proposal.proposal_id)
    if refreshed is None:  # pragma: no cover - immutable proposal cannot disappear
        raise OwnerActionExecutionError("PROPOSAL_DISAPPEARED")
    return refreshed, new_state


def _enter_live_review(
    repository: OwnerActionRepository,
    proposal: ProposalView,
    *,
    now: datetime,
) -> tuple[ProposalView, ClaimProgress]:
    decision_id, job_id = _require_exact_lineage(proposal)
    if proposal.state == LifecycleState.APPROVED.value:
        repository.queue_execution(
            proposal_id=proposal.proposal_id,
            decision_id=decision_id,
            job_id=job_id,
            expected_lifecycle_version=proposal.lifecycle_version,
            actor="owner_action_executor",
            now=now,
        )
        proposal = repository.get_proposal(proposal.proposal_id)
        if proposal is None:
            raise OwnerActionExecutionError("PROPOSAL_DISAPPEARED")
    progress = repository.get_claim_progress(
        proposal_id=proposal.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
    )
    if progress.state != "READY" or progress.next_claim_id is None:
        return proposal, progress
    if proposal.state in {
        LifecycleState.EXECUTION_QUEUED.value,
        LifecycleState.EXECUTION_RETRY_WAIT.value,
    }:
        repository.begin_claim_live_review(
            proposal_id=proposal.proposal_id,
            decision_id=decision_id,
            job_id=job_id,
            claim_id=progress.next_claim_id,
            expected_lifecycle_state=proposal.state,
            expected_lifecycle_version=proposal.lifecycle_version,
            actor="owner_action_executor",
            now=now,
        )
        proposal = repository.get_proposal(proposal.proposal_id)
        if proposal is None:
            raise OwnerActionExecutionError("PROPOSAL_DISAPPEARED")
    if proposal.state != LifecycleState.LIVE_REVIEW.value:
        raise OwnerActionExecutionError(f"EXECUTION_STATE_{proposal.state}")
    return proposal, repository.get_claim_progress(
        proposal_id=proposal.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
    )


def _terminal_attempt_state(execution: ActionExecution) -> str:
    if execution.result is ActionResult.CONFIRMED:
        return "CONFIRMED"
    if (
        execution.result is ActionResult.FAILED
        and not execution.remote_may_have_changed
    ):
        return "FAILED_NO_EFFECT"
    return "RECONCILE_REQUIRED"


def _target_for_claim(
    proposal: ProposalView,
    claim_id: str,
) -> ProposedTarget:
    target = next(
        (item for item in proposal.targets if item.claim_id == claim_id),
        None,
    )
    if target is None:
        raise OwnerActionExecutionError("CLAIM_NOT_FOUND")
    return target


def _aggregate_run(
    proposal: ProposalView,
    progress: ClaimProgress,
    *,
    reason_code: str,
    reviews: list[ActionReview],
    executions: list[ActionExecution],
) -> ActionRun:
    decision_id, job_id = _require_exact_lineage(proposal)
    reconciliation_required = progress.state == "RECONCILE_REQUIRED" or (
        progress.state == "IN_FLIGHT"
        and proposal.state == LifecycleState.ATTEMPT_STARTED.value
    )
    state = {
        "COMPLETE": LifecycleState.EXECUTED.value,
        "FAILED_NO_EFFECT": LifecycleState.FAILED_NO_EFFECT.value,
        "RECONCILE_REQUIRED": LifecycleState.RECONCILE_REQUIRED.value,
        "IN_FLIGHT": (
            LifecycleState.RECONCILE_REQUIRED.value
            if reconciliation_required
            else proposal.state
        ),
        "READY": proposal.state,
    }[progress.state]
    return ActionRun(
        proposal_id=proposal.proposal_id,
        decision_id=decision_id,
        job_id=job_id,
        claim_id=progress.next_claim_id,
        state=state,
        reason_code=reason_code,
        safety_review=reviews[-1] if reviews else None,
        execution=executions[-1] if executions else None,
        provider_mutation_count=len(executions),
        reconciliation_required=reconciliation_required,
        safety_reviews=tuple(reviews),
        executions=tuple(executions),
    )


def execute_owner_approved(
    proposal_id: str,
    *,
    worker_id: str,
    now: datetime | None = None,
    deadline_monotonic: float | None = None,
) -> ActionRun:
    """Исполняет одобренное владельцем действие и ставит ACTIVE-гейт запуска.

    Само исполнение — в _execute_owner_approved_claims. Поверх него для LAUNCH
    заводится durable watchdog (services.launch_verify.finalize_executed_launch):
    успех запуска считается только после живого подтверждения
    effective_status=ACTIVE в целевом адсете, а не по факту принятого CREATE.

    ``deadline_monotonic`` — бюджет времени на это задание (см. диспетчер).
    Проверяется ТОЛЬКО до выдачи permit: начатую мутацию бюджет не прерывает.

    Живые чтения задания идут внутри ``live_read_scope``: сборка манифеста,
    живая сводка gateway и precondition адаптера читают AMO/CDP с одним ``now``,
    то есть это одно наблюдение, а не три. FB-чтения кеш не затрагивает —
    детект дрейфа провайдера обязан оставаться живым.
    """

    with live_read_scope():
        run = _execute_owner_approved_claims(
            proposal_id,
            worker_id=worker_id,
            now=now,
            deadline_monotonic=deadline_monotonic,
        )
    if (
        run.state == LifecycleState.EXECUTED.value or _run_created_ads(run)
    ) and not run.reconciliation_required:
        from services.launch_verify import finalize_executed_launch

        # Ошибки постановки гейта не откатывают исполнение: без watchdog запуск
        # просто никогда не станет VERIFIED, то есть «запущено» не прозвучит.
        # Гейт ставится по СОЗДАННЫМ объявлениям, а не по агрегату: часть claim'ов
        # могла закончиться FAILED_NO_EFFECT (город пропущен), созданные соседи
        # всё равно обязаны получить контроль ACTIVE.
        finalize_executed_launch(run, now=now)
    report_execution_result(run, now=now)
    return run


def _run_created_ads(run: ActionRun) -> int:
    return sum(
        len(getattr(execution, "created_ids", ()) or ())
        for execution in (getattr(run, "executions", ()) or ())
    )


def _execution_result_text(run: ActionRun, *, proposal_kind: str) -> str:
    """Текст второго сообщения следа: сработало действие или нет (E5б)."""

    succeeded = (
        run.state == LifecycleState.EXECUTED.value and not run.reconciliation_required
    )
    if proposal_kind == "LAUNCH" and not run.reconciliation_required:
        created = _run_created_ads(run)
        executions = tuple(getattr(run, "executions", ()) or ())
        if succeeded or created:
            # «Запущено» объявляет только launch_verify после живого ACTIVE.
            # Частичный запуск (часть claim'ов FAILED_NO_EFFECT) называет, что создано и что пропущено.
            if executions:
                count = f"{created} из {len(executions)} шт."
            else:
                count = f"{run.provider_mutation_count} шт."
            text = f"🕒 Принято Facebook: объявления созданы, ждём подтверждения ACTIVE ({count})"
            skipped = [
                str(getattr(execution, "reason_code", "") or "")
                for execution in executions
                if not (getattr(execution, "created_ids", ()) or ())
            ]
            if skipped:
                text += " — пропущено: " + "; ".join(dict.fromkeys(skipped))
            return text
    if succeeded:
        return f"✅ Сработало: {proposal_kind}, цель {run.claim_id or '—'}"
    text = (
        f"❌ Не сработало: {proposal_kind}, состояние {run.state}, "
        f"причина {run.reason_code}"
    )
    # Отказ живой проверки называет, что именно не сошлось: «карточка Trello изменилась»
    # владелец должен увидеть сразу, а не искать по коду в журнале.
    reviews = getattr(run, "safety_reviews", ()) or tuple(
        review for review in (getattr(run, "safety_review", None),) if review is not None
    )
    details = [
        issue.message
        for review in reviews
        for issue in getattr(review, "issues", ())
        if getattr(issue, "message", None)
    ]
    if details:
        text += " — " + "; ".join(dict.fromkeys(details))
    return text


def _pause_goal_already_reached(proposal) -> bool:
    """Живая сверка: цель паузы уже выключена в кабинете (вне контура).

    Бывало, что одобренные паузы массово исполнялись напрямую, пока конвейер
    чинился, и очередь досылала владельцу «❌ Не сработало» по целям, которые
    давно выключены. Алерт обязан сверяться с реальностью: если реклама уже
    PAUSED/ARCHIVED — это «уже сделано», а не провал. Fail-safe: любая ошибка
    проверки возвращает False, и честный ❌ уходит как раньше.
    """
    try:
        target = proposal.targets[0]
        payload = getattr(target, "intended_payload", None) or {}
        ad_id = str(payload.get("ad_id") or "")
        if not ad_id.isdigit():
            return False
        import requests as _requests

        from services.fb_token_provider import get_fb_token

        live = _requests.get(
            f"https://graph.facebook.com/v21.0/{ad_id}",
            params={"fields": "status", "access_token": get_fb_token()},
            timeout=20,
        ).json()
        return live.get("status") in ("PAUSED", "ARCHIVED")
    except Exception:  # noqa: BLE001 — сверка не должна прятать честный провал
        return False


def report_execution_result(run: ActionRun, *, now: datetime | None = None) -> None:
    """Ставит владельцу ответ на карточку предложения: сработало или нет.

    Сбой отчёта никогда не откатывает и не прячет исполнение — только логируется.
    """
    reported_at = now or datetime.now(timezone.utc)
    if run.state == LifecycleState.EXECUTION_RETRY_WAIT.value and is_transient_reason(
        run.reason_code
    ):
        # «Источник недоступен» и «не хватило времени» — это НЕ «не сработало»:
        # задание доедет само. Врать владельцу про провал нельзя, а если задание
        # действительно застрянет — про него скажет страж «одобрено, но не
        # исполнено 30+ минут».
        logger.info(
            "owner_action_executor: %s отложено (%s) — владельцу не пишем",
            run.proposal_id,
            run.reason_code,
        )
        return
    try:
        from config import load_owner_approval_config
        from services.owner_delivery_outbox import (
            enqueue_trail_message,
            latest_proposal_message_id,
        )

        proposal = _repository().get_proposal(run.proposal_id)
        proposal_kind = (
            "UNKNOWN" if proposal is None else proposal.proposal_kind.value
        )
        rendered_text = _execution_result_text(run, proposal_kind=proposal_kind)
        if (
            rendered_text.startswith("❌")
            and proposal_kind == "PAUSE"
            and proposal is not None
            and _pause_goal_already_reached(proposal)
        ):
            rendered_text = (
                "✅ Уже выключено: цель этой паузы отключена вне очереди "
                "(проверено живым чтением кабинета) — задание закрыто как лишнее"
            )
        db_path = load_owner_approval_config().db_path
        enqueue_trail_message(
            db_path,
            trail_kind="EXECUTION_RESULT",
            dedupe_key=f"exec-result:{run.proposal_id}:{run.job_id}:{run.state}",
            rendered_text=rendered_text,
            proposal_id=run.proposal_id,
            reply_to_message_id=latest_proposal_message_id(db_path, run.proposal_id),
            now=_clock(reported_at),
        )
    except Exception as exc:  # noqa: BLE001 — след не должен ронять исполнение
        logger.warning(
            "owner_action_executor: результат %s не поставлен в очередь — %s",
            run.proposal_id,
            type(exc).__name__,
        )


def _execute_owner_approved_claims(
    proposal_id: str,
    *,
    worker_id: str,
    now: datetime | None = None,
    deadline_monotonic: float | None = None,
) -> ActionRun:
    """Последовательно исполняет untouched claims; attempted claim не повторяет."""

    checked_at = _clock(now)
    if not worker_id.strip():
        raise ValueError("worker_id должен быть непустой строкой")
    repository = _repository()
    reviews: list[ActionReview] = []
    executions: list[ActionExecution] = []

    while True:
        proposal = repository.get_proposal(proposal_id)
        if proposal is None:
            raise OwnerActionExecutionError("PROPOSAL_NOT_FOUND")
        decision_id, job_id = _require_exact_lineage(proposal)

        progress = repository.get_claim_progress(
            proposal_id=proposal_id,
            decision_id=decision_id,
            job_id=job_id,
        )
        if progress.state in {
            "COMPLETE",
            "FAILED_NO_EFFECT",
            "RECONCILE_REQUIRED",
        }:
            return _aggregate_run(
                proposal,
                progress,
                reason_code=(
                    "RECONCILIATION_REQUIRED"
                    if progress.state == "RECONCILE_REQUIRED"
                    else "ALL_CLAIMS_TERMINAL"
                ),
                reviews=reviews,
                executions=executions,
            )
        if progress.state == "IN_FLIGHT":
            return _aggregate_run(
                proposal,
                progress,
                reason_code=(
                    "ATTEMPT_ALREADY_STARTED_NO_RETRY"
                    if proposal.state == LifecycleState.ATTEMPT_STARTED.value
                    else "CLAIM_ALREADY_CLAIMED"
                ),
                reviews=reviews,
                executions=executions,
            )
        # Дедлайн проверяется ПОСЛЕ терминальных веток: уже случившееся на
        # провайдере отчитывается честно, а не маскируется протуханием. Гейт
        # закрывает только НАЧАЛО новой работы по claim.
        if checked_at >= _execution_deadline(proposal):
            raise OwnerActionExecutionError("PROPOSAL_EXPIRED")

        proposal, progress = _enter_live_review(
            repository,
            proposal,
            now=checked_at,
        )
        if progress.state != "READY" or progress.next_claim_id is None:
            return _aggregate_run(
                proposal,
                progress,
                reason_code="CLAIM_NOT_READY",
                reviews=reviews,
                executions=executions,
            )
        target = _target_for_claim(proposal, progress.next_claim_id)
        attestation: ActionAttemptAttestation | None = None
        permit: TechnicalPermit | None = None
        review: ActionReview | None = None
        execution: ActionExecution | None = None
        try:
            if _budget_expired(deadline_monotonic):
                # Времени на живой обзор уже нет: ничего не начинаем, задание
                # честно уходит в повтор — permit не выдан, мутаций не было.
                raise OwnerActionExecutionError(
                    f"{_BUDGET_EXCEEDED_REASON}:BEFORE_MANIFEST"
                )
            draft_manifest = _fresh_manifest_for_target(
                proposal,
                target,
                now=checked_at,
            )
            batch = build_action_batch(
                (draft_manifest,),
                correlation_id=f"{proposal.proposal_id}:{target.claim_id}",
                idempotency_key=draft_manifest.idempotency_key,
                now=checked_at,
            )
            if _budget_expired(deadline_monotonic):
                # Сборка манифеста съела бюджет (обычно это зависший источник).
                # Открывать provider-сессию и выдавать permit на остатке времени
                # нельзя: следующий тик начнёт с чистого листа.
                raise OwnerActionExecutionError(
                    f"{_BUDGET_EXCEEDED_REASON}:AFTER_MANIFEST_BEFORE_PERMIT"
                )
            with _open_owner_execution_session(draft_manifest, checked_at) as session:
                live = load_action_item_evidence(
                    batch,
                    draft_manifest,
                    0,
                    checked_at,
                )
                fresh_manifest = _build_fresh_action_manifest_for_target(
                    proposal,
                    target,
                    live,
                    now=checked_at,
                    prebuilt=draft_manifest,
                )
                if manifest_sha256(fresh_manifest) != manifest_sha256(draft_manifest):
                    raise OwnerActionExecutionError("TECHNICAL_MANIFEST_DRIFT")
                review = evaluate_action_item(
                    batch,
                    fresh_manifest,
                    0,
                    live,
                    checked_at,
                )
                reviews.append(review)
                if (
                    review.decision is not SafetyDecision.SAFE
                    or review.issues
                    or review.evidence_state_sha256 is None
                ):
                    denial_reason = _review_denial_reason(review)
                    blocked, blocked_state = _persist_blocked(
                        repository,
                        proposal,
                        reason_code=denial_reason,
                        now=checked_at,
                    )
                    return _blocked_run(
                        blocked,
                        claim_id=target.claim_id,
                        reason_code=denial_reason,
                        review=review,
                        reviews=tuple(reviews),
                        executions=tuple(executions),
                        state=blocked_state,
                    )
                precondition = session.read_precondition()
                if (
                    precondition.digest != review.evidence_state_sha256
                    or precondition.subject_ids != batch.subject_ids
                    or precondition.observed_at < review.checked_at
                    or len(precondition.unrelated_state_digest) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in precondition.unrelated_state_digest
                    )
                ):
                    blocked, blocked_state = _persist_blocked(
                        repository,
                        proposal,
                        reason_code="LIVE_EVIDENCE_DRIFT",
                        now=checked_at,
                    )
                    return _blocked_run(
                        blocked,
                        claim_id=target.claim_id,
                        reason_code="LIVE_EVIDENCE_DRIFT",
                        review=review,
                        reviews=tuple(reviews),
                        executions=tuple(executions),
                        state=blocked_state,
                    )
                permit = issue_owner_technical_permit(
                    proposal_id=proposal.proposal_id,
                    decision_id=decision_id,
                    job_id=job_id,
                    claim_id=target.claim_id,
                    manifest_sha256=manifest_sha256(fresh_manifest),
                    live_evidence_sha256=review.evidence_state_sha256,
                    now=checked_at,
                    manifest=fresh_manifest,
                )
                try:
                    attestation = consume_owner_technical_permit(
                        permit_secret=permit.secret,
                        exact_payload_sha256=target.intended_payload_sha256,
                        now=checked_at,
                    )
                except OwnerActionPermitUnavailable:
                    current = repository.get_proposal(proposal_id)
                    if current is None:
                        raise OwnerActionExecutionError("PROPOSAL_DISAPPEARED")
                    reason = (
                        PermitRetirementReason.PERMIT_EXPIRED
                        if checked_at >= permit.expires_at
                        else PermitRetirementReason.PERMIT_REVOKED
                    )
                    repository.retire_unattempted_permit(
                        permit_id=permit.permit_id,
                        expected_lifecycle_version=current.lifecycle_version,
                        reason=reason,
                        actor=f"owner_action_executor:{worker_id}",
                        now=checked_at,
                    )
                    retried = repository.get_proposal(proposal_id)
                    if retried is None:
                        raise OwnerActionExecutionError("PROPOSAL_DISAPPEARED")
                    retry_progress = repository.get_claim_progress(
                        proposal_id=proposal_id,
                        decision_id=decision_id,
                        job_id=job_id,
                    )
                    return _aggregate_run(
                        retried,
                        retry_progress,
                        reason_code=reason.value,
                        reviews=reviews,
                        executions=executions,
                    )
                execution = session.execute_attempt(attestation)
            execution = session.finalize_after_scope(execution)
        except (OwnerActionLifecycleConflict, OwnerActionRepositoryError):
            raise
        except OwnerActionExecutionError as exc:
            if permit is None:
                current = repository.get_proposal(proposal_id)
                if (
                    current is not None
                    and current.state == LifecycleState.LIVE_REVIEW.value
                ):
                    blocked, blocked_state = _persist_blocked(
                        repository,
                        current,
                        reason_code=str(exc),
                        now=checked_at,
                    )
                    return _blocked_run(
                        blocked,
                        claim_id=target.claim_id,
                        reason_code=str(exc),
                        review=review,
                        reviews=tuple(reviews),
                        executions=tuple(executions),
                        state=blocked_state,
                    )
            raise
        except Exception as exc:
            if attestation is not None:
                if execution is None:
                    execution = ActionExecution(
                        attempt_id=attestation.attempt_id,
                        item_id=target.claim_id,
                        item_index=target.ordinal,
                        action_manifest_id=target.claim_id,
                        result=ActionResult.UNKNOWN,
                        started_at=attestation.consumed_at,
                        completed_at=checked_at,
                        created_ids=(),
                        reason_code="POST_ATTEMPT_UNKNOWN",
                        remote_may_have_changed=True,
                    )
                executions.append(execution)
                transition = repository.transition_attempt(
                    attestation.attempt_id,
                    expected_state="ATTEMPT_STARTED",
                    new_state="RECONCILE_REQUIRED",
                    actor=f"owner_action_executor:{worker_id}",
                    provider_result={"reason_code": "POST_ATTEMPT_UNKNOWN"},
                    reason_code="POST_ATTEMPT_UNKNOWN",
                    now=checked_at,
                )
                current = repository.get_proposal(proposal_id)
                if current is None:
                    raise OwnerActionExecutionError("PROPOSAL_DISAPPEARED") from exc
                progress = repository.get_claim_progress(
                    proposal_id=proposal_id,
                    decision_id=decision_id,
                    job_id=job_id,
                )
                if transition.aggregate_state != LifecycleState.RECONCILE_REQUIRED.value:
                    raise OwnerActionExecutionError(
                        "RECONCILIATION_STATE_MISMATCH"
                    ) from exc
                return _aggregate_run(
                    current,
                    progress,
                    reason_code="POST_ATTEMPT_UNKNOWN",
                    reviews=reviews,
                    executions=executions,
                )
            # Таймаут/обрыв чтения источника — не «отказ live review», а чужая
            # недоступность: одобрение остаётся в силе, задание уходит в повтор.
            #
            # Исходное исключение обязано попасть в лог: терминальный
            # BLOCKED_STALE/LIVE_REVIEW_FAILED без причины не диагностируется
            # вообще (так волны запусков уже ложились «молча»).
            logger.warning(
                "owner_execution: live review %s упал — %s: %s",
                proposal_id,
                type(exc).__name__,
                exc,
            )
            failure_reason = _transport_failure_code(exc) or "LIVE_REVIEW_FAILED"
            if permit is None:
                current = repository.get_proposal(proposal_id)
                if (
                    current is not None
                    and current.state == LifecycleState.LIVE_REVIEW.value
                ):
                    blocked, blocked_state = _persist_blocked(
                        repository,
                        current,
                        reason_code=failure_reason,
                        now=checked_at,
                    )
                    return _blocked_run(
                        blocked,
                        claim_id=target.claim_id,
                        reason_code=failure_reason,
                        review=review,
                        reviews=tuple(reviews),
                        executions=tuple(executions),
                        state=blocked_state,
                    )
            raise OwnerActionExecutionError(failure_reason) from exc

        if execution is None or attestation is None:  # pragma: no cover - scope invariant
            raise OwnerActionExecutionError("ATTEMPT_RESULT_MISSING")
        terminal_state = _terminal_attempt_state(execution)
        executions.append(execution)
        transition = repository.transition_attempt(
            attestation.attempt_id,
            expected_state="ATTEMPT_STARTED",
            new_state=terminal_state,
            actor=f"owner_action_executor:{worker_id}",
            provider_result={
                "result": execution.result.value,
                "created_ids": execution.created_ids,
                "reason_code": execution.reason_code,
                "remote_may_have_changed": execution.remote_may_have_changed,
            },
            reason_code=execution.reason_code,
            now=checked_at,
        )
        if transition.aggregate_state == LifecycleState.RECONCILE_REQUIRED.value:
            current = repository.get_proposal(proposal_id)
            if current is None:
                raise OwnerActionExecutionError("PROPOSAL_DISAPPEARED")
            progress = repository.get_claim_progress(
                proposal_id=proposal_id,
                decision_id=decision_id,
                job_id=job_id,
            )
            return _aggregate_run(
                current,
                progress,
                reason_code=execution.reason_code,
                reviews=reviews,
                executions=executions,
            )


# ---------------------------------------------------------------------------
# Очередь исполнения одобренных заданий (крон _cron_owner_execution).
#
# Кнопка «Одобрить» пишет решение и ставит job в owner_execution_jobs со
# state='QUEUED' (owner_action_repository._record_owner_decision), но сама
# ничего не исполняет. Диспетчер ниже — единственный, кто регулярно забирает
# такие задания и дёргает execute_owner_approved. Без него одобрение уходит в
# пустоту: карточка «Одобрено», а в Facebook ничего не происходит.
#
# Fail-closed: сбой одного задания не роняет проход и не теряет задание; после
# потолка попыток задание помечается терминальным FAILED и владелец получает
# критический алерт — «сработало» при этом не объявляется никогда.
# ---------------------------------------------------------------------------

# Аренда задания на один прогон: заведомо больше самого долгого исполнения
# (live review + мутация), но меньше, чем терпение владельца.
# 1800: задания клеймятся пачкой в начале прогона, а прогон теперь живёт до
# 25 минут — аренда обязана переживать ХВОСТ прогона, иначе реаниматор
# перехватит ещё исполняемое задание.
OWNER_EXECUTION_LEASE_SECONDS = 1800
# Потолок попыток диспетчера по одному заданию (не путать с
# owner_execution_jobs.attempts — там считаются provider-попытки по claim).
OWNER_EXECUTION_MAX_ATTEMPTS = 3
# Пауза перед повтором после 1-й и 2-й неудачной попытки.
OWNER_EXECUTION_BACKOFF_SECONDS = (300, 900)
# «Одобрено, но не исполнено» — с какого возраста задание считается зависшим.
# 90, а не 30: при пакетном одобрении хвост очереди легально ждёт 1–1.5 часа
# (тёплый скоуп, ~10 заданий за прогон). Настоящее залипание 90 минут тоже
# поймает — это порог тревоги, а не потолок исполнения.
OWNER_EXECUTION_STALL_MINUTES = 90
# Повторный алерт по тому же зависшему заданию — не чаще, чем раз в N часов.
# 48, а не 6: первый алерт уходит мгновенно и этого достаточно для реакции,
# а шестичасовые повторы по заданиям, чьи цели уже исполнены в обход (реклама
# создана прямым Graph-путём), сутками бомбили владельца без
# новой информации. TTL предложения (48ч) закрывает такие задания раньше,
# чем случится второй повтор.
OWNER_EXECUTION_STALL_REALERT_HOURS = 48
# Бюджет времени на ОДИН прогон очереди. Больше интервала крона (3 минуты) —
# осознанно: APScheduler при живом прогоне просто пропускает тик (max_instances
# = 1), и прогон фактически идёт раз в ~6 минут. Это дешевле, чем прежний
# режим, где прогон обрывался ДО завершения первого задания и очередь не
# двигалась вообще. Ключевое требование — прогон обязан ЗАВЕРШАТЬСЯ, а не
# укладываться в тик.
# 1500 (25 мин) вместо 330: владелец одобряет ПАЧКАМИ из дайджеста (10–30 за
# раз), а прогон на 330с успевал ~2 задания — пачка рассасывалась 7 часов, и
# сторож «одобрено, но не исполнено» гарантированно будил владельца на хвосте
# каждой пачки. Теперь прогон занимает почти весь 30-минутный интервал
# и съедает пачку за 1–3 прогона.
OWNER_EXECUTION_RUN_BUDGET_SECONDS = 1500
# Бюджет на одно задание. Проверяется только ДО выдачи permit: начатую мутацию
# бюджет не прерывает никогда.
#
# Почему 120, а не 45. Замер по одобренной паузе:
# живой сбор фактов идёт ТРИЖДЫ за задание (сборка манифеста, живая сводка
# gateway, precondition адаптера), и Facebook читается заново каждый раз —
# 15.5с + 15.5с + 20.1с = 51с только на него (AMO и CDP переиспользуются внутри
# live_read_scope: 0.7с и 8с на первом проходе, 0.0с дальше). В 45 секунд это не
# помещалось никогда: дедлайн истекал на третьем проходе, следующий AMO-запрос
# падал с AMO_REQUEST_BUDGET_EXPIRED, и задание уходило в повтор с диагнозом
# «AMO:Timeout» — при том что сам AMO отвечает за 0.7с. Кешировать Facebook в
# scope нельзя: три чтения — это и есть защита от дрейфа между решением и
# мутацией, общий снимок сделал бы проверку самоподтверждающейся.
#
# Почему 300, а не 120. Замер после фикса дневного окна (полные
# сутки кабинета): первый полный сбор доказательств стоит ~110с холодным —
# FACEBOOK 30.3с + AMO 1.3с + CDP 77.8с — плюс два повторных чтения Facebook
# по ~30с. Итого ~170с на первое задание прогона; 120с обрывали его на
# последних метрах, и очередь стояла с диагнозом LIVE_EVIDENCE_INCOMPLETE.
OWNER_EXECUTION_JOB_BUDGET_SECONDS = 300
# С меньшим остатком новое задание не начинаем — оно всё равно не успеет.
OWNER_EXECUTION_MIN_SLICE_SECONDS = 10
# Бюджет чтений AMO внутри задания. Дефолт интеграции (connect 10 / read 60,
# 3 попытки) держит одну страницу до 3.5 минут, а таких чтений в задании
# несколько — это и «съедало» весь прогон.
OWNER_EXECUTION_AMO_CONNECT_SECONDS = 5.0
OWNER_EXECUTION_AMO_READ_SECONDS = 20.0
OWNER_EXECUTION_AMO_ATTEMPTS = 1
# Транзиентный исход (недоступный источник, исчерпанный бюджет) повторяем скоро:
# ситуация меняется сама, ждать 5/15 минут смысла нет.
OWNER_EXECUTION_TRANSIENT_BACKOFF_SECONDS = 60
# ...но только пока неудача одиночная. Фиксированные 60 секунд при интервале
# крона в 3 минуты означают, что залипшее задание eligible на КАЖДОМ тике, а
# claim идёт ORDER BY created_at LIMIT 10 — то есть самые старые задания вечно
# занимают голову очереди и съедают весь бюджет прогона. Так и выходило:
# несколько PAUSE, падавших на таймауте AMO, часами не пускали к провайдеру ни
# один одобренный запуск (attempts у них остаётся 0, потолок не тратится).
# Лестница отодвигает ХРОНИЧЕСКИ недоступное задание, освобождая голову живым:
# сама ситуация «источник лежит долго» уже не «повторим через минуту».
OWNER_EXECUTION_TRANSIENT_BACKOFF_LADDER_SECONDS = (60, 300, 900, 1800)
# С этой серии отказов источника подряд задание уступает очередь свежим.
OWNER_EXECUTION_DEPRIORITIZE_STREAK = 2
# Задание живёт в этих состояниях, пока диспетчер не сдвинул его дальше.
#
# REVIEWING здесь не «в работе», а восстановление зависших: воркер, умерший
# посреди live review, оставлял задание в REVIEWING навсегда — его никто больше
# не забирал. Аренда (lease_until) закрывает живого воркера, поэтому забрать
# такое задание можно только после её истечения.
_DISPATCHABLE_JOB_STATES = ("QUEUED", "WAITING_RETRY", "REVIEWING")
# Lifecycle-состояния, из которых исполнение ещё возможно.
_DISPATCHABLE_LIFECYCLE_STATES = (
    LifecycleState.APPROVED.value,
    LifecycleState.EXECUTION_QUEUED.value,
    LifecycleState.EXECUTION_RETRY_WAIT.value,
    LifecycleState.LIVE_REVIEW.value,
)
# Задание уже одобрено, но до провайдера ещё не дошло — сюда смотрит страж.
_STALLABLE_JOB_STATES = ("QUEUED", "WAITING_RETRY", "REVIEWING", "PERMIT_ISSUED")
# ...и только пока lifecycle не закрыт: терминальные фазы страж не тревожит.
_STALLABLE_LIFECYCLE_STATES = (
    LifecycleState.APPROVED.value,
    LifecycleState.EXECUTION_QUEUED.value,
    LifecycleState.EXECUTION_RETRY_WAIT.value,
    LifecycleState.LIVE_REVIEW.value,
    LifecycleState.PERMIT_ISSUED.value,
)
# Бухгалтерия по заданию хранится не вечно: старше — выметается из state-файла.
OWNER_EXECUTION_STATE_TTL_DAYS = 30

# Счётчик попыток и дедуп алертов живут в state-файле рядом с прочими кронами:
# схема 022/023 намеренно не заводит место под диспетчерскую бухгалтерию, а
# гарантии «не исполнить дважды» держит сама БД (permit/attestation/CAS).
_DISPATCH_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "owner_execution_dispatch.json"
)


@dataclass(frozen=True, slots=True)
class OwnerExecutionQueueRun:
    """Итог одного прохода диспетчера одобренных заданий."""

    claimed: int = 0
    executed: int = 0
    retried: int = 0
    exhausted: int = 0
    stall_alerts: int = 0
    errors: tuple[str, ...] = ()
    # Задания, которые прогон не стал начинать из-за исчерпанного бюджета
    # времени: аренда снята, повтор — следующим тиком.
    deferred: int = 0
    # Задания, закрытые терминально из-за истёкшего дедлайна исполнения:
    # без ретраев, одним честным сообщением владельцу.
    expired: int = 0


@dataclass(frozen=True, slots=True)
class ProposalSweepRun:
    """Итог одного прохода свипера протухших предложений."""

    expired: int = 0
    tokens_revoked: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()


def _queue_db_path(db_path: str | Path | None = None) -> str:
    """Тот же файл БД, который мутирует execute_owner_approved."""

    if db_path is not None:
        return str(db_path)
    from agent import database

    if database.DB_PATH is None:
        raise OwnerActionRepositoryError("БД не инициализирована")
    return str(database.DB_PATH)


def _queue_connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _load_dispatch_state(state_path: Path | None = None) -> dict[str, dict]:
    """Читает бухгалтерию диспетчера. Битый/пустой файл — не повод падать."""

    path = state_path or _DISPATCH_STATE_PATH
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "owner_execution: не удалось прочитать state — %s",
            type(exc).__name__,
        )
        return {}
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, dict):
        return {}
    return {
        str(job_id): dict(entry)
        for job_id, entry in jobs.items()
        if isinstance(entry, dict)
    }


def _save_dispatch_state(
    state: Mapping[str, Mapping[str, object]],
    state_path: Path | None = None,
) -> None:
    """Атомарная запись (уникальный temp + fsync + rename) — как у прочих кронов."""

    path = state_path or _DISPATCH_STATE_PATH
    payload = json.dumps({"jobs": dict(state)}, ensure_ascii=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        # Потеря счётчика попыток безопасна: она даёт лишние повторы, а не
        # лишние мутации — двойное исполнение закрыто permit/attempt в БД.
        logger.warning(
            "owner_execution: не удалось сохранить state — %s",
            type(exc).__name__,
        )


def _claim_execution_jobs(
    db_path: str,
    *,
    worker_id: str,
    now: datetime,
    limit: int,
    excluded_job_ids: tuple[str, ...] = (),
    deprioritized_job_ids: tuple[str, ...] = (),
) -> list[sqlite3.Row]:
    """Атомарно арендует QUEUED-задания (BEGIN IMMEDIATE + lease).

    ``deprioritized_job_ids`` — задания с серией отказов источника подряд: они идут после свежих,
    иначе три хронически ждущих задания каждый прогон сжигали по бюджету задания впереди очереди
    и свежие паузы ждали часами.
    """

    lease_token = f"{worker_id}:{uuid.uuid4()}"
    lease_until = now + timedelta(seconds=OWNER_EXECUTION_LEASE_SECONDS)
    job_states = ",".join("?" for _ in _DISPATCHABLE_JOB_STATES)
    lifecycle_states = ",".join("?" for _ in _DISPATCHABLE_LIFECYCLE_STATES)
    excluded = ",".join("?" for _ in excluded_job_ids)
    excluded_clause = f"AND j.job_id NOT IN ({excluded})" if excluded_job_ids else ""
    deprioritized = ",".join("?" for _ in deprioritized_job_ids)
    order_clause = (
        f"CASE WHEN j.job_id IN ({deprioritized}) THEN 1 ELSE 0 END, " if deprioritized_job_ids else ""
    )
    connection = _queue_connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        candidates = connection.execute(
            f"""
            SELECT j.job_id, j.proposal_id
            FROM owner_execution_jobs j
            JOIN owner_action_lifecycle l ON l.proposal_id = j.proposal_id
            WHERE j.state IN ({job_states})
              AND l.state IN ({lifecycle_states})
              AND l.active_job_id = j.job_id
              AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= ?)
              AND (j.lease_until IS NULL OR j.lease_until <= ?)
              {excluded_clause}
            ORDER BY {order_clause}j.created_at, j.job_id
            LIMIT ?
            """,
            (
                *_DISPATCHABLE_JOB_STATES,
                *_DISPATCHABLE_LIFECYCLE_STATES,
                _iso_utc(now),
                _iso_utc(now),
                *excluded_job_ids,
                *deprioritized_job_ids,
                limit,
            ),
        ).fetchall()
        claimed: list[sqlite3.Row] = []
        for candidate in candidates:
            connection.execute(
                f"""
                UPDATE owner_execution_jobs
                SET lease_token = ?, lease_until = ?
                WHERE job_id = ?
                  AND state IN ({job_states})
                  AND (lease_until IS NULL OR lease_until <= ?)
                """,
                (
                    lease_token,
                    _iso_utc(lease_until),
                    candidate["job_id"],
                    *_DISPATCHABLE_JOB_STATES,
                    _iso_utc(now),
                ),
            )
            row = connection.execute(
                """
                SELECT job_id, proposal_id, decision_id, state, created_at
                FROM owner_execution_jobs
                WHERE job_id = ? AND lease_token = ?
                """,
                (candidate["job_id"], lease_token),
            ).fetchone()
            if row is not None:
                claimed.append(row)
        connection.commit()
        return claimed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _release_execution_job(
    db_path: str,
    *,
    job_id: str,
    now: datetime,
    retry_after: datetime | None,
) -> str | None:
    """Снимает аренду и (для повтора) сдвигает next_attempt_at. Даёт state job."""

    connection = _queue_connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT state FROM owner_execution_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        job_state = None if row is None else str(row["state"])
        if retry_after is not None and job_state in _DISPATCHABLE_JOB_STATES:
            connection.execute(
                """
                UPDATE owner_execution_jobs
                SET lease_token = NULL, lease_until = NULL,
                    next_attempt_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (_iso_utc(retry_after), _iso_utc(now), job_id),
            )
        else:
            # Успех/терминальный исход: next_attempt_at уже выставлен теми, кто
            # двигал job (transition_attempt / retire_unattempted_permit).
            connection.execute(
                """
                UPDATE owner_execution_jobs
                SET lease_token = NULL, lease_until = NULL
                WHERE job_id = ?
                """,
                (job_id,),
            )
        connection.commit()
        return job_state
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _send_owner_critical(title: str, detail: str) -> None:
    """Критический алерт владельцу. Сбой канала не роняет проход."""

    try:
        from services.notifications import send_critical_alert

        send_critical_alert(title, detail)
    except Exception as exc:  # noqa: BLE001 — алерт не важнее самого исполнения
        logger.warning(
            "owner_execution: критический алерт не ушёл — %s",
            type(exc).__name__,
        )


def _report_dispatch_failure(
    proposal_id: str,
    *,
    job_id: str,
    attempts: int,
    reason: str,
    now: datetime,
) -> None:
    """Пишет владельцу в след карточки, что задание так и не исполнилось."""

    try:
        from config import load_owner_approval_config
        from services.owner_delivery_outbox import (
            enqueue_trail_message,
            latest_proposal_message_id,
        )

        db_path = load_owner_approval_config().db_path
        enqueue_trail_message(
            db_path,
            trail_kind="EXECUTION_RESULT",
            dedupe_key=f"exec-dispatch-failed:{proposal_id}:{job_id}",
            rendered_text=(
                f"❌ Не сработало: задание не исполнилось за {attempts} "
                f"попыток, причина {reason}"
            ),
            proposal_id=proposal_id,
            reply_to_message_id=latest_proposal_message_id(db_path, proposal_id),
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — след не должен ронять проход
        logger.warning(
            "owner_execution: отчёт о провале %s не поставлен в очередь — %s",
            proposal_id,
            type(exc).__name__,
        )


@contextlib.contextmanager
def _amo_read_budget(deadline_monotonic: float):
    """Короткие таймауты AMO на время исполнения одного задания.

    AMO-чтения живут в трёх местах одного задания (сборка манифеста, живая
    сводка gateway, precondition адаптера), и дефолт интеграции (read 60с × 3
    попытки) превращал зависший AMO в многоминутный прогон. Здесь бюджет ставится
    один раз на всё задание; сбой чтения дальше классифицируется как
    SOURCE_UNAVAILABLE и НЕ отменяет одобрение.

    Недоступный модуль интеграции (тесты, окружение без AMO) не должен ломать
    исполнение — тогда просто работаем без бюджета.
    """

    try:
        from integrations import amo
    except Exception as exc:  # noqa: BLE001 — бюджет не критичен для корректности
        logger.debug("owner_execution: бюджет AMO не выставлен — %s", type(exc).__name__)
        yield
        return
    budget = getattr(amo, "request_budget", None)
    if budget is None:  # pragma: no cover - старый модуль без бюджета
        yield
        return
    with budget(
        connect_seconds=OWNER_EXECUTION_AMO_CONNECT_SECONDS,
        read_seconds=OWNER_EXECUTION_AMO_READ_SECONDS,
        attempts=OWNER_EXECUTION_AMO_ATTEMPTS,
        deadline_monotonic=deadline_monotonic,
    ):
        yield


def run_owner_execution_queue(
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 10,
    db_path: str | Path | None = None,
    state_path: Path | None = None,
) -> OwnerExecutionQueueRun:
    """Один проход по одобренным заданиям: забрать QUEUED и исполнить.

    Идемпотентность держит не диспетчер, а execution boundary: повторный проход
    по тому же заданию видит уже начатую попытку (ClaimProgress=IN_FLIGHT →
    ATTEMPT_ALREADY_STARTED_NO_RETRY), а на уровне схемы вторую мутацию по тому
    же claim закрывают uq_owner_attempt_claim и одноразовый technical permit.

    Прогон ограничен во времени (OWNER_EXECUTION_RUN_BUDGET_SECONDS), каждое
    задание — своим бюджетом (OWNER_EXECUTION_JOB_BUDGET_SECONDS). Без этого один
    зависший источник держал прогон дольше интервала крона, следующие тики
    пропускались («maximum number of running instances reached») и вся очередь
    одобренных действий стояла. Задание, которое не начали или не успели довести
    до permit, возвращается в очередь (WAITING_RETRY) без потери одобрения.

    Весь проход идёт внутри одной области живых чтений: ``checked_at`` у всех
    заданий прогона общий, поэтому окно решения (и окно платежей CDP) у них
    совпадает — читать его заново на каждое задание незачем.
    """

    with live_read_scope():
        return _run_owner_execution_pass(
            worker_id=worker_id,
            now=now,
            limit=limit,
            db_path=db_path,
            state_path=state_path,
        )


def _run_owner_execution_pass(
    *,
    worker_id: str,
    now: datetime | None,
    limit: int,
    db_path: str | Path | None,
    state_path: Path | None,
) -> OwnerExecutionQueueRun:
    checked_at = _clock(now)
    if not worker_id.strip():
        raise ValueError("worker_id должен быть непустой строкой")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit должен быть в диапазоне 1..100")
    path = _queue_db_path(db_path)
    state = _load_dispatch_state(state_path)
    exhausted_ids = tuple(
        job_id
        for job_id, entry in state.items()
        if str(entry.get("state")) == "FAILED"
    )

    deprioritized_ids = tuple(
        job_id
        for job_id, entry in state.items()
        if int(entry.get("transient_streak") or 0) >= OWNER_EXECUTION_DEPRIORITIZE_STREAK
        and str(entry.get("state")) != "FAILED"
    )
    rows = _claim_execution_jobs(
        path,
        worker_id=worker_id,
        now=checked_at,
        limit=limit,
        excluded_job_ids=exhausted_ids,
        deprioritized_job_ids=deprioritized_ids,
    )
    executed = 0
    retried = 0
    exhausted = 0
    deferred = 0
    expired = 0
    errors: list[str] = []
    run_deadline = _monotonic() + OWNER_EXECUTION_RUN_BUDGET_SECONDS

    for row in rows:
        job_id = str(row["job_id"])
        proposal_id = str(row["proposal_id"])
        remaining = run_deadline - _monotonic()
        if remaining < OWNER_EXECUTION_MIN_SLICE_SECONDS:
            # Бюджет прогона исчерпан: задание даже не начинаем, снимаем аренду и
            # отдаём его следующему тику. Ничего не мутировано, одобрение цело.
            deferred += 1
            try:
                _release_execution_job(
                    path,
                    job_id=job_id,
                    now=checked_at,
                    retry_after=checked_at,
                )
            except Exception as exc:  # noqa: BLE001 — аренда протухнет сама
                errors.append(f"{proposal_id}:RELEASE_{type(exc).__name__}")
                logger.warning(
                    "owner_execution: аренда %s не снята — %s",
                    job_id,
                    exc,
                )
            logger.info(
                "owner_execution: задание %s (%s) отложено — бюджет прогона исчерпан",
                job_id,
                proposal_id,
            )
            continue
        job_deadline = min(
            _monotonic() + OWNER_EXECUTION_JOB_BUDGET_SECONDS,
            run_deadline,
        )
        entry = dict(state.get(job_id) or {})
        # Потолок считает именно неудачные диспетчерские попытки: успешный, но
        # не сдвинувший задание проход счётчик не тратит.
        previous_attempts = int(entry.get("attempts") or 0)
        attempts = previous_attempts + 1
        failure: str | None = None
        transient = False
        deadline_expired = False
        try:
            with _amo_read_budget(job_deadline):
                run = execute_owner_approved(
                    proposal_id,
                    worker_id=worker_id,
                    now=checked_at,
                    deadline_monotonic=job_deadline,
                )
        except Exception as exc:  # noqa: BLE001 — одно задание не роняет проход
            failure = f"{type(exc).__name__}: {exc}"
            transient = is_transient_reason(str(exc)) or bool(
                _transport_failure_code(exc)
            )
            deadline_expired = str(exc) == "PROPOSAL_EXPIRED"
            if not deadline_expired:
                # Протухание — штатное терминальное закрытие, а не сбой прогона:
                # в errors (и в cron-failure) оно не попадает, виден счётчик
                # expired и лог ниже.
                errors.append(f"{proposal_id}:{type(exc).__name__}")
            logger.warning(
                "owner_execution: задание %s (%s) упало — %s",
                job_id,
                proposal_id,
                exc,
            )
        else:
            logger.info(
                "owner_execution: задание %s (%s) — state=%s reason=%s",
                job_id,
                proposal_id,
                run.state,
                run.reason_code,
            )
            if run.state == LifecycleState.EXECUTION_RETRY_WAIT.value:
                # Исполнение честно вернуло задание в повтор (например, баг
                # сборки манифеста). Это неудачная попытка: без её учёта
                # диспетчер крутил бы задание вечно и молча.
                failure = f"EXECUTION_RETRY: {run.reason_code}"
                transient = is_transient_reason(run.reason_code)
                errors.append(f"{proposal_id}:{run.reason_code}")

        if deadline_expired:
            # Протухание детерминировано: дедлайн лежит в БД и повтором не
            # лечится. Никаких трёх попыток с бэкоффом и двойных алертов —
            # один терминальный переход lifecycle и один честный след.
            expired += 1
            try:
                # LIVE_REVIEW здесь — сирота умершего воркера: lease свободна
                # (мы её держим), permit не выдан, закрывать безопасно.
                closed = _finalize_expired_job(
                    path,
                    proposal_id=proposal_id,
                    job_id=job_id,
                    actor=f"owner_execution_dispatcher:{worker_id}",
                    now=checked_at,
                    allowed_states=(
                        *_EXPIRABLE_APPROVED_LIFECYCLE_STATES,
                        LifecycleState.LIVE_REVIEW.value,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — одно задание не роняет проход
                closed = False
                errors.append(f"{proposal_id}:EXPIRE_{type(exc).__name__}")
                logger.warning(
                    "owner_execution: протухшее %s не закрыто — %s",
                    proposal_id,
                    exc,
                )
            try:
                # Не закрылось из-за гонки — задание вернётся следующим тиком.
                _release_execution_job(
                    path,
                    job_id=job_id,
                    now=checked_at,
                    retry_after=None if closed else checked_at,
                )
            except Exception as exc:  # noqa: BLE001 — аренда протухнет сама
                errors.append(f"{proposal_id}:RELEASE_{type(exc).__name__}")
                logger.warning(
                    "owner_execution: аренда %s не снята — %s",
                    job_id,
                    exc,
                )
            if closed:
                state.pop(job_id, None)
            continue

        previous_transient_streak = int(entry.get("transient_streak") or 0)
        if transient:
            # Недоступный источник или исчерпанный бюджет — это «сейчас не
            # получилось», а не «исполнить нельзя». Потолок попыток на такое не
            # тратим: иначе минутный downtime AMO хоронил бы одобрение владельца.
            # Бесконечным повтор не станет: за висящим заданием придёт страж
            # 30 минут, а конечность даёт дедлайн исполнения
            # (max(valid_until, одобрение + OWNER_EXECUTION_APPROVAL_TTL)).
            attempts = previous_attempts
            # Считаем подряд идущие транзиентные неудачи отдельно от потолка:
            # они не хоронят задание, но обязаны отодвигать его от головы
            # очереди, иначе оно голодает всех, кто одобрен позже.
            transient_streak = previous_transient_streak + 1
        else:
            transient_streak = 0
        exhausted_now = (
            failure is not None
            and not transient
            and attempts >= OWNER_EXECUTION_MAX_ATTEMPTS
        )
        backoff_index = min(
            max(attempts - 1, 0),
            len(OWNER_EXECUTION_BACKOFF_SECONDS) - 1,
        )
        transient_backoff_index = min(
            max(transient_streak - 1, 0),
            len(OWNER_EXECUTION_TRANSIENT_BACKOFF_LADDER_SECONDS) - 1,
        )
        retry_after = (
            None
            if failure is None or exhausted_now
            else checked_at
            + timedelta(
                seconds=(
                    OWNER_EXECUTION_TRANSIENT_BACKOFF_LADDER_SECONDS[
                        transient_backoff_index
                    ]
                    if transient
                    else OWNER_EXECUTION_BACKOFF_SECONDS[backoff_index]
                )
            )
        )
        try:
            job_state = _release_execution_job(
                path,
                job_id=job_id,
                now=checked_at,
                retry_after=retry_after,
            )
        except Exception as exc:  # noqa: BLE001 — аренда протухнет сама
            job_state = None
            errors.append(f"{proposal_id}:RELEASE_{type(exc).__name__}")
            logger.warning(
                "owner_execution: аренда %s не снята — %s",
                job_id,
                exc,
            )

        if failure is None:
            if job_state in _DISPATCHABLE_JOB_STATES:
                # Исход неизвестен: задание осталось в очереди. Повторим на
                # следующем тике, а если так и не сдвинется — позовёт страж.
                retried += 1
                entry.update(
                    {
                        "proposal_id": proposal_id,
                        "attempts": previous_attempts,
                        "state": "RETRY",
                        "last_error": "NOT_ADVANCED",
                        "updated_at": _iso_utc(checked_at),
                    }
                )
                state[job_id] = entry
            else:
                executed += 1
                state.pop(job_id, None)
            continue

        if exhausted_now:
            exhausted += 1
            entry.update(
                {
                    "proposal_id": proposal_id,
                    "attempts": attempts,
                    "state": "FAILED",
                    "last_error": failure,
                    "updated_at": _iso_utc(checked_at),
                }
            )
            state[job_id] = entry
            _report_dispatch_failure(
                proposal_id,
                job_id=job_id,
                attempts=attempts,
                reason=failure,
                now=checked_at,
            )
            _send_owner_critical(
                "Одобренное действие не исполнено",
                (
                    f"Предложение {proposal_id} (job {job_id}) не исполнилось за "
                    f"{attempts} попыток: {failure}. Повторы остановлены — "
                    "нужна ручная проверка."
                ),
            )
            continue

        retried += 1
        entry.update(
            {
                "proposal_id": proposal_id,
                "attempts": attempts,
                "transient_streak": transient_streak,
                "state": "RETRY",
                "last_error": failure,
                "updated_at": _iso_utc(checked_at),
            }
        )
        state[job_id] = entry

    stall_alerts = _alert_stalled_jobs(
        path,
        state=state,
        now=checked_at,
    )
    _save_dispatch_state(_prune_dispatch_state(state, now=checked_at), state_path)
    return OwnerExecutionQueueRun(
        claimed=len(rows),
        executed=executed,
        retried=retried,
        exhausted=exhausted,
        stall_alerts=stall_alerts,
        errors=tuple(errors),
        deferred=deferred,
        expired=expired,
    )


def _prune_dispatch_state(
    state: dict[str, dict],
    *,
    now: datetime,
) -> dict[str, dict]:
    """Держит state-файл конечным: записи старше TTL выметаются.

    Выметенное FAILED-задание при следующем проходе будет взято заново и, если
    оно всё ещё безнадёжно, снова упрётся в потолок и снова позовёт владельца —
    это безопаснее, чем вечно растущий файл.
    """

    horizon = now - timedelta(days=OWNER_EXECUTION_STATE_TTL_DAYS)
    kept: dict[str, dict] = {}
    for job_id, entry in state.items():
        stamps = [
            entry.get("updated_at"),
            entry.get("stall_alerted_at"),
        ]
        newest: datetime | None = None
        for stamp in stamps:
            if not isinstance(stamp, str) or not stamp:
                continue
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if newest is None or parsed > newest:
                newest = parsed
        if newest is None or newest >= horizon:
            kept[job_id] = entry
    return kept


def _alert_stalled_jobs(
    db_path: str,
    *,
    state: dict[str, dict],
    now: datetime,
) -> int:
    """Страж «одобрено, но не исполнено»: критический алерт по зависшим заданиям.

    Зависшим считается задание, которое одобрено дольше
    OWNER_EXECUTION_STALL_MINUTES и до сих пор не дошло до провайдера (job так и
    не покинул очередь/ревью), а lifecycle всё ещё в дотерминальной фазе. Уже
    закрытые (BLOCKED_STALE/EXPIRED и прочие терминальные) сюда не попадают:
    по ним владелец получил ответ на карточку, повторно шуметь незачем.
    Дедуп — по отметке в state-файле.
    """

    threshold = now - timedelta(minutes=OWNER_EXECUTION_STALL_MINUTES)
    job_states = ",".join("?" for _ in _STALLABLE_JOB_STATES)
    lifecycle_states = ",".join("?" for _ in _STALLABLE_LIFECYCLE_STATES)
    connection = _queue_connect(db_path)
    try:
        rows = connection.execute(
            f"""
            SELECT j.job_id, j.proposal_id, j.state, j.created_at,
                   j.last_reason_code, l.state AS lifecycle_state
            FROM owner_execution_jobs j
            JOIN owner_action_lifecycle l ON l.proposal_id = j.proposal_id
            WHERE j.state IN ({job_states})
              AND l.state IN ({lifecycle_states})
              AND j.created_at <= ?
            ORDER BY j.created_at
            """,
            (
                *_STALLABLE_JOB_STATES,
                *_STALLABLE_LIFECYCLE_STATES,
                _iso_utc(threshold),
            ),
        ).fetchall()
    finally:
        connection.close()

    sent = 0
    for row in rows:
        job_id = str(row["job_id"])
        entry = dict(state.get(job_id) or {})
        alerted_raw = entry.get("stall_alerted_at")
        if isinstance(alerted_raw, str) and alerted_raw:
            try:
                alerted_at = datetime.fromisoformat(alerted_raw)
            except ValueError:
                alerted_at = None
            if alerted_at is not None and alerted_at.tzinfo is not None:
                age = now - alerted_at
                if age < timedelta(hours=OWNER_EXECUTION_STALL_REALERT_HOURS):
                    continue
        waiting_minutes = _waiting_minutes(row["created_at"], now)
        _send_owner_critical(
            "Одобрено, но не исполнено",
            (
                f"Предложение {row['proposal_id']} одобрено "
                f"{waiting_minutes} мин назад, задание {job_id} всё ещё "
                f"{row['state']} (lifecycle {row['lifecycle_state']}, "
                f"причина {row['last_reason_code'] or '—'}). "
                "Проверь крон _cron_owner_execution."
            ),
        )
        entry.update(
            {
                "proposal_id": str(row["proposal_id"]),
                "stall_alerted_at": _iso_utc(now),
            }
        )
        entry.setdefault("attempts", 0)
        entry.setdefault("state", "RETRY")
        state[job_id] = entry
        sent += 1
    return sent


def _waiting_minutes(created_at: object, now: datetime) -> int:
    try:
        created = datetime.fromisoformat(str(created_at))
    except ValueError:
        return 0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0, int((now - created).total_seconds() // 60))


# Состояния lifecycle, из которых протухшее ОДОБРЕННОЕ закрывается напрямую.
# LIVE_REVIEW / PERMIT_ISSUED / ATTEMPT_STARTED не трогаем: там владеет
# исполнитель, и мутация провайдера могла уже начаться.
_EXPIRABLE_APPROVED_LIFECYCLE_STATES = (
    LifecycleState.APPROVED.value,
    LifecycleState.EXECUTION_QUEUED.value,
    LifecycleState.EXECUTION_RETRY_WAIT.value,
)


def _finalize_expired_job(
    db_path: str,
    *,
    proposal_id: str,
    job_id: str,
    actor: str,
    now: datetime,
    allowed_states: tuple[str, ...] = _EXPIRABLE_APPROVED_LIFECYCLE_STATES,
) -> bool:
    """Терминально закрывает протухшее одобренное задание одним переходом.

    lifecycle → EXPIRED выводит задание сразу отовсюду: из клейма диспетчера
    (клейм требует живой lifecycle), из стража зависаний (терминальные фазы он
    не тревожит) и из дедупа producer'ов. Job переводится в терминальную пару
    той же транзакцией (_sync_execution_job_state). След владельцу идёт тем же
    дедуп-ключом, что и провал диспетчера: по заданию, уже получившему
    «❌ … за N попыток», повторного сообщения не будет.

    ``allowed_states`` — из каких lifecycle-состояний закрытие легально.
    Диспетчер, держащий lease задания, добавляет сюда LIVE_REVIEW: живого
    воркера в этом состоянии при свободной аренде быть не может (permit не
    выдан), а отказ закрывать сироту зациклил бы клейм на каждом тике.
    """

    repository = OwnerActionRepository(db_path)
    proposal = repository.get_proposal(proposal_id)
    if proposal is None or proposal.state not in allowed_states:
        return False
    try:
        expected_state = proposal.state
        expected_version = proposal.lifecycle_version
        if expected_state == LifecycleState.LIVE_REVIEW.value:
            # Прямого перехода LIVE_REVIEW → EXPIRED у матрицы (триггер БД)
            # нет: сироту сначала легально возвращаем в повтор, затем закрываем.
            expected_version = repository.transition_lifecycle(
                proposal_id,
                expected_state=expected_state,
                expected_version=expected_version,
                new_state=LifecycleState.EXECUTION_RETRY_WAIT.value,
                actor=actor,
                reason_code="PROPOSAL_EXPIRED",
                now=now,
            )
            expected_state = LifecycleState.EXECUTION_RETRY_WAIT.value
        repository.transition_lifecycle(
            proposal_id,
            expected_state=expected_state,
            expected_version=expected_version,
            new_state=LifecycleState.EXPIRED.value,
            actor=actor,
            reason_code="PROPOSAL_EXPIRED",
            now=now,
        )
    except (
        OwnerActionLifecycleConflict,
        OwnerActionRepositoryError,
        sqlite3.IntegrityError,
    ) as exc:
        logger.warning(
            "owner_execution: протухшее одобренное %s не закрыто — %s",
            proposal_id,
            type(exc).__name__,
        )
        return False
    _revoke_proposal_tokens(db_path, proposal_id=proposal_id, now=now)
    try:
        from config import load_owner_approval_config
        from services.owner_delivery_outbox import (
            enqueue_trail_message,
            latest_proposal_message_id,
        )

        trail_db = load_owner_approval_config().db_path
        enqueue_trail_message(
            trail_db,
            trail_kind="EXECUTION_RESULT",
            dedupe_key=f"exec-dispatch-failed:{proposal_id}:{job_id}",
            rendered_text=(
                "⌛ Не исполнено: карточка устарела до исполнения — действие в "
                "Facebook не выполнено. Свежая карточка придёт со следующим "
                "прогоном."
            ),
            proposal_id=proposal_id,
            reply_to_message_id=latest_proposal_message_id(trail_db, proposal_id),
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — след не должен ронять закрытие
        logger.warning(
            "owner_execution: след протухшего %s не поставлен в очередь — %s",
            proposal_id,
            type(exc).__name__,
        )
    return True


def sweep_expired_approved_jobs(
    *,
    now: datetime | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> ProposalSweepRun:
    """Гасит одобренные задания, чей дедлайн исполнения вышел.

    Дедлайн тот же, что у исполнителя (_execution_deadline):
    max(valid_until, одобрение + OWNER_EXECUTION_APPROVAL_TTL), поэтому
    свежеодобренные с ещё живой отсрочкой не трогаются — valid_until здесь
    только предфильтр. Закрытие терминальное и с одним честным следом:
    задание не доживает до дожигания диспетчерских попыток.
    """

    checked_at = _clock(now)
    path = _queue_db_path(db_path)
    placeholders = ",".join("?" for _ in _EXPIRABLE_APPROVED_LIFECYCLE_STATES)
    connection = _queue_connect(path)
    try:
        rows = connection.execute(
            f"""
            SELECT l.proposal_id, l.active_job_id, p.valid_until,
                   d.recorded_at AS approved_at
            FROM owner_action_lifecycle l
            JOIN owner_action_proposals p USING (proposal_id)
            LEFT JOIN owner_action_decisions d
              ON d.decision_id = l.active_decision_id
             AND d.decision_kind = 'APPROVE'
            WHERE l.state IN ({placeholders})
              AND p.valid_until <= ?
            ORDER BY p.valid_until
            LIMIT ?
            """,
            (
                *_EXPIRABLE_APPROVED_LIFECYCLE_STATES,
                _iso_utc(checked_at),
                limit,
            ),
        ).fetchall()
    finally:
        connection.close()

    expired = 0
    skipped = 0
    errors: list[str] = []
    for row in rows:
        proposal_id = str(row["proposal_id"])
        deadline = _parse_stored_utc(row["valid_until"])
        approved_at = _parse_stored_utc(row["approved_at"])
        if deadline is None:
            skipped += 1
            continue
        if approved_at is not None:
            deadline = max(deadline, approved_at + OWNER_EXECUTION_APPROVAL_TTL)
        if deadline > checked_at:
            skipped += 1
            continue
        try:
            closed = _finalize_expired_job(
                path,
                proposal_id=proposal_id,
                job_id=str(row["active_job_id"] or ""),
                actor="owner_execution_sweeper",
                now=checked_at,
            )
        except Exception as exc:  # noqa: BLE001 — одно предложение не роняет проход
            errors.append(f"{proposal_id}:{type(exc).__name__}")
            logger.warning(
                "owner_execution: протухшее одобренное %s не закрыто — %s",
                proposal_id,
                exc,
            )
            continue
        if closed:
            expired += 1
        else:
            skipped += 1
    return ProposalSweepRun(
        expired=expired,
        tokens_revoked=0,
        skipped=skipped,
        errors=tuple(errors),
    )


def _parse_stored_utc(value: object) -> datetime | None:
    """Метка времени из БД → aware UTC; битое значение — None, не крэш."""

    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# Недорешённые состояния, в которых протухшая карточка «ждёт владельца» и
# гасится свипером по valid_until: PENDING_OWNER (на руках), DELIVERY_PENDING
# (так и не доехала до Telegram), POSTPONED (отложена и не переизбрана).
# Все три «живые» для дедупа producer'ов — не закрывать их значит навсегда
# блокировать новые карточки по тому же объекту.
_SWEEPABLE_UNDECIDED_LIFECYCLE_STATES = (
    LifecycleState.PENDING_OWNER.value,
    LifecycleState.DELIVERY_PENDING.value,
    LifecycleState.POSTPONED.value,
)


def list_stale_proposals(
    *,
    now: datetime | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
    states: tuple[str, ...] = _SWEEPABLE_UNDECIDED_LIFECYCLE_STATES,
) -> list[sqlite3.Row]:
    """Предложения, чей TTL уже вышел, а lifecycle всё ещё ждёт владельца."""

    checked_at = _clock(now)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit должен быть в диапазоне 1..200")
    if not states:
        raise ValueError("states должен быть непустым")
    placeholders = ",".join("?" for _ in states)
    connection = _queue_connect(_queue_db_path(db_path))
    try:
        return connection.execute(
            f"""
            SELECT l.proposal_id, l.state, l.version, p.valid_until, p.summary
            FROM owner_action_lifecycle l
            JOIN owner_action_proposals p USING (proposal_id)
            WHERE l.state IN ({placeholders})
              AND p.valid_until <= ?
            ORDER BY p.valid_until
            LIMIT ?
            """,
            (*states, _iso_utc(checked_at), limit),
        ).fetchall()
    finally:
        connection.close()


def sweep_expired_proposals(
    *,
    now: datetime | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
    states: tuple[str, ...] = _SWEEPABLE_UNDECIDED_LIFECYCLE_STATES,
) -> ProposalSweepRun:
    """Гасит протухшие недорешённые предложения: → EXPIRED + отзыв кнопок.

    Карточка с живыми кнопками висит в чате до бесконечности: TTL предложения
    (action_producer_gateway.PROPOSAL_TTL) сам по себе ни к какому переходу
    не приводил, и нажатие владельца падало на PROPOSAL_EXPIRED. Свипер
    закрывает предложение легальным переходом lifecycle и отзывает
    неиспользованные callback-токены, чтобы кнопка честно не сработала.
    Работает при любом значении TTL — читается valid_until самого предложения.

    Крон гасит все недорешённые состояния (PENDING_OWNER, DELIVERY_PENDING,
    POSTPONED); уже одобренные протухшие закрывает sweep_expired_approved_jobs
    по дедлайну исполнения, а не по valid_until.
    """

    checked_at = _clock(now)
    path = _queue_db_path(db_path)
    rows = list_stale_proposals(
        now=checked_at,
        limit=limit,
        db_path=path,
        states=states,
    )

    repository = OwnerActionRepository(path)
    expired = 0
    tokens_revoked = 0
    skipped = 0
    errors: list[str] = []
    for row in rows:
        proposal_id = str(row["proposal_id"])
        try:
            repository.transition_lifecycle(
                proposal_id,
                expected_state=str(row["state"]),
                expected_version=int(row["version"]),
                new_state=LifecycleState.EXPIRED.value,
                actor="owner_execution_sweeper",
                reason_code="PROPOSAL_EXPIRED",
                now=checked_at,
            )
        except OwnerActionLifecycleConflict:
            # Владелец успел нажать кнопку в этот же момент — его решение важнее.
            skipped += 1
            continue
        except Exception as exc:  # noqa: BLE001 — одно предложение не роняет проход
            errors.append(f"{proposal_id}:{type(exc).__name__}")
            logger.warning(
                "owner_execution: протухшее %s не закрыто — %s",
                proposal_id,
                exc,
            )
            continue
        expired += 1
        tokens_revoked += _revoke_proposal_tokens(
            path,
            proposal_id=proposal_id,
            now=checked_at,
        )
    return ProposalSweepRun(
        expired=expired,
        tokens_revoked=tokens_revoked,
        skipped=skipped,
        errors=tuple(errors),
    )


def _revoke_proposal_tokens(
    db_path: str,
    *,
    proposal_id: str,
    now: datetime,
) -> int:
    """Отзывает неиспользованные кнопки протухшего предложения."""

    connection = _queue_connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        revoked = connection.execute(
            """
            UPDATE owner_callback_tokens
            SET revoked_at = ?, revoke_reason = 'PROPOSAL_EXPIRED'
            WHERE proposal_id = ?
              AND consumed_at IS NULL
              AND revoked_at IS NULL
            """,
            (_iso_utc(now), proposal_id),
        ).rowcount
        connection.commit()
        return int(revoked)
    except Exception as exc:  # noqa: BLE001 — переход уже состоялся, откат не нужен
        connection.rollback()
        logger.warning(
            "owner_execution: токены %s не отозваны — %s",
            proposal_id,
            type(exc).__name__,
        )
        return 0
    finally:
        connection.close()
