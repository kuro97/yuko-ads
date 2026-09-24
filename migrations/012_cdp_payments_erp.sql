-- migrations/012_cdp_payments_erp.sql
-- Шаг B: реальные платежи ERP из CDP как ground truth оплат по объявлению.
-- Отдельные колонки от AMO-полей (payments/revenue): ERP — независимый источник,
-- объединение источников делает services.cdp_payments.payments_effective (max).
-- NULL = «нет данных ERP по этому объявлению» (не сверено / нет платежей в окне),
-- НЕ «точно 0 оплат» — семантика как у AMO payments (миграция 010).
-- Идемпотентно: ADD COLUMN на SQLite падает 'duplicate column' при повторе —
-- apply-функция ловит и пропускает (см. _apply_cdp_payments_erp_migration).

ALTER TABLE creative_kb ADD COLUMN payments_erp INTEGER;
ALTER TABLE creative_kb ADD COLUMN revenue_erp_lcy REAL;
ALTER TABLE creative_kb ADD COLUMN payments_erp_synced_at TEXT;
