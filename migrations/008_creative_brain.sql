-- migrations/008_creative_brain.sql
-- Creative Brain: расширение creative_kb для полного кабинета + AMO-исходы + разметка.
-- Применяется ИДЕМПОТЕНТНО через _apply_brain_migration() в creative_intelligence.py:
--   ALTER TABLE ловит 'duplicate column', CREATE ... IF NOT EXISTS безопасен.
-- ВНИМАНИЕ: старый SQLite (<3.35) — НЕТ GENERATED-колонок, НЕТ DROP COLUMN.
-- NB: hook_type/angle/offer_type (TEXT slug) уже добавлены миграцией 007 — здесь только *_id.

-- 1 = объявление из ПОЛНОГО кабинета (бэкфилл), 0/NULL = текущие "наши" (старый sync)
ALTER TABLE creative_kb ADD COLUMN is_full_cabinet INTEGER DEFAULT 0;

-- effective_status из FB (ACTIVE/PAUSED/ARCHIVED/DELETED/ADSET_PAUSED/CAMPAIGN_PAUSED/...)
ALTER TABLE creative_kb ADD COLUMN effective_status TEXT DEFAULT '';

-- Когда последний раз привязали исходы AMO (NULL = ещё не привязывали)
ALTER TABLE creative_kb ADD COLUMN outcomes_matched_at TEXT;

-- Разметка по таксономии (slug-колонки hook_type/angle/offer_type уже есть из 007)
ALTER TABLE creative_kb ADD COLUMN labeled_at TEXT;            -- NULL = не размечено
ALTER TABLE creative_kb ADD COLUMN label_source TEXT;          -- 'gemini' / 'manual'
ALTER TABLE creative_kb ADD COLUMN hook_type_id INTEGER;       -- FK hook_types.id (без констрейнта)
ALTER TABLE creative_kb ADD COLUMN angle_id INTEGER;           -- FK angles.id
ALTER TABLE creative_kb ADD COLUMN offer_type_id INTEGER;      -- FK offer_types.id

-- Индексы для матчинга и срезов
CREATE INDEX IF NOT EXISTS idx_kb_ad_name        ON creative_kb(ad_name);
CREATE INDEX IF NOT EXISTS idx_kb_city_adset     ON creative_kb(city, adset_type);
CREATE INDEX IF NOT EXISTS idx_kb_is_full_cabinet ON creative_kb(is_full_cabinet);
CREATE INDEX IF NOT EXISTS idx_kb_labeled_at     ON creative_kb(labeled_at);
CREATE INDEX IF NOT EXISTS idx_kb_outcomes_at    ON creative_kb(outcomes_matched_at);

-- Состояние резюмируемых джоб (бэкфилл, разметка, майнер).
-- key — имя джобы, value — JSON-строка с курсором/счётчиками.
CREATE TABLE IF NOT EXISTS backfill_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
