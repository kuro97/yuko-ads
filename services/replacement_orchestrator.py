"""Безопасная orchestration замены последнего ACTIVE объявления.

Модуль связывает live pause guard, durable replacement workflow и cleaner.
Неизвестный scope, неполный inventory или отсутствующий exact CAS всегда
оставляют старое объявление включённым.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol, cast

from services.adset_pause_guard import (
    AdLiveContext,
    fetch_exact_ad_contexts,
    fetch_pause_inventory,
    find_candidate_context,
    unrelated_inventory_baseline,
)
from services.approval_checker_models import (
    ActionKind,
    ActionOrigin,
    EvidenceRecord,
    EvidenceRequest,
    EvidenceState,
    Metric,
    PauseCandidate,
    SourceEvidence,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
)

logger = logging.getLogger(__name__)

_OPEN_WORKFLOW_PHASES = {
    "WAITING_SLOT",
    "WAITING_CARD",
    "LAUNCHING",
    "WAITING_ACTIVE",
    "READY_TO_PAUSE",
    "BLOCKED",
}
_REPLACEMENT_DECISION_DAYS = 30
# Lifecycle-состояния owner proposal, после которых provider-мутация уже
# подтверждена: только они дают право записать «старое погашено».
_OWNER_EXECUTED_STATES = frozenset({"EXECUTED", "VERIFYING", "VERIFIED"})
# Владелец сказал «нет» (или предложение аннулировано) — повторять просьбу
# запрещено, workflow закрывается терминально.
_OWNER_REFUSED_STATES = frozenset({"REJECTED", "CANCELLED"})
# Предложение заглохло не по воле владельца (TTL, отказ исполнения). Фазу
# workflow это не меняет, но в отчёт уходит отдельным маркером: молча ждать
# вечно нельзя.
_OWNER_STALLED_STATES = frozenset(
    {"EXPIRED", "BLOCKED_STALE", "FAILED_NO_EFFECT", "RECONCILE_REQUIRED"}
)


@dataclass(frozen=True, slots=True)
class PauseOrReplacementOutcome:
    action: Literal["PAUSED", "REPLACEMENT_ENQUEUED", "BLOCKED"]
    ad_id: str
    workflow_id: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class ReplacementSlotOutcome:
    workflow_id: str
    action: Literal[
        "NO_DELETE_REQUIRED", "SLOTS_RELEASED", "WAITING_SAFE_CANDIDATES", "BLOCKED"
    ]
    required_slots: int
    available_before: int
    available_after: int
    deficit_before: int
    deficit_after: int
    deleted_ad_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    reason: str | None


@dataclass(frozen=True, slots=True)
class ReplacementVerifyResult:
    ran: bool
    checked: int
    waiting_workflow_ids: tuple[str, ...]
    ready_workflow_ids: tuple[str, ...]
    completed_workflow_ids: tuple[str, ...]
    blocked_workflow_ids: tuple[str, ...]
    errors: tuple[str, ...]
    # Владелец отказался гасить старое объявление: workflow закрыт терминально
    # (CANCELLED), это не ошибка и не ожидание. Значение по умолчанию сохраняет
    # позиционные вызовы старых callers/тестов.
    cancelled_workflow_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplacementMapping:
    city: str
    adset_type: str
    account_kind: Literal["offline", "online"]


class ReplacementDependencyError(RuntimeError):
    """Обязательный exact-CAS dependency ещё не доступен."""


class _WorkflowModule(Protocol):
    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None: ...

    def get_replacement_launch(self, workflow_id: str) -> dict[str, Any] | None: ...

    def enqueue_replacement(
        self,
        old_ad_id: str,
        old_ad_name: str,
        adset_id: str,
        city: str,
        adset_type: str,
    ) -> str: ...

    def link_replacement_launch(
        self,
        workflow_id: str,
        launch_attempt_key: str,
        card_id: str,
        card_name: str,
        city: str,
        account_kind: str,
        account_id: str,
        adset_id: str,
        expected_ad_names: Sequence[str],
        expected_ad_count: int,
        media_manifest_sha256: str,
    ) -> None: ...

    def record_replacement_created(
        self,
        workflow_id: str,
        replacement_ad_ids: Sequence[str],
        launch_attempt_key: str,
    ) -> None: ...

    def mark_old_paused(self, workflow_id: str) -> None: ...

    def mark_workflow_blocked(self, workflow_id: str, reason: str) -> None: ...

    def mark_workflow_cancelled(self, workflow_id: str, reason: str) -> None: ...


def _workflow_module() -> _WorkflowModule:
    from services import replacement_workflow

    return cast(_WorkflowModule, replacement_workflow)


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} не должен быть пустым")
    return text


def _strict_positive_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} должен быть положительным int")
    return value


def _strict_nonnegative_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} должен быть неотрицательным int")
    return value


def _replacement_config() -> dict[str, Any]:
    from services.autopilot import get_autopilot_config

    config = get_autopilot_config()
    replacement = config.get("replacement")
    if not isinstance(replacement, dict):
        raise ValueError("replacement_config_invalid")
    if type(replacement.get("enabled")) is not bool:
        raise ValueError("replacement_enabled_invalid")
    max_pending_hours = replacement.get("max_pending_hours", 48)
    if type(max_pending_hours) is not int or not 1 <= max_pending_hours <= 168:
        raise ValueError("replacement_max_pending_hours_invalid")
    return replacement


def resolve_replacement_mapping(adset_id: str) -> ReplacementMapping | None:
    """Находит mapping только из свежего direct FB discovery без fallback/cache."""
    from agent.adset_discovery import discover_adsets
    from services.fb_token_provider import get_active_account, get_fb_account_id

    active_account = get_active_account()
    if active_account in {None, "offline"}:
        account_kind: Literal["offline", "online"] = "offline"
    elif active_account == "online":
        account_kind = "online"
    else:
        return None

    discovered = discover_adsets(force_refresh=True)
    if not isinstance(discovered, dict) or discovered.get("source") != "fb_api":
        return None
    leadgen = discovered.get("leadgen")
    if not isinstance(leadgen, dict):
        return None

    matches: list[tuple[str, str]] = []
    for raw_city, raw_types in leadgen.items():
        if not isinstance(raw_city, str) or not raw_city.strip() or not isinstance(raw_types, dict):
            continue
        for raw_type, raw_id in raw_types.items():
            if str(raw_id or "").strip() != adset_id:
                continue
            adset_type = str(raw_type or "").strip().upper()
            if adset_type in {"L2", "L1"}:
                matches.append((raw_city.strip(), adset_type))
    if len(matches) != 1:
        return None
    try:
        if not str(get_fb_account_id() or "").removeprefix("act_").strip():
            return None
    except Exception:
        return None
    city, adset_type = matches[0]
    return ReplacementMapping(city, adset_type, account_kind)


def _validated_pause_snapshot(
    ad_id: str,
) -> tuple[str, AdLiveContext, set[str]] | tuple[None, None, None]:
    inventories = fetch_pause_inventory([ad_id])
    located = find_candidate_context(ad_id, inventories)
    if located is None:
        return None, None, None
    adset_id, context = located
    inventory = inventories.get(adset_id)
    if (
        inventory is None
        or inventory.get("complete") is not True
        or context.get("ad_id") != ad_id
        or context.get("adset_id") != adset_id
        or context.get("effective_status") != "ACTIVE"
    ):
        return None, None, None
    active_ids = inventory.get("active_ids")
    if (
        not isinstance(active_ids, set)
        or ad_id not in active_ids
        or any(not isinstance(item, str) or not item for item in active_ids)
    ):
        return None, None, None
    return adset_id, context, set(active_ids)


def safe_pause_or_enqueue_replacement(
    ad_id: str,
    source: str,
) -> PauseOrReplacementOutcome:
    """Только ставит replacement в локальную очередь, не меняя Facebook.

    PAUSE при наличии запасного ACTIVE теперь обязан строиться вызывающим
    action-контуром через sealed gateway. Эта функция обслуживает только
    fail-closed ветку последнего ACTIVE объявления.
    """
    del source
    normalized_ad_id = str(ad_id or "").strip()
    if not normalized_ad_id:
        return PauseOrReplacementOutcome("BLOCKED", normalized_ad_id, None, "missing_candidate_id")

    try:
        snapshot = _validated_pause_snapshot(normalized_ad_id)
        adset_id, context, active_ids = snapshot
        if adset_id is None or context is None or active_ids is None:
            return PauseOrReplacementOutcome(
                "BLOCKED", normalized_ad_id, None, "replacement_mapping_unknown"
            )
        if len(active_ids) >= 2:
            return PauseOrReplacementOutcome(
                "BLOCKED",
                normalized_ad_id,
                None,
                "pause_requires_approval_gateway",
            )

        replacement = _replacement_config()
        if replacement["enabled"] is not True:
            return PauseOrReplacementOutcome(
                "BLOCKED", normalized_ad_id, None, "last_active_no_replacement"
            )
        mapping = resolve_replacement_mapping(adset_id)
        if mapping is None:
            return PauseOrReplacementOutcome(
                "BLOCKED", normalized_ad_id, None, "replacement_mapping_unknown"
            )
        exact, exact_error = fetch_exact_ad_contexts([normalized_ad_id])
        exact_context = exact.get(normalized_ad_id)
        if (
            exact_error is not None
            or exact_context is None
            or exact_context.get("adset_id") != adset_id
            or exact_context.get("effective_status") != "ACTIVE"
        ):
            return PauseOrReplacementOutcome(
                "BLOCKED", normalized_ad_id, None, "replacement_mapping_unknown"
            )
        workflow_id = _workflow_module().enqueue_replacement(
            old_ad_id=normalized_ad_id,
            old_ad_name=str(exact_context.get("name") or ""),
            adset_id=adset_id,
            city=mapping.city,
            adset_type=mapping.adset_type,
        )
        return PauseOrReplacementOutcome(
            "REPLACEMENT_ENQUEUED", normalized_ad_id, workflow_id, None
        )
    except Exception as exc:
        logger.warning(
            "replacement pause orchestration blocked for %s: %s",
            normalized_ad_id,
            type(exc).__name__,
        )
        return PauseOrReplacementOutcome(
            "BLOCKED", normalized_ad_id, None, f"replacement_orchestration_error:{type(exc).__name__}"
        )


def bind_replacement_card(
    workflow_id: str,
    card_id: str,
    card_name: str,
    city: str,
    account_kind: Literal["offline", "online"],
    account_id: str,
    adset_id: str,
    expected_ad_names: Sequence[str],
    expected_ad_count: int,
    media_manifest_sha256: str,
    launch_attempt_key: str,
) -> None:
    """Immutable связывает concrete card/media batch до любого slot DELETE."""
    _workflow_module().link_replacement_launch(
        workflow_id=workflow_id,
        launch_attempt_key=launch_attempt_key,
        card_id=card_id,
        card_name=card_name,
        city=city,
        account_kind=account_kind,
        account_id=account_id,
        adset_id=adset_id,
        expected_ad_names=expected_ad_names,
        expected_ad_count=expected_ad_count,
        media_manifest_sha256=media_manifest_sha256,
    )


def _slot_reason(result: Mapping[str, Any]) -> str | None:
    reason = result.get("skipped_reason") or result.get("reason")
    if reason:
        return str(reason)
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        return str(errors[0])
    return None


def _slot_ids(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name}_invalid")
    result: list[str] = []
    for item in value:
        raw = item.get("ad_id") if isinstance(item, Mapping) else item
        text = _required_text(raw, field_name)
        result.append(text)
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name}_duplicate")
    return tuple(result)


def _meta_ids(value: object, field_name: str) -> tuple[str, ...]:
    """Meta object IDs в production контракте только decimal strings."""

    result = _slot_ids(value, field_name)
    if any(not item.isdigit() for item in result):
        raise ValueError(f"{field_name}_not_numeric")
    return result


def ensure_slot_for_workflow(workflow_id: str) -> ReplacementSlotOutcome:
    """Запускает full-preflight cleaner только для immutable bound workflow."""
    workflow_id = _required_text(workflow_id, "workflow_id")
    storage = _workflow_module()
    workflow = storage.get_workflow(workflow_id)
    link = storage.get_replacement_launch(workflow_id)
    if not isinstance(workflow, dict) or not isinstance(link, dict):
        return ReplacementSlotOutcome(
            workflow_id, "BLOCKED", 0, 0, 0, 0, 0, (), (), "bound_workflow_required"
        )
    if (
        workflow.get("phase") != "WAITING_SLOT"
        or workflow.get("adset_id") != link.get("adset_id")
        or workflow.get("city") != link.get("city")
    ):
        return ReplacementSlotOutcome(
            workflow_id, "BLOCKED", 0, 0, 0, 0, 0, (), (), "workflow_binding_invalid"
        )

    try:
        expected_count = _strict_positive_int(link.get("expected_ad_count"), "expected_ad_count")
        from services.adset_cleaner import get_cleaner_config, run_cleaner

        cleaner_config = get_cleaner_config()
        hard_reserve = _strict_positive_int(
            cleaner_config.get("hard_reserve_slots", 1), "hard_reserve_slots"
        )
        required_slots = expected_count + hard_reserve
        result = run_cleaner(mode="active", workflow_id=workflow_id)
        if not isinstance(result, dict):
            raise ValueError("cleaner_result_invalid")
        adset_id = str(workflow["adset_id"])
        before_by_adset = result.get("capacity_before")
        after_by_adset = result.get("capacity_after")
        available_before = _strict_nonnegative_int(
            before_by_adset.get(adset_id) if isinstance(before_by_adset, dict) else 0,
            "available_before",
        )
        available_after = _strict_nonnegative_int(
            after_by_adset.get(adset_id) if isinstance(after_by_adset, dict) else available_before,
            "available_after",
        )
        deficit_before = _strict_nonnegative_int(
            result.get("deficit_before", 0), "deficit_before"
        )
        deficit_after = _strict_nonnegative_int(
            result.get("deficit_after", deficit_before), "deficit_after"
        )
        raw_action = result.get("action")
        allowed_actions = {
            "NO_DELETE_REQUIRED",
            "SLOTS_RELEASED",
            "WAITING_SAFE_CANDIDATES",
            "BLOCKED",
        }
        action = str(raw_action) if raw_action in allowed_actions else "BLOCKED"
        reason = _slot_reason(result)
        if action == "BLOCKED" and reason is None:
            reason = "cleaner_result_fail_closed"
        return ReplacementSlotOutcome(
            workflow_id=workflow_id,
            action=cast(Any, action),
            required_slots=required_slots,
            available_before=available_before,
            available_after=available_after,
            deficit_before=deficit_before,
            deficit_after=deficit_after,
            deleted_ad_ids=_slot_ids(result.get("deleted"), "deleted"),
            claim_ids=_slot_ids(result.get("claim_ids"), "claim_ids"),
            reason=reason,
        )
    except Exception as exc:
        logger.warning("replacement slot blocked for %s: %s", workflow_id, type(exc).__name__)
        return ReplacementSlotOutcome(
            workflow_id,
            "BLOCKED",
            0,
            0,
            0,
            0,
            0,
            (),
            (),
            f"slot_orchestration_error:{type(exc).__name__}",
        )


def _require_exact_callable(module: object, name: str):
    callback = getattr(module, name, None)
    if not callable(callback):
        raise ReplacementDependencyError(f"{name}_unavailable")
    return callback


def claim_waiting_workflow_for_launch(workflow_id: str) -> dict[str, Any] | None:
    """CAS-забирает только exact workflow; adset-wide fallback запрещён."""
    workflow_id = _required_text(workflow_id, "workflow_id")
    storage = _workflow_module()
    workflow = storage.get_workflow(workflow_id)
    link = storage.get_replacement_launch(workflow_id)
    if not isinstance(workflow, dict) or not isinstance(link, dict):
        return None
    if workflow.get("phase") != "WAITING_CARD" or workflow.get("adset_id") != link.get("adset_id"):
        return None
    exact_claim = _require_exact_callable(storage, "claim_workflow_for_launch")
    claimed = exact_claim(workflow_id)
    if claimed is None:
        return None
    if not isinstance(claimed, dict) or claimed.get("workflow_id") != workflow_id:
        raise ReplacementDependencyError("exact_launch_claim_mismatch")
    return claimed


def record_workflow_launch_result(
    workflow_id: str,
    launch_attempt_key: str,
    city: str,
    ad_ids: Sequence[str],
) -> None:
    """Сохраняет exact result только для immutable attempt/city binding."""
    workflow_id = _required_text(workflow_id, "workflow_id")
    storage = _workflow_module()
    workflow = storage.get_workflow(workflow_id)
    link = storage.get_replacement_launch(workflow_id)
    if not isinstance(workflow, dict) or not isinstance(link, dict):
        raise ValueError("bound_workflow_required")
    if (
        link.get("launch_attempt_key") != launch_attempt_key
        or link.get("city") != city
        or workflow.get("city") != city
        or link.get("adset_id") != workflow.get("adset_id")
    ):
        raise ValueError("workflow_launch_binding_mismatch")
    exact_ad_ids = _meta_ids(ad_ids, "replacement_ad_ids")
    storage.record_replacement_created(
        workflow_id,
        exact_ad_ids,
        launch_attempt_key,
    )


def _list_replacement_workflows() -> list[dict[str, Any]]:
    from services.cleanup_repository import get_cleanup_status

    status = get_cleanup_status()
    workflows = status.get("replacement_workflows")
    if not isinstance(workflows, list) or not all(isinstance(row, dict) for row in workflows):
        raise ValueError("replacement_workflow_list_invalid")
    return workflows


def _created_ids(link: Mapping[str, Any]) -> tuple[str, ...]:
    raw = link.get("created_ad_ids")
    if raw is None:
        raw = link.get("created_ad_ids_json")
        if not isinstance(raw, str):
            raise ValueError("created_ad_ids_missing")
        raw = json.loads(raw)
    ids = _meta_ids(raw, "created_ad_ids")
    expected_count = _strict_positive_int(link.get("expected_ad_count"), "expected_ad_count")
    if len(ids) != expected_count:
        raise ValueError("created_ad_ids_count_mismatch")
    return ids


def _expected_names(link: Mapping[str, Any]) -> tuple[str, ...]:
    raw = link.get("expected_ad_names")
    if raw is None:
        raw_json = link.get("expected_ad_names_json")
        if not isinstance(raw_json, str):
            raise ValueError("expected_ad_names_missing")
        raw = json.loads(raw_json)
    names = _slot_ids(raw, "expected_ad_names")
    expected_count = _strict_positive_int(link.get("expected_ad_count"), "expected_ad_count")
    if len(names) != expected_count:
        raise ValueError("expected_ad_names_count_mismatch")
    return names


def _account_scope(link: Mapping[str, Any]) -> AbstractContextManager[None]:
    from services.fb_token_provider import fb_account

    account_kind = link.get("account_kind")
    if account_kind == "offline":
        return fb_account(None)
    if account_kind == "online":
        return fb_account("online")
    raise ValueError("workflow_account_kind_invalid")


def _assert_account(link: Mapping[str, Any]) -> None:
    from services.fb_token_provider import get_fb_account_id

    expected = _required_text(link.get("account_id"), "account_id").removeprefix("act_")
    actual = _required_text(get_fb_account_id(), "live_account_id").removeprefix("act_")
    if actual != expected:
        raise ValueError("workflow_account_mismatch")


def _verify_created_contexts(
    workflow: Mapping[str, Any],
    link: Mapping[str, Any],
) -> tuple[
    Literal["ACTIVE", "WAITING", "BLOCKED"],
    tuple[str, ...],
    tuple[dict[str, str], ...],
    str | None,
]:
    created_ids = _created_ids(link)
    expected_names = _expected_names(link)
    contexts, error = fetch_exact_ad_contexts(list(created_ids), require_names=True)
    if error is not None or set(contexts) != set(created_ids):
        return "WAITING", created_ids, (), error or "created_inventory_incomplete"
    adset_id = str(workflow.get("adset_id") or "")
    actual_names: list[str] = []
    evidence_ads: list[dict[str, str]] = []
    for created_id in created_ids:
        context = contexts[created_id]
        if context.get("ad_id") != created_id or context.get("adset_id") != adset_id:
            return "BLOCKED", created_ids, (), "replacement_wrong_adset"
        actual_names.append(str(context.get("name") or ""))
        evidence_ads.append(
            {
                "id": created_id,
                "name": str(context.get("name") or ""),
                "adset_id": adset_id,
                "effective_status": str(context.get("effective_status") or ""),
            }
        )
    if len(actual_names) != len(set(actual_names)) or tuple(actual_names) != expected_names:
        return "BLOCKED", created_ids, (), "replacement_names_mismatch"
    statuses = {str(contexts[created_id].get("effective_status") or "") for created_id in created_ids}
    if statuses == {"ACTIVE"}:
        return "ACTIVE", created_ids, tuple(evidence_ads), None
    return "WAITING", created_ids, tuple(evidence_ads), None


def _is_timed_out(workflow: Mapping[str, Any], max_pending_hours: int) -> bool:
    raw = workflow.get("replacement_created_at")
    if not isinstance(raw, str) or not raw:
        return False
    created_at = datetime.fromisoformat(raw)
    if created_at.tzinfo is None:
        raise ValueError("replacement_created_at_timezone_missing")
    return (datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)).total_seconds() > (
        max_pending_hours * 3600
    )


def _mark_blocked(storage: _WorkflowModule, workflow_id: str, reason: str) -> None:
    storage.mark_workflow_blocked(workflow_id, reason)


def _mark_cancelled(storage: _WorkflowModule, workflow_id: str, reason: str) -> None:
    storage.mark_workflow_cancelled(workflow_id, reason)


def _parse_aware_timestamp(value: object, field_name: str) -> datetime:
    raw = _required_text(value, field_name)
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name}_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _replacement_pause_identity(
    workflow_id: str,
    workflow: Mapping[str, Any],
    created_ids: tuple[str, ...],
) -> tuple[datetime, str]:
    """Восстанавливает тот же ключ после crash без чтения изменившегося FB state."""
    prepared_at = _parse_aware_timestamp(
        workflow.get("replacement_active_at"),
        "replacement_active_at",
    )
    old_ad_id = _required_text(workflow.get("old_ad_id"), "old_ad_id")
    adset_id = _required_text(workflow.get("adset_id"), "adset_id")
    idempotency_key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            ":".join(
                (
                    "acme-replacement-pause",
                    workflow_id,
                    prepared_at.isoformat(),
                    old_ad_id,
                    adset_id,
                    *created_ids,
                )
            ),
        )
    )
    return prepared_at, idempotency_key


def _require_complete_source(evidence: SourceEvidence) -> None:
    if (
        evidence.state is not EvidenceState.FRESH_COMPLETE
        or evidence.complete is not True
        or evidence.from_cache is True
    ):
        raise ReplacementDependencyError(
            evidence.error_code or f"{evidence.source.value.lower()}_evidence_incomplete"
        )


def _exact_metric_record(
    evidence: SourceEvidence,
    subject: SubjectRef,
    metric: Metric,
    window: TimeWindow,
) -> EvidenceRecord:
    matches = [
        record
        for record in evidence.records
        if record.subject == subject
        and record.metric is metric
        and record.window == window
        and record.state is EvidenceState.FRESH_COMPLETE
    ]
    if len(matches) != 1:
        raise ReplacementDependencyError(
            f"{evidence.source.value.lower()}_{metric.value.lower()}_not_exact"
        )
    return matches[0]


def _build_replacement_pause_candidate(
    workflow_id: str,
    workflow: Mapping[str, Any],
    link: Mapping[str, Any],
    created_ids: tuple[str, ...],
    old_context: Mapping[str, str],
    now: datetime,
) -> tuple[PauseCandidate, datetime, str]:
    """Собирает claims из нового force-live read; gateway перечитает их ещё раз."""
    from services.approval_source_amo import load_amo_evidence
    from services.approval_source_cdp import load_cdp_evidence
    from services.approval_source_facebook import load_facebook_evidence

    adset_id = _required_text(workflow.get("adset_id"), "adset_id")
    old_ad_id = _required_text(workflow.get("old_ad_id"), "old_ad_id")
    prepared_at, idempotency_key = _replacement_pause_identity(
        workflow_id,
        workflow,
        created_ids,
    )
    decision_window = TimeWindow(
        start=prepared_at - timedelta(days=_REPLACEMENT_DECISION_DAYS),
        end=prepared_at,
        timezone_name="UTC",
        semantic="REPLACEMENT_PAUSE_TRAILING_30D",
    )
    subject = SubjectRef(SubjectKind.AD, old_ad_id, adset_id)
    request = EvidenceRequest(
        request_id=f"replacement-pause:{workflow_id}",
        purpose="ACTION",
        action_kind=ActionKind.PAUSE,
        generated_at=now,
        subjects=(subject,),
        claims=(),
        required_sources=(
            SourceSystem.FACEBOOK,
            SourceSystem.AMO,
            SourceSystem.CDP_ERP,
        ),
        windows=(decision_window,),
        account_ids=(_required_text(link.get("account_id"), "account_id"),),
        adset_ids=(adset_id,),
        ad_ids=(old_ad_id,),
        card_ids=(),
        staged_relative_paths=(),
        include_full_inventory=True,
        force_live=True,
        max_age_seconds=1,
    )
    facebook = load_facebook_evidence(request, now, force_live=True)
    amo = load_amo_evidence(request, now, force_live=True)
    cdp = load_cdp_evidence(request, now, force_live=True)
    for evidence in (facebook, amo, cdp):
        _require_complete_source(evidence)

    spend_record = _exact_metric_record(
        facebook, subject, Metric.SPEND, decision_window
    )
    leads_record = _exact_metric_record(
        facebook, subject, Metric.LEADS, decision_window
    )
    quals_record = _exact_metric_record(amo, subject, Metric.QUALS, decision_window)
    payments_record = _exact_metric_record(
        cdp, subject, Metric.PAYMENTS, decision_window
    )
    revenue_record = _exact_metric_record(
        cdp, subject, Metric.REVENUE, decision_window
    )
    if not isinstance(spend_record.value, Decimal):
        raise ReplacementDependencyError("facebook_spend_invalid")
    if type(leads_record.value) is not int or type(quals_record.value) is not int:
        raise ReplacementDependencyError("lead_metrics_invalid")
    if type(payments_record.value) is not int or not isinstance(
        revenue_record.value, Decimal
    ):
        raise ReplacementDependencyError("payment_metrics_invalid")
    if not spend_record.currency:
        raise ReplacementDependencyError("facebook_currency_missing")

    inventories = fetch_pause_inventory([old_ad_id, *created_ids])
    inventory = inventories.get(adset_id)
    if inventory is None or inventory.get("complete") is not True:
        raise ReplacementDependencyError("pause_inventory_incomplete")
    active_ids = set(inventory.get("active_ids") or set())
    if old_ad_id not in active_ids or not set(created_ids).issubset(active_ids):
        raise ReplacementDependencyError("replacement_active_drift")
    sibling_active_ids = tuple(sorted(active_ids - {old_ad_id}))
    inventory_sha256 = _required_text(
        inventory.get("state_sha256"), "pre_inventory_sha256"
    )
    unrelated_sha256, sibling_snapshot = unrelated_inventory_baseline(
        inventory,
        target_ad_id=old_ad_id,
        expected_adset_id=adset_id,
    )

    payments = tuple(
        payment for payment in cdp.payments if payment.fb_ad_id == old_ad_id
    )
    positive_contracts = {
        payment.contract_number
        for payment in payments
        if payment.contract_net_lcy > 0
    }
    if len(positive_contracts) != payments_record.value:
        raise ReplacementDependencyError("payment_entities_count_mismatch")

    candidate = PauseCandidate(
        ad_id=old_ad_id,
        adset_id=adset_id,
        display_name=_required_text(old_context.get("name"), "old_ad_name"),
        reason_code="REPLACEMENT_READY",
        decision_window=decision_window,
        expected_before_status="ACTIVE",
        expected_after_status="PAUSED",
        spend=spend_record.value,
        spend_currency=spend_record.currency,
        leads=leads_record.value,
        quals=quals_record.value,
        payments=payments,
        revenue_lcy=revenue_record.value,
        pre_inventory_sha256=inventory_sha256,
        sibling_active_ids=sibling_active_ids,
        pre_unrelated_inventory_sha256=unrelated_sha256,
        sibling_status_snapshot=sibling_snapshot,
        replacement_ad_id=created_ids[0],
    )
    return candidate, prepared_at, idempotency_key


def _replacement_pause_scope(idempotency_key: str, old_ad_id: str) -> str:
    """Один и тот же scope у producer'а и у последующей проверки судьбы proposal."""
    return f"replacement:{idempotency_key}:{old_ad_id}"


