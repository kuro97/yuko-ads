-- Версия семантики Meta lead actions для изоляции legacy двойного счёта.
-- Существующие строки получают default=1/'legacy' и НЕ считаются v2-данными.
-- Повторный Graph backfill перезапишет их через обычные v2 parser/upsert пути.

ALTER TABLE ad_daily_metrics
ADD COLUMN lead_semantics_version INTEGER NOT NULL DEFAULT 1
CHECK (lead_semantics_version IN (1, 2));

ALTER TABLE ad_daily_metrics
ADD COLUMN lead_parse_status TEXT NOT NULL DEFAULT 'legacy'
CHECK (lead_parse_status IN ('legacy', 'ok', 'component_mismatch', 'invalid'));

ALTER TABLE ad_hourly_metrics
ADD COLUMN lead_semantics_version INTEGER NOT NULL DEFAULT 1
CHECK (lead_semantics_version IN (1, 2));

ALTER TABLE ad_hourly_metrics
ADD COLUMN lead_parse_status TEXT NOT NULL DEFAULT 'legacy'
CHECK (lead_parse_status IN ('legacy', 'ok', 'component_mismatch', 'invalid'));
