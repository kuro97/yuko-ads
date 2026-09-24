-- Durable dry-run manifests, workflow-only DELETE claims и recovery запусков.
-- Production-строки миграции 016 не меняются. Единственный DROP заменяет
-- слишком широкий unique-индекс 016 точным индексом workflow+ad.

PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

DROP INDEX IF EXISTS uq_cleanup_delete_attempt_workflow;

CREATE UNIQUE INDEX IF NOT EXISTS uq_cleanup_delete_attempt_workflow_ad
ON ad_cleanup_audit(workflow_id, ad_id)
WHERE workflow_id IS NOT NULL AND action = 'DELETE_ATTEMPT';

CREATE TABLE IF NOT EXISTS ad_cleanup_runs (
    run_id                  TEXT PRIMARY KEY,
    tenant_id               TEXT NOT NULL DEFAULT 'default',
    run_kind                TEXT NOT NULL CHECK (run_kind IN (
                                'PROACTIVE_DAILY','REPLACEMENT_SLOT','MANUAL_DRY_RUN'
                            )),
    workflow_id             TEXT,
    scheduled_date          TEXT NOT NULL,
    requested_mode          TEXT NOT NULL CHECK (requested_mode IN ('dry_run','active')),
    effective_mode          TEXT NOT NULL CHECK (effective_mode IN ('dry_run','active')),
    phase                   TEXT NOT NULL CHECK (phase IN (
                                'PLANNED','RUNNING','COMPLETED',
                                'COMPLETED_WITH_WARNINGS','BLOCKED','FAILED'
                            )),
    config_json             TEXT NOT NULL DEFAULT '{}',
    evidence_json           TEXT NOT NULL DEFAULT '{}',
    lease_owner             TEXT,
    lease_expires_at        TEXT,
    discovered_count        INTEGER NOT NULL DEFAULT 0 CHECK (discovered_count >= 0),
    eligible_count          INTEGER NOT NULL DEFAULT 0 CHECK (eligible_count >= 0),
    would_delete_count      INTEGER NOT NULL DEFAULT 0 CHECK (would_delete_count >= 0),
    deleted_count           INTEGER NOT NULL DEFAULT 0 CHECK (deleted_count >= 0),
    skipped_count           INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    warning_count           INTEGER NOT NULL DEFAULT 0 CHECK (warning_count >= 0),
    error_count             INTEGER NOT NULL DEFAULT 0 CHECK (error_count >= 0),
    error                   TEXT,
    created_at              TEXT NOT NULL,
    started_at              TEXT,
    completed_at            TEXT,
    updated_at              TEXT NOT NULL,
    CHECK (
        requested_mode = effective_mode
        AND (
        (run_kind = 'PROACTIVE_DAILY'
            AND workflow_id IS NULL
            AND requested_mode = 'dry_run'
            AND effective_mode = 'dry_run')
        OR
        (run_kind = 'REPLACEMENT_SLOT'
            AND workflow_id IS NOT NULL)
        OR
        (run_kind = 'MANUAL_DRY_RUN'
            AND workflow_id IS NULL
            AND requested_mode = 'dry_run'
            AND effective_mode = 'dry_run')
        )
    ),
    FOREIGN KEY (workflow_id) REFERENCES ad_replacement_workflows(workflow_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cleanup_daily_run
ON ad_cleanup_runs(tenant_id, run_kind, scheduled_date)
WHERE run_kind = 'PROACTIVE_DAILY';

CREATE UNIQUE INDEX IF NOT EXISTS uq_cleanup_replacement_run
ON ad_cleanup_runs(workflow_id)
WHERE run_kind = 'REPLACEMENT_SLOT';

-- Parent key гарантирует, что claim принадлежит тому же workflow-bound run.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cleanup_run_workflow_pair
ON ad_cleanup_runs(run_id, workflow_id);

CREATE INDEX IF NOT EXISTS idx_cleanup_runs_phase
ON ad_cleanup_runs(phase, updated_at);

CREATE INDEX IF NOT EXISTS idx_cleanup_runs_lease
ON ad_cleanup_runs(lease_expires_at)
WHERE phase = 'RUNNING';

CREATE TABLE IF NOT EXISTS ad_cleanup_candidates (
    run_id                  TEXT NOT NULL,
    ad_id                   TEXT NOT NULL,
    account_kind            TEXT NOT NULL,
    adset_id                TEXT NOT NULL,
    adset_name              TEXT NOT NULL DEFAULT '',
    ad_name                 TEXT NOT NULL DEFAULT '',
    ordinal                 INTEGER NOT NULL CHECK (ordinal >= 0),
    state                   TEXT NOT NULL CHECK (state IN (
                                'DISCOVERED','ELIGIBLE','CLAIMED','DELETED',
                                'DELETE_FAILED','SKIPPED','BLOCKED','RECONCILE_REQUIRED'
                            )),
    reason                  TEXT NOT NULL DEFAULT '',
    configured_status       TEXT,
    effective_status        TEXT,
    age_days                INTEGER,
    lifetime_spend_usd      REAL,
    lifetime_impressions    INTEGER,
    lifetime_clicks         INTEGER,
    local_spend_usd         REAL,
    local_impressions       INTEGER,
    local_clicks            INTEGER,
    local_leads             INTEGER,
    local_payments          INTEGER,
    capacity_before         INTEGER,
    capacity_after          INTEGER,
    evidence_json           TEXT NOT NULL DEFAULT '{}',
    claim_id                TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (run_id, ad_id),
    FOREIGN KEY (run_id) REFERENCES ad_cleanup_runs(run_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_cleanup_candidates_state
ON ad_cleanup_candidates(run_id, state, ordinal);

CREATE INDEX IF NOT EXISTS idx_cleanup_candidates_adset
ON ad_cleanup_candidates(adset_id, state);

CREATE TABLE IF NOT EXISTS ad_cleanup_delete_claims (
    claim_id                TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL,
    workflow_id             TEXT NOT NULL,
    ad_id                   TEXT NOT NULL,
    ad_name                 TEXT NOT NULL DEFAULT '',
    adset_id                TEXT NOT NULL,
    purpose                 TEXT NOT NULL CHECK (purpose = 'REPLACEMENT_SLOT'),
    state                   TEXT NOT NULL CHECK (state IN (
                                'CLAIMED','DELETED','DELETE_FAILED','RECONCILE_REQUIRED'
                            )),
    evidence_json           TEXT NOT NULL DEFAULT '{}',
    capacity_before         INTEGER NOT NULL CHECK (capacity_before >= 0),
    capacity_after          INTEGER,
    claimed_by              TEXT NOT NULL,
    claimed_at              TEXT NOT NULL,
    completed_at            TEXT,
    error                   TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (run_id, workflow_id)
        REFERENCES ad_cleanup_runs(run_id, workflow_id) ON DELETE RESTRICT,
    FOREIGN KEY (workflow_id) REFERENCES ad_replacement_workflows(workflow_id) ON DELETE RESTRICT
);

-- Любой committed automatic attempt навсегда запрещает автоматический повтор.
-- Ручной reconciliation не удаляет строку и не сбрасывает уникальность.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cleanup_delete_claim_ad
ON ad_cleanup_delete_claims(ad_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_cleanup_delete_claim_workflow_ad
ON ad_cleanup_delete_claims(workflow_id, ad_id);

CREATE INDEX IF NOT EXISTS idx_cleanup_claim_workflow
ON ad_cleanup_delete_claims(workflow_id, claimed_at);

CREATE INDEX IF NOT EXISTS idx_cleanup_claim_state
ON ad_cleanup_delete_claims(state, updated_at);

CREATE INDEX IF NOT EXISTS idx_cleanup_claim_run
ON ad_cleanup_delete_claims(run_id, claimed_at);

-- Authorization является durable state machine, а не process-local token.
-- HTTP_STARTED фиксируется до сетевого DELETE. Поэтому crash/timeout после
-- этого CAS навсегда запрещает автоматический повтор и требует reconcile.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cleanup_delete_claim_exact_binding
ON ad_cleanup_delete_claims(
    claim_id, run_id, workflow_id, adset_id, ad_id, purpose
);

CREATE TABLE IF NOT EXISTS ad_cleanup_delete_authorizations (
    token_id                TEXT PRIMARY KEY,
    claim_id                TEXT NOT NULL UNIQUE,
    run_id                  TEXT NOT NULL,
    workflow_id             TEXT NOT NULL,
    adset_id                TEXT NOT NULL,
    ad_id                   TEXT NOT NULL,
    purpose                 TEXT NOT NULL CHECK (purpose = 'REPLACEMENT_SLOT'),
    state                   TEXT NOT NULL CHECK (state IN (
                                'ISSUED','HTTP_STARTED','CONSUMED',
                                'REVOKED','RECONCILE_REQUIRED'
                            )),
    issued_at               TEXT NOT NULL,
    expires_at              TEXT NOT NULL,
    http_started_at         TEXT,
    consumed_at             TEXT,
    reconciled_at           TEXT,
    revoked_at              TEXT,
    updated_at              TEXT NOT NULL,
    CHECK (expires_at > issued_at),
    CHECK (
        (state = 'ISSUED'
            AND http_started_at IS NULL
            AND consumed_at IS NULL
            AND reconciled_at IS NULL
            AND revoked_at IS NULL)
        OR
        (state = 'HTTP_STARTED'
            AND http_started_at IS NOT NULL
            AND consumed_at IS NULL
            AND reconciled_at IS NULL
            AND revoked_at IS NULL)
        OR
        (state = 'CONSUMED'
            AND http_started_at IS NOT NULL
            AND consumed_at IS NOT NULL
            AND reconciled_at IS NULL
            AND revoked_at IS NULL)
        OR
        (state = 'RECONCILE_REQUIRED'
            AND http_started_at IS NOT NULL
            AND consumed_at IS NULL
            AND reconciled_at IS NOT NULL
            AND revoked_at IS NULL)
        OR
        (state = 'REVOKED'
            AND http_started_at IS NULL
            AND consumed_at IS NULL
            AND reconciled_at IS NULL
            AND revoked_at IS NOT NULL)
    ),
    FOREIGN KEY (
        claim_id, run_id, workflow_id, adset_id, ad_id, purpose
    ) REFERENCES ad_cleanup_delete_claims(
        claim_id, run_id, workflow_id, adset_id, ad_id, purpose
    ) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_cleanup_authorization_state
ON ad_cleanup_delete_authorizations(state, updated_at);

CREATE TABLE IF NOT EXISTS ad_replacement_launch_links (
    workflow_id             TEXT PRIMARY KEY,
    launch_attempt_key      TEXT NOT NULL UNIQUE,
    card_id                 TEXT NOT NULL,
    card_name               TEXT NOT NULL DEFAULT '',
    city                    TEXT NOT NULL,
    account_kind            TEXT NOT NULL,
    account_id              TEXT NOT NULL,
    adset_id                TEXT NOT NULL,
    expected_ad_count       INTEGER NOT NULL CHECK (expected_ad_count >= 1),
    expected_ad_names_json  TEXT NOT NULL DEFAULT '[]',
    media_manifest_sha256   TEXT NOT NULL,
    created_ad_ids_json     TEXT NOT NULL DEFAULT '[]',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES ad_replacement_workflows(workflow_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_replacement_launch_card
ON ad_replacement_launch_links(card_id, city);

CREATE TABLE IF NOT EXISTS ad_replacement_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id             TEXT NOT NULL,
    event_type              TEXT NOT NULL CHECK (event_type IN (
                                'ENQUEUED','SLOT_RESERVED','LAUNCH_CLAIMED',
                                'CREATE_RECORDED','RECONCILED','ACTIVE_CONFIRMED',
                                'OLD_PAUSE_ATTEMPT','OLD_PAUSED','BLOCKED','CANCELLED'
                            )),
    actor                   TEXT NOT NULL,
    evidence_json           TEXT NOT NULL DEFAULT '{}',
    error                   TEXT,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES ad_replacement_workflows(workflow_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_replacement_events_workflow
ON ad_replacement_events(workflow_id, id);

CREATE TABLE IF NOT EXISTS launch_recovery_cases (
    case_id                 TEXT PRIMARY KEY,
    tenant_id               TEXT NOT NULL DEFAULT 'default',
    trello_action_id        TEXT NOT NULL UNIQUE,
    card_id                 TEXT NOT NULL,
    card_name               TEXT NOT NULL DEFAULT '',
    source_completed_at     TEXT NOT NULL,
    scan_since              TEXT NOT NULL,
    campaign_type           TEXT,
    account_kind            TEXT,
    account_id              TEXT,
    phase                   TEXT NOT NULL CHECK (phase IN (
                                'DISCOVERED','NO_ACTION','REVIEW_REQUIRED',
                                'WAITING_SLOT','APPROVED','LAUNCHING',
                                'WAITING_ACTIVE','RECOVERED','BLOCKED','CANCELLED'
                            )),
    target_cities_json      TEXT NOT NULL DEFAULT '[]',
    found_ads_json          TEXT NOT NULL DEFAULT '{}',
    missing_cities_json     TEXT NOT NULL DEFAULT '[]',
    expected_names_json     TEXT NOT NULL DEFAULT '{}',
    media_manifest_json     TEXT NOT NULL DEFAULT '{}',
    media_manifest_sha256   TEXT,
    fb_evidence_json        TEXT NOT NULL DEFAULT '{}',
    launch_attempt_key      TEXT,
    approved_by             TEXT,
    approved_at             TEXT,
    last_error              TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    completed_at            TEXT
);

CREATE INDEX IF NOT EXISTS idx_launch_recovery_phase
ON launch_recovery_cases(phase, updated_at);

CREATE INDEX IF NOT EXISTS idx_launch_recovery_card
ON launch_recovery_cases(card_id, source_completed_at);

DROP INDEX IF EXISTS uq_launch_recovery_open_card;

CREATE UNIQUE INDEX uq_launch_recovery_open_card
ON launch_recovery_cases(card_id)
WHERE phase IN ('DISCOVERED','WAITING_SLOT','APPROVED','LAUNCHING','WAITING_ACTIVE');

-- REVIEW_REQUIRED/BLOCKED — quarantined audit records: они могут сосуществовать
-- с одной runnable/in-flight case той же карточки, но сами не допускаются к apply.
CREATE TABLE IF NOT EXISTS launch_recovery_city_plans (
    plan_id                  TEXT PRIMARY KEY,
    case_id                  TEXT NOT NULL,
    city                     TEXT NOT NULL,
    account_kind             TEXT NOT NULL CHECK (account_kind IN ('offline','online')),
    account_id               TEXT NOT NULL,
    adset_id                 TEXT NOT NULL,
    expected_ad_names_json   TEXT NOT NULL,
    expected_ad_count        INTEGER NOT NULL CHECK (expected_ad_count >= 1),
    reconcile_from           TEXT NOT NULL,
    reconcile_until          TEXT NOT NULL,
    phase                    TEXT NOT NULL CHECK (phase IN (
                                 'DISCOVERED','COMPLETE','MISSING','REVIEW_REQUIRED',
                                 'WAITING_SLOT','APPROVED','LAUNCHING','WAITING_ACTIVE',
                                 'RECOVERED','BLOCKED','CANCELLED'
                             )),
    found_ad_ids_json        TEXT NOT NULL DEFAULT '[]',
    media_manifest_sha256    TEXT NOT NULL,
    launch_attempt_key       TEXT UNIQUE,
    capacity_available       INTEGER CHECK (
                                 capacity_available IS NULL
                                 OR capacity_available BETWEEN 0 AND 50
                             ),
    evidence_json            TEXT NOT NULL DEFAULT '{}',
    approved_by              TEXT,
    approved_at              TEXT,
    last_rechecked_at        TEXT,
    last_error               TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    completed_at             TEXT,
    UNIQUE (case_id, city),
    CHECK (reconcile_until >= reconcile_from),
    FOREIGN KEY (case_id) REFERENCES launch_recovery_cases(case_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_recovery_city_phase
ON launch_recovery_city_plans(phase, updated_at);

CREATE INDEX IF NOT EXISTS idx_recovery_city_scope
ON launch_recovery_city_plans(account_kind, account_id, adset_id, phase);

COMMIT;
