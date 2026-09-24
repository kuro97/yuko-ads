-- Одноразовые authorizations для CREATE из existing creative.
CREATE TABLE IF NOT EXISTS asset_recovery_authorizations (
    auth_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE,
    secret_sha256 TEXT NOT NULL CHECK(length(secret_sha256) = 64),
    item_manifest_sha256 TEXT NOT NULL CHECK(length(item_manifest_sha256) = 64),
    account_id TEXT NOT NULL,
    source_ad_id TEXT NOT NULL,
    source_adset_id TEXT NOT NULL,
    source_creative_id TEXT NOT NULL,
    source_identity_sha256 TEXT NOT NULL CHECK(length(source_identity_sha256) = 64),
    target_adset_id TEXT NOT NULL,
    target_ad_name TEXT NOT NULL,
    target_identity_key TEXT NOT NULL,
    pre_inventory_sha256 TEXT NOT NULL CHECK(length(pre_inventory_sha256) = 64),
    phase TEXT NOT NULL CHECK(phase IN (
        'RESERVED','CREATE_STARTED','CREATED','FAILED','BLOCKED_RECONCILE'
    )),
    created_ad_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_recovery_open_target
ON asset_recovery_authorizations(account_id, target_adset_id, target_identity_key)
WHERE phase IN ('RESERVED','CREATE_STARTED','BLOCKED_RECONCILE');
