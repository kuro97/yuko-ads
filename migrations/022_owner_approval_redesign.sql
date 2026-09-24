CREATE TABLE IF NOT EXISTS owner_action_proposals (
    proposal_id TEXT PRIMARY KEY,
    proposal_version INTEGER NOT NULL DEFAULT 1 CHECK(proposal_version = 1),
    proposal_kind TEXT NOT NULL CHECK(proposal_kind IN (
        'LAUNCH','PAUSE','UNPAUSE','SCALE','ASSET_RECOVERY'
    )),
    origin TEXT NOT NULL CHECK(origin IN (
        'WEB','CRON','AUTOPILOT','TELEGRAM_COMMAND','RECOVERY'
    )),
    idempotency_key TEXT NOT NULL UNIQUE,
    source_ref TEXT NOT NULL,
    requested_by_actor TEXT NOT NULL,
    summary TEXT NOT NULL,
    plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
    plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256) = 64),
    targets_sha256 TEXT NOT NULL CHECK(length(targets_sha256) = 64),
    evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256) = 64),
    config_version_sha256 TEXT NOT NULL CHECK(length(config_version_sha256) = 64),
    proposal_sha256 TEXT NOT NULL UNIQUE CHECK(length(proposal_sha256) = 64),
    staged_media_root TEXT,
    created_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    CHECK(valid_until > created_at)
);

