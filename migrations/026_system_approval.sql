-- Автономные паузы подтверждённых сливов: решение, принятое ботом, а не владельцем.
--
-- Зачем. Контур переведён в approval-first: любая мутация рекламы идёт
-- через предложение владельцу и кнопку в Telegram. Решение владельца —
-- вернуть боту автономию, но ровно в ОДНОМ классе действий: пауза
-- подтверждённого слива (сверка с AMO прошла, оплат ноль, расход значимый).
-- Всё остальное — запуски, бюджеты, паузы по нулевым лидам и по тренду —
-- по-прежнему только через кнопку владельца.
--
-- Почему нельзя было обойтись без миграции. Единственная дорога к исполнению —
-- owner_execution_jobs, а он по внешнему ключу и по триггеру
-- trg_owner_job_requires_approval требует строку в owner_action_decisions с
-- decision_kind='APPROVE'. Сама эта строка до сих пор требовала полной
-- телеграм-родословной: consumed callback token, trusted ingress, update_id.
-- Подделывать телеграм-родословную ради автономного действия нельзя — это
-- ровно то смешение, которого нельзя допустить: аудит владельца и датасет
-- калибровки правил обязаны отличать «нажал владелец» от «решил бот».
--
-- Что делает миграция.
--   1. Перестраивает owner_action_decisions: телеграм-родословная становится
--      NULL-able, появляются decision_source ('OWNER'|'SYSTEM') и
--      automation_rule (машинное имя правила, по которому бот решил сам).
--      Табличный CHECK держит ровно две законные формы строки: полная
--      телеграм-родословная у OWNER и полностью пустая — у SYSTEM.
--   2. Пересобирает триггер валидации: ветка OWNER — прежняя проверка связки
--      token/proposal БЕЗ послаблений; ветка SYSTEM — совпадение
--      proposal_sha256 и запрет перебивать любое уже записанное решение
--      владельца (включая POSTPONE, который уникальным индексом не ловится).
--   3. Разрешает переход DELIVERY_PENDING → APPROVED только для SYSTEM-решения.
--      Автономное предложение не попадает в очередь владельца вообще: оно не
--      доставляется, у него нет кнопок, оно самоодобряется в том же прогоне.
--      Путь владельца PENDING_OWNER → APPROVED остался дословно прежним.
--
-- ВНИМАНИЕ: старый SQLite (<3.35) — НЕТ DROP COLUMN, а ALTER TABLE RENAME на 3.25+
-- переразбирает всю схему и падает на триггерах, которые ссылаются на временно
-- отсутствующую таблицу. Поэтому таблица пересобирается через нейтральную
-- копию без RENAME: копия → DROP → CREATE под тем же именем → возврат данных.
-- Внешние ключи на время пересборки откладываются до COMMIT (defer_foreign_keys):
-- к моменту коммита строки на месте, и PRAGMA foreign_key_check в runner'е
-- проверяет уже восстановленную схему.

PRAGMA defer_foreign_keys = ON;

CREATE TABLE _mig026_decisions_backup AS
SELECT * FROM owner_action_decisions;

DROP TRIGGER IF EXISTS trg_owner_decisions_no_update;
DROP TRIGGER IF EXISTS trg_owner_decisions_no_delete;
DROP TRIGGER IF EXISTS trg_owner_decision_validate;
DROP TRIGGER IF EXISTS trg_owner_decision_revoke_siblings;

DROP TABLE owner_action_decisions;

