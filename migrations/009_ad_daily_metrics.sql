-- migrations/009_ad_daily_metrics.sql
-- Каркас дневных срезов метрик объявлений (фундамент для паттернов «день 1-7»).
-- Применяется ИДЕМПОТЕНТНО через _apply_daily_metrics_migration() в creative_intelligence.py:
--   CREATE TABLE / CREATE INDEX IF NOT EXISTS — безопасны при повторном запуске.
-- ВНИМАНИЕ: старый SQLite (<3.35) — НЕТ GENERATED-колонок, НЕТ DROP COLUMN.
-- Резюмируемость снапшота держится на UNIQUE(ad_id, date): UPSERT перезаписывает день.

CREATE TABLE IF NOT EXISTS ad_daily_metrics (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id            TEXT NOT NULL,
    date             TEXT NOT NULL,            -- день метрик, формат 'YYYY-MM-DD'
    spend            REAL    NOT NULL DEFAULT 0,
    impressions      INTEGER NOT NULL DEFAULT 0,
    clicks           INTEGER NOT NULL DEFAULT 0,
    ctr              REAL    NOT NULL DEFAULT 0,   -- %, как отдаёт FB
    leads            INTEGER NOT NULL DEFAULT 0,
    cpl              REAL    NOT NULL DEFAULT 0,   -- spend/leads, 0 если лидов нет
    hook_rate        REAL,                          -- video_3s / impressions * 100, NULL если не видео
    hold_rate        REAL,                          -- video_thruplay / impressions * 100, NULL если не видео
    video_views_3s   INTEGER NOT NULL DEFAULT 0,
    day_since_launch INTEGER NOT NULL DEFAULT 0,    -- (date - created_time) в днях, >= 0
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ad_id, date)
);

-- Индекс для выборок по объявлению (хронология день 1..N)
CREATE INDEX IF NOT EXISTS idx_adm_ad_id ON ad_daily_metrics(ad_id);
-- Индекс для выборок по дате (что снято за конкретный день)
CREATE INDEX IF NOT EXISTS idx_adm_date  ON ad_daily_metrics(date);
-- Индекс для паттернов «день N от запуска»
CREATE INDEX IF NOT EXISTS idx_adm_dsl   ON ad_daily_metrics(day_since_launch);
