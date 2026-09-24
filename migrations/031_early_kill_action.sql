-- migrations/031_early_kill_action.sql
-- Волна 2b раннего стопа: журнал оценок хранит, что сделано с решением PAUSE.
--
-- action: shadow (только наблюдение), approved (предложение создано и самоодобрено),
-- dedup (предложение уже есть), capped (дневной лимит исполнения), excluded (маркер
-- исключения, например intl_line), error:<код> (гейтвей отказал). Решение PAUSE при этом
-- НЕ переписывается — журнал остаётся датасетом калибровки, а лимит и ошибки видны отдельно.
-- proposal_id — ссылка на owner_action_proposals для аудита.
-- Применяется идемпотентно через _apply_early_kill_migration() (повторный ALTER даёт
-- «duplicate column», применялка это глотает). Точку с запятой в комментариях не писать.

ALTER TABLE early_kill_evaluations ADD COLUMN action TEXT;
ALTER TABLE early_kill_evaluations ADD COLUMN proposal_id TEXT;

CREATE INDEX IF NOT EXISTS idx_eke_action ON early_kill_evaluations(action, evaluated_at);
