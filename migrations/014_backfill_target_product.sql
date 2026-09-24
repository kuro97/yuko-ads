-- migrations/014_backfill_target_product.sql
-- Продуктовые метки: нормализация старых кодов target_product (продуктовая линия)
-- под канон-реестр services/product_tags.PRODUCTS (product_aliases).
-- ОСНОВНОЙ бэкфилл NULL-значений делает код (services/creative_intelligence.backfill_target_product),
-- т.к. он зависит от Python-классификатора по name/desc + LLM-добора. Здесь —
-- только идемпотентная нормализация уже записанных старых кодов в канон.
-- Безопасно для повторного запуска: WHERE после первого прогона не совпадает.

UPDATE creative_kb SET target_product = 'PRODA'
 WHERE target_product IN ('PRODA', 'PRODC', 'PRODD', 'PRODE');

UPDATE creative_kb SET target_product = 'PRODB'
 WHERE target_product IN ('PRODB');

UPDATE creative_kb SET target_product = 'СТАРТ'
 WHERE target_product IN ('START', 'BASIC');

UPDATE creative_kb SET target_product = 'ОБЩАЯ'
 WHERE target_product IN ('GENERAL', 'COMMON');
