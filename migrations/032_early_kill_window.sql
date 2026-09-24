-- migrations/032_early_kill_window.sql
-- Волна 2c раннего стопа: окно 14 дней в кэше состояния (правило C «квалы дорогие или выродились»).
-- Применяется идемпотентно через _apply_early_kill_migration()
-- (повторный ALTER даёт «duplicate column», применялка это глотает). Точку с запятой в комментариях не писать.

ALTER TABLE early_kill_ad_state ADD COLUMN amo_leads_14d INTEGER;
ALTER TABLE early_kill_ad_state ADD COLUMN amo_mature_14d INTEGER;
ALTER TABLE early_kill_ad_state ADD COLUMN amo_quals_14d INTEGER;
ALTER TABLE early_kill_ad_state ADD COLUMN window_at TEXT;
