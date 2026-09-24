-- Отметка «запущено» на карточке Trello — журнал исполнения TRELLO_COMPLETE.
--
-- Зачем. Миграция 022 завела в telegram_delivery_outbox назначение
-- 'TRELLO_COMPLETE': при переходе запуска в VERIFIED туда ложится строка
-- (services/launch_repository.py, ветка WATCHDOG_VERIFY). Потребителя у неё не
-- было ни одного — owner_delivery_outbox разбирает только 'OWNER_PROPOSAL'.
-- Итог в боевом режиме: объявления создавались и верифицировались, а зелёный чек на
-- карточке не появлялся никогда. Единственный живой вызов mark_card_done жил в
-- reconciliation авто-запуска под гейтом checker_mode=ENFORCE (дефолт observe)
-- и суточным кроном, то есть на боевом пути одобрения владельцем не работал.
--
-- Почему отдельная таблица, а не state='SENT' в самом outbox. CHECK строки
-- outbox требует у 'SENT' непустой telegram_message_id: состояние спроектировано
-- под отправку в Telegram. Отметка в Trello сообщением не является, и подгонять
-- под неё CHECK значило бы пересобирать таблицу с иммутабельной историей
-- доставок. Факт исполнения живёт рядом, в append-only журнале; outbox-строка
-- остаётся PENDING как запись «событие произошло», а повторную работу отсекает
-- PRIMARY KEY по delivery_id.
--
-- Идемпотентность двойная: PUT dueComplete=true в Trello сам по себе повторяем,
-- а журнал не даёт даже сделать лишний сетевой вызов.
--
-- ВНИМАНИЕ: старый SQLite (<3.35) — НЕТ GENERATED-колонок, НЕТ DROP COLUMN,
-- НЕТ «ADD COLUMN IF NOT EXISTS». Идемпотентность версии обеспечивает runner
-- (services/database_migrations.py): миграция применяется ровно один раз.

CREATE TABLE IF NOT EXISTS trello_completion_log (
    delivery_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('COMPLETED','SKIPPED')),
    reason_code TEXT,
    attempts INTEGER NOT NULL DEFAULT 1 CHECK(attempts >= 1),
    created_at TEXT NOT NULL,
    FOREIGN KEY(delivery_id) REFERENCES telegram_delivery_outbox(delivery_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(proposal_id) REFERENCES owner_action_proposals(proposal_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_trello_completion_card
ON trello_completion_log(card_id, created_at);

-- Журнал append-only: отметка карточки — свершившийся внешний факт, переписать
-- или стереть его задним числом нельзя, как и остальную историю контура.
CREATE TRIGGER IF NOT EXISTS trg_trello_completion_no_update
BEFORE UPDATE ON trello_completion_log BEGIN
    SELECT RAISE(ABORT, 'trello_completion_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_trello_completion_no_delete
BEFORE DELETE ON trello_completion_log BEGIN
    SELECT RAISE(ABORT, 'trello_completion_log cannot be deleted');
END;
