"""Атомарный runner обязательных runtime-миграций SQLite.

Файлы миграций считаются неизменяемыми после применения: контрольная сумма
вычисляется по исходным байтам. Любое расхождение или повреждение схемы
останавливает запуск приложения.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator


APPLICATION_ID = "yuko-owner-approval"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_FILES: dict[int, Path] = {
    19: PROJECT_ROOT / "migrations" / "019_launch_checker_authorizations.sql",
    20: PROJECT_ROOT / "migrations" / "020_asset_recovery_authorizations.sql",
    21: PROJECT_ROOT / "migrations" / "021_action_producer_effects.sql",
    22: PROJECT_ROOT / "migrations" / "022_owner_approval_redesign.sql",
    23: PROJECT_ROOT / "migrations" / "023_verifier_digest_feedback.sql",
    24: PROJECT_ROOT / "migrations" / "024_ad_weekly_cohorts.sql",
    25: PROJECT_ROOT / "migrations" / "025_cohort_revenue.sql",
    26: PROJECT_ROOT / "migrations" / "026_system_approval.sql",
    27: PROJECT_ROOT / "migrations" / "027_trello_completion.sql",
    28: PROJECT_ROOT / "migrations" / "028_coverage_prodb_groups.sql",
}

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version > 0),
    name TEXT NOT NULL UNIQUE,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    applied_at TEXT NOT NULL,
    application_id TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """Базовая ошибка runtime-миграций."""


class MigrationChecksumMismatch(MigrationError):
    """Записанная checksum не совпадает с текущими байтами миграции."""


class MigrationForeignKeyViolation(MigrationError):
    """После миграции обнаружена нарушенная внешняя ссылка."""


class SchemaVerificationError(MigrationError):
    """Обязательная runtime-схема отсутствует или повреждена."""


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    """Результат проверки одной миграции."""

    version: int
    name: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Отчёт runner без изменяемых коллекций."""

    db_path: str
    applied: tuple[MigrationRecord, ...]
    already_applied: tuple[MigrationRecord, ...]

    @property
    def applied_versions(self) -> tuple[int, ...]:
        return tuple(record.version for record in self.applied)

    @property
    def verified_versions(self) -> tuple[int, ...]:
        records = (*self.already_applied, *self.applied)
        return tuple(sorted(record.version for record in records))


@dataclass(frozen=True, slots=True)
class SchemaHealth:
    """Успешный результат полной проверки runtime-схемы."""

    db_path: str
    healthy: bool
    verified_versions: tuple[int, ...]
    table_count: int
    index_count: int
    trigger_count: int


@dataclass(frozen=True, slots=True)
class _ForeignKeySpec:
    target_table: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]
    on_delete: str


@dataclass(frozen=True, slots=True)
class _UniqueIndexSpec:
    table: str
    columns: tuple[str, ...]
    partial: bool


