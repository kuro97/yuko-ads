"""Fail-closed cleaner нулевых PAUSED-объявлений.

Cleaner по умолчанию строит только manifest. Необратимый DELETE разрешён лишь
для конкретного replacement workflow и после тройного подтверждения нулевой
доставки: живой Facebook lifetime, локальная KB и повторная проверка под lock.
"""

from __future__ import annotations

import logging
import math
import hashlib
import json
import sqlite3
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict

from integrations.facebook import (
    CleanupDeleteReconcileRequired,
    cleanup_stale_ads,
    get_adset_capacity,
    get_cleanup_capacity,
)
from services.adset_pause_guard import adset_mutation_lock
from services.cleanup_authorization import (
    issue_delete_authorization,
    revoke_delete_authorization,
)
from services.cleanup_repository import (
    CleanupCandidateRecord,
    acquire_cleanup_run,
    claim_replacement_slot_delete,
    finish_cleanup_run,
    upsert_cleanup_candidate,
    update_cleanup_run_evidence,
)
from services.replacement_workflow import (
    get_replacement_launch,
    get_workflow,
    mark_slot_available,
    mark_workflow_blocked,
    sanitize_text,
)

logger = logging.getLogger(__name__)

_TENANT = "default"
_ACTOR = "adset_cleaner"
_TZ_UTC = timezone.utc
_DECISIONS_DB_PATH = Path(__file__).parent.parent / "data" / "decisions.db"
_SAFE_EFFECTIVE_STATUSES = {"PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED"}
_MIN_STALE_DAYS = 15

CLEANER_DEFAULTS = {
    "enabled": False,
    "dry_run": True,
    "proactive_enabled": False,
    "stale_days": 15,
    "adset_threshold": 45,
    "target_free": 5,
    "critical_free_slots": 2,
    "hard_reserve_slots": 1,
    "allow_irreversible_delete": False,
    "max_manifest_candidates_per_adset": 10,
    "max_deletes_per_workflow": 2,
    "alert_dedup_hours": 6,
    "managed_account_kinds": ["offline"],
}


@dataclass(frozen=True)
class CleanerEvidence:
    ad_id: str
    adset_id: str
    status: str
    effective_status: str
    age_days: int
    lifetime_spend: float
    lifetime_impressions: int
    lifetime_clicks: int
    checked_at: str


class LocalZeroEvidence(TypedDict):
    kb_found: bool
    local_spend_usd: float | None
    local_impressions: int | None
    local_clicks: int | None
    local_leads: int | None
    local_payments: int | None
    any_positive_delivery: bool
    any_positive_outcome: bool
    complete: bool
    error: str | None


class CandidateReferenceEvidence(TypedDict):
    complete: bool
    replacement_reference: bool
    prior_delete_claim: bool
    error: str | None


def get_cleaner_config() -> dict[str, Any]:
    """Возвращает полный cleaner config поверх единых безопасных defaults.

    ``get_autopilot_config`` уже deep-merge-ит nested settings, а повторный merge
    здесь сохраняет fail-safe контракт для тестовых/legacy callers.
    """
    from services.autopilot import get_autopilot_config

    autopilot = get_autopilot_config()
    nested = autopilot.get("cleaner")
    cleaner = {**CLEANER_DEFAULTS, **(nested if isinstance(nested, dict) else {})}
    cleaner["kill_switch"] = autopilot.get("kill_switch", False)
    replacement = autopilot.get("replacement")
    cleaner["replacement_enabled"] = (
        replacement.get("enabled", False) if isinstance(replacement, dict) else False
    )
    cleaner["config_generation"] = autopilot.get("config_generation")
    return cleaner


def _safe_error(value: object) -> str:
    """Единая граница: наружу никогда не выходит сырой exception с секретом."""
    return sanitize_text(value)


def _literal_bool(config: dict[str, Any], field_name: str) -> bool:
    value = config.get(field_name)
    if type(value) is not bool:
        raise ValueError(f"{field_name}_must_be_literal_bool")
    return value


def _stale_days(config: dict[str, Any]) -> int:
    value = config.get("stale_days")
    if type(value) is not int:
        raise ValueError("stale_days_must_be_int")
    return max(value, _MIN_STALE_DAYS)


def _active_gate_error(config: dict[str, Any]) -> str | None:
    """Проверяет destructive-гейты без truthy/coercion."""
    try:
        if not _literal_bool(config, "enabled"):
            return "cleaner_disabled"
        if _literal_bool(config, "dry_run"):
            return "cleaner_dry_run_enabled"
        if not _literal_bool(config, "allow_irreversible_delete"):
            return "irreversible_delete_not_allowed"
        if not _literal_bool(config, "replacement_enabled"):
            return "replacement_disabled"
        if _literal_bool(config, "kill_switch"):
            return "kill_switch"
        stale_days = config.get("stale_days")
        if type(stale_days) is not int or stale_days < _MIN_STALE_DAYS:
            return "stale_days_below_15"
    except ValueError as exc:
        return _safe_error(exc)
    return None


