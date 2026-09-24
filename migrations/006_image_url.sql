-- migrations/006_image_url.sql
-- Sprint 2.1: Колонка image_url для creative_kb — хранит HD превью из FB API
-- Применяется идемпотентно через _apply_image_url_migration() в creative_intelligence.py

ALTER TABLE creative_kb ADD COLUMN image_url TEXT;

CREATE INDEX IF NOT EXISTS idx_kb_has_image ON creative_kb(image_url) WHERE image_url IS NOT NULL;
