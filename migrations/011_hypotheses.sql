-- migrations/011_hypotheses.sql
-- Журнал гипотез Фазы 4: при каждом авто-запуске бот фиксирует измеримое
-- ожидание, через 7-14 дней сверяет с фактом продаж и выносит вердикт.
-- Применяется ИДЕМПОТЕНТНО через _apply_hypotheses_migration() в
-- creative_intelligence.py: CREATE TABLE / INDEX IF NOT EXISTS безопасны.
-- ВНИМАНИЕ: старый SQLite (<3.35) — НЕТ GENERATED-колонок, НЕТ DROP COLUMN.

CREATE TABLE IF NOT EXISTS hypotheses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Измерения гипотезы (что именно проверяем)
    angle           TEXT NOT NULL DEFAULT '',   -- угол/оффер (из topic.angle или card_name)
    city            TEXT NOT NULL DEFAULT '',    -- город запуска ('' = все города / несколько)
    ad_format       TEXT NOT NULL DEFAULT '',    -- video_speaker/carousel/static/... (из topic.ad_format)
    segment         TEXT NOT NULL DEFAULT 'общий', -- PRODA/PRODB/общий
    source          TEXT NOT NULL DEFAULT 'coverage', -- coverage/teardown/manual — откуда пришла тема
    card_name       TEXT NOT NULL DEFAULT '',    -- имя карточки Trello (для читаемости отчёта)

    -- Связка с фактом
    ad_ids          TEXT NOT NULL DEFAULT '[]',  -- JSON-массив ad_id запущенных объявлений
    reference_ad_id TEXT,                        -- ad_id референса-победителя (только для teardown)

    -- Ожидание (измеримое, из данных на момент запуска)
    expectation_json TEXT NOT NULL DEFAULT '{}', -- {"metric": "...", "op": "<=|>=", "threshold": num, "basis": "..."}

    -- Вердикт
    status          TEXT NOT NULL DEFAULT 'open', -- open/confirmed/refuted/inconclusive
    verdict_at      TEXT,                          -- когда вынесен вердикт (ISO)
    lesson          TEXT,                          -- человекочитаемый урок (копия statement в learnings)
    facts_json      TEXT,                          -- {"payments": n, "qual_pct": f, "cpl": f, "spend": f, "leads": n, "days": n} на момент вердикта
    learning_id     INTEGER,                       -- FK learnings.id (без констрейнта)

    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Открытые гипотезы по возрасту (крон вердикта)
CREATE INDEX IF NOT EXISTS idx_hyp_status_created ON hypotheses(status, created_at);
-- Комбо угол×город×формат для влияния на topic_selector
CREATE INDEX IF NOT EXISTS idx_hyp_combo ON hypotheses(angle, city, ad_format);
-- Вердикты по дате (недельный отчёт)
CREATE INDEX IF NOT EXISTS idx_hyp_verdict_at ON hypotheses(verdict_at);
