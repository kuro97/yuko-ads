-- Волна E: независимый верификатор исполненных действий, дневной дайджест
-- предложений с батч-кнопками, свободный фидбек владельца реплаем и полный
-- видимый след «нажал → исполнено → проверено».
--
-- Схема 022 остаётся неизменной: здесь только НОВЫЕ таблицы. Повторное
-- исполнение по-прежнему идёт исключительно через execution boundary
-- (owner_execution_jobs/owner_technical_permits), верификатор ничего не мутирует.

-- ---------------------------------------------------------------------------
-- E1. Журнал верификатора: каждая проверка исполненного действия живым FB.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS action_verifications (
    verification_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    attempt_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('LAUNCH','PAUSE','UNPAUSE','SCALE')),
    check_seq INTEGER NOT NULL CHECK(check_seq > 0),
    retry_no INTEGER NOT NULL DEFAULT 0 CHECK(retry_no >= 0 AND retry_no <= 3),
    expected_json TEXT NOT NULL CHECK(json_valid(expected_json)),
    observed_json TEXT NOT NULL CHECK(json_valid(observed_json)),
    verdict TEXT NOT NULL CHECK(verdict IN ('VERIFIED','MISMATCH','UNVERIFIABLE')),
    reason_code TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    UNIQUE(proposal_id, claim_id, check_seq),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_action_verifications_time
ON action_verifications(proposal_id, checked_at);

-- Курсор верификатора на claim: до какого терминального вердикта дошли и
-- сколько повторов уже израсходовано (потолок 3 по требованию владельца).
CREATE TABLE IF NOT EXISTS action_verification_state (
    proposal_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('LAUNCH','PAUSE','UNPAUSE','SCALE')),
    state TEXT NOT NULL CHECK(state IN ('PENDING','VERIFIED','ESCALATED')),
    check_seq INTEGER NOT NULL DEFAULT 0 CHECK(check_seq >= 0),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0 AND retry_count <= 3),
    last_verdict TEXT CHECK(
        last_verdict IS NULL
        OR last_verdict IN ('VERIFIED','MISMATCH','UNVERIFIABLE')
    ),
    retry_proposal_id TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(proposal_id, claim_id),
    CHECK(state <> 'VERIFIED' OR last_verdict = 'VERIFIED'),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_action_verification_work
ON action_verification_state(state, updated_at);

-- ---------------------------------------------------------------------------
-- E2/E3. Дневной дайджест предложений и его одноразовые батч-кнопки.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS owner_digest_runs (
    digest_id TEXT PRIMARY KEY,
    digest_date TEXT NOT NULL,
    digest_hour INTEGER NOT NULL CHECK(digest_hour >= 0 AND digest_hour <= 23),
    digest_trigger TEXT NOT NULL CHECK(digest_trigger IN ('SCHEDULE','MANUAL')),
    item_count INTEGER NOT NULL CHECK(item_count >= 0),
    created_at TEXT NOT NULL
);

-- Дедуп расписания: за сутки ровно один плановый дайджест.
CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_digest_schedule_slot
ON owner_digest_runs(digest_date)
WHERE digest_trigger = 'SCHEDULE';

