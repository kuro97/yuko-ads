"""Типизированные read-only ответы cleaner/recovery status API."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from services.launch_recovery import RecoveryCasePhase


class CleanupRunResponse(BaseModel):
    run_id: str
    run_kind: Literal["PROACTIVE_DAILY", "REPLACEMENT_SLOT", "MANUAL_DRY_RUN"]
    workflow_id: str | None
    scheduled_date: date
    requested_mode: Literal["dry_run", "active"]
    effective_mode: Literal["dry_run", "active"]
    phase: Literal[
        "PLANNED",
        "RUNNING",
        "COMPLETED",
        "COMPLETED_WITH_WARNINGS",
        "BLOCKED",
        "FAILED",
    ]
    discovered_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    would_delete_count: int = Field(ge=0)
    deleted_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None


class AdsetPressureResponse(BaseModel):
    account_kind: Literal["offline", "online"]
    adset_id: str
    adset_name: str
    live_status: Literal["ok", "unavailable", "incomplete", "unknown_status"]
    used: int | None = Field(default=None, ge=0, le=50)
    available: int | None = Field(default=None, ge=0, le=50)
    active_count: int | None = Field(default=None, ge=0)
    safe_candidate_count: int = Field(ge=0)
    deficit_to_target: int | None = Field(default=None, ge=0, le=50)
    severity: Literal["ok", "warning", "critical"]
    reason: str | None


class CleanupClaimResponse(BaseModel):
    claim_id: str
    run_id: str
    workflow_id: str
    ad_id: str
    adset_id: str
    purpose: Literal["REPLACEMENT_SLOT"]
    state: Literal["CLAIMED", "DELETE_FAILED", "RECONCILE_REQUIRED"]
    claimed_at: datetime
    error: str | None


class ReplacementWorkflowResponse(BaseModel):
    workflow_id: str
    old_ad_id: str
    adset_id: str
    phase: Literal[
        "WAITING_SLOT",
        "WAITING_CARD",
        "LAUNCHING",
        "WAITING_ACTIVE",
        "READY_TO_PAUSE",
        "BLOCKED",
    ]
    replacement_ad_id: str | None
    age_hours: float = Field(ge=0)
    severity: Literal["ok", "warning", "critical"]
    last_error: str | None


class RecoveryCityPlanResponse(BaseModel):
    plan_id: str
    city: str
    account_kind: Literal["offline", "online"]
    account_id: str
    adset_id: str
    expected_ad_names: list[str]
    expected_ad_count: int = Field(ge=1)
    reconcile_from: datetime
    reconcile_until: datetime
    phase: Literal[
        "DISCOVERED",
        "COMPLETE",
        "MISSING",
        "REVIEW_REQUIRED",
        "WAITING_SLOT",
        "APPROVED",
        "LAUNCHING",
        "WAITING_ACTIVE",
        "RECOVERED",
        "BLOCKED",
        "CANCELLED",
    ]
    found_ad_ids: list[str]
    media_manifest_sha256: str
    launch_attempt_key: str | None
    capacity_available: int | None = Field(default=None, ge=0, le=50)
    last_rechecked_at: datetime | None
    last_error: str | None


class LaunchRecoveryCaseResponse(BaseModel):
    case_id: str
    card_id: str
    card_name: str
    source_completed_at: datetime
    phase: RecoveryCasePhase
    missing_cities: list[str]
    media_manifest_sha256: str | None
    last_error: str | None
    city_plans: list[RecoveryCityPlanResponse]


class CleanerStatusResponse(BaseModel):
    generated_at: datetime
    cleaner_enabled: bool
    proactive_enabled: bool
    proactive_mode: Literal["disabled", "dry_run"]
    replacement_delete_enabled: bool
    last_run: CleanupRunResponse | None
    adsets: list[AdsetPressureResponse]
    unresolved_claims: list[CleanupClaimResponse]
    replacement_workflows: list[ReplacementWorkflowResponse]
    recovery_cases: list[LaunchRecoveryCaseResponse]


class DisabledCleanupResponse(BaseModel):
    detail: str
