-- migrations/015_payments_7d.sql
-- Wave 3A: честные source-specific оплаты РОВНО за 7 календарных дней (TZ CityA).
--
-- Проблема (находка ревью #7): интерфейс/решение писали «оплаты за 7 дней», а
-- фактические поля payments/revenue (AMO) и payments_erp/revenue_erp_lcy (ERP)
-- накапливаются за ДЛИННОЕ окно (сверка AMO ≈2 мес, ERP ≈3 мес). Текст врал.
--
-- Решение: ОТДЕЛЬНЫЕ source-specific колонки строго за 7d — НЕ переопределяют
-- молча исторические lifetime/long-window поля. Имена явно содержат источник и 7d.
--
-- Семантика NULL (как у миграций 010/012): NULL / *_complete=0 = «нет
-- подтверждённых полных данных за 7d-окно», НЕ «точно 0 оплат». Ноль допустим
-- ТОЛЬКО после успешного полного refresh соответствующего окна (тогда известное
-- объявление без оплат получает payments_*_7d=0 и *_complete=1).
--
-- Метаданные окна на СТРОКУ (одинаковы для всех строк одного успешного refresh —
-- окно и synced_at свойства прогона, но хранение на строку даёт per-ad свежесть
-- и гарантирует «нет смеси старых/новых строк» через единую транзакцию refresh):
--   *_window_from — включительная нижняя граница окна (ISO date, CityA)
--   *_window_to   — ИСКЛючительная верхняя граница (ISO date, = сегодня CityA),
--                   окно покрывает [window_from, window_to) = ровно 7 суток
--   *_synced_at   — момент успешной полной транзакции refresh (ISO datetime UTC)
--   *_complete    — 1 только после успешного ПОЛНОГО refresh окна, иначе NULL/0
--
-- Идемпотентно: ADD COLUMN на SQLite падает 'duplicate column' при повторе —
-- apply-функция ловит и пропускает (см. _apply_payments_7d_migration).
-- ВНИМАНИЕ: в SQLite НЕТ ADD COLUMN IF NOT EXISTS.

-- AMO 7d
ALTER TABLE creative_kb ADD COLUMN payments_amo_7d INTEGER;
ALTER TABLE creative_kb ADD COLUMN revenue_amo_7d REAL;
ALTER TABLE creative_kb ADD COLUMN amo_7d_window_from TEXT;
ALTER TABLE creative_kb ADD COLUMN amo_7d_window_to TEXT;
ALTER TABLE creative_kb ADD COLUMN amo_7d_synced_at TEXT;
ALTER TABLE creative_kb ADD COLUMN amo_7d_complete INTEGER;

-- ERP (CDP) 7d
ALTER TABLE creative_kb ADD COLUMN payments_erp_7d INTEGER;
ALTER TABLE creative_kb ADD COLUMN revenue_erp_7d REAL;
ALTER TABLE creative_kb ADD COLUMN erp_7d_window_from TEXT;
ALTER TABLE creative_kb ADD COLUMN erp_7d_window_to TEXT;
ALTER TABLE creative_kb ADD COLUMN erp_7d_synced_at TEXT;
ALTER TABLE creative_kb ADD COLUMN erp_7d_complete INTEGER;