def _execute_checked_replacement_pause(
    candidate: PauseCandidate,
    prepared_at: datetime,
    idempotency_key: str,
) -> str:
    """Создаёт отдельный PAUSE proposal после read-only replacement проверки."""
    from services.action_producer_gateway import propose_pause

    # ``idempotency_key`` здесь — детерминированный UUID5 replacement-идентити
    # (_replacement_pause_identity), а proposal принимает только UUID4. Поэтому
    # идентити уходит в scope: durable binding scope → UUID4 сам возвращает тот
    # же ключ после crash, а UUID5 в качестве ключа ронял ValueError.
    outcome = propose_pause(
        candidate.ad_id,
        origin=ActionOrigin.REPLACEMENT,
        scope=_replacement_pause_scope(idempotency_key, candidate.ad_id),
        reason_code="REPLACEMENT_READY",
        now=prepared_at,
    )
    if outcome.receipt is None:
        raise ReplacementDependencyError("replacement_pause_proposal_missing")
    return outcome.receipt.proposal_id


def _owner_pause_lifecycle(
    workflow_id: str,
    workflow: Mapping[str, Any],
    created_ids: tuple[str, ...],
) -> tuple[str, str] | None:
    """Судьба СВОЕГО PAUSE-предложения: ``(proposal_id, lifecycle state)``.

    Ключ восстанавливается из durable identity (_replacement_pause_identity),
    поэтому после рестарта смотрим ровно на то предложение, которое создали
    сами, а не на любую паузу этого объявления. Любая недоступность журнала —
    ``None``: fail-closed, workflow продолжит ждать подтверждения исполнителя.
    """
    try:
        from services.owner_action_repository import find_lifecycle_by_source_ref

        _prepared_at, idempotency_key = _replacement_pause_identity(
            workflow_id, workflow, created_ids
        )
        old_ad_id = _required_text(workflow.get("old_ad_id"), "old_ad_id")
        found = find_lifecycle_by_source_ref(
            _replacement_pause_scope(idempotency_key, old_ad_id)
        )
    except Exception as exc:
        logger.debug(
            "replacement %s: судьба PAUSE proposal недоступна — %s",
            workflow_id,
            type(exc).__name__,
        )
        return None
    if found is None:
        return None
    proposal_id, state = found
    if not isinstance(proposal_id, str) or not isinstance(state, str):
        return None
    return proposal_id, state


