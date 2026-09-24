-- migrations/029_early_kill_evaluations.sql
-- Журнал проверок раннего стопа «расход ≥ N цен лида без заявки» (services/early_kill.py).
--
-- Зачем. Правило проверяется каждый час по молодым объявлениям, и каждая проверка
-- (расход, лиды FB, заявки AMO, эталон CPL, порог, решение, режим) пишется сюда.
-- В режиме shadow это единственный след работы правила и датасет калибровки
-- множителей, в боевом режиме — объяснение каждой паузы рядом с owner_action_events.
--
-- Применяется ИДЕМПОТЕНТНО через _apply_early_kill_migration() в
-- services/creative_intelligence.py (тот же приём, что 013 ad_hourly_metrics):
-- CREATE TABLE / CREATE INDEX IF NOT EXISTS безопасны при повторном запуске.
-- ВНИМАНИЕ: применялка режет файл по точке с запятой — в комментариях её не писать.
-- ВНИМАНИЕ: старый SQLite (<3.35) — НЕТ GENERATED-колонок, НЕТ DROP COLUMN.
-- Ретеншн: не чистим — объём небольшой, research читает историю SQL-запросом.

CREATE TABLE IF NOT EXISTS early_kill_evaluations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT    NOT NULL,                  -- один прогон крона = один run_id
    evaluated_at   TEXT    NOT NULL,                  -- ISO UTC
    mode           TEXT    NOT NULL,                  -- off | shadow | active
    account_id     TEXT    NOT NULL,                  -- кабинет без префикса act_
    ad_id          TEXT    NOT NULL,
    ad_name        TEXT,
    adset_id       TEXT,
    created_time   TEXT,                              -- created_time объявления от FB
    age_hours      REAL,                              -- возраст на момент проверки
    spend_usd      REAL,                              -- lifetime расход (живой FB)
    impressions    INTEGER,
    fb_leads       INTEGER,                           -- lifetime лиды по Meta actions
    amo_leads      INTEGER,                           -- заявки AMO с момента запуска, NULL = не запрашивали или недоступно
    cpl_ref_usd    REAL,                              -- эталон CPL кабинета (скользящий)
    multiplier     REAL,                              -- применённый множитель
    threshold_usd  REAL,                              -- порог расхода после пола/потолка
    decision       TEXT    NOT NULL,                  -- PAUSE | WAIT | KEEP
    reason         TEXT    NOT NULL,                  -- машинная причина
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_eke_ad_id ON early_kill_evaluations(ad_id);
CREATE INDEX IF NOT EXISTS idx_eke_evaluated_at ON early_kill_evaluations(evaluated_at);
CREATE INDEX IF NOT EXISTS idx_eke_decision ON early_kill_evaluations(decision, evaluated_at);