# Независимый от SQL-файлов manifest. Порядок колонок тоже является контрактом.
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "schema_migrations": (
        "version", "name", "content_sha256", "applied_at", "application_id",
    ),
    "launch_authorizations": (
        "auth_id", "secret_sha256", "card_id", "card_name", "source",
        "checker_mode", "campaign_type", "account_kind", "account_id",
        "plan_sha256", "media_sha256", "phase", "topic_override",
        "topic_override_reason", "actor", "recovery_plan_id", "created_at",
        "updated_at", "expires_at", "finished_at",
    ),
    "launch_authorization_targets": (
        "auth_id", "city", "ordinal", "account_kind", "account_id", "adset_id",
        "identity_key", "expected_names_json", "expected_ads_count",
        "reserved_slots", "phase", "reservation_expires_at", "created_at",
        "updated_at",
    ),
    "launch_authorization_ads": (
        "auth_id", "city", "ordinal", "ad_name", "ad_name_key", "account_id",
        "adset_id", "phase", "claim_id", "created_ad_id", "create_started_at",
        "created_at", "updated_at",
    ),
    "launch_check_audit": (
        "id", "event_id", "check_id", "auth_id", "event_type", "source",
        "card_id", "actor", "reason_codes_json", "evidence_json", "created_at",
    ),
    "asset_recovery_authorizations": (
        "auth_id", "attempt_id", "secret_sha256", "item_manifest_sha256",
        "account_id", "source_ad_id", "source_adset_id", "source_creative_id",
        "source_identity_sha256", "target_adset_id", "target_ad_name",
        "target_identity_key", "pre_inventory_sha256", "phase",
        "created_ad_id", "created_at", "updated_at",
    ),
    "action_producer_commands": (
        "idempotency_key", "scope", "command_payload_sha256", "operation_id",
        "created_at",
    ),
    "action_effects": (
        "effect_id", "operation_id", "effect_kind", "payload_sha256", "state",
        "created_at", "applied_at",
    ),
    "action_outbox": (
        "effect_id", "channel", "typed_payload_json", "state", "attempts",
        "lease_until", "sent_at",
    ),
    "action_state_projections": (
        "effect_id", "projection_kind", "subject_id", "payload_json", "created_at",
    ),
    "launch_provider_asset_bindings": (
        "auth_id", "asset_id", "content_sha256", "phase",
        "provider_payload_json", "created_at", "updated_at",
    ),
    "launch_provider_ad_bindings": (
        "claim_id", "auth_id", "ad_name", "adset_id", "expected_fingerprint",
        "expected_payload_json", "phase", "ad_id", "creative_id",
        "verified_fingerprint", "created_at", "updated_at",
    ),
    "owner_action_proposals": (
        "proposal_id", "proposal_version", "proposal_kind", "origin",
        "idempotency_key", "source_ref", "requested_by_actor", "summary",
        "plan_json", "plan_sha256", "targets_sha256", "evidence_sha256",
        "config_version_sha256", "proposal_sha256", "staged_media_root",
        "created_at", "valid_until",
    ),
    "owner_action_proposal_targets": (
        "proposal_id", "claim_id", "ordinal", "action_kind", "account_id",
        "adset_id", "subject_id", "city", "language", "intended_payload_json",
        "intended_payload_sha256", "created_at",
    ),
    "owner_action_evidence": (
        "evidence_id", "proposal_id", "evidence_kind", "source_system",
        "subject_id", "observed_at", "complete", "payload_json",
        "payload_sha256", "created_at",
    ),
    "owner_action_lifecycle": (
        "proposal_id", "state", "version", "delivery_generation",
        "active_decision_id", "active_job_id", "latest_reason_code",
        "next_action_at", "updated_at",
    ),
    "telegram_update_inbox": (
        "update_id", "ingress_kind", "bot_token_identity_sha256",
        "raw_update_json", "raw_update_sha256", "state", "attempts",
        "lease_token", "lease_until", "received_at", "processed_at",
        "last_error_code",
    ),
    "telegram_poll_cursor": (
        "singleton", "next_update_id", "version", "updated_at",
    ),
    "owner_callback_tokens": (
        "token_id", "public_nonce", "token_mac_sha256", "proposal_id",
        "delivery_id", "delivery_generation", "decision_kind",
        "expected_owner_user_id", "expected_chat_id", "expected_message_id",
        "created_at", "expires_at", "bound_at", "consumed_at",
        "consumed_update_id", "consumed_callback_query_id", "revoked_at",
        "revoke_reason",
    ),
    "owner_action_decisions": (
        "decision_id", "proposal_id", "proposal_sha256", "decision_kind",
        "owner_user_id", "chat_id", "message_id", "delivery_generation",
        "telegram_update_id", "callback_query_id", "callback_token_id",
        "trusted_ingress_sha256", "reason_text", "recorded_at",
        # Миграция 026: чьё решение. 'OWNER' — кнопка владельца с полной
        # телеграм-родословной, 'SYSTEM' — самоодобрение бота с именем правила
        # в automation_rule. Смешивать одно с другим нельзя ни в аудите, ни в
        # датасете обучения, поэтому признак живёт в самой строке решения.
        "decision_source", "automation_rule",
    ),
    "owner_execution_jobs": (
        "job_id", "proposal_id", "decision_id", "state", "attempts",
        "lease_token", "lease_until", "next_attempt_at", "operation_id",
        "last_reason_code", "created_at", "updated_at",
    ),
    "owner_technical_permits": (
        "permit_id", "secret_sha256", "proposal_id", "decision_id", "job_id",
        "claim_id", "operation_kind", "account_id", "resource_id",
        "exact_payload_sha256", "manifest_json", "manifest_sha256",
        "live_evidence_sha256", "phase", "sequence_no", "issued_at",
        "expires_at", "consumed_at", "revoked_at", "revoke_reason",
    ),
    "owner_action_attempts": (
        "attempt_id", "permit_id", "proposal_id", "decision_id", "job_id",
        "claim_id", "operation_kind", "account_id", "resource_id",
        "exact_payload_sha256", "state", "provider_request_id",
        "provider_result_sha256", "started_at", "completed_at",
        "last_reason_code",
    ),
    "owner_action_events": (
        "event_id", "proposal_id", "event_seq", "event_type", "actor",
        "reason_code", "payload_json", "payload_sha256", "created_at",
    ),
    "coverage_snapshots": (
        "snapshot_id", "started_at", "completed_at", "fetch_complete",
        "configured_group_count", "observed_group_count", "page_count",
        "inventory_sha256", "error_code",
    ),
    "coverage_snapshot_groups": (
        "snapshot_id", "group_key", "account_id", "city", "language",
        "adset_id", "min_active", "effective_active_count",
        "configured_active_count", "status", "inventory_sha256",
    ),
    "coverage_incidents": (
        "incident_id", "group_key", "incident_kind", "state",
        "opened_snapshot_id", "latest_snapshot_id", "consecutive_complete_ok",
        "reminder_seq", "next_reminder_at", "opened_at", "updated_at",
        "resolved_at",
    ),
    "coverage_incident_events": (
        "event_id", "incident_id", "snapshot_id", "event_type", "payload_json",
        "payload_sha256", "created_at",
    ),
    "telegram_delivery_outbox": (
        "delivery_id", "purpose", "proposal_id", "incident_id", "generation",
        "dedupe_key", "rendered_text", "rendered_text_sha256",
        "button_spec_json", "button_spec_sha256", "state", "attempts",
        "lease_token", "lease_until", "next_attempt_at", "telegram_chat_id",
        "telegram_message_id", "created_at", "sent_at", "last_error_code",
    ),
    "launch_watchdogs": (
        "watchdog_id", "proposal_id", "decision_id", "job_id", "state",
        "expected_count", "verified_count", "verification_attempts",
        "lease_token", "lease_until", "next_verify_at", "verify_deadline_at",
        "created_at", "updated_at", "verified_at", "last_reason_code",
    ),
    "launch_watchdog_targets": (
        "proposal_id", "watchdog_id", "claim_id", "account_id", "adset_id",
        "expected_ad_name", "expected_fingerprint", "created_ad_id",
        "expected_configured_status", "expected_effective_status", "created_at",
    ),
    "launch_verification_observations": (
        "observation_id", "watchdog_id", "attempt_no", "fetch_complete",
        "verified_count", "outcome", "evidence_json", "evidence_sha256",
        "observed_at",
    ),
    "scheduler_action_runs": (
        "run_id", "scheduler_name", "slot_key", "state", "proposal_id",
        "watchdog_id", "lease_token", "lease_until", "created_at",
        "updated_at", "verified_at", "last_reason_code",
    ),
    "owner_action_outcomes": (
        "outcome_id", "proposal_id", "job_id", "claim_id", "attempt_id",
        "horizon", "observed_at", "metrics_json", "metrics_sha256",
    ),
    # Миграция 023 — волна E (верификатор, дайджест, фидбек, след решения).
    "action_verifications": (
        "verification_id", "proposal_id", "claim_id", "attempt_id", "kind",
        "check_seq", "retry_no", "expected_json", "observed_json", "verdict",
        "reason_code", "checked_at",
    ),
    "action_verification_state": (
        "proposal_id", "claim_id", "kind", "state", "check_seq", "retry_count",
        "last_verdict", "retry_proposal_id", "first_seen_at", "updated_at",
    ),
    "owner_digest_runs": (
        "digest_id", "digest_date", "digest_hour", "digest_trigger",
        "item_count", "created_at",
    ),
    "owner_digest_items": (
        "digest_id", "proposal_id", "ordinal", "group_key", "evidence_stale",
        "created_at",
    ),
    "owner_digest_batch_tokens": (
        "token_id", "public_nonce", "token_mac_sha256", "digest_id",
        "batch_kind", "expected_owner_user_id", "expected_chat_id",
        "expected_message_id", "created_at", "expires_at", "bound_at",
        "consumed_at", "consumed_update_id", "consumed_callback_query_id",
    ),
    "owner_feedback": (
        "feedback_id", "proposal_id", "digest_id", "telegram_update_id",
        "message_id", "reply_to_message_id", "owner_user_id", "chat_id",
        "text", "parsed_action", "parsed_until", "created_at",
    ),
    "owner_trail_messages": (
        "trail_id", "trail_kind", "proposal_id", "digest_id", "dedupe_key",
        "reply_to_message_id", "rendered_text", "rendered_text_sha256",
        "button_spec_json", "state", "attempts", "lease_token", "lease_until",
        "next_attempt_at", "telegram_chat_id", "telegram_message_id",
        "created_at", "sent_at", "last_error_code",
    ),
    "ad_weekly_cohorts": (
        "ad_id", "week_start", "adset_id", "adset_name", "ad_name", "city",
        "spend_usd", "impressions", "fb_leads", "amo_leads", "quals",
        "days_covered", "days_expected", "comparable", "not_comparable_reason",
        "builder_version", "computed_at",
        # Волна 4 (миграция 025): когортная выручка и ROMI. Порядок — тот, в
        # котором ALTER TABLE ADD COLUMN дописал их в конец таблицы.
        "revenue_lcy", "payments", "revenue_horizon_days", "revenue_mature",
        "usd_lcy_rate", "romi_pct",
    ),
    # Миграция 027: журнал отметок «запущено» на карточках Trello.
    "trello_completion_log": (
        "delivery_id", "proposal_id", "card_id", "outcome", "reason_code",
        "attempts", "created_at",
    ),
}

