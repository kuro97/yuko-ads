"""Безопасное применение durable recovery-планов.

Audit и apply намеренно разделены. Этот модуль допускает CREATE только после
fresh exact inventory recheck, SQLite CAS и повторной проверки identity внутри
общего lock адсета. Cleaner, DELETE, PAUSE и Trello mutation здесь отсутствуют.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from services import launch_recovery as recovery
from services.launch_recovery import (
    RecoveryCasePhase,
    RecoveryCityPlan,
)


_TZ_LOCAL = timezone(timedelta(hours=5))
_TERMINAL_CITY_PHASES = frozenset({"COMPLETE", "RECOVERED", "CANCELLED"})
_APPLYABLE_CITY_PHASES = frozenset({"MISSING", "WAITING_SLOT", "APPROVED", "LAUNCHING"})


class RecoveryApplyError(RuntimeError):
    """Безопасная ошибка apply без разрешения повторного слепого CREATE."""


class RecoveryApplyBlocked(RecoveryApplyError):
    """Apply остановлен одним из обязательных safety gates."""


@dataclass(frozen=True, slots=True)
class LaunchRecoveryResult:
    """Результат одного идемпотентного recovery apply."""

    case_id: str
    phase: RecoveryCasePhase
    applied_plan_ids: tuple[str, ...]
    created_ad_ids_by_plan: dict[str, tuple[str, ...]]
    skipped_plan_ids: tuple[str, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ApplyContext:
    case: dict[str, Any]
    plan: RecoveryCityPlan
    card: dict[str, Any]


def _safe_error(value: object) -> str:
    return recovery._safe_error(value)[:1000]


def _aware_local(moment: datetime, field_name: str) -> datetime:
    return recovery._aware_local(moment, field_name)


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} не должен быть пустым")
    return text


def _exact_manifest_sha256(value: object) -> str:
    manifest = _required_text(value, "approved_manifest_sha256")
    if len(manifest) != 64 or any(char not in "0123456789abcdef" for char in manifest):
        raise ValueError("approved_manifest_sha256 должен быть lowercase SHA-256")
    return manifest


def _connection():
    return recovery._connection()


def _load_plan_row(plan_id: str) -> dict[str, Any]:
    plan_id = _required_text(plan_id, "plan_id")
    conn = _connection()
    try:
        row = conn.execute(
            "SELECT * FROM launch_recovery_city_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise RecoveryApplyBlocked("recovery_plan_not_found")
        return dict(row)
    finally:
        conn.close()


def _load_case_row(case_id: str) -> dict[str, Any]:
    case_id = _required_text(case_id, "case_id")
    conn = _connection()
    try:
        row = conn.execute(
            "SELECT * FROM launch_recovery_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if row is None:
            raise RecoveryApplyBlocked("recovery_case_not_found")
        return dict(row)
    finally:
        conn.close()


def _load_case_plan_rows(case_id: str) -> list[dict[str, Any]]:
    conn = _connection()
    try:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM launch_recovery_city_plans
                WHERE case_id = ? ORDER BY city, plan_id
                """,
                (case_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def _plan_from_row(row: Mapping[str, Any]) -> RecoveryCityPlan:
    return recovery._city_plan_from_row(row)


def _json_list(raw: object, field_name: str) -> tuple[str, ...]:
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RecoveryApplyBlocked(f"{field_name}_invalid") from exc
    if not isinstance(value, list):
        raise RecoveryApplyBlocked(f"{field_name}_invalid")
    normalized = tuple(str(item or "").strip() for item in value)
    if not normalized or any(not item for item in normalized):
        raise RecoveryApplyBlocked(f"{field_name}_empty")
    if len(normalized) != len(set(normalized)):
        raise RecoveryApplyBlocked(f"{field_name}_duplicate")
    return normalized


def _case_phase_from_city_rows(rows: Sequence[Mapping[str, Any]]) -> RecoveryCasePhase:
    phases = [str(row["phase"]) for row in rows]
    if not phases:
        return "REVIEW_REQUIRED"
    if any(phase == "BLOCKED" for phase in phases):
        return "BLOCKED"
    if any(phase == "REVIEW_REQUIRED" for phase in phases):
        return "REVIEW_REQUIRED"
    if any(phase == "LAUNCHING" for phase in phases):
        return "LAUNCHING"
    if any(phase == "WAITING_ACTIVE" for phase in phases):
        return "WAITING_ACTIVE"
    if any(phase == "WAITING_SLOT" for phase in phases):
        return "WAITING_SLOT"
    if all(phase in {"COMPLETE", "RECOVERED"} for phase in phases):
        return "RECOVERED" if "RECOVERED" in phases else "NO_ACTION"
    if any(phase == "APPROVED" for phase in phases):
        return "APPROVED"
    return "DISCOVERED"


def _sync_case_in_transaction(conn, case_id: str, now: datetime) -> RecoveryCasePhase:
    rows = conn.execute(
        """
        SELECT city, phase, found_ad_ids_json
        FROM launch_recovery_city_plans
        WHERE case_id = ? ORDER BY city, plan_id
        """,
        (case_id,),
    ).fetchall()
    phase = _case_phase_from_city_rows(rows)
    found_ads = {str(row["city"]): json.loads(row["found_ad_ids_json"]) for row in rows}
    missing_cities = [
        str(row["city"])
        for row in rows
        if row["phase"] in {"MISSING", "WAITING_SLOT", "APPROVED", "LAUNCHING"}
    ]
    completed_at = (
        now.isoformat() if phase in recovery.RECOVERY_CASE_TERMINAL_PHASES else None
    )
    conn.execute(
        """
        UPDATE launch_recovery_cases
        SET phase = ?, found_ads_json = ?, missing_cities_json = ?,
            updated_at = ?, completed_at = ?
        WHERE case_id = ?
        """,
        (
            phase,
            recovery._json_dumps(found_ads),
            recovery._json_dumps(missing_cities),
            now.isoformat(),
            completed_at,
            case_id,
        ),
    )
    return phase


def _safe_evidence(
    *,
    classification: str,
    plan: RecoveryCityPlan,
    checked_at: datetime,
    capacity_available: int,
    other_reserved_slots: int,
    found_ad_ids: Sequence[str],
) -> dict[str, Any]:
    evidence = {
        "classification": classification,
        "inventory_complete": True,
        "account_kind": plan.account_kind,
        "account_id": plan.account_id,
        "adset_id": plan.adset_id,
        "expected_ad_count": plan.expected_ad_count,
        "expected_names_sha256": hashlib.sha256(
            json.dumps(
                list(plan.expected_ad_names),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "found_ad_ids": list(found_ad_ids),
        "capacity_available": capacity_available,
        "other_reserved_slots": other_reserved_slots,
        "checked_at": checked_at.isoformat(),
        "window": {
            "from": plan.reconcile_from.isoformat(),
            "until": checked_at.isoformat(),
            "inclusive": True,
        },
    }
    if plan.provider_media_sha256 is not None:
        evidence["provider_media_sha256"] = plan.provider_media_sha256
        media_type = plan.evidence.get("provider_media_type")
        if isinstance(media_type, str) and media_type:
            evidence["provider_media_type"] = media_type
    return evidence


def _block_snapshot(
    row: Mapping[str, Any], reason: str, now: datetime
) -> RecoveryCityPlan:
    safe_reason = _safe_error(reason)
    conn = _connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE launch_recovery_city_plans
            SET phase = 'BLOCKED', last_error = ?, last_rechecked_at = ?, updated_at = ?
            WHERE plan_id = ? AND updated_at = ?
            """,
            (
                safe_reason,
                now.isoformat(),
                now.isoformat(),
                row["plan_id"],
                row["updated_at"],
            ),
        )
        if cursor.rowcount != 1:
            raise RecoveryApplyBlocked("recovery_plan_cas_lost")
        _sync_case_in_transaction(conn, str(row["case_id"]), now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _plan_from_row(_load_plan_row(str(row["plan_id"])))


def _fetch_exact_card(case: Mapping[str, Any]) -> dict[str, Any]:
    from integrations.trello import get_card

    card = get_card(str(case["card_id"]))
    if not isinstance(card, Mapping):
        raise RecoveryApplyBlocked("recovery_card_invalid")
    result = dict(card)
    if str(result.get("id") or "") != str(case["card_id"]):
        raise RecoveryApplyBlocked("recovery_card_identity_drift")
    if str(result.get("name") or "").strip() != str(case["card_name"]):
        raise RecoveryApplyBlocked("recovery_card_name_drift")
    if result.get("dueComplete") is not True or result.get("closed") is True:
        raise RecoveryApplyBlocked("recovery_card_completion_drift")
    return result


def _current_scope_and_manifest(
    case: Mapping[str, Any],
    plan: RecoveryCityPlan,
    live_adsets: Mapping[recovery.AccountKind, recovery.LiveAdsetDiscovery]
    | None = None,
) -> tuple[dict[str, Any], recovery.MediaManifest]:
    card = _fetch_exact_card(case)
    campaign_type = recovery._campaign_type(card)
    if campaign_type != str(case.get("campaign_type") or ""):
        raise RecoveryApplyBlocked("recovery_campaign_type_drift")
    manifest = recovery.build_media_manifest(card)
    if plan.provider_media_sha256 is None:
        raise RecoveryApplyBlocked("legacy_recovery_media_sha_requires_audit")
    if manifest.provider_media_sha256 != plan.provider_media_sha256:
        raise RecoveryApplyBlocked("recovery_provider_media_sha_drift")
    current_names = manifest.expected_names_by_city.get(plan.city)
    if current_names is None:
        raise RecoveryApplyBlocked("recovery_city_removed_from_manifest")
    if tuple(current_names) != plan.expected_ad_names:
        raise RecoveryApplyBlocked("recovery_expected_names_drift")
    if len(current_names) != plan.expected_ad_count:
        raise RecoveryApplyBlocked("recovery_expected_count_drift")
    account_kind, account_id, adset_id = recovery._resolve_city_scope(
        card,
        plan.city,
        live_adsets,
    )
    if (
        account_kind != plan.account_kind
        or account_id != plan.account_id
        or adset_id != plan.adset_id
    ):
        raise RecoveryApplyBlocked("recovery_scope_drift")
    return card, manifest


def _other_reserved_slots(adset_id: str, *, exclude_launch_slots: int = 0) -> int:
    from integrations.facebook import _get_other_launch_reserved_slots
    from services import launch_repository

    replacement_slots = _get_other_launch_reserved_slots(None, adset_id)
    launch_slots = launch_repository.get_reserved_slots(
        adset_id,
        datetime.now(timezone.utc),
    )
    if (
        type(replacement_slots) is not int
        or replacement_slots < 0
        or type(launch_slots) is not int
        or type(exclude_launch_slots) is not int
        or exclude_launch_slots < 0
        or launch_slots < exclude_launch_slots
    ):
        raise RecoveryApplyBlocked("recovery_reservations_invalid")
    return replacement_slots + launch_slots - exclude_launch_slots


def _hard_reserve_slots() -> int:
    from services.autopilot import get_autopilot_config

    config = get_autopilot_config().get("cleaner")
    if not isinstance(config, Mapping):
        raise RecoveryApplyBlocked("recovery_cleaner_config_invalid")
    value = config.get("hard_reserve_slots", 1)
    if type(value) is not int or not 1 <= value <= 5:
        raise RecoveryApplyBlocked("recovery_hard_reserve_invalid")
    return value


def _fresh_inventory(plan: RecoveryCityPlan, checked_at: datetime):
    inventory = recovery.fetch_complete_launch_inventory(("offline", "online"))
    classification, found_ids, _raw_evidence = recovery._classify_city_inventory(
        plan.expected_ad_names,
        plan.account_kind,
        plan.account_id,
        plan.adset_id,
        plan.reconcile_from,
        checked_at,
        inventory,
    )
    capacity = recovery._capacity_for_scope(
        inventory,
        plan.account_kind,
        plan.account_id,
        plan.adset_id,
    )
    return classification, found_ids, capacity


def _assert_parent_action_is_latest(case: Mapping[str, Any]) -> None:
    """Проверяет exact Trello completion ещё раз непосредственно перед CREATE."""
    source_completed_at = recovery._parse_aware(
        case.get("source_completed_at"),
        "source_completed_at",
    )
    parent_action_id = _required_text(case.get("trello_action_id"), "trello_action_id")
    card_id = _required_text(case.get("card_id"), "card_id")
    completions = recovery.scan_completed_cards_since(source_completed_at)
    conflicting = [
        completion
        for completion in completions
        if completion.card_id == card_id
        and completion.action_id != parent_action_id
        and completion.completed_at >= source_completed_at
    ]
    if conflicting:
        raise RecoveryApplyBlocked("newer_trello_completion_before_create")


def _final_precreate_recheck_and_bind(
    context: _ApplyContext,
    approved_manifest_sha256: str,
    launch_media_manifest: object,
    *,
    authorization_reserved: bool = False,
) -> None:
    """Последний live recheck и единый parent+city CAS перед первым CREATE."""
    plan = context.plan
    case = context.case
    checked_at = datetime.now(_TZ_LOCAL)
    _assert_parent_action_is_latest(case)
    live_adsets = recovery.discover_live_recovery_adsets(("offline", "online"))
    _fresh_card, fresh_manifest = _current_scope_and_manifest(
        case,
        plan,
        live_adsets,
    )
    if fresh_manifest.manifest_sha256 != approved_manifest_sha256:
        raise RecoveryApplyBlocked("manifest_changed_before_create")
    from integrations.facebook import LaunchMediaManifest

    if not isinstance(launch_media_manifest, LaunchMediaManifest):
        raise RecoveryApplyBlocked("launch_media_manifest_invalid")
    expected_card_name_hash = hashlib.sha256(
        str(case["card_name"]).encode("utf-8")
    ).hexdigest()
    actual_files = tuple(
        (item.relative_name, item.size, item.content_sha256)
        for item in launch_media_manifest.files
    )
    approved_files = tuple(
        (item.relative_name, item.size, item.content_sha256)
        for item in fresh_manifest.files
    )
    if (
        launch_media_manifest.card_name_sha256 != expected_card_name_hash
        or launch_media_manifest.city != plan.city
        or launch_media_manifest.account_kind != plan.account_kind
        or launch_media_manifest.account_id != plan.account_id
        or launch_media_manifest.adset_id != plan.adset_id
        or launch_media_manifest.expected_ad_names != plan.expected_ad_names
        or actual_files != approved_files
    ):
        raise RecoveryApplyBlocked("launch_media_manifest_changed_before_create")
    launch_binding_sha256 = hashlib.sha256(
        json.dumps(
            {
                "card_id": str(case["card_id"]),
                "launch_media_manifest_sha256": launch_media_manifest.manifest_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    provider_media_sha256 = recovery._provider_media_sha256(
        launch_media_manifest.media_type,
        tuple(
            recovery.MediaFileEvidence(
                item.relative_name,
                item.size,
                item.content_sha256,
            )
            for item in launch_media_manifest.files
        ),
    )
    if (
        fresh_manifest.provider_media_sha256 is None
        or provider_media_sha256 != fresh_manifest.provider_media_sha256
        or provider_media_sha256 != plan.provider_media_sha256
    ):
        raise RecoveryApplyBlocked("launch_provider_media_sha_changed_before_create")
    classification, found_ids, capacity = _fresh_inventory(plan, checked_at)
    if classification != "MISSING" or found_ids:
        raise RecoveryApplyBlocked("fresh_inventory_not_missing_before_create")
    other_reserved = _other_reserved_slots(
        plan.adset_id,
        exclude_launch_slots=(
            plan.expected_ad_count if authorization_reserved else 0
        ),
    )
    reserve = _hard_reserve_slots()
    if capacity < plan.expected_ad_count + reserve + other_reserved:
        raise RecoveryApplyBlocked("capacity_changed_before_create")

    evidence = _safe_evidence(
        classification=classification,
        plan=plan,
        checked_at=checked_at,
        capacity_available=capacity,
        other_reserved_slots=other_reserved,
        found_ad_ids=found_ids,
    )
    evidence["pre_create_parent_binding"] = {
        "case_id": plan.case_id,
        "trello_action_id": str(case["trello_action_id"]),
        "parent_phase": "LAUNCHING",
    }
    evidence["launch_media_manifest_sha256"] = provider_media_sha256
    evidence["launch_scope_manifest_sha256"] = launch_binding_sha256
    conn = _connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        parent = conn.execute(
            """
            SELECT case_id
            FROM launch_recovery_cases AS parent
            WHERE parent.case_id = ? AND parent.trello_action_id = ?
              AND parent.card_id = ? AND parent.phase = 'LAUNCHING'
              AND parent.media_manifest_sha256 = ?
              AND parent.approved_by IS NOT NULL AND parent.approved_at IS NOT NULL
              AND parent.last_error IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM launch_recovery_cases AS newer
                  WHERE newer.card_id = parent.card_id
                    AND newer.trello_action_id <> parent.trello_action_id
                    AND newer.source_completed_at >= parent.source_completed_at
              )
            """,
            (
                plan.case_id,
                case["trello_action_id"],
                case["card_id"],
                approved_manifest_sha256,
            ),
        ).fetchone()
        if parent is None:
            raise RecoveryApplyBlocked("recovery_parent_pre_create_cas_lost")
        cursor = conn.execute(
            """
            UPDATE launch_recovery_city_plans
            SET reconcile_until = ?, capacity_available = ?, evidence_json = ?,
                last_rechecked_at = ?, updated_at = ?
            WHERE plan_id = ? AND case_id = ? AND phase = 'LAUNCHING'
              AND launch_attempt_key = ? AND account_kind = ? AND account_id = ?
              AND adset_id = ? AND expected_ad_names_json = ?
              AND expected_ad_count = ? AND media_manifest_sha256 = ?
              AND approved_by IS NOT NULL AND approved_at IS NOT NULL
              AND last_error IS NULL
            """,
            (
                checked_at.isoformat(),
                capacity,
                recovery._json_dumps(evidence),
                checked_at.isoformat(),
                checked_at.isoformat(),
                plan.plan_id,
                plan.case_id,
                plan.launch_attempt_key,
                plan.account_kind,
                plan.account_id,
                plan.adset_id,
                recovery._json_dumps(list(plan.expected_ad_names)),
                plan.expected_ad_count,
                approved_manifest_sha256,
            ),
        )
        if cursor.rowcount != 1:
            raise RecoveryApplyBlocked("recovery_plan_pre_create_cas_lost")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mark_recovery_create_started(
    plan: RecoveryCityPlan,
    approved_manifest_sha256: str,
) -> None:
    """CAS marker у самой provider CREATE-границы."""
    now = datetime.now(_TZ_LOCAL)
    conn = _connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM launch_recovery_city_plans WHERE plan_id = ?",
            (plan.plan_id,),
        ).fetchone()
        if (
            row is None
            or row["phase"] != "LAUNCHING"
            or row["launch_attempt_key"] != plan.launch_attempt_key
            or row["media_manifest_sha256"] != approved_manifest_sha256
        ):
            raise RecoveryApplyBlocked("recovery_create_started_cas_lost")
        evidence = json.loads(row["evidence_json"])
        if not isinstance(evidence, dict) or not evidence.get(
            "launch_media_manifest_sha256"
        ):
            raise RecoveryApplyBlocked("recovery_create_started_binding_missing")
        evidence.setdefault("provider_create_started_at", now.isoformat())
        cursor = conn.execute(
            """
            UPDATE launch_recovery_city_plans
            SET evidence_json = ?, updated_at = ?
            WHERE plan_id = ? AND phase = 'LAUNCHING'
              AND launch_attempt_key = ? AND media_manifest_sha256 = ?
            """,
            (
                recovery._json_dumps(evidence),
                now.isoformat(),
                plan.plan_id,
                plan.launch_attempt_key,
                approved_manifest_sha256,
            ),
        )
        if cursor.rowcount != 1:
            raise RecoveryApplyBlocked("recovery_create_started_cas_lost")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fresh_recheck_city_plan(
    plan_id: str,
    approved_manifest_sha256: str,
    checked_at: datetime,
) -> RecoveryCityPlan:
    """Под lock повторяет exact scope/names/window и CAS-фиксирует решение.

    Безопасный all-zero snapshot переводит план в ``LAUNCHING`` до CREATE.
    Полный найденный batch и любая неоднозначность отменяют CREATE.
    """
    manifest_sha256 = _exact_manifest_sha256(approved_manifest_sha256)
    checked_at = _aware_local(checked_at, "checked_at")
    initial_row = _load_plan_row(plan_id)
    initial_plan = _plan_from_row(initial_row)
    if checked_at < initial_plan.reconcile_from:
        raise ValueError("checked_at раньше reconcile_from")
    if initial_plan.phase not in _APPLYABLE_CITY_PHASES:
        return initial_plan
    if initial_plan.media_manifest_sha256 != manifest_sha256:
        return _block_snapshot(initial_row, "manifest_changed", checked_at)
    if initial_plan.provider_media_sha256 is None:
        return _block_snapshot(
            initial_row,
            "legacy_recovery_media_sha_requires_audit",
            checked_at,
        )
    if (
        initial_plan.expected_ad_count != len(initial_plan.expected_ad_names)
        or not initial_plan.account_id
        or not initial_plan.adset_id
    ):
        return _block_snapshot(initial_row, "durable_scope_invalid", checked_at)

    from services.adset_pause_guard import adset_mutation_lock

    with adset_mutation_lock(initial_plan.adset_id):
        # Повторное чтение после захвата lock закрывает DB race до provider calls.
        row = _load_plan_row(plan_id)
        plan = _plan_from_row(row)
        if plan.phase not in _APPLYABLE_CITY_PHASES:
            return plan
        if (
            plan.media_manifest_sha256 != manifest_sha256
            or plan.account_kind != initial_plan.account_kind
            or plan.account_id != initial_plan.account_id
            or plan.adset_id != initial_plan.adset_id
            or plan.expected_ad_names != initial_plan.expected_ad_names
            or plan.expected_ad_count != initial_plan.expected_ad_count
            or plan.reconcile_from != initial_plan.reconcile_from
        ):
            return _block_snapshot(row, "durable_scope_drift", checked_at)
        case = _load_case_row(plan.case_id)
        try:
            live_adsets = recovery.discover_live_recovery_adsets(("offline", "online"))
            _card, current_manifest = _current_scope_and_manifest(
                case,
                plan,
                live_adsets,
            )
            if (
                current_manifest.manifest_sha256 != manifest_sha256
                or str(case.get("media_manifest_sha256") or "") != manifest_sha256
            ):
                raise RecoveryApplyBlocked("manifest_changed")
            classification, found_ids, capacity = _fresh_inventory(plan, checked_at)
            other_reserved = _other_reserved_slots(plan.adset_id)
            reserve = _hard_reserve_slots()
        except Exception as exc:
            return _block_snapshot(row, _safe_error(exc), checked_at)

        if classification == "COMPLETE":
            next_phase = "WAITING_ACTIVE" if plan.phase == "LAUNCHING" else "COMPLETE"
        elif classification == "REVIEW_REQUIRED":
            next_phase = "REVIEW_REQUIRED"
        elif classification != "MISSING":
            return _block_snapshot(
                row, "fresh_inventory_classification_invalid", checked_at
            )
        elif plan.phase == "LAUNCHING" and plan.evidence.get(
            "provider_create_started_at"
        ):
            # После durable LAUNCHING provider CREATE уже мог завершиться, а
            # callback — нет. Один zero snapshot не доказывает отсутствие ad.
            next_phase = "REVIEW_REQUIRED"
        elif capacity < plan.expected_ad_count + reserve + other_reserved:
            next_phase = "WAITING_SLOT"
        else:
            next_phase = "LAUNCHING"

        attempt_key = plan.launch_attempt_key
        if next_phase == "LAUNCHING" and not attempt_key:
            attempt_key = str(uuid.uuid4())
        evidence = _safe_evidence(
            classification=classification,
            plan=plan,
            checked_at=checked_at,
            capacity_available=capacity,
            other_reserved_slots=other_reserved,
            found_ad_ids=found_ids,
        )
        completed_at = checked_at.isoformat() if next_phase == "COMPLETE" else None
        error = None
        if next_phase == "REVIEW_REQUIRED":
            error = (
                "post_create_inventory_missing_requires_manual_review"
                if classification == "MISSING" and plan.phase == "LAUNCHING"
                else "fresh_inventory_ambiguous"
            )
        conn = _connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE launch_recovery_city_plans
                SET reconcile_until = ?, phase = ?, found_ad_ids_json = ?,
                    launch_attempt_key = ?, capacity_available = ?, evidence_json = ?,
                    last_rechecked_at = ?, last_error = ?, updated_at = ?, completed_at = ?
                WHERE plan_id = ? AND case_id = ? AND phase = ?
                  AND account_kind = ? AND account_id = ? AND adset_id = ?
                  AND expected_ad_names_json = ? AND expected_ad_count = ?
                  AND reconcile_from = ? AND media_manifest_sha256 = ?
                  AND updated_at = ?
                """,
                (
                    checked_at.isoformat(),
                    next_phase,
                    recovery._json_dumps(list(found_ids)),
                    attempt_key,
                    capacity,
                    recovery._json_dumps(evidence),
                    checked_at.isoformat(),
                    error,
                    checked_at.isoformat(),
                    completed_at,
                    plan.plan_id,
                    plan.case_id,
                    plan.phase,
                    plan.account_kind,
                    plan.account_id,
                    plan.adset_id,
                    row["expected_ad_names_json"],
                    plan.expected_ad_count,
                    row["reconcile_from"],
                    manifest_sha256,
                    row["updated_at"],
                ),
            )
            if cursor.rowcount != 1:
                raise RecoveryApplyBlocked("recovery_plan_cas_lost")
            _sync_case_in_transaction(conn, plan.case_id, checked_at)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return _plan_from_row(_load_plan_row(plan.plan_id))


def _validate_apply_config(account_kind: str) -> tuple[dict[str, Any], int]:
    from services.autopilot import get_autopilot_config

    config = get_autopilot_config()
    recovery_config = config.get("recovery")
    if not isinstance(recovery_config, Mapping):
        raise RecoveryApplyBlocked("recovery_config_invalid")
    if (
        config.get("enabled") is not True
        or config.get("launch_enabled") is not True
        or config.get("kill_switch") is not False
        or config.get("mode") != "active"
        or recovery_config.get("enabled") is not True
    ):
        raise RecoveryApplyBlocked("recovery_launch_flags_not_enabled")
    account_kinds = recovery_config.get("managed_account_kinds")
    if not isinstance(account_kinds, list) or account_kind not in account_kinds:
        raise RecoveryApplyBlocked("recovery_account_kind_not_approved")
    configured_cap = recovery_config.get("max_cards_per_day", 1)
    if type(configured_cap) is not int or configured_cap < 1:
        raise RecoveryApplyBlocked("recovery_daily_cap_invalid")
    # Rollout-инвариант жёстче настройки: не более одной recovery case в день.
    return config, 1


def _claim_case_approval(
    case_id: str,
    manifest_sha256: str,
    actor: str,
    now: datetime,
    daily_cap: int,
) -> None:
    conn = _connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        case = conn.execute(
            "SELECT * FROM launch_recovery_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if case is None:
            raise RecoveryApplyBlocked("recovery_case_not_found")
        if str(case["media_manifest_sha256"] or "") != manifest_sha256:
            raise RecoveryApplyBlocked("manifest_changed")
        if case["phase"] in {"REVIEW_REQUIRED", "BLOCKED", "CANCELLED"}:
            raise RecoveryApplyBlocked("recovery_case_not_approvable")
        rows = conn.execute(
            "SELECT * FROM launch_recovery_city_plans WHERE case_id = ?",
            (case_id,),
        ).fetchall()
        if not rows:
            raise RecoveryApplyBlocked("recovery_case_without_city_plans")
        if any(str(row["media_manifest_sha256"]) != manifest_sha256 for row in rows):
            raise RecoveryApplyBlocked("manifest_changed")
        if any(
            row["phase"] in {"REVIEW_REQUIRED", "BLOCKED", "CANCELLED"} for row in rows
        ):
            raise RecoveryApplyBlocked("recovery_case_not_approvable")

        local_date = now.date().isoformat()
        approved_other = conn.execute(
            """
            SELECT COUNT(*)
            FROM launch_recovery_cases
            WHERE case_id <> ? AND approved_at IS NOT NULL
              AND substr(approved_at, 1, 10) = ?
            """,
            (case_id, local_date),
        ).fetchone()[0]
        if int(approved_other) >= daily_cap:
            raise RecoveryApplyBlocked("recovery_daily_case_cap_reached")

        conn.execute(
            """
            UPDATE launch_recovery_city_plans
            SET phase = CASE WHEN phase IN ('MISSING','WAITING_SLOT')
                             THEN 'APPROVED' ELSE phase END,
                approved_by = ?, approved_at = ?, updated_at = ?
            WHERE case_id = ?
              AND phase IN ('MISSING','WAITING_SLOT')
            """,
            (actor, now.isoformat(), now.isoformat(), case_id),
        )
        conn.execute(
            """
            UPDATE launch_recovery_cases
            SET phase = CASE WHEN phase IN ('DISCOVERED','WAITING_SLOT')
                             THEN 'APPROVED' ELSE phase END,
                approved_by = ?, approved_at = ?, updated_at = ?
            WHERE case_id = ?
            """,
            (actor, now.isoformat(), now.isoformat(), case_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normal_launch_slot_is_available(card_id: str) -> None:
    from services import auto_launch

    today = auto_launch._get_today_str()
    state = auto_launch._rollover_daily_state(today)
    launched_today = [str(value) for value in state.get("launched_today", [])]
    if launched_today and card_id not in launched_today:
        raise RecoveryApplyBlocked("daily_launch_cap_reached")


def _record_daily_launch_slot(card_id: str) -> None:
    from services import auto_launch

    today = auto_launch._get_today_str()
    with auto_launch._STATE_LOCK:
        state = auto_launch._load_auto_launch_state()
        if state.get("last_launch_date") != today:
            state["launched_today"] = []
        launched_today = state.setdefault("launched_today", [])
        if card_id not in launched_today:
            launched_today.append(card_id)
        state["last_launch_date"] = today
        auto_launch._save_auto_launch_state(state)


def _acquire_launch_lease():
    from services.auto_launch import _acquire_active_run_lease

    lease = _acquire_active_run_lease()
    if lease is None:
        raise RecoveryApplyBlocked("launch_already_running")
    return lease


def _release_launch_lease(lease) -> None:
    from services.auto_launch import _release_active_run_lease

    _release_active_run_lease(lease)


def _load_apply_context(plan: RecoveryCityPlan) -> _ApplyContext:
    case = _load_case_row(plan.case_id)
    card = _fetch_exact_card(case)
    return _ApplyContext(case=case, plan=plan, card=card)


def _record_created_ids(
    plan: RecoveryCityPlan,
    ad_ids: Sequence[str],
    now: datetime,
) -> None:
    normalized = tuple(_required_text(ad_id, "ad_id") for ad_id in ad_ids)
    if len(normalized) != plan.expected_ad_count or len(normalized) != len(
        set(normalized)
    ):
        raise RecoveryApplyBlocked("recovery_created_ids_mismatch")
    conn = _connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM launch_recovery_city_plans WHERE plan_id = ?",
            (plan.plan_id,),
        ).fetchone()
        if row is None or row["phase"] != "LAUNCHING":
            raise RecoveryApplyBlocked("recovery_plan_not_launching")
        if row["launch_attempt_key"] != plan.launch_attempt_key:
            raise RecoveryApplyBlocked("recovery_launch_attempt_drift")
        evidence = json.loads(row["evidence_json"])
        evidence["create_recorded"] = True
        evidence["created_ad_ids"] = list(normalized)
        evidence["created_at"] = now.isoformat()
        cursor = conn.execute(
            """
            UPDATE launch_recovery_city_plans
            SET phase = 'WAITING_ACTIVE', found_ad_ids_json = ?, evidence_json = ?,
                last_error = NULL, updated_at = ?
            WHERE plan_id = ? AND phase = 'LAUNCHING' AND launch_attempt_key = ?
              AND account_kind = ? AND account_id = ? AND adset_id = ?
              AND expected_ad_count = ? AND media_manifest_sha256 = ?
            """,
            (
                recovery._json_dumps(list(normalized)),
                recovery._json_dumps(evidence),
                now.isoformat(),
                plan.plan_id,
                plan.launch_attempt_key,
                plan.account_kind,
                plan.account_id,
                plan.adset_id,
                plan.expected_ad_count,
                plan.media_manifest_sha256,
            ),
        )
        if cursor.rowcount != 1:
            raise RecoveryApplyBlocked("recovery_create_callback_cas_lost")
        _sync_case_in_transaction(conn, plan.case_id, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _build_prepared_recovery_media(
    context: _ApplyContext,
    approved_manifest_sha256: str,
) -> tuple[dict[str, Any], recovery.MediaManifest, object]:
    """Готовит exact media mapping и city manifest до выдачи provider proof."""
    from integrations.facebook import LaunchMediaFileEvidence, LaunchMediaManifest

    media = recovery._download_media_for_card(context.card)
    if not isinstance(media, Mapping):
        raise RecoveryApplyBlocked("recovery_media_payload_invalid")
    prepared_media = dict(media)
    manifest = recovery._build_media_manifest_from_media(context.card, prepared_media)
    plan = context.plan
    if manifest.manifest_sha256 != approved_manifest_sha256:
        raise RecoveryApplyBlocked("manifest_changed_before_authorization")
    if manifest.provider_media_sha256 is None or manifest.media_type is None:
        raise RecoveryApplyBlocked("canonical_provider_media_sha_missing")
    if manifest.provider_media_sha256 != plan.provider_media_sha256:
        raise RecoveryApplyBlocked("recovery_provider_media_sha_drift")
    if manifest.expected_names_by_city.get(plan.city) != plan.expected_ad_names:
        raise RecoveryApplyBlocked("recovery_expected_names_drift")

    files = tuple(
        LaunchMediaFileEvidence(
            item.relative_name,
            item.size,
            item.content_sha256,
        )
        for item in manifest.files
    )
    card_name_sha256 = hashlib.sha256(
        str(context.case["card_name"]).encode("utf-8")
    ).hexdigest()
    scope_payload = {
        "media_type": manifest.media_type,
        "card_name_sha256": card_name_sha256,
        "city": plan.city,
        "account_kind": plan.account_kind,
        "account_id": plan.account_id,
        "adset_id": plan.adset_id,
        "expected_ad_names": list(plan.expected_ad_names),
        "files": [
            {
                "relative_name": item.relative_name,
                "size": item.size,
                "content_sha256": item.content_sha256,
            }
            for item in files
        ],
    }
    scope_sha256 = hashlib.sha256(
        json.dumps(
            scope_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    launch_manifest = LaunchMediaManifest(
        manifest_sha256=scope_sha256,
        media_type=manifest.media_type,
        card_name_sha256=card_name_sha256,
        city=plan.city,
        account_kind=plan.account_kind,
        account_id=plan.account_id,
        adset_id=plan.adset_id,
        expected_ad_names=plan.expected_ad_names,
        files=files,
    )
    return prepared_media, manifest, launch_manifest


def _issue_trusted_recovery_proof(
    context: _ApplyContext,
    approved_manifest_sha256: str,
    provider_media_sha256: str,
    actor: str,
):
    """Выдаёт ordinary proof только через атомарный repository recovery hook."""
    from services import launch_repository
    from services.launch_checker import (
        ProviderLaunchAuthorization,
        issue_trusted_recovery_authorization,
    )

    plan = context.plan
    case = context.case

    def issuer(plan_id: str, approved_sha: str, issuer_actor: str):
        if not plan.launch_attempt_key:
            raise RecoveryApplyBlocked("recovery_launch_attempt_missing")
        data = launch_repository.reserve_trusted_recovery_authorization(
            plan_id=plan_id,
            approved_manifest_sha256=approved_sha,
            launch_attempt_key=plan.launch_attempt_key,
            card_id=str(case["card_id"]),
            card_name=str(case["card_name"]),
            campaign_type=str(case["campaign_type"]),
            account_kind=plan.account_kind,
            account_id=plan.account_id,
            city=plan.city,
            adset_id=plan.adset_id,
            expected_ad_names=plan.expected_ad_names,
            expected_ad_count=plan.expected_ad_count,
            media_sha256=provider_media_sha256,
            actor=issuer_actor,
            now=datetime.now(timezone.utc),
        )
        return ProviderLaunchAuthorization(auth_id=data.auth_id, secret=data.secret)

    return issue_trusted_recovery_authorization(
        plan.plan_id,
        approved_manifest_sha256,
        actor,
        issuer=issuer,
    )


def _persist_recovery_launch_block(
    plan: RecoveryCityPlan,
    reason: str,
    *,
    reconcile_required: bool,
) -> None:
    """Фиксирует typed provider/checker block и запрещает слепой повтор."""
    now = datetime.now(_TZ_LOCAL)
    conn = _connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM launch_recovery_city_plans WHERE plan_id = ?",
            (plan.plan_id,),
        ).fetchone()
        if row is None:
            raise RecoveryApplyBlocked("recovery_plan_not_found")
        if row["phase"] != "LAUNCHING":
            conn.commit()
            return
        evidence = json.loads(str(row["evidence_json"]))
        if not isinstance(evidence, dict):
            raise RecoveryApplyBlocked("recovery_evidence_invalid")
        safe_reason = _safe_error(reason)
        create_started = bool(evidence.get("provider_create_started_at"))
        next_phase = (
            "REVIEW_REQUIRED"
            if reconcile_required or create_started
            else "BLOCKED"
        )
        evidence["launch_checker_block"] = {
            "reason": safe_reason,
            "reconcile_required": next_phase == "REVIEW_REQUIRED",
            "recorded_at": now.isoformat(),
        }
        cursor = conn.execute(
            """
            UPDATE launch_recovery_city_plans
            SET phase = ?, evidence_json = ?, last_error = ?,
                last_rechecked_at = ?, updated_at = ?
            WHERE plan_id = ? AND phase = 'LAUNCHING'
              AND launch_attempt_key = ? AND media_manifest_sha256 = ?
            """,
            (
                next_phase,
                recovery._json_dumps(evidence),
                safe_reason,
                now.isoformat(),
                now.isoformat(),
                plan.plan_id,
                plan.launch_attempt_key,
                plan.media_manifest_sha256,
            ),
        )
        if cursor.rowcount != 1:
            raise RecoveryApplyBlocked("recovery_launch_block_cas_lost")
        _sync_case_in_transaction(conn, plan.case_id, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _execute_launch(
    context: _ApplyContext,
    approved_manifest_sha256: str,
    *,
    actor: str,
) -> str:
    """Создаёт отдельный recovery proposal без staging и provider I/O."""
    from services.action_producer_gateway import propose_asset_recovery

    plan = context.plan
    case = context.case
    outcome = propose_asset_recovery(
        account_id=plan.account_id,
        adset_id=plan.adset_id,
        subject_id=plan.plan_id,
        city=plan.city,
        language=None,
        recovery_payload={
            "recovery_kind": "LAUNCH_CITY",
            "case_id": plan.case_id,
            "plan_id": plan.plan_id,
            "card_id": str(case["card_id"]),
            "campaign_type": str(case["campaign_type"]),
            "expected_ad_names": plan.expected_ad_names,
            "expected_ad_count": plan.expected_ad_count,
            "approved_manifest_sha256": approved_manifest_sha256,
        },
        source_ref=f"launch-recovery:{plan.plan_id}",
        actor=actor,
        # Ключ предложения не берём из launch_attempt_key: тот появляется только
        # с фазой LAUNCHING и живёт как CAS-токен строки плана. Durable binding
        # scope (launch-recovery:{plan_id}) → UUID4 даёт стабильный ключ ещё до
        # появления attempt-токена, поэтому один план = одно предложение.
        now=datetime.now(timezone.utc),
    )
    if outcome.receipt is None:
        raise RecoveryApplyBlocked("recovery_proposal_missing")
    return outcome.receipt.proposal_id


def _result_for_case(
    case_id: str,
    *,
    applied: Sequence[str] = (),
    created: Mapping[str, tuple[str, ...]] | None = None,
    skipped: Sequence[str] = (),
    errors: Sequence[str] = (),
) -> LaunchRecoveryResult:
    case = _load_case_row(case_id)
    return LaunchRecoveryResult(
        case_id=case_id,
        phase=str(case["phase"]),  # type: ignore[arg-type]
        applied_plan_ids=tuple(applied),
        created_ad_ids_by_plan=dict(created or {}),
        skipped_plan_ids=tuple(skipped),
        errors=tuple(_safe_error(error) for error in errors),
    )


def _apply_city_under_lease(
    plan_id: str,
    manifest_sha256: str,
    actor: str,
    *,
    approval_claimed: bool = False,
) -> LaunchRecoveryResult:
    initial = _plan_from_row(_load_plan_row(plan_id))
    del approval_claimed
    _validate_apply_config(initial.account_kind)
    context = _load_apply_context(initial)
    try:
        proposal_id = _execute_launch(
            context,
            manifest_sha256,
            actor=actor,
        )
    except RecoveryApplyBlocked as exc:
        return _result_for_case(initial.case_id, errors=(str(exc),))
    return _result_for_case(
        initial.case_id,
        skipped=(plan_id,),
        errors=(f"OWNER_PROPOSAL_PENDING:{proposal_id}",),
    )


def apply_recovery_city(
    plan_id: str,
    approved_manifest_sha256: str,
    *,
    actor: str,
) -> LaunchRecoveryResult:
    """Создаёт отдельный owner proposal для одного recovery city plan."""
    manifest_sha256 = _exact_manifest_sha256(approved_manifest_sha256)
    actor = _safe_error(_required_text(actor, "actor"))
    return _apply_city_under_lease(plan_id, manifest_sha256, actor)


def apply_recovery_case(
    case_id: str,
    approved_manifest_sha256: str,
    *,
    actor: str,
) -> LaunchRecoveryResult:
    """Создаёт независимые owner proposals для missing city plans."""
    case_id = _required_text(case_id, "case_id")
    manifest_sha256 = _exact_manifest_sha256(approved_manifest_sha256)
    actor = _safe_error(_required_text(actor, "actor"))
    applied: list[str] = []
    skipped: list[str] = []
    created: dict[str, tuple[str, ...]] = {}
    errors: list[str] = []
    rows = _load_case_plan_rows(case_id)
    if not rows:
        raise RecoveryApplyBlocked("recovery_case_without_city_plans")
    account_kinds = {str(row["account_kind"]) for row in rows}
    for account_kind in account_kinds:
        _validate_apply_config(account_kind)
    for row in rows:
        phase = str(row["phase"])
        plan_id = str(row["plan_id"])
        if phase in _TERMINAL_CITY_PHASES or phase == "WAITING_ACTIVE":
            skipped.append(plan_id)
            continue
        if phase not in _APPLYABLE_CITY_PHASES:
            skipped.append(plan_id)
            errors.append(f"{plan_id}:city_phase_not_applyable")
            continue
        result = _apply_city_under_lease(
            plan_id,
            manifest_sha256,
            actor,
            approval_claimed=True,
        )
        applied.extend(result.applied_plan_ids)
        skipped.extend(result.skipped_plan_ids)
        created.update(result.created_ad_ids_by_plan)
        errors.extend(result.errors)
    return _result_for_case(
        case_id,
        applied=applied,
        created=created,
        skipped=skipped,
        errors=errors,
    )


def verify_case_action_identity(case_id: str, trello_action_id: str) -> bool:
    """CLI gate: exact durable case должна принадлежать exact Trello action."""
    case = _load_case_row(case_id)
    return str(case["trello_action_id"]) == _required_text(
        trello_action_id, "trello_action_id"
    )
