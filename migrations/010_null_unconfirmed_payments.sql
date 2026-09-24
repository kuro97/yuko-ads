-- migrations/010_null_unconfirmed_payments.sql
-- B1: «нет данных из AMO» ≠ «0 оплат».
-- Раньше creative_kb.payments имел DEFAULT 0, и несверённые с AMO строки
-- выглядели как «точно 0 оплат» → автопилот мог запаузить прибыльную рекламу
-- с потерянной связкой CRM. Признак «сверка НЕ проводилась» = outcomes_matched_at IS NULL.
-- Обнуляем фейковые нули: payments 0 при неотматченных исходах → NULL (нет данных).
-- ИДЕМПОТЕНТНО: повторный запуск не находит строк (после первого они уже NULL).
-- ВНИМАНИЕ: в SQLite НЕТ ALTER COLUMN / DROP DEFAULT. Колонка
-- сохраняет DEFAULT 0 на уровне схемы. «Нет данных» гарантируется на уровне ЧТЕНИЯ
-- через outcomes_matched_at IS NULL (правило decision_policy) и на уровне ЗАПИСИ
-- (sync/backfill пишут NULL для несверённых) — см. код B1.
--
-- ASSESS перед UPDATE на боевой базе (DATA MUTATION PROTOCOL, обязательно перед применением):
--   SELECT COUNT(*) AS to_null
--   FROM creative_kb
--   WHERE payments = 0 AND outcomes_matched_at IS NULL
-- Ожидается часть строк таблицы. Если число резко иное (например 0 или все
-- строки таблицы) — стоп, показать координатору перед применением.
-- Это UPDATE (не DELETE), обратим восстановлением из ежедневного бэкапа
-- data/backups/decisions-*.db.

UPDATE creative_kb
SET payments = NULL
WHERE payments = 0
  AND outcomes_matched_at IS NULL;

-- Симметрично для qual_leads / revenue (те же DEFAULT 0, та же семантика «нет данных»).
UPDATE creative_kb
SET qual_leads = NULL
WHERE qual_leads = 0
  AND outcomes_matched_at IS NULL;

UPDATE creative_kb
SET revenue = NULL
WHERE revenue = 0
  AND outcomes_matched_at IS NULL;