INTEGER_COLUMNS: set[tuple[str, str]] = {
    ("schema_migrations", "version"),
    ("launch_authorizations", "topic_override"),
    ("launch_authorization_targets", "ordinal"),
    ("launch_authorization_targets", "expected_ads_count"),
    ("launch_authorization_targets", "reserved_slots"),
    ("launch_authorization_ads", "ordinal"),
    ("launch_check_audit", "id"),
    ("action_outbox", "attempts"),
    ("owner_action_proposals", "proposal_version"),
    ("owner_action_proposal_targets", "ordinal"),
    ("owner_action_evidence", "complete"),
    ("owner_action_lifecycle", "version"),
    ("owner_action_lifecycle", "delivery_generation"),
    ("telegram_update_inbox", "update_id"),
    ("telegram_update_inbox", "attempts"),
    ("telegram_poll_cursor", "singleton"),
    ("telegram_poll_cursor", "next_update_id"),
    ("telegram_poll_cursor", "version"),
    ("owner_callback_tokens", "delivery_generation"),
    ("owner_callback_tokens", "expected_owner_user_id"),
    ("owner_callback_tokens", "expected_chat_id"),
    ("owner_callback_tokens", "expected_message_id"),
    ("owner_callback_tokens", "consumed_update_id"),
    ("owner_action_decisions", "owner_user_id"),
    ("owner_action_decisions", "chat_id"),
    ("owner_action_decisions", "message_id"),
    ("owner_action_decisions", "delivery_generation"),
    ("owner_action_decisions", "telegram_update_id"),
    ("owner_execution_jobs", "attempts"),
    ("owner_technical_permits", "sequence_no"),
    ("owner_action_events", "event_seq"),
    ("coverage_snapshots", "fetch_complete"),
    ("coverage_snapshots", "configured_group_count"),
    ("coverage_snapshots", "observed_group_count"),
    ("coverage_snapshots", "page_count"),
    ("coverage_snapshot_groups", "min_active"),
    ("coverage_snapshot_groups", "effective_active_count"),
    ("coverage_snapshot_groups", "configured_active_count"),
    ("coverage_incidents", "consecutive_complete_ok"),
    ("coverage_incidents", "reminder_seq"),
    ("telegram_delivery_outbox", "generation"),
    ("telegram_delivery_outbox", "attempts"),
    ("telegram_delivery_outbox", "telegram_chat_id"),
    ("telegram_delivery_outbox", "telegram_message_id"),
    ("launch_watchdogs", "expected_count"),
    ("launch_watchdogs", "verified_count"),
    ("launch_watchdogs", "verification_attempts"),
    ("launch_verification_observations", "attempt_no"),
    ("launch_verification_observations", "fetch_complete"),
    ("launch_verification_observations", "verified_count"),
    ("action_verifications", "check_seq"),
    ("action_verifications", "retry_no"),
    ("action_verification_state", "check_seq"),
    ("action_verification_state", "retry_count"),
    ("owner_digest_runs", "digest_hour"),
    ("owner_digest_runs", "item_count"),
    ("owner_digest_items", "ordinal"),
    ("owner_digest_items", "evidence_stale"),
    ("owner_digest_batch_tokens", "expected_owner_user_id"),
    ("owner_digest_batch_tokens", "expected_chat_id"),
    ("owner_digest_batch_tokens", "expected_message_id"),
    ("owner_digest_batch_tokens", "consumed_update_id"),
    ("owner_feedback", "telegram_update_id"),
    ("owner_feedback", "message_id"),
    ("owner_feedback", "reply_to_message_id"),
    ("owner_feedback", "owner_user_id"),
    ("owner_feedback", "chat_id"),
    ("owner_trail_messages", "reply_to_message_id"),
    ("owner_trail_messages", "attempts"),
    ("owner_trail_messages", "telegram_chat_id"),
    ("owner_trail_messages", "telegram_message_id"),
    ("ad_weekly_cohorts", "impressions"),
    ("ad_weekly_cohorts", "fb_leads"),
    ("ad_weekly_cohorts", "amo_leads"),
    ("ad_weekly_cohorts", "quals"),
    ("ad_weekly_cohorts", "days_covered"),
    ("ad_weekly_cohorts", "days_expected"),
    ("ad_weekly_cohorts", "comparable"),
    ("ad_weekly_cohorts", "builder_version"),
    ("ad_weekly_cohorts", "payments"),
    ("ad_weekly_cohorts", "revenue_horizon_days"),
    ("ad_weekly_cohorts", "revenue_mature"),
    ("trello_completion_log", "attempts"),
}

