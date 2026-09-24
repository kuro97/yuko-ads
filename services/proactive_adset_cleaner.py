"""Durable daily planner давления adset без destructive API.

Сервис только читает Facebook/SQLite, сохраняет dry-run manifest и отправляет
один summary. В модуле намеренно нет active-mode, claim или DELETE вызовов.
"""

from __future__ import annotations

import html
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Sequence, TypedDict

from agent.adset_discovery import discover_adsets
from integrations.facebook import get_adset_info
from services.adset_cleaner import (
    CLEANER_DEFAULTS,
    build_cleanup_candidate_record,
)
from services.cleanup_repository import (
    CleanupCandidateRecord,
    acquire_cleanup_run,
    finish_cleanup_run,
    sanitize_text,
    update_cleanup_run_evidence,
    upsert_cleanup_candidate,
)
from services.notifications import send_telegram

AccountKind = Literal["offline", "online"]
CleanupRunPhase = Literal[
    "PLANNED",
    "RUNNING",
    "COMPLETED",
    "COMPLETED_WITH_WARNINGS",
    "BLOCKED",
    "FAILED",
]

_TZ_LOCAL = timezone(timedelta(hours=5))
_MAX_ADS = 50
_LEASE_TTL = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class ProactiveCleanerConfig:
    proactive_enabled: bool
    stale_days: int
    target_free: int
    critical_free_slots: int
    max_manifest_candidates_per_adset: int
    alert_dedup_hours: int
    managed_account_kinds: tuple[AccountKind, ...]


@dataclass(frozen=True, slots=True)
class ManagedAdset:
    account_kind: AccountKind
    account_id: str
    source: Literal["fb_api", "unavailable"]
    adset_id: str
    adset_name: str
    effective_status: str
    inventory_complete: bool
    used: int | None
    available: int | None
    active_count: int | None
    ads: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class AdsetPressureRecord:
    account_kind: AccountKind
    adset_id: str
    adset_name: str
    used: int | None
    available: int | None
    active_count: int | None
    safe_candidate_count: int
    severity: Literal["ok", "warning", "critical"]
    reason: str | None


@dataclass(frozen=True, slots=True)
class CleanupManifest:
    run_id: str
    scheduled_date: date
    created_at: datetime
    adsets: tuple[ManagedAdset, ...]
    candidates: tuple[CleanupCandidateRecord, ...]
    pressure: tuple[AdsetPressureRecord, ...]
    errors: tuple[str, ...]


class CleanupCounters(TypedDict):
    discovered: int
    eligible: int
    would_delete: int
    deleted: int
    skipped: int
    warnings: int
    errors: int


class ProactiveCleanerResult(TypedDict):
    run_id: str
    ran: bool
    requested_mode: Literal["dry_run"]
    effective_mode: Literal["dry_run"]
    phase: CleanupRunPhase
    would_delete: list[str]
    skipped: list[dict[str, str]]
    warnings: list[str]
    errors: list[str]
    counters: CleanupCounters


