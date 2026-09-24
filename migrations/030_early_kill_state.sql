-- migrations/030_early_kill_state.sql
-- Волна 2a раннего стопа: состояние по объявлению + правило B «зрелый ноль» в журнале оценок.
--
-- early_kill_ad_state — кэш ответов AMO по объявлению, чтобы часовой проход не переспрашивал
-- одно и то же: квалы назад не исчезают, поэтому keep_forever=1 выводит объявление из обоих
-- правил навсегда без новых запросов.
--
-- early_kill_evaluations получает колонки rule (A — ноль заявок при расходе, B — зрелый ноль),
-- mature_leads и quals. Применяется идемпотентно через _apply_early_kill_migration()
-- (ALTER TABLE ADD COLUMN повторно даёт «duplicate column», применялка это глотает).
-- ВНИМАНИЕ: применялка режет файл по точке с запятой — в комментариях её не писать.
-- ВНИМАНИЕ: старый SQLite (<3.35) — НЕТ GENERATED-колонок, НЕТ DROP COLUMN.

CREATE TABLE IF NOT EXISTS early_kill_ad_state (
    ad_id              TEXT    PRIMARY KEY,
    account_id         TEXT    NOT NULL,
    created_time       TEXT,                          -- created_time объявления (UTC ISO)
    keep_forever       INTEGER NOT NULL DEFAULT 0,    -- 1 = есть квал, правила не применяются
    first_qual_seen_at TEXT,
    last_amo_at        TEXT,                          -- когда последний раз спрашивали AMO
    amo_leads          INTEGER,                       -- заявки с момента запуска
    amo_mature_leads   INTEGER,                       -- заявки старше 72 ч
    amo_quals          INTEGER,
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ekas_account ON early_kill_ad_state(account_id, keep_forever);

ALTER TABLE early_kill_evaluations ADD COLUMN rule TEXT NOT NULL DEFAULT 'A';
ALTER TABLE early_kill_evaluations ADD COLUMN mature_leads INTEGER;
ALTER TABLE early_kill_evaluations ADD COLUMN quals INTEGER;

CREATE INDEX IF NOT EXISTS idx_eke_rule_ad ON early_kill_evaluations(rule, ad_id, decision);