CREATE TABLE owner_action_decisions (
    decision_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    proposal_sha256 TEXT NOT NULL CHECK(length(proposal_sha256) = 64),
    decision_kind TEXT NOT NULL CHECK(decision_kind IN ('APPROVE','REJECT','POSTPONE')),
    owner_user_id INTEGER,
    chat_id INTEGER,
    message_id INTEGER,
    delivery_generation INTEGER CHECK(
        delivery_generation IS NULL OR delivery_generation > 0
    ),
    telegram_update_id INTEGER UNIQUE,
    callback_query_id TEXT UNIQUE,
    callback_token_id TEXT UNIQUE,
    trusted_ingress_sha256 TEXT CHECK(
        trusted_ingress_sha256 IS NULL OR length(trusted_ingress_sha256) = 64
    ),
    reason_text TEXT,
    recorded_at TEXT NOT NULL,
    decision_source TEXT NOT NULL DEFAULT 'OWNER'
        CHECK(decision_source IN ('OWNER','SYSTEM')),
    automation_rule TEXT,
    CHECK(
        (
            decision_source = 'OWNER'
            AND owner_user_id IS NOT NULL
            AND chat_id IS NOT NULL
            AND message_id IS NOT NULL
            AND delivery_generation IS NOT NULL
            AND telegram_update_id IS NOT NULL
            AND callback_query_id IS NOT NULL
            AND callback_token_id IS NOT NULL
            AND trusted_ingress_sha256 IS NOT NULL
            AND automation_rule IS NULL
        ) OR (
            decision_source = 'SYSTEM'
            AND decision_kind = 'APPROVE'
            AND owner_user_id IS NULL
            AND chat_id IS NULL
            AND message_id IS NULL
            AND delivery_generation IS NULL
            AND telegram_update_id IS NULL
            AND callback_query_id IS NULL
            AND callback_token_id IS NULL
            AND trusted_ingress_sha256 IS NULL
            AND automation_rule IS NOT NULL
        )
    ),
    UNIQUE(proposal_id, decision_id),
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(telegram_update_id) REFERENCES telegram_update_inbox(update_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(callback_token_id) REFERENCES owner_callback_tokens(token_id)
        ON DELETE RESTRICT
);

INSERT INTO owner_action_decisions (
    decision_id, proposal_id, proposal_sha256, decision_kind, owner_user_id,
    chat_id, message_id, delivery_generation, telegram_update_id,
    callback_query_id, callback_token_id, trusted_ingress_sha256, reason_text,
    recorded_at, decision_source, automation_rule
)
SELECT decision_id, proposal_id, proposal_sha256, decision_kind, owner_user_id,
       chat_id, message_id, delivery_generation, telegram_update_id,
       callback_query_id, callback_token_id, trusted_ingress_sha256, reason_text,
       recorded_at, 'OWNER', NULL
FROM _mig026_decisions_backup;

DROP TABLE _mig026_decisions_backup;

CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_terminal_decision
ON owner_action_decisions(proposal_id)
WHERE decision_kind IN ('APPROVE','REJECT');

CREATE TRIGGER IF NOT EXISTS trg_owner_decisions_no_update
BEFORE UPDATE ON owner_action_decisions BEGIN
    SELECT RAISE(ABORT, 'owner_action_decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_owner_decisions_no_delete
BEFORE DELETE ON owner_action_decisions BEGIN
    SELECT RAISE(ABORT, 'owner_action_decisions cannot be deleted');
END;

-- Owner decision must bind the exact immutable proposal and consumed trusted
-- token; system decision must bind the same immutable proposal and may never
-- overwrite a decision the owner already made.
CREATE TRIGGER IF NOT EXISTS trg_owner_decision_validate
BEFORE INSERT ON owner_action_decisions BEGIN
    SELECT CASE WHEN NEW.decision_source = 'OWNER' AND NOT EXISTS (
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
    SELECT CASE WHEN NEW.decision_source = 'SYSTEM' AND NOT EXISTS (
        SELECT 1
        FROM owner_action_proposals p
        WHERE p.proposal_id = NEW.proposal_id
          AND p.proposal_sha256 = NEW.proposal_sha256
    ) THEN RAISE(ABORT, 'decision/token/proposal binding mismatch') END;
    SELECT CASE WHEN NEW.decision_source = 'SYSTEM' AND EXISTS (
        SELECT 1
        FROM owner_action_decisions d
        WHERE d.proposal_id = NEW.proposal_id
    ) THEN RAISE(ABORT, 'system decision cannot override owner decision') END;
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

-- Переход DELIVERY_PENDING → APPROVED существует ТОЛЬКО для автономного
-- (SYSTEM) решения: предложение бота не доставляется владельцу и не имеет
-- кнопок, поэтому проходить через PENDING_OWNER ему незачем. Все прежние
-- переходы сохранены дословно.
DROP TRIGGER IF EXISTS trg_owner_lifecycle_transition;
CREATE TRIGGER IF NOT EXISTS trg_owner_lifecycle_transition
BEFORE UPDATE ON owner_action_lifecycle BEGIN
    SELECT CASE WHEN NEW.version <> OLD.version + 1
        THEN RAISE(ABORT, 'lifecycle CAS version mismatch') END;
    SELECT CASE WHEN NOT (
        (OLD.state='DELIVERY_PENDING' AND NEW.state IN ('PENDING_OWNER','EXPIRED','CANCELLED')) OR
        (
            OLD.state='DELIVERY_PENDING'
            AND NEW.state='APPROVED'
            AND NEW.active_decision_id IS NOT NULL
            AND NEW.active_job_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM owner_action_decisions d
                WHERE d.proposal_id = NEW.proposal_id
                  AND d.decision_id = NEW.active_decision_id
                  AND d.decision_source = 'SYSTEM'
                  AND d.decision_kind = 'APPROVE'
            )
        ) OR
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