# Колонки с плавающей точкой. Отдельный manifest рядом с INTEGER_COLUMNS:
# деньги нельзя хранить ни TEXT, ни INTEGER, а верификатор типов обязан
# отличать REAL от «всё остальное — TEXT».
REAL_COLUMNS: set[tuple[str, str]] = {
    ("ad_weekly_cohorts", "spend_usd"),
    ("ad_weekly_cohorts", "revenue_lcy"),
    ("ad_weekly_cohorts", "usd_lcy_rate"),
    ("ad_weekly_cohorts", "romi_pct"),
}

# SHA-256 canonical JSON всех полей ``PRAGMA table_info``:
# cid, name, type, notnull, default и позиция primary key.
TABLE_INFO_SHA256: dict[str, str] = {
    "schema_migrations": "936e06f6477d3a41e35310fe9a81eebc1915675b4f268a6210bfa29f7d437485",
    "launch_authorizations": "3c14217dfacaaa703c5268850e234f35a93eface64c3938368d049890f562d04",
    "launch_authorization_targets": "f33654d9776c41226a86aa51bb95f3f2d6d0c9fef8bceab7670adba83b34655c",
    "launch_authorization_ads": "856f57ab44d968570021a5d5169721aa0a3bea2854bf56039bcf577a0033a363",
    "launch_check_audit": "4acc540b897f12377751b81d5a4492ac13433a9b0d394c640609b833d94fe095",
    "asset_recovery_authorizations": "d5ee226fdf4897b530c4c844da3270f672c6bae2d528d92968a027b7452ec682",
    "action_producer_commands": "9b4c89056d8736cfd6df77c2114d570edcf9db83244ab03696b86150043cb971",
    "action_effects": "8922065a375de20e781ebee41bfb0a5254977e30dfeabb23dd98491f6855bf32",
    "action_outbox": "7742eb3e0bc42bc8eedb61a67ec279e977efdbb4b8cd9d857c230e1d672ca46f",
    "action_state_projections": "115f6fa00e09d660d93a59bb523a14c1d4af91174fda938a7d9e3b9980483881",
    "launch_provider_asset_bindings": "62f43503434b8275349e3cd5b9d62a857d47863b692c7929c24734d37dde7926",
    "launch_provider_ad_bindings": "52825a91f139e6357d1b738dca3eb5b2533a4b1e2ca6a98b9e8a20dc7b86a31a",
    "owner_action_proposals": "3d8e0bc86f46af141b9e0a905e1cceecd294f9fc3f78dfe5215bef9fa17e524e",
    "owner_action_proposal_targets": "af49a8ebc203950e1a02704cc6136f24389eb542fafdef48d904e7b6843c2bc6",
    "owner_action_evidence": "b87e363db6f4ea196b3a9f7ebee7a2a29acadaefc39222a2d879d46a7b677db7",
    "owner_action_lifecycle": "77a10aa62cf482d6b0d099ea218f055753cfc928d260ccd7028f2ea6af364bf8",
    "telegram_update_inbox": "bac6c578dc73cc83582ddce0815210f70cde3c298e834b150ce6469337131ed6",
    "telegram_poll_cursor": "02849a29e80009852ea14229bc6d972dbf61a14f97272bfce44a3b9769a99fd4",
    "owner_callback_tokens": "328993ecd00be75397bcda875c4eb316c693e3cc5eda33fbe11711b96c1faa63",
    # Миграция 026 пересобрала таблицу: телеграм-родословная стала NULL-able,
    # добавлены decision_source и automation_rule.
    "owner_action_decisions": "34a4399f15417fa5da89791ccc6e30e5fc174426b9a8e6b051c433cfd717a147",
    "owner_execution_jobs": "8962a87c7be0e8e4615e13c67c27889055459b20e6485f6c0623858b874f22a7",
    "owner_technical_permits": "7a43d26a443e1a9c56daee0a526fa1466c887836f0193843f7e3f2891f98c70b",
    "owner_action_attempts": "b37e1c65885845aa45f9315c99739427e5e9fb750716297d219cc1c50874ca67",
    "owner_action_events": "2df17df1abe093c210001cdfd7de422d98f365c065f76e588a3bba2e4bf67571",
    "coverage_snapshots": "fce54c6837cf547c5ae0a2f5c0fbc18d90cd400b58c6bdd0482e1953bcb86fc3",
    # Миграция 028 пересобрала таблицу ради CHECK language ('PRODB' добавлен);
    # table_info не изменился — подпись прежняя.
    "coverage_snapshot_groups": "a2436bc26478e5de8927e2bf2fe4555ef627bb25cdf9bc23914d79e6db0c8ee2",
    "coverage_incidents": "8f0d888d92387f39dc05a7f64025e1ce910000261a9ae1d5f8b6e5a630fa3eb4",
    "coverage_incident_events": "cc2e5dfa516b92512e77cf998d28c3a60c2319d322d420cdbb829c64e7145e71",
    "telegram_delivery_outbox": "176f5e291baabcf58e405f2cb290a371f4f0951cecd10c25297f8990764a9d9d",
    "launch_watchdogs": "e512a770ab11bdef00c2b231c2c64fdd3ca8cf744b6db504b977e8b1de0e80d7",
    "launch_watchdog_targets": "bcb4e9227e2cf49b442987921911de9dba3d2465cecd355748c301d6c2b12261",
    "launch_verification_observations": "66916ea9dbda7292bb4d270bf232f4a539a2edb624b9e83f879d8c9116e51b0f",
    "scheduler_action_runs": "225c50c49af4a2fd1e977a2f71f661c9fd4ab700e260e84bf53fdfda4dbd1b45",
    "owner_action_outcomes": "5a1ea76d57bb6b32b7300f1538c6b337b03760cc2381f78a09a5b744ab5ce5d5",
    "action_verifications": "cafefbd5c64c03f2b5ed292dbc69f0cd86e45ec7ff691c2239a632f902f36963",
    "action_verification_state": "7ea9a7c1880f09ba05a7bf9d18d959e680121c922c04ff0f5e1730b5c6503d83",
    "owner_digest_runs": "60d02721a1f91e79a2881ddc03e827b1c69dafdf1f19377883fe1615b2214d5a",
    "owner_digest_items": "d014525f01afde3f65199dd9c7a0ea7f85673584e322d765a366dec1caad380a",
    "owner_digest_batch_tokens": "c185dcd6b7e3d08b69d8b8b71dd592878fb6c634ed7e63cfcbcd08f88fea0bb5",
    "owner_feedback": "dff9cabe8d15f52f98e68d9682553ce3a6be69ed1fd201a07d1dd60cff5acc23",
    "owner_trail_messages": "7fd817305b01e06196d587074027676c545b90e32f181735c7f9fb148175dbd6",
    "ad_weekly_cohorts": "ec39734debfde8d9a17d898a58607830e79710febf703e42426c9b44391f07f4",
    "trello_completion_log": "99e980564e2de0884386b8bf73990d5a234b587f2e7f026961fe33d829361c44",
}