CREATE TABLE IF NOT EXISTS owner_digest_items (
    digest_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    group_key TEXT NOT NULL,
    evidence_stale INTEGER NOT NULL CHECK(evidence_stale IN (0,1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(digest_id, proposal_id),
    UNIQUE(digest_id, ordinal),
    FOREIGN KEY(digest_id) REFERENCES owner_digest_runs(digest_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT
);

-- Одно предложение не попадает в два дайджеста.
CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_digest_item_proposal
ON owner_digest_items(proposal_id);

CREATE TABLE IF NOT EXISTS owner_digest_batch_tokens (
    token_id TEXT PRIMARY KEY,
    public_nonce TEXT NOT NULL UNIQUE,
    token_mac_sha256 TEXT NOT NULL CHECK(length(token_mac_sha256) = 64),
    digest_id TEXT NOT NULL,
    batch_kind TEXT NOT NULL CHECK(
        batch_kind IN ('APPROVE_ALL','REJECT_ALL','ONE_BY_ONE')
    ),
    expected_owner_user_id INTEGER NOT NULL,
    expected_chat_id INTEGER NOT NULL,
    expected_message_id INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    bound_at TEXT,
    consumed_at TEXT,
    consumed_update_id INTEGER UNIQUE,
    consumed_callback_query_id TEXT UNIQUE,
    CHECK(expires_at > created_at),
    CHECK((expected_message_id IS NULL) = (bound_at IS NULL)),
    CHECK(consumed_at IS NULL OR expected_message_id IS NOT NULL),
    UNIQUE(digest_id, batch_kind),
    FOREIGN KEY(digest_id) REFERENCES owner_digest_runs(digest_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_owner_digest_tokens_lookup
ON owner_digest_batch_tokens(digest_id, expires_at);

-- ---------------------------------------------------------------------------
-- E4. Свободный фидбек владельца реплаем — целиком, включая нераспознанный текст.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS owner_feedback (
    feedback_id TEXT PRIMARY KEY,
    proposal_id TEXT,
    digest_id TEXT,
    telegram_update_id INTEGER NOT NULL UNIQUE,
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    reply_to_message_id INTEGER,
    owner_user_id INTEGER NOT NULL CHECK(owner_user_id > 0),
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    parsed_action TEXT NOT NULL CHECK(
        parsed_action IN ('POSTPONE','COMMENT','DIGEST_NOW')
    ),
    parsed_until TEXT,
    created_at TEXT NOT NULL,
    CHECK(parsed_action <> 'POSTPONE' OR parsed_until IS NOT NULL),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(digest_id) REFERENCES owner_digest_runs(digest_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_owner_feedback_proposal
ON owner_feedback(proposal_id, created_at);

-- ---------------------------------------------------------------------------
-- E5. Полный видимый след решения: шапка дайджеста, результат исполнения,
-- вердикт верификатора и отчёт батча — одной durable очередью.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS owner_trail_messages (
    trail_id TEXT PRIMARY KEY,
    trail_kind TEXT NOT NULL CHECK(trail_kind IN (
        'DIGEST_HEADER','EXECUTION_RESULT','VERIFICATION_VERDICT',
        'BATCH_REPORT','FEEDBACK_ACK'
    )),
    proposal_id TEXT,
    digest_id TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    reply_to_message_id INTEGER,
    rendered_text TEXT NOT NULL,
    rendered_text_sha256 TEXT NOT NULL CHECK(length(rendered_text_sha256) = 64),
    button_spec_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(button_spec_json)),
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
    CHECK(state <> 'SENT' OR (telegram_message_id IS NOT NULL AND sent_at IS NOT NULL)),
    CHECK(trail_kind <> 'DIGEST_HEADER' OR digest_id IS NOT NULL),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(digest_id) REFERENCES owner_digest_runs(digest_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_owner_trail_work
ON owner_trail_messages(state, next_attempt_at, lease_until);

-- ---------------------------------------------------------------------------
-- Иммутабельность бизнес-фактов волны E (как в 022).
-- ---------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_action_verifications_no_update
BEFORE UPDATE ON action_verifications BEGIN
    SELECT RAISE(ABORT, 'action_verifications are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_action_verifications_no_delete
BEFORE DELETE ON action_verifications BEGIN
    SELECT RAISE(ABORT, 'action_verifications cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_feedback_no_update
BEFORE UPDATE ON owner_feedback BEGIN
    SELECT RAISE(ABORT, 'owner_feedback is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_feedback_no_delete
BEFORE DELETE ON owner_feedback BEGIN
    SELECT RAISE(ABORT, 'owner_feedback cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_digest_runs_no_update
BEFORE UPDATE ON owner_digest_runs BEGIN
    SELECT RAISE(ABORT, 'owner_digest_runs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_digest_items_no_update
BEFORE UPDATE ON owner_digest_items BEGIN
    SELECT RAISE(ABORT, 'owner_digest_items are immutable');
END;

-- Верификатор двигает курсор только вперёд и никогда не «размораживает»
-- терминальный вердикт.
CREATE TRIGGER IF NOT EXISTS trg_action_verification_state_forward
BEFORE UPDATE ON action_verification_state
WHEN NOT (
    OLD.state = 'PENDING'
    AND NEW.check_seq >= OLD.check_seq
    AND NEW.retry_count >= OLD.retry_count
    AND NEW.proposal_id IS OLD.proposal_id
    AND NEW.claim_id IS OLD.claim_id
    AND NEW.kind IS OLD.kind
    AND NEW.first_seen_at IS OLD.first_seen_at
)
BEGIN
    SELECT RAISE(ABORT, 'verification state moves forward from PENDING only');
END;

-- Батч-токен дайджеста одноразовый: привязка к message_id и consume — по разу.
CREATE TRIGGER IF NOT EXISTS trg_owner_digest_token_one_time_bind
BEFORE UPDATE OF expected_message_id, bound_at ON owner_digest_batch_tokens
WHEN NOT (
    OLD.expected_message_id IS NULL
    AND OLD.bound_at IS NULL
    AND NEW.expected_message_id IS NOT NULL
    AND NEW.bound_at IS NOT NULL
    AND OLD.consumed_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'digest batch token binding is one-time');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_digest_token_single_consume
BEFORE UPDATE OF consumed_at ON owner_digest_batch_tokens
WHEN OLD.consumed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'digest batch token is unavailable');
END;

CREATE TRIGGER IF NOT EXISTS trg_owner_digest_token_identity_lock
BEFORE UPDATE ON owner_digest_batch_tokens
WHEN NEW.token_id IS NOT OLD.token_id
  OR NEW.public_nonce IS NOT OLD.public_nonce
  OR NEW.token_mac_sha256 IS NOT OLD.token_mac_sha256
  OR NEW.digest_id IS NOT OLD.digest_id
  OR NEW.batch_kind IS NOT OLD.batch_kind
  OR NEW.expected_owner_user_id IS NOT OLD.expected_owner_user_id
  OR NEW.expected_chat_id IS NOT OLD.expected_chat_id
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.expires_at IS NOT OLD.expires_at
BEGIN
    SELECT RAISE(ABORT, 'digest batch token identity is immutable');
END;
