-- Волна 1 недельных когорт: данные для суждения о траектории объявления.
--
-- Зачем: бот судит рекламу по среднему за период и не видит динамику.
-- «ROMI за месяц 500%» одинаково выглядит у выдыхающейся звезды (первая
-- неделя 1500%, последняя 50%) и у разгоняющейся (было 50%, стало 200%).
-- Недельная когорта хранит срез (объявление, неделя) отдельной строкой,
-- поэтому тренд считается по строкам, а не по усреднению.
--
-- Границы волны: только данные. Правил тренда и встраивания в решения здесь
-- нет. Выручки и ROMI тоже нет — оплата привязана к дате документа, а не к
-- дате лида, поэтому недельный ROMI был бы тем же самым усреднением.
--
-- Полнота — fail-closed. Метрики nullable намеренно: отсутствующие данные
-- записываются как NULL, никогда как 0 (та же дисциплина, что у правила
-- NULL_COERCED_TO_ZERO в services/approval_rules.py). Сравнивать между собой
-- можно только строки с comparable=1; у остальных обязана быть причина.
--
-- ВНИМАНИЕ: старый SQLite (<3.35) — НЕТ GENERATED-колонок, НЕТ DROP COLUMN.
-- Миграция идемпотентна: CREATE TABLE/INDEX IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS ad_weekly_cohorts (
    ad_id                 TEXT NOT NULL,
    -- Понедельник ISO-недели, 'YYYY-MM-DD'. Неделя лида считается по дате
    -- создания лида (CityA), расход — по тем же календарным границам в
    -- таймзоне кабинета (Лос-Анджелес).
    week_start            TEXT NOT NULL,
    -- Привязка объявления к адсету фиксируется НА ПАРУ (объявление, неделя):
    -- объявление может переезжать между адсетами, история не переписывается.
    adset_id              TEXT,
    adset_name            TEXT,
    ad_name               TEXT,
    city                  TEXT,
    -- Метрики nullable: NULL = данных за неделю нет, а не ноль.
    spend_usd             REAL,
    impressions           INTEGER,
    fb_leads              INTEGER,
    amo_leads             INTEGER,
    quals                 INTEGER,
    -- Полнота недели: сколько дней реально получено против скольких ожидалось.
    days_covered          INTEGER NOT NULL,
    days_expected         INTEGER NOT NULL,
    comparable            INTEGER NOT NULL,
    not_comparable_reason TEXT,
    builder_version       INTEGER NOT NULL,
    computed_at           TEXT NOT NULL,
    CHECK(comparable IN (0, 1)),
    CHECK(days_covered >= 0),
    CHECK(days_expected >= 1 AND days_expected <= 7),
    CHECK(days_covered <= days_expected),
    -- Сравнимая строка обязана быть полной и без причины несравнимости.
    CHECK(comparable = 0 OR (
        days_covered = days_expected
        AND days_expected = 7
        AND not_comparable_reason IS NULL
        AND spend_usd IS NOT NULL
        AND impressions IS NOT NULL
        AND fb_leads IS NOT NULL
        AND amo_leads IS NOT NULL
        AND quals IS NOT NULL
    )),
    -- Несравнимая строка обязана назвать причину.
    CHECK(comparable = 1 OR not_comparable_reason IS NOT NULL),
    CHECK(builder_version >= 1)
);

-- Идентичность строки — пара (объявление, неделя). Именованный уникальный
-- индекс вместо inline UNIQUE: он проверяется манифестом схемы и служит
-- конфликт-таргетом для UPSERT при пересчёте свежих недель.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ad_weekly_cohort
ON ad_weekly_cohorts(ad_id, week_start);

-- Срез «все объявления за неделю» — основная выборка для сравнения когорт.
CREATE INDEX IF NOT EXISTS idx_ad_weekly_cohorts_week
ON ad_weekly_cohorts(week_start);

-- Срез по адсету — траектория группы объявлений.
CREATE INDEX IF NOT EXISTS idx_ad_weekly_cohorts_adset
ON ad_weekly_cohorts(adset_id, week_start);