def _empty_result(
    *,
    run_id: str,
    ran: bool,
    phase: CleanupRunPhase,
    errors: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> ProactiveCleanerResult:
    return {
        "run_id": run_id,
        "ran": ran,
        "requested_mode": "dry_run",
        "effective_mode": "dry_run",
        "phase": phase,
        "would_delete": [],
        "skipped": [],
        "warnings": list(warnings),
        "errors": list(errors),
        "counters": {
            "discovered": 0,
            "eligible": 0,
            "would_delete": 0,
            "deleted": 0,
            "skipped": 0,
            "warnings": len(warnings),
            "errors": len(errors),
        },
    }


def _strict_int(
    value: object,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field_name}_must_be_int_{minimum}_{maximum}")
    return value


def get_proactive_cleaner_config() -> ProactiveCleanerConfig:
    """Читает и строго валидирует только read-only proactive settings."""
    from services.autopilot import get_autopilot_config

    autopilot = get_autopilot_config()
    nested = autopilot.get("cleaner")
    raw = {**CLEANER_DEFAULTS, **(nested if isinstance(nested, dict) else {})}
    if type(raw.get("proactive_enabled")) is not bool:
        raise ValueError("proactive_enabled_must_be_literal_bool")
    stale_days = _strict_int(raw.get("stale_days"), "stale_days", minimum=15, maximum=365)
    target_free = _strict_int(raw.get("target_free"), "target_free", minimum=1, maximum=10)
    critical = _strict_int(
        raw.get("critical_free_slots"),
        "critical_free_slots",
        minimum=0,
        maximum=target_free,
    )
    manifest_cap = _strict_int(
        raw.get("max_manifest_candidates_per_adset"),
        "max_manifest_candidates_per_adset",
        minimum=1,
        maximum=10,
    )
    dedup_hours = _strict_int(
        raw.get("alert_dedup_hours"),
        "alert_dedup_hours",
        minimum=1,
        maximum=24,
    )
    threshold = raw.get("adset_threshold")
    if type(threshold) is not int or threshold != _MAX_ADS - target_free:
        raise ValueError("adset_threshold_target_free_mismatch")
    account_kinds = raw.get("managed_account_kinds")
    if not isinstance(account_kinds, list) or not account_kinds:
        raise ValueError("managed_account_kinds_must_be_nonempty_list")
    if any(type(kind) is not str or kind not in {"offline", "online"} for kind in account_kinds):
        raise ValueError("managed_account_kinds_contains_unknown")
    if len(account_kinds) != len(set(account_kinds)):
        raise ValueError("managed_account_kinds_contains_duplicate")
    return ProactiveCleanerConfig(
        proactive_enabled=raw["proactive_enabled"],
        stale_days=stale_days,
        target_free=target_free,
        critical_free_slots=critical,
        max_manifest_candidates_per_adset=manifest_cap,
        alert_dedup_hours=dedup_hours,
        managed_account_kinds=tuple(account_kinds),  # type: ignore[arg-type]
    )


def _account_scope(account_kind: AccountKind):
    from services.fb_token_provider import fb_account

    return fb_account("online") if account_kind == "online" else nullcontext()


def _flatten_discovered_adset_ids(discovered: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    leadgen = discovered.get("leadgen")
    if isinstance(leadgen, dict):
        for city in sorted(leadgen):
            pair = leadgen[city]
            if isinstance(pair, dict):
                for adset_type in ("L2", "L1"):
                    adset_id = pair.get(adset_type)
                    if type(adset_id) is str and adset_id:
                        ids.append(adset_id)
    mql = discovered.get("mql")
    if isinstance(mql, dict):
        for city in sorted(mql):
            adset_id = mql[city]
            if type(adset_id) is str and adset_id:
                ids.append(adset_id)
    return list(dict.fromkeys(ids))


def _exact_adset_status(adset_id: str) -> tuple[str, str]:
    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import get_fb_token

    response = _throttled_get(
        f"{API}/{adset_id}",
        params={
            "access_token": get_fb_token(),
            "fields": "id,name,effective_status",
        },
    )
    if getattr(response, "status_code", None) != 200:
        raise RuntimeError("adset_status_unavailable")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("id") != adset_id:
        raise RuntimeError("adset_status_scope_mismatch")
    name = payload.get("name")
    effective_status = payload.get("effective_status")
    if type(name) is not str or type(effective_status) is not str or not effective_status:
        raise RuntimeError("adset_status_incomplete")
    return name, effective_status.upper()


def _load_managed_adset(
    account_kind: AccountKind,
    account_id: str,
    adset_id: str,
) -> ManagedAdset:
    name, effective_status = _exact_adset_status(adset_id)
    info = get_adset_info(adset_id)
    if info.get("inventory_complete") is not True:
        raise RuntimeError("inventory_incomplete")
    ads = info.get("ads")
    if not isinstance(ads, list) or any(not isinstance(ad, dict) for ad in ads):
        raise RuntimeError("inventory_ads_invalid")
    ad_ids = [ad.get("id") for ad in ads]
    if any(type(ad_id) is not str or not ad_id for ad_id in ad_ids):
        raise RuntimeError("inventory_ad_id_invalid")
    if len(ad_ids) != len(set(ad_ids)):
        raise RuntimeError("inventory_duplicate_ad_id")
    unknown = info.get("unknown_effective_status_ids")
    if not isinstance(unknown, list) or unknown:
        raise RuntimeError("inventory_unknown_status")
    used = len(ads)
    if used > _MAX_ADS:
        raise RuntimeError("inventory_count_exceeds_limit")
    active_ids = info.get("effective_active_ids")
    if not isinstance(active_ids, list) or any(type(item) is not str for item in active_ids):
        raise RuntimeError("inventory_active_ids_invalid")
    if not set(active_ids).issubset(set(ad_ids)):
        raise RuntimeError("inventory_active_ids_mismatch")
    return ManagedAdset(
        account_kind=account_kind,
        account_id=account_id,
        source="fb_api",
        adset_id=adset_id,
        adset_name=name,
        effective_status=effective_status,
        inventory_complete=True,
        used=used,
        available=_MAX_ADS - used,
        active_count=len(active_ids),
        ads=tuple(dict(ad) for ad in ads),
    )


def discover_managed_adsets(
    config: ProactiveCleanerConfig,
) -> tuple[list[ManagedAdset], list[str]]:
    """Всегда делает force-refresh discovery и читает complete inventory."""
    managed: list[ManagedAdset] = []
    errors: list[str] = []
    for account_kind in config.managed_account_kinds:
        with _account_scope(account_kind):
            try:
                from services.fb_token_provider import get_fb_account_id

                account_id = str(get_fb_account_id()).removeprefix("act_")
                if not account_id:
                    raise RuntimeError("account_id_unavailable")
                discovered = discover_adsets(force_refresh=True)
                if not isinstance(discovered, dict) or discovered.get("source") != "fb_api":
                    raise RuntimeError("direct_fb_discovery_required")
                adset_ids = _flatten_discovered_adset_ids(discovered)
                if not adset_ids:
                    raise RuntimeError("managed_adsets_not_found")
            except Exception as exc:
                errors.append(f"{account_kind}:discovery:{sanitize_text(exc)}")
                continue

            for adset_id in adset_ids:
                try:
                    managed.append(_load_managed_adset(account_kind, account_id, adset_id))
                except Exception as exc:
                    errors.append(f"{account_kind}:{adset_id}:{sanitize_text(exc)}")
                    managed.append(
                        ManagedAdset(
                            account_kind=account_kind,
                            account_id=account_id,
                            source="unavailable",
                            adset_id=adset_id,
                            adset_name="",
                            effective_status="UNKNOWN",
                            inventory_complete=False,
                            used=None,
                            available=None,
                            active_count=None,
                            ads=(),
                        )
                    )
    managed.sort(
        key=lambda item: (
            item.available is None,
            item.available if item.available is not None else _MAX_ADS + 1,
            item.account_kind,
            item.adset_id,
        )
    )
    return managed, errors


def _pressure_for_adset(
    adset: ManagedAdset,
    config: ProactiveCleanerConfig,
    safe_candidate_count: int,
) -> AdsetPressureRecord:
    reason: str | None = None
    if adset.source != "fb_api":
        severity: Literal["ok", "warning", "critical"] = "critical"
        reason = "live_status_unavailable"
    elif not adset.inventory_complete or adset.available is None:
        severity = "critical"
        reason = "inventory_incomplete"
    elif adset.effective_status != "ACTIVE":
        severity = "critical"
        reason = "adset_not_active"
    elif adset.active_count is None or adset.active_count < 1:
        severity = "critical"
        reason = "effective_active_zero"
    elif adset.available <= config.critical_free_slots:
        severity = "critical"
        reason = "adset_full" if adset.available == 0 else "critical_capacity"
    elif adset.available <= config.target_free:
        severity = "warning"
        reason = "safe_capacity_deficit"
    else:
        severity = "ok"
    return AdsetPressureRecord(
        account_kind=adset.account_kind,
        adset_id=adset.adset_id,
        adset_name=adset.adset_name,
        used=adset.used,
        available=adset.available,
        active_count=adset.active_count,
        safe_candidate_count=safe_candidate_count,
        severity=severity,
        reason=reason,
    )


def build_proactive_manifest(
    run_id: str,
    adsets: Sequence[ManagedAdset],
    config: ProactiveCleanerConfig,
) -> CleanupManifest:
    """Оценивает exact zero evidence, но не создаёт claim/authorization."""
    candidates: list[CleanupCandidateRecord] = []
    pressure: list[AdsetPressureRecord] = []
    for adset in sorted(
        adsets,
        key=lambda item: (
            item.available is None,
            item.available if item.available is not None else _MAX_ADS + 1,
            item.adset_id,
        ),
    ):
        safe_count = 0
        if (
            adset.source == "fb_api"
            and adset.inventory_complete
            and adset.effective_status == "ACTIVE"
            and adset.active_count is not None
            and adset.active_count >= 1
            and adset.available is not None
        ):
            capacity = {
                "adset_id": adset.adset_id,
                "name": adset.adset_name,
                "available": adset.available,
                "inventory_complete": True,
                "effective_active_count": adset.active_count,
            }
            ordered_ads = sorted(
                adset.ads,
                key=lambda ad: (
                    str(ad.get("created_time") or ""),
                    str(ad.get("id") or ""),
                ),
            )
            for ordinal, ad in enumerate(ordered_ads):
                candidate = build_cleanup_candidate_record(
                    run_id=run_id,
                    account_kind=adset.account_kind,
                    adset=capacity,
                    ad=ad,
                    ordinal=ordinal,
                    stale_days=config.stale_days,
                )
                if candidate["state"] == "ELIGIBLE":
                    safe_count += 1
                    if safe_count > config.max_manifest_candidates_per_adset:
                        candidate = {
                            **candidate,
                            "state": "SKIPPED",
                            "reason": "manifest_candidate_limit",
                        }
                candidates.append(candidate)
        pressure.append(_pressure_for_adset(adset, config, safe_count))
    return CleanupManifest(
        run_id=run_id,
        scheduled_date=datetime.now(_TZ_LOCAL).date(),
        created_at=datetime.now(timezone.utc),
        adsets=tuple(adsets),
        candidates=tuple(candidates),
        pressure=tuple(pressure),
        errors=(),
    )


def _would_delete_ids(
    manifest: CleanupManifest,
    config: ProactiveCleanerConfig,
) -> list[str]:
    ids: list[str] = []
    for pressure in manifest.pressure:
        if pressure.available is None:
            continue
        deficit = max(config.target_free - pressure.available, 0)
        if deficit == 0:
            continue
        safe = [
            candidate
            for candidate in manifest.candidates
            if candidate["adset_id"] == pressure.adset_id
            and candidate["state"] == "ELIGIBLE"
        ]
        ids.extend(candidate["ad_id"] for candidate in safe[:deficit])
    return ids


def _send_pressure_summary(manifest: CleanupManifest) -> bool:
    alert_rows = [row for row in manifest.pressure if row.severity != "ok"]
    if not alert_rows:
        return True
    lines = [
        "⚠️ <b>Давление capacity adset</b>",
        f"Run: <code>{html.escape(manifest.run_id)}</code>",
    ]
    for row in alert_rows:
        lines.append(
            "• "
            f"{html.escape(row.account_kind)} / <code>{html.escape(row.adset_id)}</code>: "
            f"used={row.used}, available={row.available}, safe={row.safe_candidate_count}, "
            f"reason={html.escape(row.reason or row.severity)}"
        )
    return send_telegram("\n".join(lines)) is True


def run_proactive_cleaner(
    scheduled_date: date | None = None,
    tenant_id: str = "default",
) -> ProactiveCleanerResult:
    """Выполняет один durable read-only scan за tenant/calendar day."""
    if scheduled_date is None:
        scheduled_date = datetime.now(_TZ_LOCAL).date()
    if isinstance(scheduled_date, datetime) or not isinstance(scheduled_date, date):
        raise TypeError("scheduled_date должен быть date")
    if type(tenant_id) is not str or not tenant_id.strip():
        raise ValueError("tenant_id не должен быть пустым")
    provisional_run_id = f"proactive-unacquired-{uuid.uuid4().hex}"
    try:
        config = get_proactive_cleaner_config()
    except Exception as exc:
        return _empty_result(
            run_id=provisional_run_id,
            ran=False,
            phase="BLOCKED",
            errors=[f"invalid_config:{sanitize_text(exc)}"],
        )
    if not config.proactive_enabled:
        return _empty_result(
            run_id=provisional_run_id,
            ran=False,
            phase="BLOCKED",
            warnings=["proactive_disabled"],
        )

    lease_owner = f"proactive-{uuid.uuid4().hex}"
    lease = acquire_cleanup_run(
        tenant_id.strip(),
        "PROACTIVE_DAILY",
        scheduled_date,
        None,
        lease_owner,
        _LEASE_TTL,
        {
            **asdict(config),
            "managed_account_kinds": list(config.managed_account_kinds),
            "requested_mode": "dry_run",
            "effective_mode": "dry_run",
        },
    )
    if not lease.acquired:
        result = _empty_result(
            run_id=lease.run_id,
            ran=False,
            phase=lease.phase,
            warnings=[lease.reason or "daily_run_not_acquired"],
        )
        return result

    errors: list[str] = []
    warnings: list[str] = []
    try:
        adsets, discovery_errors = discover_managed_adsets(config)
        errors.extend(discovery_errors)
        manifest = replace(
            build_proactive_manifest(lease.run_id, adsets, config),
            scheduled_date=scheduled_date,
            errors=tuple(discovery_errors),
        )
        for candidate in manifest.candidates:
            upsert_cleanup_candidate(
                lease.run_id,
                candidate,
                lease_owner=lease_owner,
            )
        would_delete = _would_delete_ids(manifest, config)
        warnings.extend(
            f"{row.severity}:{row.account_kind}:{row.adset_id}:{row.reason}"
            for row in manifest.pressure
            if row.severity != "ok"
        )
        update_cleanup_run_evidence(
            lease.run_id,
            {
                "scheduled_date": scheduled_date.isoformat(),
                "adsets": [
                    {
                        "account_kind": item.account_kind,
                        "account_id": item.account_id,
                        "source": item.source,
                        "adset_id": item.adset_id,
                        "adset_name": item.adset_name,
                        "effective_status": item.effective_status,
                        "inventory_complete": item.inventory_complete,
                        "used": item.used,
                        "available": item.available,
                        "active_count": item.active_count,
                    }
                    for item in manifest.adsets
                ],
                "pressure": [asdict(item) for item in manifest.pressure],
                "alert_keys": [
                    f"cleaner:{item.severity}:{item.account_kind}:{item.adset_id}:{item.reason}"
                    for item in manifest.pressure
                    if item.severity != "ok"
                ],
            },
            lease_owner=lease_owner,
        )
        counters: CleanupCounters = {
            "discovered": len(manifest.candidates),
            "eligible": sum(item["state"] == "ELIGIBLE" for item in manifest.candidates),
            "would_delete": len(would_delete),
            "deleted": 0,
            "skipped": sum(item["state"] != "ELIGIBLE" for item in manifest.candidates),
            "warnings": len(warnings),
            "errors": len(errors),
        }
        phase: CleanupRunPhase = (
            "COMPLETED_WITH_WARNINGS" if errors or warnings else "COMPLETED"
        )
        finish_cleanup_run(
            lease.run_id,
            phase,
            counters,
            errors,
            lease_owner=lease_owner,
        )
        if warnings and not _send_pressure_summary(manifest):
            warnings.append("pressure_alert_not_sent")
            counters["warnings"] = len(warnings)
        return {
            "run_id": lease.run_id,
            "ran": True,
            "requested_mode": "dry_run",
            "effective_mode": "dry_run",
            "phase": phase,
            "would_delete": would_delete,
            "skipped": [
                {"ad_id": item["ad_id"], "reason": item["reason"]}
                for item in manifest.candidates
                if item["state"] != "ELIGIBLE"
            ],
            "warnings": warnings,
            "errors": errors,
            "counters": counters,
        }
    except Exception as exc:
        errors.append(f"proactive_scan_failed:{sanitize_text(exc)}")
        counters = {
            "discovered": 0,
            "eligible": 0,
            "would_delete": 0,
            "deleted": 0,
            "skipped": 0,
            "warnings": len(warnings),
            "errors": len(errors),
        }
        try:
            finish_cleanup_run(
                lease.run_id,
                "FAILED",
                counters,
                errors,
                lease_owner=lease_owner,
            )
        except Exception as finish_exc:
            errors.append(f"finish_failed:{sanitize_text(finish_exc)}")
            counters["errors"] = len(errors)
        result = _empty_result(
            run_id=lease.run_id,
            ran=True,
            phase="FAILED",
            errors=errors,
            warnings=warnings,
        )
        result["counters"] = counters
        return result