REQUIRED_INDEXES: set[str] = {
    "uq_launch_auth_open_card_campaign",
    "idx_launch_auth_phase_expiry",
    "idx_launch_target_reservations",
    "uq_launch_target_open_identity",
    "idx_launch_ads_phase",
    "uq_launch_ads_open_exact_name",
    "idx_launch_audit_card_time",
    "idx_launch_audit_auth",
    "uq_asset_recovery_open_target",
    "idx_launch_provider_ad_auth",
    "idx_owner_targets_scope",
    "idx_owner_evidence_proposal",
    "idx_owner_lifecycle_work",
    "idx_telegram_inbox_work",
    "idx_owner_tokens_lookup",
    "uq_owner_token_decision_generation",
    "uq_owner_terminal_decision",
    "idx_owner_jobs_work",
    "uq_owner_open_permit_claim",
    "uq_owner_consumed_permit_claim",
    "uq_owner_permit_claim_sequence",
    "uq_owner_attempt_claim",
    "idx_owner_events_time",
    "idx_coverage_group_status",
    "uq_coverage_open_incident",
    "idx_telegram_outbox_work",
    "uq_owner_delivery_generation",
    "idx_launch_watchdog_work",
    "idx_scheduler_runs_work",
    "idx_action_verifications_time",
    "idx_action_verification_work",
    "uq_owner_digest_schedule_slot",
    "uq_owner_digest_item_proposal",
    "idx_owner_digest_tokens_lookup",
    "idx_owner_feedback_proposal",
    "idx_owner_trail_work",
    "uq_ad_weekly_cohort",
    "idx_ad_weekly_cohorts_week",
    "idx_ad_weekly_cohorts_adset",
    "idx_trello_completion_card",
}

REQUIRED_TRIGGERS: set[str] = {
    "trg_owner_proposals_no_update",
    "trg_owner_proposals_no_delete",
    "trg_owner_targets_no_update",
    "trg_owner_targets_no_delete",
    "trg_owner_evidence_no_update",
    "trg_owner_evidence_no_delete",
    "trg_owner_decisions_no_update",
    "trg_owner_decisions_no_delete",
    "trg_owner_events_no_update",
    "trg_owner_events_no_delete",
    "trg_coverage_snapshots_no_update",
    "trg_coverage_snapshots_no_delete",
    "trg_coverage_groups_no_update",
    "trg_coverage_groups_no_delete",
    "trg_coverage_events_no_update",
    "trg_coverage_events_no_delete",
    "trg_launch_targets_no_update",
    "trg_launch_targets_no_delete",
    "trg_launch_observations_no_update",
    "trg_launch_observations_no_delete",
    "trg_owner_outcomes_no_update",
    "trg_owner_outcomes_no_delete",
    "trg_owner_decision_validate",
    "trg_owner_decision_revoke_siblings",
    "trg_owner_job_requires_approval",
    "trg_owner_job_identity_lock",
    "trg_owner_job_claim_requeue",
    "trg_owner_token_revoke_old_generation",
    "trg_owner_token_identity_lock",
    "trg_owner_token_one_time_bind",
    "trg_owner_token_single_consume",
    "trg_owner_lifecycle_transition",
    "trg_owner_permit_identity_lock",
    "trg_owner_permit_issue_validate",
    "trg_owner_permit_transition",
    "trg_owner_permit_terminal_fields",
    "trg_owner_permit_no_delete",
    "trg_owner_attempt_identity_lock",
    "trg_owner_attempt_requires_consumed_permit",
    "trg_owner_attempt_transition",
    "trg_owner_attempt_no_delete",
    "trg_delivery_payload_lock",
    "trg_delivery_no_delete",
    "trg_launch_check_audit_no_update",
    "trg_launch_check_audit_no_delete",
    "trg_action_commands_no_update",
    "trg_action_commands_no_delete",
    "trg_provider_ad_binding_identity_lock",
    "trg_provider_ad_binding_no_delete",
    "trg_asset_recovery_identity_lock",
    "trg_asset_recovery_no_delete",
    "trg_action_verifications_no_update",
    "trg_action_verifications_no_delete",
    "trg_owner_feedback_no_update",
    "trg_owner_feedback_no_delete",
    "trg_owner_digest_runs_no_update",
    "trg_owner_digest_items_no_update",
    "trg_action_verification_state_forward",
    "trg_owner_digest_token_one_time_bind",
    "trg_owner_digest_token_single_consume",
    "trg_owner_digest_token_identity_lock",
    "trg_trello_completion_no_update",
    "trg_trello_completion_no_delete",
}