def verify_and_complete_replacements(limit: int = 20) -> ReplacementVerifyResult:
    """Продвигает replacement workflow по owner-approval state machine.

    Полный цикл одного workflow:

    1. ``WAITING_ACTIVE`` → замена подтверждена ACTIVE → ``READY_TO_PAUSE``.
    2. Старое ещё ACTIVE и предложения нет → создаётся PAUSE proposal, workflow
       ЖДЁТ (``owner_pause_proposal_pending``). Повторный тик видит своё
       предложение и не просит второй раз.
    3. Владелец одобрил, execution boundary исполнил PAUSE (lifecycle
       EXECUTED/VERIFYING/VERIFIED) и живой статус старого — PAUSED →
       ``mark_old_paused`` → ``COMPLETED``.
    4. Владелец отклонил (REJECTED/CANCELLED) → терминальный ``CANCELLED``:
       замена живёт, старое остаётся включённым по решению владельца.

    Одного live PAUSED недостаточно для завершения: объявление мог погасить
    человек в Ads Manager, и «старое погашено нами» было бы неправдой.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("limit должен быть положительным int")
    try:
        config = _replacement_config()
    except Exception as exc:
        return ReplacementVerifyResult(
            False, 0, (), (), (), (), (f"invalid_replacement_config:{type(exc).__name__}",)
        )
    if config["enabled"] is not True:
        return ReplacementVerifyResult(False, 0, (), (), (), (), ("replacement_disabled",))

    storage = _workflow_module()
    waiting: list[str] = []
    ready: list[str] = []
    completed: list[str] = []
    blocked: list[str] = []
    cancelled: list[str] = []
    errors: list[str] = []
    checked = 0
    try:
        rows = [
            row
            for row in _list_replacement_workflows()
            if row.get("phase") in {"WAITING_ACTIVE", "READY_TO_PAUSE"}
        ][:limit]
    except Exception as exc:
        return ReplacementVerifyResult(
            False, 0, (), (), (), (), (f"workflow_list_error:{type(exc).__name__}",)
        )

    for durable_row in rows:
        workflow_id = str(durable_row.get("workflow_id") or "")
        checked += 1
        try:
            workflow = storage.get_workflow(workflow_id)
            link = storage.get_replacement_launch(workflow_id)
            if not isinstance(workflow, dict) or not isinstance(link, dict):
                raise ValueError("bound_workflow_required")
            phase = workflow.get("phase")
            if phase not in {"WAITING_ACTIVE", "READY_TO_PAUSE"}:
                waiting.append(workflow_id)
                continue
            adset_id = _required_text(workflow.get("adset_id"), "adset_id")
            with _account_scope(link):
                _assert_account(link)
                verification, created_ids, evidence_ads, reason = _verify_created_contexts(
                    workflow, link
                )
                if verification == "BLOCKED":
                    _mark_blocked(storage, workflow_id, reason or "replacement_identity_invalid")
                    blocked.append(workflow_id)
                    continue
                if verification != "ACTIVE":
                    if _is_timed_out(workflow, int(config["max_pending_hours"])):
                        _mark_blocked(storage, workflow_id, "replacement_active_timeout")
                        blocked.append(workflow_id)
                    else:
                        waiting.append(workflow_id)
                    if reason:
                        errors.append(f"{workflow_id}:{reason}")
                    continue

                if phase == "WAITING_ACTIVE":
                    confirm_active = _require_exact_callable(storage, "confirm_replacement_active")
                    confirm_active(
                        workflow_id,
                        created_ids,
                        evidence={"ads": list(evidence_ads)},
                    )
                    ready.append(workflow_id)
                    workflow = storage.get_workflow(workflow_id)
                    if not isinstance(workflow, dict) or workflow.get("phase") != "READY_TO_PAUSE":
                        raise ReplacementDependencyError("confirm_replacement_active_failed")

                old_ad_id = _required_text(workflow.get("old_ad_id"), "old_ad_id")
                old_contexts, old_error = fetch_exact_ad_contexts([old_ad_id])
                old_context = old_contexts.get(old_ad_id)
                if old_error is not None or old_context is None:
                    waiting.append(workflow_id)
                    if old_error:
                        errors.append(f"{workflow_id}:{old_error}")
                    continue
                if old_context.get("adset_id") != adset_id:
                    _mark_blocked(storage, workflow_id, "old_ad_wrong_adset")
                    blocked.append(workflow_id)
                    continue
                old_effective = str(old_context.get("effective_status") or "")
                lifecycle = _owner_pause_lifecycle(workflow_id, workflow, created_ids)
                if lifecycle is not None and lifecycle[1] in _OWNER_REFUSED_STATES:
                    # Владелец отказался гасить старое — просить снова нельзя.
                    _mark_cancelled(
                        storage,
                        workflow_id,
                        f"owner_refused_pause:{lifecycle[1]}",
                    )
                    cancelled.append(workflow_id)
                    errors.append(
                        f"{workflow_id}:owner_refused_pause:{lifecycle[0]}:{lifecycle[1]}"
                    )
                    continue
                if old_effective == "ACTIVE":
                    if lifecycle is not None:
                        # Предложение уже висит у владельца: второй раз не просим
                        # и тяжёлый force-live сбор доказательств не повторяем.
                        # Заглохшее предложение (истёк TTL, исполнение не дало
                        # эффекта) отделяем от живого ожидания — иначе workflow
                        # молча ждал бы вечно, и владелец об этом не узнал.
                        marker = (
                            "owner_pause_proposal_stalled"
                            if lifecycle[1] in _OWNER_STALLED_STATES
                            else "owner_pause_proposal_pending"
                        )
                        waiting.append(workflow_id)
                        errors.append(
                            f"{workflow_id}:{marker}:{lifecycle[0]}"
                            + (f":{lifecycle[1]}" if marker.endswith("stalled") else "")
                        )
                        continue
                    candidate, prepared_at, idempotency_key = (
                        _build_replacement_pause_candidate(
                            workflow_id,
                            workflow,
                            link,
                            created_ids,
                            old_context,
                            datetime.now(timezone.utc),
                        )
                    )
                    proposal_id = _execute_checked_replacement_pause(
                        candidate,
                        prepared_at,
                        idempotency_key,
                    )
                    waiting.append(workflow_id)
                    errors.append(
                        f"{workflow_id}:owner_pause_proposal_pending:{proposal_id}"
                    )
                    continue
                elif old_effective == "PAUSED":
                    # Сам live PAUSED не доказывает выполнение ЭТОГО proposal:
                    # рекламу мог погасить человек в Ads Manager. Завершаем
                    # workflow только когда наше предложение реально исполнено
                    # execution boundary И живой статус это подтверждает.
                    if lifecycle is None or lifecycle[1] not in _OWNER_EXECUTED_STATES:
                        waiting.append(workflow_id)
                        errors.append(f"{workflow_id}:executor_confirmation_required")
                        continue
                    storage.mark_old_paused(workflow_id)
                    completed.append(workflow_id)
                    continue
                else:
                    _mark_blocked(storage, workflow_id, "old_ad_status_unknown")
                    blocked.append(workflow_id)
                    continue
        except ReplacementDependencyError as exc:
            waiting.append(workflow_id)
            errors.append(f"{workflow_id}:{exc}")
        except Exception as exc:
            logger.warning("replacement verify blocked for %s: %s", workflow_id, type(exc).__name__)
            waiting.append(workflow_id)
            errors.append(f"{workflow_id}:verify_error:{type(exc).__name__}")

    return ReplacementVerifyResult(
        ran=True,
        checked=checked,
        waiting_workflow_ids=tuple(dict.fromkeys(waiting)),
        ready_workflow_ids=tuple(dict.fromkeys(ready)),
        completed_workflow_ids=tuple(dict.fromkeys(completed)),
        blocked_workflow_ids=tuple(dict.fromkeys(blocked)),
        errors=tuple(errors),
        cancelled_workflow_ids=tuple(dict.fromkeys(cancelled)),
    )
