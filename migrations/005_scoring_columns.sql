-- migrations/005_scoring_columns.sql
-- Sprint 2.1: Колонки скоринга 0-100 для creative_kb
-- Применяется идемпотентно через _apply_scoring_migration() в creative_intelligence.py

ALTER TABLE creative_kb ADD COLUMN total_score INTEGER;
ALTER TABLE creative_kb ADD COLUMN visual_score INTEGER;     -- 0-40
ALTER TABLE creative_kb ADD COLUMN text_score INTEGER;       -- 0-20
ALTER TABLE creative_kb ADD COLUMN customer_score INTEGER;     -- 0-25
ALTER TABLE creative_kb ADD COLUMN score_grade TEXT;         -- excellent/good/mediocre/poor
ALTER TABLE creative_kb ADD COLUMN score_breakdown TEXT;     -- JSON с детализацией
ALTER TABLE creative_kb ADD COLUMN scored_at TEXT;           -- timestamp последнего скоринга

CREATE INDEX IF NOT EXISTS idx_kb_total_score ON creative_kb(total_score);
CREATE INDEX IF NOT EXISTS idx_kb_grade ON creative_kb(score_grade);