UNIQUE_INDEXES: dict[str, _UniqueIndexSpec] = {
    "uq_launch_auth_open_card_campaign": _UniqueIndexSpec(
        "launch_authorizations", ("card_id", "campaign_type"), True,
    ),
    "uq_launch_target_open_identity": _UniqueIndexSpec(
        "launch_authorization_targets",
        ("account_id", "adset_id", "identity_key"),
        True,
    ),
    "uq_launch_ads_open_exact_name": _UniqueIndexSpec(
        "launch_authorization_ads",
        ("account_id", "adset_id", "ad_name_key"),
        True,
    ),
    "uq_asset_recovery_open_target": _UniqueIndexSpec(
        "asset_recovery_authorizations",
        ("account_id", "target_adset_id", "target_identity_key"),
        True,
    ),
    "uq_owner_terminal_decision": _UniqueIndexSpec(
        "owner_action_decisions", ("proposal_id",), True,
    ),
    "uq_owner_token_decision_generation": _UniqueIndexSpec(
        "owner_callback_tokens",
        ("proposal_id", "delivery_generation", "decision_kind"),
        False,
    ),
    "uq_owner_open_permit_claim": _UniqueIndexSpec(
        "owner_technical_permits", ("proposal_id", "claim_id"), True,
    ),
    "uq_owner_consumed_permit_claim": _UniqueIndexSpec(
        "owner_technical_permits", ("proposal_id", "claim_id"), True,
    ),
    "uq_owner_permit_claim_sequence": _UniqueIndexSpec(
        "owner_technical_permits",
        ("proposal_id", "claim_id", "sequence_no"),
        False,
    ),
    "uq_owner_attempt_claim": _UniqueIndexSpec(
        "owner_action_attempts", ("proposal_id", "claim_id"), False,
    ),
    "uq_owner_delivery_generation": _UniqueIndexSpec(
        "telegram_delivery_outbox", ("proposal_id", "generation"), True,
    ),
    "uq_coverage_open_incident": _UniqueIndexSpec(
        "coverage_incidents", ("group_key", "incident_kind"), True,
    ),
    "uq_owner_digest_schedule_slot": _UniqueIndexSpec(
        "owner_digest_runs", ("digest_date",), True,
    ),
    "uq_owner_digest_item_proposal": _UniqueIndexSpec(
        "owner_digest_items", ("proposal_id",), False,
    ),
    "uq_ad_weekly_cohort": _UniqueIndexSpec(
        "ad_weekly_cohorts", ("ad_id", "week_start"), False,
    ),
}

FOREIGN_KEYS: dict[str, tuple[_ForeignKeySpec, ...]] = {
    "launch_authorization_targets": (
        _ForeignKeySpec(
            "launch_authorizations", ("auth_id",), ("auth_id",), "RESTRICT",
        ),
    ),
    "launch_authorization_ads": (
        _ForeignKeySpec(
            "launch_authorization_targets",
            ("auth_id", "city"),
            ("auth_id", "city"),
            "RESTRICT",
        ),
    ),
    "launch_check_audit": (
        _ForeignKeySpec(
            "launch_authorizations", ("auth_id",), ("auth_id",), "RESTRICT",
        ),
    ),
    "action_outbox": (
        _ForeignKeySpec("action_effects", ("effect_id",), ("effect_id",), "NO ACTION"),
    ),
    "action_state_projections": (
        _ForeignKeySpec("action_effects", ("effect_id",), ("effect_id",), "NO ACTION"),
    ),
    "owner_action_proposal_targets": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
    ),
    "owner_action_evidence": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
    ),
    "owner_action_lifecycle": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
    ),
    "owner_callback_tokens": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
        _ForeignKeySpec(
            "telegram_delivery_outbox", ("delivery_id",), ("delivery_id",), "RESTRICT",
        ),
        _ForeignKeySpec(
            "telegram_update_inbox",
            ("consumed_update_id",),
            ("update_id",),
            "RESTRICT",
        ),
    ),
    "owner_action_decisions": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
        _ForeignKeySpec(
            "telegram_update_inbox",
            ("telegram_update_id",),
            ("update_id",),
            "RESTRICT",
        ),
        _ForeignKeySpec(
            "owner_callback_tokens",
            ("callback_token_id",),
            ("token_id",),
            "RESTRICT",
        ),
    ),
    "owner_execution_jobs": (
        _ForeignKeySpec(
            "owner_action_decisions",
            ("proposal_id", "decision_id"),
            ("proposal_id", "decision_id"),
            "RESTRICT",
        ),
    ),
    "owner_technical_permits": (
        _ForeignKeySpec(
            "owner_execution_jobs",
            ("proposal_id", "decision_id", "job_id"),
            ("proposal_id", "decision_id", "job_id"),
            "RESTRICT",
        ),
        _ForeignKeySpec(
            "owner_action_proposal_targets",
            ("proposal_id", "claim_id"),
            ("proposal_id", "claim_id"),
            "RESTRICT",
        ),
    ),
    "owner_action_attempts": (
        _ForeignKeySpec(
            "owner_technical_permits",
            ("proposal_id", "decision_id", "job_id", "claim_id", "permit_id"),
            ("proposal_id", "decision_id", "job_id", "claim_id", "permit_id"),
            "RESTRICT",
        ),
    ),
    "owner_action_events": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
    ),
    "coverage_snapshot_groups": (
        _ForeignKeySpec(
            "coverage_snapshots", ("snapshot_id",), ("snapshot_id",), "RESTRICT",
        ),
    ),
    "coverage_incidents": (
        _ForeignKeySpec(
            "coverage_snapshots",
            ("opened_snapshot_id",),
            ("snapshot_id",),
            "RESTRICT",
        ),
        _ForeignKeySpec(
            "coverage_snapshots",
            ("latest_snapshot_id",),
            ("snapshot_id",),
            "RESTRICT",
        ),
    ),
    "coverage_incident_events": (
        _ForeignKeySpec(
            "coverage_incidents", ("incident_id",), ("incident_id",), "RESTRICT",
        ),
        _ForeignKeySpec(
            "coverage_snapshots", ("snapshot_id",), ("snapshot_id",), "RESTRICT",
        ),
    ),
    "telegram_delivery_outbox": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
        _ForeignKeySpec(
            "coverage_incidents", ("incident_id",), ("incident_id",), "RESTRICT",
        ),
    ),
    "launch_watchdogs": (
        _ForeignKeySpec(
            "owner_execution_jobs",
            ("proposal_id", "decision_id", "job_id"),
            ("proposal_id", "decision_id", "job_id"),
            "RESTRICT",
        ),
    ),
    "launch_watchdog_targets": (
        _ForeignKeySpec(
            "launch_watchdogs",
            ("proposal_id", "watchdog_id"),
            ("proposal_id", "watchdog_id"),
            "RESTRICT",
        ),
        _ForeignKeySpec(
            "owner_action_proposal_targets",
            ("proposal_id", "claim_id"),
            ("proposal_id", "claim_id"),
            "RESTRICT",
        ),
    ),
    "launch_verification_observations": (
        _ForeignKeySpec(
            "launch_watchdogs", ("watchdog_id",), ("watchdog_id",), "RESTRICT",
        ),
    ),
    "scheduler_action_runs": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
        _ForeignKeySpec(
            "launch_watchdogs",
            ("proposal_id", "watchdog_id"),
            ("proposal_id", "watchdog_id"),
            "RESTRICT",
        ),
    ),
    "owner_action_outcomes": (
        _ForeignKeySpec(
            "owner_action_attempts",
            ("proposal_id", "job_id", "claim_id", "attempt_id"),
            ("proposal_id", "job_id", "claim_id", "attempt_id"),
            "RESTRICT",
        ),
    ),
    "action_verifications": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
    ),
    "action_verification_state": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
    ),
    "owner_digest_items": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
        _ForeignKeySpec(
            "owner_digest_runs", ("digest_id",), ("digest_id",), "RESTRICT",
        ),
    ),
    "owner_digest_batch_tokens": (
        _ForeignKeySpec(
            "owner_digest_runs", ("digest_id",), ("digest_id",), "RESTRICT",
        ),
    ),
    "owner_feedback": (
        _ForeignKeySpec(
            "owner_digest_runs", ("digest_id",), ("digest_id",), "RESTRICT",
        ),
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
    ),
    "owner_trail_messages": (
        _ForeignKeySpec(
            "owner_digest_runs", ("digest_id",), ("digest_id",), "RESTRICT",
        ),
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
    ),
    "trello_completion_log": (
        _ForeignKeySpec(
            "owner_action_proposals", ("proposal_id",), ("proposal_id",), "RESTRICT",
        ),
        _ForeignKeySpec(
            "telegram_delivery_outbox", ("delivery_id",), ("delivery_id",), "RESTRICT",
        ),
    ),
}