def cleanup_runtime_config_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Возвращает generation/hash текущего destructive-конфига.

    Хэш покрывает все cleaner-настройки и оба внешних стоп-крана. Generation
    опционален для старых settings-файлов; при его отсутствии изменение всё
    равно обнаруживается по хэшу.
    """
    gate_error = _active_gate_error(config)
    if gate_error is not None:
        raise ValueError(f"destructive_gate_closed:{gate_error}")
    generation = config.get("config_generation")
    if generation is not None and (
        isinstance(generation, bool)
        or not isinstance(generation, (int, str))
        or (isinstance(generation, str) and not generation.strip())
    ):
        raise ValueError("config_generation_invalid")
    canonical = {
        field: config.get(field)
        for field in (
            *CLEANER_DEFAULTS,
            "kill_switch",
            "replacement_enabled",
            "config_generation",
        )
    }
    try:
        payload = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("cleanup_runtime_config_not_canonical") from exc
    return {
        "config_generation": generation,
        "config_hash": hashlib.sha256(payload).hexdigest(),
    }


def _strict_nonnegative_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name}_must_be_nonnegative_int")
    return value


def _validate_capacity(payload: object, expected_adset_id: str) -> dict[str, Any]:
    """Проверяет полный и внутренне согласованный snapshot ёмкости."""
    if not isinstance(payload, dict):
        raise ValueError("capacity_not_object")
    required = {
        "adset_id",
        "name",
        "ad_count",
        "max_ads",
        "available",
        "stale_ads",
        "ad_ids",
        "effective_active_ids",
        "effective_active_count",
        "inventory_complete",
    }
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"capacity_missing:{','.join(sorted(missing))}")
    adset_id = str(payload.get("adset_id") or "")
    if adset_id != expected_adset_id:
        raise ValueError("capacity_adset_mismatch")
    if not isinstance(payload.get("name"), str):
        raise ValueError("capacity_name_not_string")
    ad_count = _strict_nonnegative_int(payload.get("ad_count"), "ad_count")
    max_ads = _strict_nonnegative_int(payload.get("max_ads"), "max_ads")
    available = _strict_nonnegative_int(payload.get("available"), "available")
    if max_ads < 1 or ad_count > max_ads or available != max_ads - ad_count:
        raise ValueError("capacity_count_inconsistent")
    stale_ads = payload.get("stale_ads")
    if not isinstance(stale_ads, list) or any(not isinstance(ad, dict) for ad in stale_ads):
        raise ValueError("capacity_stale_ads_invalid")
    if payload.get("inventory_complete") is not True:
        raise ValueError("capacity_inventory_incomplete")
    ad_ids = payload.get("ad_ids")
    active_ids = payload.get("effective_active_ids")
    active_count = payload.get("effective_active_count")
    if (
        not isinstance(ad_ids, list)
        or any(not isinstance(ad_id, str) or not ad_id for ad_id in ad_ids)
        or len(set(ad_ids)) != len(ad_ids)
        or len(ad_ids) != ad_count
    ):
        raise ValueError("capacity_ad_ids_invalid")
    if (
        not isinstance(active_ids, list)
        or any(not isinstance(ad_id, str) or not ad_id for ad_id in active_ids)
        or len(set(active_ids)) != len(active_ids)
        or not set(active_ids).issubset(set(ad_ids))
        or type(active_count) is not int
        or active_count != len(active_ids)
    ):
        raise ValueError("capacity_effective_active_invalid")
    return dict(payload)


def _ad_age_days(created_time: str) -> int:
    """Считает число полных суток с момента создания объявления."""
    created = datetime.fromisoformat(str(created_time).replace("+0000", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=_TZ_UTC)
    return (datetime.now(created.tzinfo) - created).days


def _finite_number(value: object, field_name: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name} отсутствует")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} не является конечным числом")
    return number


def _response_payload(response: Any, context: str) -> dict[str, Any]:
    if response.status_code != 200:
        raise RuntimeError(f"{context}: FB HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"{context}: FB вернул не JSON object")
    return payload


def fetch_live_zero_spend_evidence_with_reason(
    ad_id: str,
    adset_id: str,
) -> tuple[CleanerEvidence | None, str]:
    """Подтверждает exact PAUSED ad и непустой lifetime insights с нулевой доставкой.

    Пустой, неоднозначный или непарсибельный ответ не является доказательством нуля.
    """
    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import get_fb_token

    ad_id = str(ad_id or "").strip()
    adset_id = str(adset_id or "").strip()
    if not ad_id or not adset_id:
        return None, "invalid_ad_scope"

    try:
        exact_response = _throttled_get(
            f"{API}/{ad_id}",
            params={
                "access_token": get_fb_token(),
                "fields": "id,adset_id,status,effective_status,created_time",
            },
        )
        exact = _response_payload(exact_response, f"ad {ad_id}")
        if str(exact.get("id") or "") != ad_id:
            raise RuntimeError("FB не вернул exact ad")
        if str(exact.get("adset_id") or "") != adset_id:
            raise RuntimeError("ad принадлежит другому adset")

        status = str(exact.get("status") or "").upper()
        effective_status = str(exact.get("effective_status") or "").upper()
        created_time = str(exact.get("created_time") or "")
        if not status or not effective_status or not created_time:
            raise RuntimeError("неполные live-поля ad")

        insights_response = _throttled_get(
            f"{API}/{ad_id}/insights",
            params={
                "access_token": get_fb_token(),
                "date_preset": "maximum",
                "fields": "spend,impressions,clicks",
                "limit": 2,
            },
        )
        insights_payload = _response_payload(insights_response, f"insights {ad_id}")
        rows = insights_payload.get("data")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return None, "ambiguous_lifetime_insights"
        paging = insights_payload.get("paging")
        if paging is not None:
            if not isinstance(paging, dict):
                return None, "ambiguous_lifetime_insights"
            if paging.get("next"):
                return None, "ambiguous_lifetime_insights"
        row = rows[0]
        try:
            spend = _finite_number(row.get("spend"), "spend")
            impressions_number = _finite_number(row.get("impressions"), "impressions")
            clicks_number = _finite_number(row.get("clicks"), "clicks")
        except (TypeError, ValueError):
            return None, "invalid_metric"
        if spend < 0 or impressions_number < 0 or clicks_number < 0:
            return None, "invalid_metric"
        if not impressions_number.is_integer() or not clicks_number.is_integer():
            return None, "invalid_metric"

        return (
            CleanerEvidence(
                ad_id=ad_id,
                adset_id=adset_id,
                status=status,
                effective_status=effective_status,
                age_days=_ad_age_days(created_time),
                lifetime_spend=spend,
                lifetime_impressions=int(impressions_number),
                lifetime_clicks=int(clicks_number),
                checked_at=datetime.now(_TZ_UTC).isoformat(timespec="seconds"),
            ),
            "ok",
        )
    except Exception as exc:
        logger.warning(
            "Не удалось подтвердить lifetime zero для ad=%s: %s",
            ad_id,
            _safe_error(exc),
        )
        return None, "live_evidence_unavailable"


def fetch_live_zero_spend_evidence(
    ad_id: str,
    adset_id: str,
) -> CleanerEvidence | None:
    """Обратносуместимый wrapper для callers, которым не нужна причина veto."""
    evidence, _reason = fetch_live_zero_spend_evidence_with_reason(ad_id, adset_id)
    return evidence


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ?",
        ("table",),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _local_number(
    value: object,
    field_name: str,
    *,
    allow_none: bool = False,
    integer: bool = False,
) -> float | None:
    """SQLite evidence принимает только реальные numeric-типы, не TEXT coercion."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name}_must_be_numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name}_must_be_finite_nonnegative")
    if integer and not number.is_integer():
        raise ValueError(f"{field_name}_must_be_integer")
    return number