CREATE TABLE IF NOT EXISTS owner_action_proposal_targets (
    proposal_id TEXT NOT NULL,
    claim_id TEXT NOT NULL UNIQUE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    action_kind TEXT NOT NULL CHECK(action_kind IN (
        'CREATE_AD','PAUSE_AD','UNPAUSE_AD','SET_ADSET_BUDGET','RECOVER_AD'
    )),
    account_id TEXT NOT NULL,
    adset_id TEXT,
    subject_id TEXT NOT NULL,
    city TEXT,
    language TEXT CHECK(language IS NULL OR language IN ('L2','L1')),
    intended_payload_json TEXT NOT NULL CHECK(json_valid(intended_payload_json)),
    intended_payload_sha256 TEXT NOT NULL CHECK(length(intended_payload_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(proposal_id, ordinal),
    UNIQUE(proposal_id, claim_id),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_owner_targets_scope
ON owner_action_proposal_targets(account_id, adset_id, action_kind);

CREATE TABLE IF NOT EXISTS owner_action_evidence (
    evidence_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    source_system TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK(complete IN (0,1)),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(proposal_id, evidence_kind, subject_id, payload_sha256),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_owner_evidence_proposal
ON owner_action_evidence(proposal_id, observed_at);

CREATE TABLE IF NOT EXISTS owner_action_lifecycle (
    proposal_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN (
        'DELIVERY_PENDING','PENDING_OWNER','POSTPONED','APPROVED','REJECTED',
        'EXPIRED','EXECUTION_QUEUED','LIVE_REVIEW','EXECUTION_RETRY_WAIT',
        'PERMIT_ISSUED','ATTEMPT_STARTED','EXECUTED','VERIFYING','VERIFIED',
        'BLOCKED_STALE','FAILED_NO_EFFECT','RECONCILE_REQUIRED','CANCELLED'
    )),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    delivery_generation INTEGER NOT NULL DEFAULT 0 CHECK(delivery_generation >= 0),
    active_decision_id TEXT,
    active_job_id TEXT,
    latest_reason_code TEXT,
    next_action_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_owner_lifecycle_work
ON owner_action_lifecycle(state, next_action_at);

CREATE TABLE IF NOT EXISTS telegram_update_inbox (
    update_id INTEGER PRIMARY KEY,
    ingress_kind TEXT NOT NULL CHECK(ingress_kind IN ('GET_UPDATES','WEBHOOK')),
    bot_token_identity_sha256 TEXT NOT NULL CHECK(length(bot_token_identity_sha256) = 64),
    raw_update_json TEXT NOT NULL CHECK(json_valid(raw_update_json)),
    raw_update_sha256 TEXT NOT NULL UNIQUE CHECK(length(raw_update_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('RECEIVED','PROCESSING','PROCESSED','FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    lease_token TEXT,
    lease_until TEXT,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    last_error_code TEXT
);

CREATE INDEX IF NOT EXISTS idx_telegram_inbox_work
ON telegram_update_inbox(state, lease_until, update_id);

CREATE TABLE IF NOT EXISTS telegram_poll_cursor (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    next_update_id INTEGER NOT NULL CHECK(next_update_id >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS owner_callback_tokens (
    token_id TEXT PRIMARY KEY,
    public_nonce TEXT NOT NULL UNIQUE,
    token_mac_sha256 TEXT NOT NULL CHECK(length(token_mac_sha256) = 64),
    proposal_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    delivery_generation INTEGER NOT NULL CHECK(delivery_generation > 0),
    decision_kind TEXT NOT NULL CHECK(decision_kind IN ('APPROVE','REJECT','POSTPONE')),
    expected_owner_user_id INTEGER NOT NULL,
    expected_chat_id INTEGER NOT NULL,
    expected_message_id INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    bound_at TEXT,
    consumed_at TEXT,
    consumed_update_id INTEGER UNIQUE,
    consumed_callback_query_id TEXT UNIQUE,
    revoked_at TEXT,
    revoke_reason TEXT,
    CHECK(expires_at > created_at),
    CHECK(NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)),
    CHECK((expected_message_id IS NULL) = (bound_at IS NULL)),
    CHECK(consumed_at IS NULL OR expected_message_id IS NOT NULL),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(delivery_id) REFERENCES telegram_delivery_outbox(delivery_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(consumed_update_id) REFERENCES telegram_update_inbox(update_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_owner_tokens_lookup
ON owner_callback_tokens(proposal_id, delivery_generation, expires_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_token_decision_generation
ON owner_callback_tokens(proposal_id, delivery_generation, decision_kind);

CREATE TABLE IF NOT EXISTS owner_action_decisions (
    decision_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    proposal_sha256 TEXT NOT NULL CHECK(length(proposal_sha256) = 64),
    decision_kind TEXT NOT NULL CHECK(decision_kind IN ('APPROVE','REJECT','POSTPONE')),
    owner_user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    delivery_generation INTEGER NOT NULL CHECK(delivery_generation > 0),
    telegram_update_id INTEGER NOT NULL UNIQUE,
    callback_query_id TEXT NOT NULL UNIQUE,
    callback_token_id TEXT NOT NULL UNIQUE,
    trusted_ingress_sha256 TEXT NOT NULL CHECK(length(trusted_ingress_sha256) = 64),
    reason_text TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE(proposal_id, decision_id),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(telegram_update_id) REFERENCES telegram_update_inbox(update_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(callback_token_id) REFERENCES owner_callback_tokens(token_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_terminal_decision
ON owner_action_decisions(proposal_id)
WHERE decision_kind IN ('APPROVE','REJECT');

CREATE TABLE IF NOT EXISTS owner_execution_jobs (
    job_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN (
        'QUEUED','REVIEWING','WAITING_RETRY','PERMIT_ISSUED',
        'ATTEMPT_STARTED','EXECUTED','VERIFYING','COMPLETE',
        'BLOCKED_STALE','FAILED_NO_EFFECT','RECONCILE_REQUIRED'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    lease_token TEXT,
    lease_until TEXT,
    next_attempt_at TEXT,
    operation_id TEXT UNIQUE,
    last_reason_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(proposal_id, decision_id),
    UNIQUE(proposal_id, job_id),
    UNIQUE(proposal_id, decision_id, job_id),
    FOREIGN KEY(proposal_id, decision_id)
        REFERENCES owner_action_decisions(proposal_id, decision_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_owner_jobs_work
ON owner_execution_jobs(state, next_attempt_at, lease_until);

CREATE TABLE IF NOT EXISTS owner_technical_permits (
    permit_id TEXT PRIMARY KEY,
    secret_sha256 TEXT NOT NULL UNIQUE CHECK(length(secret_sha256) = 64),
    proposal_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    account_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    exact_payload_sha256 TEXT NOT NULL CHECK(length(exact_payload_sha256) = 64),
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256) = 64),
    live_evidence_sha256 TEXT NOT NULL CHECK(length(live_evidence_sha256) = 64),
    phase TEXT NOT NULL CHECK(phase IN ('ISSUED','CONSUMED','EXPIRED','REVOKED')),
    sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    revoked_at TEXT,
    revoke_reason TEXT,
    CHECK(expires_at > issued_at),
    UNIQUE(proposal_id, decision_id, job_id, claim_id, permit_id),
    FOREIGN KEY(proposal_id, decision_id, job_id)
        REFERENCES owner_execution_jobs(proposal_id, decision_id, job_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(proposal_id, claim_id)
        REFERENCES owner_action_proposal_targets(proposal_id, claim_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_open_permit_claim
ON owner_technical_permits(proposal_id, claim_id)
WHERE phase = 'ISSUED';

CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_consumed_permit_claim
ON owner_technical_permits(proposal_id, claim_id)
WHERE phase = 'CONSUMED';

CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_permit_claim_sequence
ON owner_technical_permits(proposal_id, claim_id, sequence_no);

CREATE TABLE IF NOT EXISTS owner_action_attempts (
    attempt_id TEXT PRIMARY KEY,
    permit_id TEXT NOT NULL UNIQUE,
    proposal_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    account_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    exact_payload_sha256 TEXT NOT NULL CHECK(length(exact_payload_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN (
        'ATTEMPT_STARTED','CONFIRMED','FAILED_NO_EFFECT','RECONCILE_REQUIRED'
    )),
    provider_request_id TEXT,
    provider_result_sha256 TEXT CHECK(
        provider_result_sha256 IS NULL OR length(provider_result_sha256) = 64
    ),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    last_reason_code TEXT,
    UNIQUE(proposal_id, job_id, claim_id, attempt_id),
    FOREIGN KEY(proposal_id, decision_id, job_id, claim_id, permit_id)
        REFERENCES owner_technical_permits(
            proposal_id, decision_id, job_id, claim_id, permit_id
        )
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_attempt_claim
ON owner_action_attempts(proposal_id, claim_id);

CREATE TABLE IF NOT EXISTS owner_action_events (
    event_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK(event_seq > 0),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason_code TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(proposal_id, event_seq),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_owner_events_time
ON owner_action_events(proposal_id, created_at);

CREATE TABLE IF NOT EXISTS coverage_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    fetch_complete INTEGER NOT NULL CHECK(fetch_complete IN (0,1)),
    configured_group_count INTEGER NOT NULL CHECK(configured_group_count > 0),
    observed_group_count INTEGER NOT NULL CHECK(observed_group_count >= 0),
    page_count INTEGER NOT NULL CHECK(page_count >= 0),
    inventory_sha256 TEXT NOT NULL CHECK(length(inventory_sha256) = 64),
    error_code TEXT,
    CHECK(
        (fetch_complete = 1 AND observed_group_count = configured_group_count AND error_code IS NULL)
        OR fetch_complete = 0
    )
);

CREATE TABLE IF NOT EXISTS coverage_snapshot_groups (
    snapshot_id TEXT NOT NULL,
    group_key TEXT NOT NULL,
    account_id TEXT NOT NULL,
    city TEXT NOT NULL,
    language TEXT NOT NULL CHECK(language IN ('L2','L1')),
    adset_id TEXT NOT NULL,
    min_active INTEGER NOT NULL CHECK(min_active > 0),
    effective_active_count INTEGER,
    configured_active_count INTEGER,
    status TEXT NOT NULL CHECK(status IN ('ZERO','THIN','OK','UNKNOWN')),
    inventory_sha256 TEXT NOT NULL CHECK(length(inventory_sha256) = 64),
    PRIMARY KEY(snapshot_id, group_key),
    UNIQUE(snapshot_id, account_id, adset_id),
    FOREIGN KEY(snapshot_id) REFERENCES coverage_snapshots(snapshot_id)
        ON DELETE RESTRICT,
    CHECK(
        (status = 'UNKNOWN' AND effective_active_count IS NULL)
        OR (status = 'ZERO' AND effective_active_count = 0)
        OR (status = 'THIN' AND effective_active_count > 0
            AND effective_active_count < min_active)
        OR (status = 'OK' AND effective_active_count >= min_active)
    )
);

CREATE INDEX IF NOT EXISTS idx_coverage_group_status
ON coverage_snapshot_groups(group_key, status);

CREATE TABLE IF NOT EXISTS coverage_incidents (
    incident_id TEXT PRIMARY KEY,
    group_key TEXT NOT NULL,
    incident_kind TEXT NOT NULL CHECK(incident_kind IN ('ZERO','THIN','UNKNOWN')),
    state TEXT NOT NULL CHECK(state IN ('OPEN','RESOLVED')),
    opened_snapshot_id TEXT NOT NULL,
    latest_snapshot_id TEXT NOT NULL,
    consecutive_complete_ok INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_complete_ok >= 0),
    reminder_seq INTEGER NOT NULL DEFAULT 0 CHECK(reminder_seq >= 0),
    next_reminder_at TEXT,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(opened_snapshot_id) REFERENCES coverage_snapshots(snapshot_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(latest_snapshot_id) REFERENCES coverage_snapshots(snapshot_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_coverage_open_incident
ON coverage_incidents(group_key, incident_kind)
WHERE state = 'OPEN';

CREATE TABLE IF NOT EXISTS coverage_incident_events (
    event_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('OPENED','REMINDER','DELIVERED','RESOLVED')),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at TEXT NOT NULL,
    FOREIGN KEY(incident_id) REFERENCES coverage_incidents(incident_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(snapshot_id) REFERENCES coverage_snapshots(snapshot_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS telegram_delivery_outbox (
    delivery_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK(purpose IN ('OWNER_PROPOSAL','COVERAGE_ALERT','ACTION_OUTCOME','TRELLO_COMPLETE')),
    proposal_id TEXT,
    incident_id TEXT,
    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
    dedupe_key TEXT NOT NULL UNIQUE,
    rendered_text TEXT NOT NULL,
    rendered_text_sha256 TEXT NOT NULL CHECK(length(rendered_text_sha256) = 64),
    button_spec_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(button_spec_json)),
    button_spec_sha256 TEXT CHECK(button_spec_sha256 IS NULL OR length(button_spec_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('PENDING','LEASED','SENT','FAILED_VISIBLE')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    lease_token TEXT,
    lease_until TEXT,
    next_attempt_at TEXT,
    telegram_chat_id INTEGER,
    telegram_message_id INTEGER,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    last_error_code TEXT,
    CHECK(
        (purpose = 'OWNER_PROPOSAL' AND proposal_id IS NOT NULL AND incident_id IS NULL AND generation > 0)
        OR (purpose = 'COVERAGE_ALERT' AND incident_id IS NOT NULL AND proposal_id IS NULL)
        OR purpose IN ('ACTION_OUTCOME','TRELLO_COMPLETE')
    ),
    CHECK(state <> 'SENT' OR (telegram_message_id IS NOT NULL AND sent_at IS NOT NULL)),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(incident_id) REFERENCES coverage_incidents(incident_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_telegram_outbox_work
ON telegram_delivery_outbox(state, next_attempt_at, lease_until);

CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_delivery_generation
ON telegram_delivery_outbox(proposal_id, generation)
WHERE purpose = 'OWNER_PROPOSAL';

CREATE TABLE IF NOT EXISTS launch_watchdogs (
    watchdog_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL,
    job_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN (
        'EXECUTED','VERIFYING','VERIFIED','FAILED_VERIFICATION','RECONCILE_REQUIRED'
    )),
    expected_count INTEGER NOT NULL CHECK(expected_count > 0),
    verified_count INTEGER NOT NULL DEFAULT 0 CHECK(verified_count >= 0),
    verification_attempts INTEGER NOT NULL DEFAULT 0 CHECK(verification_attempts >= 0),
    lease_token TEXT,
    lease_until TEXT,
    next_verify_at TEXT,
    verify_deadline_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    verified_at TEXT,
    last_reason_code TEXT,
    CHECK(verified_count <= expected_count),
    CHECK(state <> 'VERIFIED' OR (
        verified_count = expected_count AND verified_at IS NOT NULL
    )),
    UNIQUE(proposal_id, watchdog_id),
    FOREIGN KEY(proposal_id, decision_id, job_id)
        REFERENCES owner_execution_jobs(proposal_id, decision_id, job_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_launch_watchdog_work
ON launch_watchdogs(state, next_verify_at, lease_until);

CREATE TABLE IF NOT EXISTS launch_watchdog_targets (
    proposal_id TEXT NOT NULL,
    watchdog_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    adset_id TEXT NOT NULL,
    expected_ad_name TEXT NOT NULL,
    expected_fingerprint TEXT NOT NULL CHECK(length(expected_fingerprint) = 64),
    created_ad_id TEXT NOT NULL UNIQUE,
    expected_configured_status TEXT NOT NULL CHECK(expected_configured_status = 'ACTIVE'),
    expected_effective_status TEXT NOT NULL CHECK(expected_effective_status = 'ACTIVE'),
    created_at TEXT NOT NULL,
    PRIMARY KEY(watchdog_id, claim_id),
    FOREIGN KEY(proposal_id, watchdog_id)
        REFERENCES launch_watchdogs(proposal_id, watchdog_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(proposal_id, claim_id)
        REFERENCES owner_action_proposal_targets(proposal_id, claim_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS launch_verification_observations (
    observation_id TEXT PRIMARY KEY,
    watchdog_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
    fetch_complete INTEGER NOT NULL CHECK(fetch_complete IN (0,1)),
    verified_count INTEGER NOT NULL CHECK(verified_count >= 0),
    outcome TEXT NOT NULL CHECK(outcome IN ('PENDING','VERIFIED','FAILED','UNKNOWN')),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256) = 64),
    observed_at TEXT NOT NULL,
    UNIQUE(watchdog_id, attempt_no),
    FOREIGN KEY(watchdog_id) REFERENCES launch_watchdogs(watchdog_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS scheduler_action_runs (
    run_id TEXT PRIMARY KEY,
    scheduler_name TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'CLAIMED','NO_ELIGIBLE','PROPOSAL_CREATED','PENDING_OWNER',
        'EXECUTING','VERIFYING','VERIFIED','FAILED_VISIBLE'
    )),
    proposal_id TEXT,
    watchdog_id TEXT,
    lease_token TEXT,
    lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    verified_at TEXT,
    last_reason_code TEXT,
    UNIQUE(scheduler_name, slot_key),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(proposal_id, watchdog_id)
        REFERENCES launch_watchdogs(proposal_id, watchdog_id)
        ON DELETE RESTRICT,
    CHECK(watchdog_id IS NULL OR proposal_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_scheduler_runs_work
ON scheduler_action_runs(state, lease_until);

CREATE TABLE IF NOT EXISTS owner_action_outcomes (
    outcome_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    horizon TEXT NOT NULL CHECK(horizon IN ('IMMEDIATE','D1','D3','D7','D30')),
    observed_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)),
    metrics_sha256 TEXT NOT NULL CHECK(length(metrics_sha256) = 64),
    UNIQUE(proposal_id, claim_id, horizon),
    FOREIGN KEY(proposal_id, job_id, claim_id, attempt_id)
        REFERENCES owner_action_attempts(
            proposal_id, job_id, claim_id, attempt_id
        )
        ON DELETE RESTRICT
);

-- Immutable business facts.
CREATE TRIGGER IF NOT EXISTS trg_owner_proposals_no_update
BEFORE UPDATE ON owner_action_proposals BEGIN
    SELECT RAISE(ABORT, 'owner_action_proposals are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_proposals_no_delete
BEFORE DELETE ON owner_action_proposals BEGIN
    SELECT RAISE(ABORT, 'owner_action_proposals cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_targets_no_update
BEFORE UPDATE ON owner_action_proposal_targets BEGIN
    SELECT RAISE(ABORT, 'owner_action_proposal_targets are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_targets_no_delete
BEFORE DELETE ON owner_action_proposal_targets BEGIN
    SELECT RAISE(ABORT, 'owner_action_proposal_targets cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_evidence_no_update
BEFORE UPDATE ON owner_action_evidence BEGIN
    SELECT RAISE(ABORT, 'owner_action_evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_evidence_no_delete
BEFORE DELETE ON owner_action_evidence BEGIN
    SELECT RAISE(ABORT, 'owner_action_evidence cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_decisions_no_update
BEFORE UPDATE ON owner_action_decisions BEGIN
    SELECT RAISE(ABORT, 'owner_action_decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_decisions_no_delete
BEFORE DELETE ON owner_action_decisions BEGIN
    SELECT RAISE(ABORT, 'owner_action_decisions cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_events_no_update
BEFORE UPDATE ON owner_action_events BEGIN
    SELECT RAISE(ABORT, 'owner_action_events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_events_no_delete
BEFORE DELETE ON owner_action_events BEGIN
    SELECT RAISE(ABORT, 'owner_action_events cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_coverage_snapshots_no_update
BEFORE UPDATE ON coverage_snapshots BEGIN
    SELECT RAISE(ABORT, 'coverage_snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_coverage_snapshots_no_delete
BEFORE DELETE ON coverage_snapshots BEGIN
    SELECT RAISE(ABORT, 'coverage_snapshots cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_coverage_groups_no_update
BEFORE UPDATE ON coverage_snapshot_groups BEGIN
    SELECT RAISE(ABORT, 'coverage_snapshot_groups are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_coverage_groups_no_delete
BEFORE DELETE ON coverage_snapshot_groups BEGIN
    SELECT RAISE(ABORT, 'coverage_snapshot_groups cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_coverage_events_no_update
BEFORE UPDATE ON coverage_incident_events BEGIN
    SELECT RAISE(ABORT, 'coverage_incident_events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_coverage_events_no_delete
BEFORE DELETE ON coverage_incident_events BEGIN
    SELECT RAISE(ABORT, 'coverage_incident_events cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_launch_targets_no_update
BEFORE UPDATE ON launch_watchdog_targets BEGIN
    SELECT RAISE(ABORT, 'launch_watchdog_targets are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_launch_targets_no_delete
BEFORE DELETE ON launch_watchdog_targets BEGIN
    SELECT RAISE(ABORT, 'launch_watchdog_targets cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_launch_observations_no_update
BEFORE UPDATE ON launch_verification_observations BEGIN
    SELECT RAISE(ABORT, 'launch observations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_launch_observations_no_delete
BEFORE DELETE ON launch_verification_observations BEGIN
    SELECT RAISE(ABORT, 'launch observations cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_outcomes_no_update
BEFORE UPDATE ON owner_action_outcomes BEGIN
    SELECT RAISE(ABORT, 'owner_action_outcomes are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_outcomes_no_delete
BEFORE DELETE ON owner_action_outcomes BEGIN
    SELECT RAISE(ABORT, 'owner_action_outcomes cannot be deleted');
END;

-- Decision must bind the exact immutable proposal and consumed trusted token.
CREATE TRIGGER IF NOT EXISTS trg_owner_decision_validate
BEFORE INSERT ON owner_action_decisions BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM owner_action_proposals p
        JOIN owner_callback_tokens t ON t.token_id = NEW.callback_token_id
        WHERE p.proposal_id = NEW.proposal_id
          AND p.proposal_sha256 = NEW.proposal_sha256
          AND t.proposal_id = NEW.proposal_id
          AND t.decision_kind = NEW.decision_kind
          AND t.delivery_generation = NEW.delivery_generation
          AND t.expected_owner_user_id = NEW.owner_user_id
          AND t.expected_chat_id = NEW.chat_id
          AND t.expected_message_id = NEW.message_id
          AND t.consumed_update_id = NEW.telegram_update_id
          AND t.consumed_callback_query_id = NEW.callback_query_id
          AND t.consumed_at IS NOT NULL
          AND t.revoked_at IS NULL
    ) THEN RAISE(ABORT, 'decision/token/proposal binding mismatch') END;
END;

-- A decision or new generation invalidates all sibling/older unused tokens.
CREATE TRIGGER IF NOT EXISTS trg_owner_decision_revoke_siblings
AFTER INSERT ON owner_action_decisions BEGIN
    UPDATE owner_callback_tokens
       SET revoked_at = NEW.recorded_at,
           revoke_reason = 'SIBLING_DECISION'
     WHERE proposal_id = NEW.proposal_id
       AND consumed_at IS NULL
       AND revoked_at IS NULL;
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_job_requires_approval
BEFORE INSERT ON owner_execution_jobs BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM owner_action_decisions d
        WHERE d.proposal_id = NEW.proposal_id
          AND d.decision_id = NEW.decision_id
          AND d.decision_kind = 'APPROVE'
    ) THEN RAISE(ABORT, 'execution job requires exact APPROVE decision') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_job_identity_lock
BEFORE UPDATE ON owner_execution_jobs
WHEN NEW.job_id IS NOT OLD.job_id
  OR NEW.proposal_id IS NOT OLD.proposal_id
  OR NEW.decision_id IS NOT OLD.decision_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'execution job identity is immutable');
END;

-- Следующий claim можно поставить в очередь только после известного исхода
-- предыдущего claim. RECONCILE_REQUIRED навсегда блокирует автоматический цикл.
CREATE TRIGGER IF NOT EXISTS trg_owner_job_claim_requeue
BEFORE UPDATE OF state ON owner_execution_jobs
WHEN NEW.state = 'QUEUED' BEGIN
    SELECT CASE WHEN OLD.state <> 'ATTEMPT_STARTED'
        THEN RAISE(ABORT, 'claim requeue requires ATTEMPT_STARTED job') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM owner_action_attempts a
        WHERE a.proposal_id = NEW.proposal_id
          AND a.job_id = NEW.job_id
          AND a.state NOT IN ('CONFIRMED','FAILED_NO_EFFECT')
    ) THEN RAISE(ABORT, 'claim requeue requires terminal-known attempts') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM owner_action_attempts a
        WHERE a.proposal_id = NEW.proposal_id
          AND a.job_id = NEW.job_id
          AND a.state IN ('CONFIRMED','FAILED_NO_EFFECT')
    ) THEN RAISE(ABORT, 'claim requeue requires completed claim') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM owner_action_proposal_targets t
        WHERE t.proposal_id = NEW.proposal_id
          AND NOT EXISTS (
              SELECT 1 FROM owner_action_attempts a
              WHERE a.proposal_id = t.proposal_id
                AND a.claim_id = t.claim_id
          )
    ) THEN RAISE(ABORT, 'claim requeue requires untouched claim') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_token_revoke_old_generation
AFTER INSERT ON owner_callback_tokens BEGIN
    UPDATE owner_callback_tokens
       SET revoked_at = NEW.created_at,
           revoke_reason = 'NEW_DELIVERY_GENERATION'
     WHERE proposal_id = NEW.proposal_id
       AND delivery_generation < NEW.delivery_generation
       AND consumed_at IS NULL
       AND revoked_at IS NULL;
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_token_identity_lock
BEFORE UPDATE ON owner_callback_tokens
WHEN NEW.token_id IS NOT OLD.token_id
  OR NEW.public_nonce IS NOT OLD.public_nonce
  OR NEW.token_mac_sha256 IS NOT OLD.token_mac_sha256
  OR NEW.proposal_id IS NOT OLD.proposal_id
  OR NEW.delivery_id IS NOT OLD.delivery_id
  OR NEW.delivery_generation IS NOT OLD.delivery_generation
  OR NEW.decision_kind IS NOT OLD.decision_kind
  OR NEW.expected_owner_user_id IS NOT OLD.expected_owner_user_id
  OR NEW.expected_chat_id IS NOT OLD.expected_chat_id
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.expires_at IS NOT OLD.expires_at
BEGIN
    SELECT RAISE(ABORT, 'callback token identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_token_one_time_bind
BEFORE UPDATE OF expected_message_id, bound_at ON owner_callback_tokens
WHEN NOT (
    OLD.expected_message_id IS NULL
    AND OLD.bound_at IS NULL
    AND NEW.expected_message_id IS NOT NULL
    AND NEW.bound_at IS NOT NULL
    AND OLD.consumed_at IS NULL
    AND OLD.revoked_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'callback token message binding is one-time');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_token_single_consume
BEFORE UPDATE OF consumed_at ON owner_callback_tokens
WHEN OLD.consumed_at IS NOT NULL OR OLD.revoked_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'callback token is unavailable');
END;

-- Legal lifecycle transitions only; version is CAS-incremented by one.
CREATE TRIGGER IF NOT EXISTS trg_owner_lifecycle_transition
BEFORE UPDATE ON owner_action_lifecycle BEGIN
    SELECT CASE WHEN NEW.version <> OLD.version + 1
        THEN RAISE(ABORT, 'lifecycle CAS version mismatch') END;
    SELECT CASE WHEN NOT (
        (OLD.state='DELIVERY_PENDING' AND NEW.state IN ('PENDING_OWNER','EXPIRED','CANCELLED')) OR
        (OLD.state='PENDING_OWNER' AND NEW.state IN ('POSTPONED','APPROVED','REJECTED','EXPIRED','CANCELLED')) OR
        (
            OLD.state='PENDING_OWNER'
            AND NEW.state='DELIVERY_PENDING'
            AND NEW.latest_reason_code IN ('CALLBACK_SECRET_ROTATION','REDELIVERY')
            AND NEW.delivery_generation = OLD.delivery_generation + 1
            AND OLD.active_decision_id IS NULL
            AND OLD.active_job_id IS NULL
            AND NEW.active_decision_id IS NULL
            AND NEW.active_job_id IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM owner_action_decisions d
                WHERE d.proposal_id = NEW.proposal_id
            )
            AND NOT EXISTS (
                SELECT 1 FROM owner_execution_jobs j
                WHERE j.proposal_id = NEW.proposal_id
            )
            AND EXISTS (
                SELECT 1 FROM telegram_delivery_outbox o
                WHERE o.proposal_id = NEW.proposal_id
                  AND o.purpose = 'OWNER_PROPOSAL'
                  AND o.generation = NEW.delivery_generation
                  AND o.state = 'PENDING'
            )
            AND 3 = (
                SELECT COUNT(*) FROM owner_callback_tokens t
                WHERE t.proposal_id = NEW.proposal_id
                  AND t.delivery_generation = NEW.delivery_generation
                  AND t.expected_message_id IS NULL
                  AND t.bound_at IS NULL
                  AND t.consumed_at IS NULL
                  AND t.revoked_at IS NULL
            )
            AND 3 = (
                SELECT COUNT(DISTINCT t.decision_kind)
                FROM owner_callback_tokens t
                WHERE t.proposal_id = NEW.proposal_id
                  AND t.delivery_generation = NEW.delivery_generation
                  AND t.delivery_id = (
                      SELECT o.delivery_id
                      FROM telegram_delivery_outbox o
                      WHERE o.proposal_id = NEW.proposal_id
                        AND o.generation = NEW.delivery_generation
                        AND o.purpose = 'OWNER_PROPOSAL'
                  )
            )
            AND NOT EXISTS (
                SELECT 1 FROM owner_callback_tokens t
                WHERE t.proposal_id = NEW.proposal_id
                  AND t.delivery_generation < NEW.delivery_generation
                  AND t.consumed_at IS NULL
                  AND t.revoked_at IS NULL
            )
        ) OR
        (OLD.state='POSTPONED' AND NEW.state IN ('DELIVERY_PENDING','EXPIRED','CANCELLED')) OR
        (OLD.state='APPROVED' AND NEW.state IN ('EXECUTION_QUEUED','EXPIRED')) OR
        (OLD.state='EXECUTION_QUEUED' AND NEW.state IN ('LIVE_REVIEW','EXECUTION_RETRY_WAIT','EXPIRED')) OR
        (OLD.state='EXECUTION_RETRY_WAIT' AND NEW.state IN ('LIVE_REVIEW','EXPIRED')) OR
        (OLD.state='LIVE_REVIEW' AND NEW.state IN ('PERMIT_ISSUED','EXECUTION_RETRY_WAIT','BLOCKED_STALE','FAILED_NO_EFFECT')) OR
        (OLD.state='PERMIT_ISSUED' AND NEW.state IN ('ATTEMPT_STARTED','EXECUTION_RETRY_WAIT','EXPIRED')) OR
        (OLD.state='ATTEMPT_STARTED' AND NEW.state IN ('EXECUTED','FAILED_NO_EFFECT','RECONCILE_REQUIRED')) OR
        (
            OLD.state='ATTEMPT_STARTED'
            AND NEW.state='EXECUTION_QUEUED'
            AND NEW.delivery_generation = OLD.delivery_generation
            AND NEW.active_decision_id IS OLD.active_decision_id
            AND NEW.active_job_id IS OLD.active_job_id
            AND EXISTS (
                SELECT 1 FROM owner_execution_jobs j
                WHERE j.proposal_id = NEW.proposal_id
                  AND j.decision_id = NEW.active_decision_id
                  AND j.job_id = NEW.active_job_id
                  AND j.state = 'QUEUED'
            )
            AND NOT EXISTS (
                SELECT 1 FROM owner_action_attempts a
                WHERE a.proposal_id = NEW.proposal_id
                  AND a.job_id = NEW.active_job_id
                  AND a.state NOT IN ('CONFIRMED','FAILED_NO_EFFECT')
            )
            AND EXISTS (
                SELECT 1 FROM owner_action_attempts a
                WHERE a.proposal_id = NEW.proposal_id
                  AND a.job_id = NEW.active_job_id
                  AND a.state IN ('CONFIRMED','FAILED_NO_EFFECT')
            )
            AND EXISTS (
                SELECT 1 FROM owner_action_proposal_targets t
                WHERE t.proposal_id = NEW.proposal_id
                  AND NOT EXISTS (
                      SELECT 1 FROM owner_action_attempts a
                      WHERE a.proposal_id = t.proposal_id
                        AND a.claim_id = t.claim_id
                  )
            )
        ) OR
        (OLD.state='EXECUTED' AND NEW.state IN ('VERIFYING','VERIFIED','FAILED_NO_EFFECT','RECONCILE_REQUIRED')) OR
        (OLD.state='VERIFYING' AND NEW.state IN ('VERIFIED','EXECUTION_RETRY_WAIT','FAILED_NO_EFFECT','RECONCILE_REQUIRED')) OR
        (OLD.state='RECONCILE_REQUIRED' AND NEW.state IN ('VERIFYING','VERIFIED','FAILED_NO_EFFECT'))
    ) THEN RAISE(ABORT, 'illegal owner action lifecycle transition') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_permit_identity_lock
BEFORE UPDATE ON owner_technical_permits
WHEN NEW.permit_id IS NOT OLD.permit_id
  OR NEW.secret_sha256 IS NOT OLD.secret_sha256
  OR NEW.proposal_id IS NOT OLD.proposal_id
  OR NEW.decision_id IS NOT OLD.decision_id
  OR NEW.job_id IS NOT OLD.job_id
  OR NEW.claim_id IS NOT OLD.claim_id
  OR NEW.operation_kind IS NOT OLD.operation_kind
  OR NEW.account_id IS NOT OLD.account_id
  OR NEW.resource_id IS NOT OLD.resource_id
  OR NEW.exact_payload_sha256 IS NOT OLD.exact_payload_sha256
  OR NEW.manifest_json IS NOT OLD.manifest_json
  OR NEW.manifest_sha256 IS NOT OLD.manifest_sha256
  OR NEW.live_evidence_sha256 IS NOT OLD.live_evidence_sha256
  OR NEW.issued_at IS NOT OLD.issued_at
  OR NEW.expires_at IS NOT OLD.expires_at
BEGIN
    SELECT RAISE(ABORT, 'permit identity is immutable');
END;

-- Permit всегда выдаётся первому untouched claim. Повторная выдача возможна
-- только после EXPIRED/REVOKED без attempt и с новым live evidence.
CREATE TRIGGER IF NOT EXISTS trg_owner_permit_issue_validate
BEFORE INSERT ON owner_technical_permits BEGIN
    SELECT CASE WHEN NEW.phase <> 'ISSUED'
        OR NEW.consumed_at IS NOT NULL
        OR NEW.revoked_at IS NOT NULL
        OR NEW.revoke_reason IS NOT NULL
        THEN RAISE(ABORT, 'new permit must start ISSUED') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM owner_action_lifecycle l
        JOIN owner_action_decisions d
          ON d.proposal_id = l.proposal_id
         AND d.decision_id = NEW.decision_id
        JOIN owner_execution_jobs j
          ON j.proposal_id = NEW.proposal_id
         AND j.decision_id = NEW.decision_id
         AND j.job_id = NEW.job_id
        JOIN owner_action_proposal_targets t
          ON t.proposal_id = NEW.proposal_id
         AND t.claim_id = NEW.claim_id
        WHERE l.proposal_id = NEW.proposal_id
          AND l.state = 'LIVE_REVIEW'
          AND l.active_decision_id = NEW.decision_id
          AND l.active_job_id = NEW.job_id
          AND d.decision_kind = 'APPROVE'
          AND j.state = 'REVIEWING'
          AND t.action_kind = NEW.operation_kind
          AND t.account_id = NEW.account_id
          AND t.intended_payload_sha256 = NEW.exact_payload_sha256
          AND NEW.resource_id = CASE
              WHEN t.action_kind IN ('PAUSE_AD','UNPAUSE_AD') THEN t.subject_id
              ELSE COALESCE(t.adset_id, t.subject_id)
          END
    ) THEN RAISE(ABORT, 'permit exact claim lineage mismatch') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM owner_action_attempts a
        WHERE a.proposal_id = NEW.proposal_id
          AND a.claim_id = NEW.claim_id
    ) THEN RAISE(ABORT, 'attempted claim cannot receive another permit') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM owner_action_attempts a
        WHERE a.proposal_id = NEW.proposal_id
          AND a.job_id = NEW.job_id
          AND a.state = 'RECONCILE_REQUIRED'
    ) THEN RAISE(ABORT, 'reconciliation blocks aggregate execution') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM owner_action_proposal_targets current_target
        JOIN owner_action_proposal_targets prior_target
          ON prior_target.proposal_id = current_target.proposal_id
         AND prior_target.ordinal < current_target.ordinal
        WHERE current_target.proposal_id = NEW.proposal_id
          AND current_target.claim_id = NEW.claim_id
          AND NOT EXISTS (
              SELECT 1 FROM owner_action_attempts prior_attempt
              WHERE prior_attempt.proposal_id = prior_target.proposal_id
                AND prior_attempt.claim_id = prior_target.claim_id
                AND prior_attempt.state IN ('CONFIRMED','FAILED_NO_EFFECT')
          )
    ) THEN RAISE(ABORT, 'prior claim is not terminal-known') END;
    SELECT CASE WHEN NEW.sequence_no <> (
        SELECT COALESCE(MAX(p.sequence_no), 0) + 1
        FROM owner_technical_permits p
        WHERE p.proposal_id = NEW.proposal_id
          AND p.claim_id = NEW.claim_id
    ) THEN RAISE(ABORT, 'permit sequence mismatch') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM owner_technical_permits p
        WHERE p.proposal_id = NEW.proposal_id
          AND p.claim_id = NEW.claim_id
          AND p.phase NOT IN ('EXPIRED','REVOKED')
    ) THEN RAISE(ABORT, 'previous permit is not replaceable') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM owner_technical_permits p
        WHERE p.proposal_id = NEW.proposal_id
          AND p.claim_id = NEW.claim_id
          AND p.live_evidence_sha256 = NEW.live_evidence_sha256
    ) THEN RAISE(ABORT, 'permit reissue requires fresh live review') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_permit_transition
BEFORE UPDATE OF phase ON owner_technical_permits
WHEN NOT (
    OLD.phase = 'ISSUED'
    AND (
        (
            NEW.phase = 'CONSUMED'
            AND NEW.consumed_at IS NOT NULL
            AND NEW.revoked_at IS NULL
            AND NEW.revoke_reason IS NULL
        )
        OR (
            NEW.phase IN ('EXPIRED','REVOKED')
            AND NEW.consumed_at IS NULL
            AND NEW.revoked_at IS NOT NULL
            AND NEW.revoke_reason IS NOT NULL
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'illegal permit transition');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_permit_terminal_fields
BEFORE UPDATE OF consumed_at, revoked_at, revoke_reason
ON owner_technical_permits
WHEN NOT (
    OLD.phase = 'ISSUED'
    AND (
        (
            NEW.phase = 'CONSUMED'
            AND OLD.consumed_at IS NULL
            AND NEW.consumed_at IS NOT NULL
            AND NEW.revoked_at IS NULL
            AND NEW.revoke_reason IS NULL
        )
        OR (
            NEW.phase IN ('EXPIRED','REVOKED')
            AND OLD.consumed_at IS NULL
            AND NEW.consumed_at IS NULL
            AND OLD.revoked_at IS NULL
            AND NEW.revoked_at IS NOT NULL
            AND NEW.revoke_reason IS NOT NULL
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'permit terminal fields require one legal transition');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_permit_no_delete
BEFORE DELETE ON owner_technical_permits BEGIN
    SELECT RAISE(ABORT, 'permits cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_attempt_identity_lock
BEFORE UPDATE ON owner_action_attempts
WHEN NEW.attempt_id IS NOT OLD.attempt_id
  OR NEW.permit_id IS NOT OLD.permit_id
  OR NEW.proposal_id IS NOT OLD.proposal_id
  OR NEW.decision_id IS NOT OLD.decision_id
  OR NEW.job_id IS NOT OLD.job_id
  OR NEW.claim_id IS NOT OLD.claim_id
  OR NEW.operation_kind IS NOT OLD.operation_kind
  OR NEW.account_id IS NOT OLD.account_id
  OR NEW.resource_id IS NOT OLD.resource_id
  OR NEW.exact_payload_sha256 IS NOT OLD.exact_payload_sha256
  OR NEW.started_at IS NOT OLD.started_at
BEGIN
    SELECT RAISE(ABORT, 'attempt identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_attempt_requires_consumed_permit
BEFORE INSERT ON owner_action_attempts BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM owner_technical_permits p
        WHERE p.proposal_id = NEW.proposal_id
          AND p.decision_id = NEW.decision_id
          AND p.job_id = NEW.job_id
          AND p.claim_id = NEW.claim_id
          AND p.permit_id = NEW.permit_id
          AND p.phase = 'CONSUMED'
          AND p.consumed_at IS NOT NULL
          AND p.operation_kind = NEW.operation_kind
          AND p.account_id = NEW.account_id
          AND p.resource_id = NEW.resource_id
          AND p.exact_payload_sha256 = NEW.exact_payload_sha256
    ) THEN RAISE(ABORT, 'attempt requires consumed exact-lineage permit') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_attempt_transition
BEFORE UPDATE OF state ON owner_action_attempts
WHEN NOT (
    OLD.state='ATTEMPT_STARTED'
    AND NEW.state IN ('CONFIRMED','FAILED_NO_EFFECT','RECONCILE_REQUIRED')
)
BEGIN
    SELECT RAISE(ABORT, 'illegal attempt transition');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_attempt_no_delete
BEFORE DELETE ON owner_action_attempts BEGIN
    SELECT RAISE(ABORT, 'attempts cannot be deleted');
END;

-- Outbox content/dedupe identity cannot be changed by retries.
CREATE TRIGGER IF NOT EXISTS trg_delivery_payload_lock
BEFORE UPDATE ON telegram_delivery_outbox
WHEN NEW.delivery_id IS NOT OLD.delivery_id
  OR NEW.purpose IS NOT OLD.purpose
  OR NEW.proposal_id IS NOT OLD.proposal_id
  OR NEW.incident_id IS NOT OLD.incident_id
  OR NEW.generation IS NOT OLD.generation
  OR NEW.dedupe_key IS NOT OLD.dedupe_key
  OR NEW.rendered_text IS NOT OLD.rendered_text
  OR NEW.rendered_text_sha256 IS NOT OLD.rendered_text_sha256
  OR NEW.button_spec_json IS NOT OLD.button_spec_json
  OR NEW.button_spec_sha256 IS NOT OLD.button_spec_sha256
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'delivery payload is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_delivery_no_delete
BEFORE DELETE ON telegram_delivery_outbox BEGIN
    SELECT RAISE(ABORT, 'delivery history cannot be deleted');
END;

-- Migration 019–021 business identities and audits are tightened additively.
CREATE TRIGGER IF NOT EXISTS trg_launch_check_audit_no_update
BEFORE UPDATE ON launch_check_audit BEGIN
    SELECT RAISE(ABORT, 'launch_check_audit is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_launch_check_audit_no_delete
BEFORE DELETE ON launch_check_audit BEGIN
    SELECT RAISE(ABORT, 'launch_check_audit cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_action_commands_no_update
BEFORE UPDATE ON action_producer_commands BEGIN
    SELECT RAISE(ABORT, 'action producer commands are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_action_commands_no_delete
BEFORE DELETE ON action_producer_commands BEGIN
    SELECT RAISE(ABORT, 'action producer commands cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_provider_ad_binding_identity_lock
BEFORE UPDATE ON launch_provider_ad_bindings
WHEN NEW.claim_id IS NOT OLD.claim_id
  OR NEW.auth_id IS NOT OLD.auth_id
  OR NEW.ad_name IS NOT OLD.ad_name
  OR NEW.adset_id IS NOT OLD.adset_id
  OR NEW.expected_fingerprint IS NOT OLD.expected_fingerprint
  OR NEW.expected_payload_json IS NOT OLD.expected_payload_json
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'provider ad binding identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_provider_ad_binding_no_delete
BEFORE DELETE ON launch_provider_ad_bindings BEGIN
    SELECT RAISE(ABORT, 'provider ad bindings cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_asset_recovery_identity_lock
BEFORE UPDATE ON asset_recovery_authorizations
WHEN NEW.auth_id IS NOT OLD.auth_id
  OR NEW.attempt_id IS NOT OLD.attempt_id
  OR NEW.item_manifest_sha256 IS NOT OLD.item_manifest_sha256
  OR NEW.account_id IS NOT OLD.account_id
  OR NEW.source_ad_id IS NOT OLD.source_ad_id
  OR NEW.target_adset_id IS NOT OLD.target_adset_id
  OR NEW.target_identity_key IS NOT OLD.target_identity_key
BEGIN
    SELECT RAISE(ABORT, 'asset recovery identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_asset_recovery_no_delete
BEFORE DELETE ON asset_recovery_authorizations BEGIN
    SELECT RAISE(ABORT, 'asset recovery authorizations cannot be deleted');
END;
