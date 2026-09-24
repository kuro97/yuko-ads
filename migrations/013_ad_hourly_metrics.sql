-- migrations/013_ad_hourly_metrics.sql
-- Почасовые метрики первых ~48ч жизни объявления — датасет для «Раннего прогноза» v3.
-- Копим сами, т.к. FB отдаёт hourly-разбивку задним числом только ~3 недели
-- (research-v2 §"Почасовой пробник"). Сборщик: services/hourly_collector.py.
-- Применяется ИДЕМПОТЕНТНО через _apply_hourly_metrics_migration() в creative_intelligence.py:
--   CREATE TABLE / CREATE INDEX IF NOT EXISTS — безопасны при повторном запуске.
-- ВНИМАНИЕ: старый SQLite (<3.35) — НЕТ GENERATED-колонок, НЕТ DROP COLUMN.
-- Резюмируемость держится на UNIQUE(ad_id, datetime_hour): UPSERT перезаписывает час.
-- datetime_hour — 'YYYY-MM-DDTHH:00:00' по advertiser_time_zone кабинета (Лос-Анджелес),
-- то же ТЗ, в котором FB отдаёт hourly_stats_aggregated_by_advertiser_time_zone.
-- Ретеншн: НЕ чистим — объём копеечный (новые объявления × 48 часовых строк),
-- research потом читает всю историю SQL-запросом.

CREATE TABLE IF NOT EXISTS ad_hourly_metrics (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id          TEXT    NOT NULL,
    datetime_hour  TEXT    NOT NULL,          -- 'YYYY-MM-DDTHH:00:00', advertiser_time_zone
    spend          REAL    NOT NULL DEFAULT 0,
    impressions    INTEGER NOT NULL DEFAULT 0,
    clicks         INTEGER NOT NULL DEFAULT 0,
    actions_lead   INTEGER NOT NULL DEFAULT 0,  -- lead + onsite_conversion.lead_grouped
    video_3s       INTEGER NOT NULL DEFAULT 0,  -- video_view (3-сек просмотры)
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ad_id, datetime_hour)
);

-- Хронология по объявлению (первые часы жизни)
CREATE INDEX IF NOT EXISTS idx_ahm_ad_id ON ad_hourly_metrics(ad_id);
-- Выборки по часу (что снято за конкретный час/день)
CREATE INDEX IF NOT EXISTS idx_ahm_hour  ON ad_hourly_metrics(datetime_hour);
