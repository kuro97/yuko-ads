-- Страж покрытия видит PRODB-адсеты: язык группы PRODB в coverage_snapshot_groups.
--
-- Зачем. В карту роутинга (services/launch_routing.py) добавлен
-- тип PRODB — по PRODB-адсету на город («Owner | PRODB | MQL | Geo <Город> | ver1») в
-- кабинете cabinet_b, а страж покрытия (services/coverage_monitor.TRACKED_GROUPS)
-- берёт их под наблюдение рядом с PRODA-парами L2/L1. Миграция 022 заводила
-- coverage_snapshot_groups с CHECK(language IN ('L2','L1')): первый же снимок с
-- группой PRODB падал бы на INSERT, и страж не рапортовал бы ничего — ни по PRODB,
-- ни по PRODA (снимок пишется одной транзакцией).
--
-- Что меняется. Только CHECK колонки language: 'PRODB' добавлен к 'L2','L1'.
-- Колонки, их порядок, типы, PRIMARY KEY, UNIQUE, FK, индекс и триггеры
-- иммутабельности — дословно из 022 (PRAGMA table_info не меняется, манифест
-- TABLE_INFO_SHA256 в services/database_migrations.py прежний).
--
-- ВНИМАНИЕ: старый SQLite (<3.35) — CHECK нельзя изменить ALTER'ом, НЕТ DROP
-- COLUMN, а ALTER TABLE RENAME на 3.25+ переразбирает схему и падает на
-- триггерах. Поэтому таблица пересобирается тем же приёмом, что в 026:
-- нейтральная копия → DROP → CREATE под тем же именем → возврат данных.
-- Внешние ключи на время пересборки откладываются до COMMIT
-- (defer_foreign_keys): к коммиту строки на месте, PRAGMA foreign_key_check в
-- runner'е проверяет уже восстановленную схему. Идемпотентность версии
-- обеспечивает runner: миграция применяется ровно один раз.

PRAGMA defer_foreign_keys = ON;

CREATE TABLE _mig028_coverage_groups_backup AS
SELECT * FROM coverage_snapshot_groups;

DROP TRIGGER IF EXISTS trg_coverage_groups_no_update;
DROP TRIGGER IF EXISTS trg_coverage_groups_no_delete;
DROP INDEX IF EXISTS idx_coverage_group_status;

DROP TABLE coverage_snapshot_groups;

CREATE TABLE coverage_snapshot_groups (
    snapshot_id TEXT NOT NULL,
    group_key TEXT NOT NULL,
    account_id TEXT NOT NULL,
    city TEXT NOT NULL,
    language TEXT NOT NULL CHECK(language IN ('L2','L1','PRODB')),
    adset_id TEXT NOT NULL,
    min_active INTEGER NOT NULL CHECK(min_active > 0),
    effective_active_count INTEGER,
    configured_active_count INTEGER,
    status TEXT NOT NULL CHECK(status IN ('ZERO','THIN','OK','UNKNOWN')),
    inventory_sha256 TEXT NOT NULL CHECK(length(inventory_sha256) = 64),
    PRIMARY KEY(snapshot_id, group_key),
    UNIQUE(snapshot_id, account_id, adset_id),
    FOREIGN KEY(snapshot_id) REFERENCES coverage_snapshots(snapshot_id)
        ON DELETE RESTRICT,
    CHECK(
        (status = 'UNKNOWN' AND effective_active_count IS NULL)
        OR (status = 'ZERO' AND effective_active_count = 0)
        OR (status = 'THIN' AND effective_active_count > 0
            AND effective_active_count < min_active)
        OR (status = 'OK' AND effective_active_count >= min_active)
    )
);

INSERT INTO coverage_snapshot_groups (
    snapshot_id, group_key, account_id, city, language, adset_id, min_active,
    effective_active_count, configured_active_count, status, inventory_sha256
)
SELECT snapshot_id, group_key, account_id, city, language, adset_id, min_active,
       effective_active_count, configured_active_count, status, inventory_sha256
FROM _mig028_coverage_groups_backup;

DROP TABLE _mig028_coverage_groups_backup;

CREATE INDEX IF NOT EXISTS idx_coverage_group_status
ON coverage_snapshot_groups(group_key, status);

-- История снимков иммутабельна — триггеры возвращаются дословно из 022.
CREATE TRIGGER IF NOT EXISTS trg_coverage_groups_no_update
BEFORE UPDATE ON coverage_snapshot_groups BEGIN
    SELECT RAISE(ABORT, 'coverage_snapshot_groups are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_coverage_groups_no_delete
BEFORE DELETE ON coverage_snapshot_groups BEGIN
    SELECT RAISE(ABORT, 'coverage_snapshot_groups cannot be deleted');
END;