def _migration_record(version: int) -> MigrationRecord:
    try:
        path = MIGRATION_FILES[version]
    except KeyError as exc:
        raise ValueError(f"Неизвестная обязательная миграция: {version}") from exc
    if not path.is_file():
        raise FileNotFoundError(f"Файл миграции не найден: {path}")
    return MigrationRecord(
        version=version,
        name=path.name,
        content_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _required_records(required: tuple[int, ...]) -> tuple[MigrationRecord, ...]:
    if not required:
        raise ValueError("Список обязательных миграций не может быть пустым")
    if len(set(required)) != len(required):
        raise ValueError("Версии обязательных миграций не должны повторяться")
    if tuple(sorted(required)) != required:
        raise ValueError("Версии обязательных миграций должны идти по возрастанию")
    return tuple(_migration_record(version) for version in required)


def _iter_sql_statements(sql: str) -> Iterator[str]:
    """Делит SQLite SQL без разрушения ``CREATE TRIGGER ... BEGIN ... END``."""

    buffer: list[str] = []
    for char in sql:
        buffer.append(char)
        if char != ";":
            continue
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            if re.sub(r"--[^\n]*(?:\n|$)", "", candidate).strip():
                yield candidate.strip()
            buffer.clear()

    tail = "".join(buffer)
    if re.sub(r"--[^\n]*(?:\n|$)", "", tail).strip():
        raise MigrationError("SQL миграции содержит незавершённый statement")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise MigrationError("SQLite foreign_keys не удалось включить")
    return conn


def _validate_recorded_checksums(
    conn: sqlite3.Connection,
    records: tuple[MigrationRecord, ...],
) -> dict[int, MigrationRecord]:
    rows = {
        int(row[0]): (str(row[1]), str(row[2]), str(row[3]))
        for row in conn.execute(
            """
            SELECT version, name, content_sha256, application_id
            FROM schema_migrations
            """
        )
    }
    existing: dict[int, MigrationRecord] = {}
    for record in records:
        stored = rows.get(record.version)
        if stored is None:
            continue
        stored_name, stored_sha256, stored_application_id = stored
        if (
            stored_name != record.name
            or stored_sha256 != record.content_sha256
            or stored_application_id != APPLICATION_ID
        ):
            raise MigrationChecksumMismatch(
                "Checksum миграции "
                f"{record.version} не совпадает: "
                f"БД={stored_name}:{stored_sha256}:{stored_application_id}, "
                f"файл={record.name}:{record.content_sha256}:{APPLICATION_ID}"
            )
        existing[record.version] = record
    return existing


def apply_runtime_migrations(
    db_path: str,
    *,
    required: tuple[int, ...] = (19, 20, 21, 22, 23, 24, 25, 26, 27, 28),
) -> MigrationReport:
    """Атомарно применяет отсутствующие runtime-миграции.

    Каждая версия выполняется внутри ровно одного ``BEGIN IMMEDIATE``. Checksum
    вставляется в ту же транзакцию, а проверка внешних ключей идёт до commit.
    """

    records = _required_records(required)
    conn = _connect(db_path)
    applied: list[MigrationRecord] = []
    existing: dict[int, MigrationRecord] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(SCHEMA_MIGRATIONS_DDL)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Сначала проверяем все записанные версии, до выполнения migration SQL.
        existing = _validate_recorded_checksums(conn, records)

        for record in records:
            if record.version in existing:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Повторная проверка закрывает гонку двух startup-процессов.
                concurrent_row = conn.execute(
                    """
                    SELECT name, content_sha256, application_id
                    FROM schema_migrations
                    WHERE version = ?
                    """,
                    (record.version,),
                ).fetchone()
                if concurrent_row is not None:
                    if tuple(concurrent_row) != (
                        record.name,
                        record.content_sha256,
                        APPLICATION_ID,
                    ):
                        raise MigrationChecksumMismatch(
                            f"Checksum миграции {record.version} изменился в гонке"
                        )
                    conn.commit()
                    existing[record.version] = record
                    continue

                sql = MIGRATION_FILES[record.version].read_text(encoding="utf-8")
                for statement in _iter_sql_statements(sql):
                    conn.execute(statement)
                conn.execute(
                    """
                    INSERT INTO schema_migrations (
                        version, name, content_sha256, applied_at, application_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.version,
                        record.name,
                        record.content_sha256,
                        datetime.now(UTC).isoformat(),
                        APPLICATION_ID,
                    ),
                )
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise MigrationForeignKeyViolation(
                        f"Нарушены внешние ключи после миграции "
                        f"{record.version}: {violations!r}"
                    )
                conn.commit()
                applied.append(record)
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()

    already_applied = tuple(
        existing[version] for version in sorted(existing)
    )
    return MigrationReport(
        db_path=db_path,
        applied=tuple(applied),
        already_applied=already_applied,
    )


def _actual_foreign_keys(
    conn: sqlite3.Connection,
    table: str,
) -> set[_ForeignKeySpec]:
    grouped: dict[int, list[sqlite3.Row | tuple]] = {}
    for row in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
        grouped.setdefault(int(row[0]), []).append(row)

    result: set[_ForeignKeySpec] = set()
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda row: int(row[1]))
        result.add(
            _ForeignKeySpec(
                target_table=str(ordered[0][2]),
                from_columns=tuple(str(row[3]) for row in ordered),
                to_columns=tuple(str(row[4]) for row in ordered),
                on_delete=str(ordered[0][6]).upper(),
            )
        )
    return result


def _verify_objects(conn: sqlite3.Connection) -> tuple[int, int, int]:
    objects = {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            """
            SELECT type, name
            FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger')
            """
        )
    }
    required_objects = (
        {("table", name) for name in TABLE_COLUMNS}
        | {("index", name) for name in REQUIRED_INDEXES}
        | {("trigger", name) for name in REQUIRED_TRIGGERS}
    )
    missing = sorted(required_objects - objects)
    if missing:
        raise SchemaVerificationError(
            f"В runtime-схеме отсутствуют обязательные объекты: {missing!r}"
        )
    return (
        sum(kind == "table" for kind, _ in required_objects),
        sum(kind == "index" for kind, _ in required_objects),
        sum(kind == "trigger" for kind, _ in required_objects),
    )


def _verify_columns(conn: sqlite3.Connection) -> None:
    for table, expected_names in TABLE_COLUMNS.items():
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        actual_names = tuple(str(row[1]) for row in rows)
        if actual_names != expected_names:
            raise SchemaVerificationError(
                f"Колонки {table} не совпадают: "
                f"ожидались={expected_names!r}, получены={actual_names!r}"
            )
        for row in rows:
            column = str(row[1])
            if (table, column) in INTEGER_COLUMNS:
                expected_type = "INTEGER"
            elif (table, column) in REAL_COLUMNS:
                expected_type = "REAL"
            else:
                expected_type = "TEXT"
            actual_type = str(row[2]).upper()
            if actual_type != expected_type:
                raise SchemaVerificationError(
                    f"Тип {table}.{column} не совпадает: "
                    f"ожидался={expected_type}, получен={actual_type}"
                )
        signature_payload = json.dumps(
            [list(row) for row in rows],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        actual_signature = hashlib.sha256(signature_payload).hexdigest()
        expected_signature = TABLE_INFO_SHA256[table]
        if actual_signature != expected_signature:
            raise SchemaVerificationError(
                f"PRAGMA table_info для {table} не совпадает: "
                f"ожидался={expected_signature}, получен={actual_signature}"
            )


def _verify_foreign_keys(conn: sqlite3.Connection) -> None:
    for table in TABLE_COLUMNS:
        expected = set(FOREIGN_KEYS.get(table, ()))
        actual = _actual_foreign_keys(conn, table)
        if actual != expected:
            raise SchemaVerificationError(
                f"Внешние ключи {table} не совпадают: "
                f"ожидались={expected!r}, получены={actual!r}"
            )


def _verify_unique_indexes(conn: sqlite3.Connection) -> None:
    for name, spec in UNIQUE_INDEXES.items():
        index_rows = {
            str(row[1]): row
            for row in conn.execute(f'PRAGMA index_list("{spec.table}")')
        }
        row = index_rows.get(name)
        if row is None:
            raise SchemaVerificationError(f"Уникальный индекс {name} отсутствует")
        is_unique = bool(row[2])
        is_partial = bool(row[4])
        columns = tuple(
            str(index_row[2])
            for index_row in conn.execute(f'PRAGMA index_info("{name}")')
        )
        if (
            not is_unique
            or is_partial != spec.partial
            or columns != spec.columns
        ):
            raise SchemaVerificationError(
                f"Уникальный индекс {name} повреждён: "
                f"unique={is_unique}, partial={is_partial}, columns={columns!r}"
            )


def verify_runtime_schema(db_path: str) -> SchemaHealth:
    """Проверяет checksum, объекты, колонки, FK и уникальные индексы."""

    if db_path != ":memory:" and not Path(db_path).is_file():
        raise SchemaVerificationError(f"SQLite БД не найдена: {db_path}")

    records = _required_records((19, 20, 21, 22, 23, 24, 25, 26, 27, 28))
    conn = _connect(db_path)
    try:
        schema_table = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_migrations'
            """
        ).fetchone()
        if schema_table is None:
            raise SchemaVerificationError("Таблица schema_migrations отсутствует")

        existing = _validate_recorded_checksums(conn, records)
        missing_versions = [
            record.version
            for record in records
            if record.version not in existing
        ]
        if missing_versions:
            raise SchemaVerificationError(
                f"Обязательные миграции не применены: {missing_versions!r}"
            )

        table_count, index_count, trigger_count = _verify_objects(conn)
        _verify_columns(conn)
        _verify_foreign_keys(conn)
        _verify_unique_indexes(conn)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SchemaVerificationError(
                f"Runtime-схема содержит нарушенные внешние ключи: {violations!r}"
            )
    finally:
        conn.close()

    return SchemaHealth(
        db_path=db_path,
        healthy=True,
        verified_versions=tuple(record.version for record in records),
        table_count=table_count,
        index_count=index_count,
        trigger_count=trigger_count,
    )
