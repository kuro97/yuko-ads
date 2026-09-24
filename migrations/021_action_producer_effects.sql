-- Durable producer commands and post-CONFIRMED effects.
CREATE TABLE IF NOT EXISTS action_producer_commands (
    idempotency_key TEXT PRIMARY KEY,
    scope TEXT NOT NULL UNIQUE,
    command_payload_sha256 TEXT NOT NULL,
    operation_id TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS action_effects (
    effect_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    effect_kind TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'APPLIED')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at TEXT,
    UNIQUE(operation_id, effect_kind)
);

CREATE TABLE IF NOT EXISTS action_outbox (
    effect_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    typed_payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'SENDING', 'SENT', 'FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    sent_at TEXT,
    FOREIGN KEY(effect_id) REFERENCES action_effects(effect_id)
);

CREATE TABLE IF NOT EXISTS action_state_projections (
    effect_id TEXT PRIMARY KEY,
    projection_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(effect_id) REFERENCES action_effects(effect_id)
);

CREATE TABLE IF NOT EXISTS launch_provider_asset_bindings (
    auth_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('UPLOAD_STARTED','BOUND','AMBIGUOUS')),
    provider_payload_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(auth_id, asset_id)
);

CREATE TABLE IF NOT EXISTS launch_provider_ad_bindings (
    claim_id TEXT PRIMARY KEY,
    auth_id TEXT NOT NULL,
    ad_name TEXT NOT NULL,
    adset_id TEXT NOT NULL,
    expected_fingerprint TEXT NOT NULL CHECK(length(expected_fingerprint) = 64),
    expected_payload_json TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('CLAIMED','VERIFIED','AMBIGUOUS')),
    ad_id TEXT UNIQUE,
    creative_id TEXT,
    verified_fingerprint TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_launch_provider_ad_auth
ON launch_provider_ad_bindings(auth_id, phase);