def load_local_zero_evidence(ad_id: str) -> LocalZeroEvidence:
    """Читает четыре локальных источника в SQLite mode=ro.

    Нет KB, требуемой таблицы или parseable spend — evidence неполное, DELETE
    запрещён. Все запросы параметризованы.
    """
    incomplete: LocalZeroEvidence = {
        "kb_found": False,
        "local_spend_usd": None,
        "local_impressions": None,
        "local_clicks": None,
        "local_leads": None,
        "local_payments": None,
        "any_positive_delivery": False,
        "any_positive_outcome": False,
        "complete": False,
        "error": None,
    }
    required_tables = {
        "creative_kb",
        "ad_daily_metrics",
        "ad_hourly_metrics",
        "decisions",
    }

    try:
        conn = sqlite3.connect(f"file:{_DECISIONS_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            missing = required_tables - _table_names(conn)
            if missing:
                return {**incomplete, "error": f"missing_tables:{','.join(sorted(missing))}"}

            required_provenance = {"lead_semantics_version", "lead_parse_status"}
            for table_name in ("ad_daily_metrics", "ad_hourly_metrics"):
                columns = {
                    str(row[1])
                    for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                }
                missing_columns = required_provenance - columns
                if missing_columns:
                    return {
                        **incomplete,
                        "error": (
                            f"missing_lead_provenance:{table_name}:"
                            f"{','.join(sorted(missing_columns))}"
                        ),
                    }

            kb = conn.execute(
                "SELECT * FROM creative_kb WHERE ad_id = ?",
                (ad_id,),
            ).fetchone()
            if kb is None:
                return {**incomplete, "error": "kb_not_found"}
            kb_columns = set(kb.keys())
            kb_spend = _local_number(kb["spend"], "creative_kb.spend")
            kb_impressions = _local_number(
                kb["impressions"], "creative_kb.impressions", integer=True
            )
            kb_clicks = _local_number(kb["clicks"], "creative_kb.clicks", integer=True)
            kb_leads = (
                _local_number(kb["leads"], "creative_kb.leads", integer=True)
                if "leads" in kb_columns
                else 0.0
            )
            kb_outcomes = [
                _local_number(kb[field], f"creative_kb.{field}", allow_none=True)
                for field in ("payments", "romi", "revenue", "payments_erp", "revenue_erp_lcy")
                if field in kb_columns
            ]
            payment_values = [
                _local_number(kb[field], f"creative_kb.{field}", allow_none=True, integer=True)
                for field in ("payments", "payments_erp")
                if field in kb_columns
            ]

            daily_rows = conn.execute(
                "SELECT * FROM ad_daily_metrics WHERE ad_id = ?",
                (ad_id,),
            ).fetchall()
            hourly_rows = conn.execute(
                "SELECT * FROM ad_hourly_metrics WHERE ad_id = ?",
                (ad_id,),
            ).fetchall()
            if any(
                int(row["lead_semantics_version"]) != 2
                or row["lead_parse_status"] != "ok"
                for row in (*daily_rows, *hourly_rows)
            ):
                return {**incomplete, "error": "untrusted_lead_semantics"}
            decision_rows = conn.execute(
                "SELECT * FROM decisions WHERE ad_id = ?",
                (ad_id,),
            ).fetchall()
            daily_values = [
                (
                    _local_number(row["spend"], "ad_daily_metrics.spend"),
                    _local_number(row["impressions"], "ad_daily_metrics.impressions", integer=True),
                    _local_number(row["clicks"], "ad_daily_metrics.clicks", integer=True),
                    _local_number(row["leads"], "ad_daily_metrics.leads", integer=True)
                    if "leads" in row.keys()
                    else 0.0,
                )
                for row in daily_rows
            ]
            hourly_values = [
                (
                    _local_number(row["spend"], "ad_hourly_metrics.spend"),
                    _local_number(row["impressions"], "ad_hourly_metrics.impressions", integer=True),
                    _local_number(row["clicks"], "ad_hourly_metrics.clicks", integer=True),
                    _local_number(row["actions_lead"], "ad_hourly_metrics.actions_lead", integer=True)
                    if "actions_lead" in row.keys()
                    else 0.0,
                )
                for row in hourly_rows
            ]
            decision_values = [
                (
                    _local_number(row["spend"], "decisions.spend", allow_none=True),
                    _local_number(row["romi"], "decisions.romi", allow_none=True),
                    _local_number(row["leads"], "decisions.leads", allow_none=True, integer=True)
                    if "leads" in row.keys()
                    else None,
                )
                for row in decision_rows
            ]

            daily_spend = sum(row[0] for row in daily_values)
            hourly_spend = sum(row[0] for row in hourly_values)
            delivery_values = (
                kb_spend,
                kb_impressions,
                kb_clicks,
                *(value for row in daily_values for value in row),
                *(value for row in hourly_values for value in row),
                *(row[0] for row in decision_values if row[0] is not None),
            )
            any_positive_outcome = any(value is not None and value > 0 for value in kb_outcomes)
            any_positive_outcome = any_positive_outcome or any(
                row[1] is not None and row[1] > 0 for row in decision_values
            )
            local_impressions = int(max(
                kb_impressions,
                sum(row[1] for row in daily_values),
                sum(row[1] for row in hourly_values),
            ))
            local_clicks = int(max(
                kb_clicks,
                sum(row[2] for row in daily_values),
                sum(row[2] for row in hourly_values),
            ))
            local_leads = int(max(
                kb_leads,
                sum(row[3] for row in daily_values),
                sum(row[3] for row in hourly_values),
                sum(row[2] or 0 for row in decision_values),
            ))
            non_null_payments = [value for value in payment_values if value is not None]
            local_payments = (
                int(max(non_null_payments)) if non_null_payments else None
            )
            return {
                "kb_found": True,
                # Источники перекрываются по времени, поэтому не суммируем их дважды.
                "local_spend_usd": max(kb_spend, daily_spend, hourly_spend),
                "local_impressions": local_impressions,
                "local_clicks": local_clicks,
                "local_leads": local_leads,
                "local_payments": local_payments,
                "any_positive_delivery": any(value > 0 for value in delivery_values),
                "any_positive_outcome": any_positive_outcome,
                "complete": True,
                "error": None,
            }
        finally:
            conn.close()
    except Exception as exc:
        safe_error = _safe_error(exc)
        logger.warning("Локальная zero-evidence недоступна для ad=%s: %s", ad_id, safe_error)
        return {**incomplete, "error": safe_error}


def _is_in_kb(ad_id: str) -> bool:
    """Обратносуместимый безопасный helper для старых callers/tests."""
    return load_local_zero_evidence(ad_id)["kb_found"]


def _has_payments(ad_id: str) -> bool:
    """Fail-closed helper: неполная evidence эквивалентна запрету удаления."""
    local = load_local_zero_evidence(ad_id)
    return not local["complete"] or local["any_positive_outcome"]


def is_safe_zero_candidate(
    ad: dict[str, Any],
    live: CleanerEvidence,
    local: LocalZeroEvidence,
) -> tuple[bool, str]:
    """Объединяет live и local evidence без оптимистичных defaults."""
    if str(ad.get("id") or ad.get("ad_id") or "") != live.ad_id:
        return False, "ad_id_mismatch"
    if live.status != "PAUSED":
        return False, "configured_status_not_paused"
    if live.effective_status not in _SAFE_EFFECTIVE_STATUSES:
        return False, "unsafe_effective_status"
    if live.lifetime_spend != 0.0:
        return False, "positive_lifetime_spend"
    if live.lifetime_impressions != 0 or live.lifetime_clicks != 0:
        return False, "positive_lifetime_delivery"
    if not local["complete"] or not local["kb_found"]:
        return False, local.get("error") or "local_evidence_incomplete"
    if local["any_positive_delivery"]:
        return False, "positive_local_delivery"
    if local["any_positive_outcome"]:
        return False, "positive_local_outcome"
    return True, "confirmed_zero_spend"


def load_candidate_reference_evidence(ad_id: str) -> CandidateReferenceEvidence:
    """Fail-closed проверяет open workflow references и любой прошлый claim."""
    incomplete: CandidateReferenceEvidence = {
        "complete": False,
        "replacement_reference": False,
        "prior_delete_claim": False,
        "error": None,
    }
    try:
        conn = sqlite3.connect(f"file:{_DECISIONS_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            required = {
                "ad_replacement_workflows",
                "ad_replacement_launch_links",
                "ad_cleanup_delete_claims",
            }
            missing = required - _table_names(conn)
            if missing:
                return {
                    **incomplete,
                    "error": f"missing_reference_tables:{','.join(sorted(missing))}",
                }
            workflows = conn.execute(
                """
                SELECT workflow.old_ad_id, workflow.replacement_ad_id,
                       workflow.released_ad_id, launch.created_ad_ids_json
                FROM ad_replacement_workflows AS workflow
                LEFT JOIN ad_replacement_launch_links AS launch
                  ON launch.workflow_id = workflow.workflow_id
                WHERE workflow.phase NOT IN ('COMPLETED','CANCELLED')
                """,
            ).fetchall()
            replacement_reference = False
            for workflow in workflows:
                direct_references = {
                    str(workflow[field]).strip()
                    for field in ("old_ad_id", "replacement_ad_id", "released_ad_id")
                    if workflow[field] is not None and str(workflow[field]).strip()
                }
                created_ids: list[str] = []
                if workflow["created_ad_ids_json"] is not None:
                    parsed = json.loads(str(workflow["created_ad_ids_json"]))
                    if not isinstance(parsed, list) or any(
                        not isinstance(created_id, str) or not created_id.strip()
                        for created_id in parsed
                    ):
                        raise ValueError("invalid_open_workflow_created_ad_ids")
                    created_ids = [created_id.strip() for created_id in parsed]
                    if len(created_ids) != len(set(created_ids)):
                        raise ValueError("duplicate_open_workflow_created_ad_ids")
                if ad_id in direct_references or ad_id in created_ids:
                    replacement_reference = True
                    break
            prior_claim = conn.execute(
                "SELECT 1 FROM ad_cleanup_delete_claims WHERE ad_id = ? LIMIT 1",
                (ad_id,),
            ).fetchone()
            return {
                "complete": True,
                "replacement_reference": replacement_reference,
                "prior_delete_claim": prior_claim is not None,
                "error": None,
            }
        finally:
            conn.close()
    except Exception as exc:
        return {**incomplete, "error": _safe_error(exc)}


def build_cleanup_candidate_record(
    *,
    run_id: str,
    account_kind: Literal["offline", "online"],
    adset: dict[str, Any],
    ad: dict[str, Any],
    ordinal: int,
    stale_days: int,
) -> CleanupCandidateRecord:
    """Строит одну durable candidate row из exact live/local evidence.

    ``run_id`` входит в evidence только для трассировки и не является правом
    DELETE. Capability здесь не создаётся.
    """
    ad_id = str(ad.get("id") or "").strip()
    adset_id = str(adset.get("adset_id") or "").strip()
    configured_status = str(ad.get("status") or "").upper() or None
    effective_status = str(ad.get("effective_status") or "").upper() or None
    reason = "eligible"
    live: CleanerEvidence | None = None
    local: LocalZeroEvidence | None = None
    references: CandidateReferenceEvidence | None = None

    if not ad_id or str(ad.get("adset_id") or "") != adset_id:
        reason = "ad_scope_mismatch"
    elif configured_status != "PAUSED":
        reason = "configured_status_not_paused"
    elif effective_status not in _SAFE_EFFECTIVE_STATUSES:
        reason = "unsafe_effective_status"
    else:
        live, live_reason = fetch_live_zero_spend_evidence_with_reason(ad_id, adset_id)
        if live is None:
            reason = live_reason
        elif live.age_days < max(stale_days, _MIN_STALE_DAYS):
            reason = "recent_ad"
        elif live.lifetime_spend != 0.0 or (
            live.lifetime_impressions != 0 or live.lifetime_clicks != 0
        ):
            reason = "live_nonzero"
        else:
            local = load_local_zero_evidence(ad_id)
            if not local["complete"] or not local["kb_found"]:
                reason = local.get("error") or "local_evidence_incomplete"
            elif any(
                local.get(field) is None
                for field in (
                    "local_spend_usd",
                    "local_impressions",
                    "local_clicks",
                    "local_leads",
                    "local_payments",
                )
            ):
                reason = "local_metric_unknown"
            elif local["any_positive_delivery"] or local["any_positive_outcome"]:
                reason = "local_nonzero"
            else:
                references = load_candidate_reference_evidence(ad_id)
                if not references["complete"]:
                    reason = references.get("error") or "reference_evidence_incomplete"
                elif references["replacement_reference"]:
                    reason = "replacement_reference"
                elif references["prior_delete_claim"]:
                    reason = "prior_delete_claim"

    eligible = reason == "eligible"
    return {
        "account_kind": account_kind,
        "adset_id": adset_id,
        "adset_name": str(adset.get("name") or ""),
        "ad_id": ad_id or f"invalid-{ordinal}",
        "ad_name": str(ad.get("name") or ""),
        "ordinal": ordinal,
        "state": "ELIGIBLE" if eligible else "SKIPPED",
        "reason": "exact_zero_evidence" if eligible else reason,
        "configured_status": live.status if live else configured_status,
        "effective_status": live.effective_status if live else effective_status,
        "age_days": live.age_days if live else None,
        "lifetime_spend_usd": live.lifetime_spend if live else None,
        "lifetime_impressions": live.lifetime_impressions if live else None,
        "lifetime_clicks": live.lifetime_clicks if live else None,
        "local_spend_usd": local.get("local_spend_usd") if local else None,
        "local_impressions": local.get("local_impressions") if local else None,
        "local_clicks": local.get("local_clicks") if local else None,
        "local_leads": local.get("local_leads") if local else None,
        "local_payments": local.get("local_payments") if local else None,
        "evidence": {
            "run_id": run_id,
            "inventory_complete": adset.get("inventory_complete") is True,
            "lifetime_row_count": 1 if live else 0,
            "local_evidence_complete": bool(local and local["complete"]),
            "kb_present": bool(local and local["kb_found"]),
            "active_count": adset.get("effective_active_count"),
            "live": asdict(live) if live else {},
            "local": dict(local) if local else {},
            "references": dict(references) if references else {},
        },
        "capacity_before": int(adset.get("available", 0)),
    }


def _manifest_row(
    ad: dict[str, Any],
    adset: dict[str, Any],
    *,
    reason: str,
    live: CleanerEvidence | None = None,
    local: LocalZeroEvidence | None = None,
) -> dict[str, Any]:
    row = {
        "ad_id": str(ad.get("id") or ad.get("ad_id") or ""),
        "ad_name": str(ad.get("name") or ad.get("ad_name") or ""),
        "adset_id": str(adset.get("adset_id") or ""),
        "adset_name": str(adset.get("name") or ""),
        "age_days": live.age_days if live else None,
        "reason": reason,
    }
    if live is not None:
        row["live_evidence"] = asdict(live)
    if local is not None:
        row["local_evidence"] = dict(local)
    return row


def select_safe_candidates(
    adset: dict[str, Any],
    need: int,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Сканирует старые ads до заполнения ``need`` безопасными кандидатами.

    Ключевой инвариант: сначала safety-фильтры, затем остановка по need. Поэтому
    unsafe первые строки не мешают refill следующими старыми объявлениями.
    """
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if need <= 0:
        return selected, skipped
    adset_id = str(adset.get("adset_id") or "")
    adset = _validate_capacity(adset, adset_id)
    if int(adset.get("ad_count", 0)) < int(cfg["adset_threshold"]):
        return selected, skipped

    minimum_age_days = _stale_days(cfg)
    for ad in adset.get("stale_ads") or []:
        if len(selected) >= need:
            break
        try:
            raw_age = _ad_age_days(str(ad.get("created_time") or ""))
        except Exception:
            skipped.append(_manifest_row(ad, adset, reason="invalid_created_time"))
            continue
        if raw_age < minimum_age_days:
            skipped.append(_manifest_row(ad, adset, reason="recent_ad"))
            continue
        if str(ad.get("status") or "").upper() != "PAUSED":
            skipped.append(_manifest_row(ad, adset, reason="configured_status_not_paused"))
            continue

        live = fetch_live_zero_spend_evidence(str(ad.get("id") or ""), adset_id)
        if live is None:
            skipped.append(_manifest_row(ad, adset, reason="zero_spend_unproven"))
            continue
        if live.age_days < minimum_age_days:
            skipped.append(_manifest_row(ad, adset, reason="recent_ad", live=live))
            continue
        local = load_local_zero_evidence(live.ad_id)
        safe, reason = is_safe_zero_candidate(ad, live, local)
        row = _manifest_row(ad, adset, reason=reason, live=live, local=local)
        if safe:
            selected.append(row)
        else:
            skipped.append(row)

    return selected, skipped


def _empty_result(*, ran: bool, reason: str | None, run_id: str) -> dict[str, Any]:
    return {
        "ran": ran,
        "run_id": run_id,
        "skipped_reason": reason,
        "mode": "active",
        "phase": "BLOCKED" if reason else "RUNNING",
        "action": "BLOCKED" if reason else None,
        "deleted": [],
        "would_delete": [],
        "skipped": [],
        "errors": [],
        "capacity_before": {},
        "capacity_after": {},
        "deficit_before": None,
        "deficit_after": None,
        "claim_ids": [],
    }


def _block_workflow(workflow_id: str, reason: str, errors: list[str]) -> None:
    try:
        mark_workflow_blocked(workflow_id, reason)
    except Exception as exc:
        errors.append(f"mark_workflow_blocked({workflow_id}): {_safe_error(exc)}")


def _validate_cleanup_workflow(
    workflow: object,
    workflow_id: str,
    adset_id: str,
) -> dict[str, Any]:
    if not isinstance(workflow, dict):
        raise ValueError("workflow_not_found")
    if str(workflow.get("workflow_id") or "") != workflow_id:
        raise ValueError("workflow_id_mismatch")
    if str(workflow.get("adset_id") or "") != adset_id:
        raise ValueError("workflow_adset_mismatch")
    if workflow.get("phase") != "WAITING_SLOT":
        raise ValueError("workflow_not_waiting_slot")
    if str(workflow.get("released_ad_id") or "").strip():
        raise ValueError("workflow_already_has_released_ad")
    if str(workflow.get("replacement_ad_id") or "").strip():
        raise ValueError("workflow_already_has_replacement")
    return workflow


def _account_scope(account_kind: str):
    from services.fb_token_provider import fb_account

    return fb_account("online") if account_kind == "online" else nullcontext()


def _other_reserved_slots(workflow_id: str, adset_id: str) -> int:
    """Считает durable reservations других связанных workflow fail-closed."""
    conn = sqlite3.connect(f"file:{_DECISIONS_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        required = {"ad_replacement_workflows", "ad_replacement_launch_links"}
        missing = required - _table_names(conn)
        if missing:
            raise RuntimeError("replacement_reservations_unavailable")
        rows = conn.execute(
            """
            SELECT link.expected_ad_count
            FROM ad_replacement_workflows AS workflow
            JOIN ad_replacement_launch_links AS link
              ON link.workflow_id = workflow.workflow_id
            WHERE workflow.workflow_id <> ?
              AND workflow.adset_id = ?
              AND workflow.phase IN ('WAITING_CARD','LAUNCHING','WAITING_ACTIVE')
            """,
            (workflow_id, adset_id),
        ).fetchall()
        return sum(_strict_nonnegative_int(row["expected_ad_count"], "reservation") for row in rows)
    finally:
        conn.close()


def _finish_slot_run(
    result: dict[str, Any],
    *,
    phase: Literal["COMPLETED", "COMPLETED_WITH_WARNINGS", "BLOCKED", "FAILED"],
    lease_owner: str,
) -> None:
    counters = {
        "discovered": len(result["would_delete"]) + len(result["skipped"]),
        "eligible": len(result["would_delete"]),
        "would_delete": len(result["would_delete"]),
        "deleted": len(result["deleted"]),
        "skipped": len(result["skipped"]),
        "warnings": 0,
        "errors": len(result["errors"]),
    }
    finish_cleanup_run(
        result["run_id"],
        phase,
        counters,
        result["errors"],
        lease_owner=lease_owner,
    )
    result["phase"] = phase


def _plan_workflow_slot(
    result: dict[str, Any],
    cfg: dict[str, Any],
    workflow: dict[str, Any],
    link: dict[str, Any],
) -> None:
    """Освобождает exact deficit последовательными durable single-ad DELETE."""
    workflow_id = str(workflow["workflow_id"])
    adset_id = str(workflow["adset_id"])
    account_kind = str(link.get("account_kind") or "")
    account_id = str(link.get("account_id") or "").removeprefix("act_")
    expected_count = link.get("expected_ad_count")
    max_deletes = cfg.get("max_deletes_per_workflow")
    hard_reserve = cfg.get("hard_reserve_slots")
    if (
        account_kind not in {"offline", "online"}
        or not account_id
        or type(expected_count) is not int
        or expected_count < 1
        or type(max_deletes) is not int
        or not 1 <= max_deletes <= 5
        or type(hard_reserve) is not int
        or not 1 <= hard_reserve <= 5
    ):
        result["errors"].append("invalid_bound_workflow")
        _block_workflow(workflow_id, "invalid_bound_workflow", result["errors"])
        return

    lease_owner = f"slot-cleaner-{uuid.uuid4().hex}"
    initial_config_identity = cleanup_runtime_config_identity(cfg)
    with adset_mutation_lock(adset_id), _account_scope(account_kind):
        from services.fb_token_provider import get_fb_account_id

        live_account_id = str(get_fb_account_id()).removeprefix("act_")
        if live_account_id != account_id:
            result["errors"].append("workflow_account_mismatch")
            _block_workflow(workflow_id, "workflow_account_mismatch", result["errors"])
            return
        latest_workflow = _validate_cleanup_workflow(
            get_workflow(workflow_id), workflow_id, adset_id
        )
        latest_link = get_replacement_launch(workflow_id)
        if latest_link != link or latest_workflow != workflow:
            result["errors"].append("workflow_binding_drift")
            _block_workflow(workflow_id, "workflow_binding_drift", result["errors"])
            return
        other_reserved = _other_reserved_slots(workflow_id, adset_id)
        capacity = _validate_capacity(
            get_adset_capacity(adset_id, stale_days=_stale_days(cfg)),
            adset_id,
        )
        available = int(capacity["available"])
        available_after_reservations = max(available - other_reserved, 0)
        required_slots = expected_count + hard_reserve
        deficit = max(required_slots - available_after_reservations, 0)
        result["capacity_before"][adset_id] = available
        result["capacity_after"][adset_id] = available
        result["deficit_before"] = deficit
        result["deficit_after"] = deficit

        lease = acquire_cleanup_run(
            _TENANT,
            "REPLACEMENT_SLOT",
            date.today(),
            workflow_id,
            lease_owner,
            timedelta(minutes=15),
            {
                "requested_mode": "active",
                "effective_mode": "active",
                "enabled": cfg["enabled"],
                "dry_run": cfg["dry_run"],
                "allow_irreversible_delete": cfg["allow_irreversible_delete"],
                "replacement_enabled": cfg["replacement_enabled"],
                "kill_switch": cfg["kill_switch"],
                "stale_days": _stale_days(cfg),
                "hard_reserve_slots": hard_reserve,
                "max_deletes_per_workflow": max_deletes,
                "other_reserved_slots": other_reserved,
                **initial_config_identity,
            },
        )
        result["run_id"] = lease.run_id
        if not lease.acquired:
            result["errors"].append(lease.reason or "slot_run_not_acquired")
            result["phase"] = lease.phase
            return
        result["_lease_owner"] = lease_owner

        if deficit == 0:
            update_cleanup_run_evidence(
                lease.run_id,
                {
                    "required_slots": required_slots,
                    "available": available,
                    "other_reserved_slots": other_reserved,
                    "slot_deficit": 0,
                },
                lease_owner=lease_owner,
            )
            mark_slot_available(workflow_id)
            result["action"] = "NO_DELETE_REQUIRED"
            result["deficit_after"] = 0
            _finish_slot_run(result, phase="COMPLETED", lease_owner=lease_owner)
            return
        if deficit > max_deletes:
            reason = "slot_deficit_exceeds_cap"
            result["errors"].append(reason)
            _block_workflow(workflow_id, reason, result["errors"])
            _finish_slot_run(result, phase="BLOCKED", lease_owner=lease_owner)
            return

        candidates: list[CleanupCandidateRecord] = []
        skipped: list[CleanupCandidateRecord] = []
        ordered_stale = sorted(
            capacity["stale_ads"],
            key=lambda ad: (str(ad.get("created_time") or ""), str(ad.get("id") or "")),
        )
        for ordinal, ad in enumerate(ordered_stale):
            candidate = build_cleanup_candidate_record(
                run_id=lease.run_id,
                account_kind=account_kind,  # type: ignore[arg-type]
                adset=capacity,
                ad=ad,
                ordinal=ordinal,
                stale_days=_stale_days(cfg),
            )
            upsert_cleanup_candidate(
                lease.run_id,
                candidate,
                lease_owner=lease_owner,
            )
            (candidates if candidate["state"] == "ELIGIBLE" else skipped).append(candidate)
        result["would_delete"] = candidates[:deficit]
        result["skipped"] = skipped
        update_cleanup_run_evidence(
            lease.run_id,
            {
                "required_slots": required_slots,
                "available": available,
                "available_after_other_reservations": available_after_reservations,
                "other_reserved_slots": other_reserved,
                "slot_deficit": deficit,
                "safe_candidate_count": len(candidates),
                "preflight_covers_deficit": len(candidates) >= deficit,
            },
            lease_owner=lease_owner,
        )
        if len(candidates) < deficit:
            reason = "safe_preflight_below_exact_deficit"
            result["errors"].append(reason)
            result["action"] = "WAITING_SAFE_CANDIDATES"
            _finish_slot_run(
                result,
                phase="COMPLETED_WITH_WARNINGS",
                lease_owner=lease_owner,
            )
            return

        # Claim создаётся только после полного preflight. Каждый следующий
        # кандидат заново доказывается на свежем capacity snapshot.
        candidate_ids = [candidate["ad_id"] for candidate in candidates[:deficit]]
        last_deleted_id: str | None = None
        for candidate_id in candidate_ids:
            latest_cfg = get_cleaner_config()
            gate_error = _active_gate_error(latest_cfg)
            if gate_error is not None:
                raise RuntimeError(f"destructive_gate_drift:{gate_error}")
            if cleanup_runtime_config_identity(latest_cfg) != initial_config_identity:
                raise RuntimeError("cleaner_config_generation_or_hash_drift")
            if any(
                latest_cfg.get(field) != cfg.get(field)
                for field in (
                    "stale_days",
                    "hard_reserve_slots",
                    "max_deletes_per_workflow",
                )
            ):
                raise RuntimeError("cleaner_config_drift")
            if _other_reserved_slots(workflow_id, adset_id) != other_reserved:
                raise RuntimeError("replacement_reservations_drift")
            current_workflow = _validate_cleanup_workflow(
                get_workflow(workflow_id), workflow_id, adset_id
            )
            current_link = get_replacement_launch(workflow_id)
            if current_workflow != workflow or current_link != link:
                raise RuntimeError("workflow_binding_drift")

            current_capacity = _validate_capacity(
                get_adset_capacity(adset_id, stale_days=_stale_days(cfg)),
                adset_id,
            )
            current_available = int(current_capacity["available"])
            current_deficit = max(
                required_slots - max(current_available - other_reserved, 0),
                0,
            )
            if current_deficit <= 0:
                break
            stale_by_id = {
                str(ad.get("id") or ""): ad for ad in current_capacity["stale_ads"]
            }
            current_ad = stale_by_id.get(candidate_id)
            if current_ad is None:
                raise RuntimeError("preflight_candidate_drift")
            ordinal = next(
                candidate["ordinal"]
                for candidate in candidates
                if candidate["ad_id"] == candidate_id
            )
            current_candidate = build_cleanup_candidate_record(
                run_id=lease.run_id,
                account_kind=account_kind,  # type: ignore[arg-type]
                adset=current_capacity,
                ad=current_ad,
                ordinal=ordinal,
                stale_days=_stale_days(cfg),
            )
            if current_candidate["state"] != "ELIGIBLE":
                raise RuntimeError(
                    f"candidate_recheck_failed:{current_candidate['reason']}"
                )
            upsert_cleanup_candidate(
                lease.run_id,
                current_candidate,
                lease_owner=lease_owner,
            )

            claim = claim_replacement_slot_delete(
                lease.run_id,
                workflow_id,
                current_candidate,
                _ACTOR,
                lease_owner,
            )
            result["claim_ids"].append(claim.claim_id)
            authorization = issue_delete_authorization(claim)
            try:
                guarded_capacity = _validate_capacity(
                    get_cleanup_capacity(
                        adset_id,
                        stale_days=_stale_days(cfg),
                        candidate_id=candidate_id,
                    ),
                    adset_id,
                )
                exact_ads = [
                    ad
                    for ad in guarded_capacity["stale_ads"]
                    if str(ad.get("id") or "") == candidate_id
                ]
                if len(exact_ads) != 1:
                    raise RuntimeError("cleanup_exact_candidate_missing")
                guard_evidence = guarded_capacity.get("cleanup_guard_evidence")
                if guard_evidence is None:
                    raise RuntimeError("cleanup_guard_evidence_missing")
                deleted_ids = cleanup_stale_ads(
                    exact_ads,
                    1,
                    guard_evidence=guard_evidence,
                    authorization=authorization,
                )
            except CleanupDeleteReconcileRequired:
                reason = "cleanup_delete_reconcile_required"
                result["errors"].append(reason)
                result["action"] = "BLOCKED"
                result["phase"] = "BLOCKED"
                _block_workflow(workflow_id, reason, result["errors"])
                return
            except Exception:
                revoke_delete_authorization(claim.claim_id)
                raise
            if deleted_ids != [candidate_id]:
                raise RuntimeError("cleanup_delete_result_mismatch")
            result["deleted"].append(candidate_id)
            last_deleted_id = candidate_id

        final_capacity = _validate_capacity(
            get_adset_capacity(adset_id, stale_days=_stale_days(cfg)),
            adset_id,
        )
        final_available = int(final_capacity["available"])
        final_deficit = max(
            required_slots - max(final_available - other_reserved, 0),
            0,
        )
        result["capacity_after"][adset_id] = final_available
        result["deficit_after"] = final_deficit
        if final_deficit != 0:
            raise RuntimeError("slot_deficit_not_closed")
        mark_slot_available(workflow_id, last_deleted_id)
        result["action"] = "SLOTS_RELEASED"
        _finish_slot_run(result, phase="COMPLETED", lease_owner=lease_owner)


def run_cleaner(mode: str = "dry_run", workflow_id: str | None = None) -> dict[str, Any]:
    """Планирует exact slot только для concrete replacement workflow.

    Daily dry-run вынесен в ``services.proactive_adset_cleaner``. Active путь
    разрешён только для exact replacement workflow с durable authorization.
    """
    run_id = f"cleaner-{uuid.uuid4().hex}"
    if mode != "active" or not workflow_id:
        return _empty_result(
            ran=False,
            reason="workflow_active_only",
            run_id=run_id,
        )
    try:
        cfg = get_cleaner_config()
        _literal_bool(cfg, "enabled")
        _literal_bool(cfg, "dry_run")
        _literal_bool(cfg, "allow_irreversible_delete")
        _literal_bool(cfg, "kill_switch")
        _literal_bool(cfg, "replacement_enabled")
        _stale_days(cfg)
    except Exception as exc:
        result = _empty_result(ran=False, reason="invalid_config", run_id=run_id)
        result["errors"].append(_safe_error(exc))
        return result

    gate_reason = _active_gate_error(cfg)
    if gate_reason:
        result = _empty_result(ran=False, reason=gate_reason, run_id=run_id)
        _block_workflow(workflow_id, gate_reason, result["errors"])
        return result

    result = _empty_result(ran=True, reason=None, run_id=run_id)
    try:
        workflow = get_workflow(workflow_id)
        link = get_replacement_launch(workflow_id)
    except Exception as exc:
        workflow = None
        link = None
        result["errors"].append(f"get_workflow:{_safe_error(exc)}")
    if workflow is None or link is None:
        reason = "bound_workflow_required"
        result["errors"].append(reason)
        _block_workflow(workflow_id, reason, result["errors"])
        return result
    try:
        _validate_cleanup_workflow(workflow, workflow_id, str(workflow["adset_id"]))
        _plan_workflow_slot(result, cfg, workflow, link)
    except Exception as exc:
        reason = f"workflow_slot_plan_failed:{_safe_error(exc)}"
        result["errors"].append(reason)
        _block_workflow(workflow_id, reason, result["errors"])
        lease_owner = result.get("_lease_owner")
        if isinstance(lease_owner, str) and result.get("phase") == "RUNNING":
            try:
                _finish_slot_run(
                    result,
                    phase="BLOCKED",
                    lease_owner=lease_owner,
                )
            except Exception as finish_exc:
                result["errors"].append(
                    f"slot_run_finish_failed:{_safe_error(finish_exc)}"
                )
    result.pop("_lease_owner", None)
    return result
