-- Durable authorization, all-target reservations, provider CREATE claims и аудит.
-- Миграция только добавляет объекты и безопасна для повторного запуска.

-- Один короткоживущий proof на точный план запуска карточки.
CREATE TABLE IF NOT EXISTS launch_authorizations (
    auth_id TEXT PRIMARY KEY,
    secret_sha256 TEXT NOT NULL CHECK(length(secret_sha256) = 64),
    card_id TEXT NOT NULL,
    card_name TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN (
        'CRON','AUTO_LAUNCH_NOW','MANUAL','BATCH','AGENT_RUN','RECOVERY'
    )),
    checker_mode TEXT NOT NULL CHECK(checker_mode = 'enforce'),
    campaign_type TEXT NOT NULL,
    account_kind TEXT NOT NULL CHECK(account_kind IN ('offline','online')),
    account_id TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256) = 64),
    media_sha256 TEXT NOT NULL CHECK(length(media_sha256) = 64),
    phase TEXT NOT NULL CHECK(phase IN (
        'RESERVED','CREATE_STARTED','PARTIAL','COMPLETED','RELEASED',
        'BLOCKED','BLOCKED_RECONCILE'
    )),
    topic_override INTEGER NOT NULL DEFAULT 0 CHECK(topic_override IN (0,1)),
    topic_override_reason TEXT,
    actor TEXT NOT NULL,
    recovery_plan_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    finished_at TEXT,
    CHECK((source = 'RECOVERY') = (recovery_plan_id IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_launch_auth_open_card_campaign
ON launch_authorizations(card_id, campaign_type)
WHERE phase IN ('RESERVED','CREATE_STARTED','PARTIAL','BLOCKED_RECONCILE');

CREATE INDEX IF NOT EXISTS idx_launch_auth_phase_expiry
ON launch_authorizations(phase, expires_at);

-- Резервация каждого adset. Все target-строки создаются одной transaction.
CREATE TABLE IF NOT EXISTS launch_authorization_targets (
    auth_id TEXT NOT NULL,
    city TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    account_kind TEXT NOT NULL CHECK(account_kind IN ('offline','online')),
    account_id TEXT NOT NULL,
    adset_id TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    expected_names_json TEXT NOT NULL,
    expected_ads_count INTEGER NOT NULL CHECK(expected_ads_count > 0),
    reserved_slots INTEGER NOT NULL CHECK(reserved_slots > 0),
    phase TEXT NOT NULL CHECK(phase IN (
        'RESERVED','CREATE_STARTED','PARTIAL','COMPLETED','RELEASED',
        'BLOCKED_RECONCILE'
    )),
    reservation_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(auth_id, city),
    UNIQUE(auth_id, ordinal),
    FOREIGN KEY(auth_id) REFERENCES launch_authorizations(auth_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_launch_target_reservations
ON launch_authorization_targets(adset_id, phase, reservation_expires_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_launch_target_open_identity
ON launch_authorization_targets(account_id, adset_id, identity_key)
WHERE phase IN ('RESERVED','CREATE_STARTED','PARTIAL','BLOCKED_RECONCILE');

-- Поимённые claims: CREATE_STARTED фиксируется до сетевого POST.
CREATE TABLE IF NOT EXISTS launch_authorization_ads (
    auth_id TEXT NOT NULL,
    city TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    ad_name TEXT NOT NULL,
    ad_name_key TEXT NOT NULL,
    account_id TEXT NOT NULL,
    adset_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN (
        'RESERVED','CREATE_STARTED','CREATED','RELEASED','BLOCKED_RECONCILE'
    )),
    claim_id TEXT,
    created_ad_id TEXT,
    create_started_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(auth_id, city, ad_name),
    UNIQUE(auth_id, city, ordinal),
    UNIQUE(claim_id),
    UNIQUE(created_ad_id),
    FOREIGN KEY(auth_id, city)
        REFERENCES launch_authorization_targets(auth_id, city) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_launch_ads_phase
ON launch_authorization_ads(auth_id, phase);

CREATE UNIQUE INDEX IF NOT EXISTS uq_launch_ads_open_exact_name
ON launch_authorization_ads(account_id, adset_id, ad_name_key)
WHERE phase IN ('RESERVED','CREATE_STARTED','BLOCKED_RECONCILE');

-- Append-only журнал решений. Прикладной код не UPDATE/DELETE эти строки.
CREATE TABLE IF NOT EXISTS launch_check_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    check_id TEXT NOT NULL,
    auth_id TEXT,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'CANDIDATE','PREFLIGHT','RESERVED','DENIED','OVERRIDE_ACCEPTED',
        'CREATE_CLAIMED','CREATE_CONFIRMED','PARTIAL','COMPLETED','BLOCKED','RELEASED'
    )),
    source TEXT NOT NULL,
    card_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(auth_id) REFERENCES launch_authorizations(auth_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_launch_audit_card_time
ON launch_check_audit(card_id, created_at);

CREATE INDEX IF NOT EXISTS idx_launch_audit_auth
ON launch_check_audit(auth_id, id);
