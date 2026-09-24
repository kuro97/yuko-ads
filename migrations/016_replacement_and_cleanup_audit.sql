-- migrations/016_replacement_and_cleanup_audit.sql
-- Durable workflow замены рекламы и append-only аудит необратимой чистки.
-- Миграция безопасна для повторного запуска: только CREATE ... IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS ad_replacement_workflows (
    workflow_id             TEXT PRIMARY KEY,
    tenant_id               TEXT NOT NULL DEFAULT 'default',
    old_ad_id               TEXT NOT NULL,
    old_ad_name             TEXT NOT NULL DEFAULT '',
    adset_id                TEXT NOT NULL,
    city                    TEXT NOT NULL DEFAULT '',
    adset_type              TEXT NOT NULL DEFAULT '',
    card_id                 TEXT,
    card_name               TEXT,
    replacement_ad_id       TEXT,
    released_ad_id          TEXT,
    phase                   TEXT NOT NULL CHECK (phase IN (
        'WAITING_SLOT','WAITING_CARD','LAUNCHING','WAITING_ACTIVE',
        'READY_TO_PAUSE','COMPLETED','BLOCKED','CANCELLED'
    )),
    attempt_count           INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error              TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    replacement_created_at  TEXT,
    replacement_active_at   TEXT,
    old_paused_at           TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_replacement_open_old
ON ad_replacement_workflows(old_ad_id, adset_id)
WHERE phase NOT IN ('COMPLETED','CANCELLED');

CREATE INDEX IF NOT EXISTS idx_replacement_phase
ON ad_replacement_workflows(phase, updated_at);

CREATE INDEX IF NOT EXISTS idx_replacement_adset
ON ad_replacement_workflows(adset_id, phase);

CREATE TABLE IF NOT EXISTS ad_cleanup_audit (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                   TEXT NOT NULL,
    workflow_id              TEXT,
    ad_id                    TEXT NOT NULL,
    ad_name                  TEXT NOT NULL DEFAULT '',
    adset_id                 TEXT NOT NULL,
    action                   TEXT NOT NULL CHECK (action IN (
        'DRY_RUN_CANDIDATE','SKIPPED','DELETE_ATTEMPT','DELETED','DELETE_FAILED'
    )),
    reason                   TEXT NOT NULL,
    configured_status        TEXT,
    effective_status         TEXT,
    age_days                 INTEGER,
    lifetime_spend_usd       REAL,
    local_spend_usd          REAL,
    capacity_before          INTEGER,
    capacity_after           INTEGER,
    evidence_json            TEXT NOT NULL DEFAULT '{}',
    error                    TEXT,
    actor                    TEXT NOT NULL,
    created_at               TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cleanup_audit_run
ON ad_cleanup_audit(run_id, id);

CREATE INDEX IF NOT EXISTS idx_cleanup_audit_ad
ON ad_cleanup_audit(ad_id, created_at);

-- DELETE_ATTEMPT одновременно является durable once-only claim. Даже если
-- процесс упал сразу после COMMIT, второй процесс не сможет повторить DELETE:
-- он обязан остановиться и reconcile живое состояние вручную.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cleanup_delete_attempt_workflow
ON ad_cleanup_audit(workflow_id)
WHERE workflow_id IS NOT NULL AND action = 'DELETE_ATTEMPT';
