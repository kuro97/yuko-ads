-- migrations/007_ad_agent_v2.sql
-- AD Agent V2: таксономия, эксперименты, черновики, логирование LLM

-- ======================================================
-- Таблица 1: hook_types — типы хуков
-- ======================================================
CREATE TABLE IF NOT EXISTS hook_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name_ru TEXT NOT NULL,
    name_l2 TEXT NOT NULL DEFAULT '',  -- название на втором языке (L2)
    description TEXT NOT NULL DEFAULT '',
    example TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Seed данные для hook_types (шаблон — замените примерами своей компании).
-- name_l2 (название на втором языке) заполняется при необходимости отдельно.
INSERT OR IGNORE INTO hook_types (slug, name_ru, description, example) VALUES
    ('pain_point', 'Бытовая боль', 'Называем рутинную проблему, с которой клиент сталкивается каждую неделю', 'Снова потратили выходные на то, что можно было поручить специалисту?'),
    ('social_proof', 'Социальное доказательство', 'Другие уже выбрали сервис и довольны', 'Наши клиенты уже пользуются «Продуктом A» — присоединяйтесь'),
    ('price_anchor', 'Якорь цены', 'Сравниваем цену с привычной ежедневной тратой клиента', 'В день это дешевле, чем обед в кафе'),
    ('fear_left_behind', 'Страх упустить', 'Клиент боится упустить выгодные условия, пока они действуют', 'Цена фиксируется только при подключении в этом сезоне'),
    ('behind_the_scenes', 'Закулисье', 'Показываем, как сервис работает изнутри', 'Показываем, что происходит с заявкой после кнопки «Отправить»'),
    ('before_after', 'До и после', 'Контраст: как было у клиента до сервиса и как стало', 'Было: пять звонков, чтобы всё согласовать. Стало: один менеджер и один чат'),
    ('testimonial', 'Отзыв/история', 'Реальная история клиента его словами', 'Клиент Сергей: "Наконец-то не нужно всё контролировать самому"'),
    ('question', 'Вопрос', 'Провокационный или риторический вопрос', 'Сколько часов в месяц у вас уходит на то, что можно передать?'),
    ('myth_busting', 'Разрушение мифа', 'Опровергаем расхожее заблуждение о категории услуг', '«Это дорого и долго» — разбираем, почему это не так');

-- ======================================================
-- Таблица 2: angles — углы подачи
-- ======================================================
CREATE TABLE IF NOT EXISTS angles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name_ru TEXT NOT NULL,
    name_l2 TEXT NOT NULL DEFAULT '',  -- название на втором языке (L2)
    description TEXT NOT NULL DEFAULT '',
    target_emotion TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO angles (slug, name_ru, description, target_emotion) VALUES
    ('time_saving', 'Экономия времени', 'Сервис освобождает часы, которые клиент тратит на рутину', 'облегчение'),
    ('convenience', 'Удобство', 'Всё в одном месте — без лишних звонков и поездок', 'спокойствие'),
    ('value_for_money', 'Выгода', 'Больше пользы за те же деньги', 'рациональность'),
    ('family_pride', 'Забота о близких', 'Покупка как подарок или забота о близких', 'теплота'),
    ('status_upgrade', 'Новый уровень сервиса', '«Продукт B» — для тех, кому уже мало «Продукта A»', 'амбиция'),
    ('risk_free', 'Без риска', 'Пробный период и гарантия снимают страх ошибиться с выбором', 'уверенность'),
    ('curiosity', 'Любопытство', 'Интрига: как устроен сервис и почему он работает', 'интерес'),
    ('financial_relief', 'Финансовое облегчение', 'Рассрочка, скидка, бесплатная консультация', 'облегчение');

-- ======================================================
-- Таблица 3: offer_types — типы офферов
-- ======================================================
CREATE TABLE IF NOT EXISTS offer_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name_ru TEXT NOT NULL,
    name_l2 TEXT NOT NULL DEFAULT '',  -- название на втором языке (L2)
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO offer_types (slug, name_ru, description) VALUES
    ('free_trial', 'Пробный период', 'Бесплатный пробный период для знакомства с сервисом'),
    ('discount', 'Скидка', 'Процентная или фиксированная скидка'),
    ('money_back_guarantee', 'Гарантия возврата', 'Если не понравится — вернём деньги'),
    ('installment', 'Рассрочка', 'Оплата частями без переплаты'),
    ('bundle', 'Пакет услуг', 'Несколько услуг в одном пакете дешевле, чем по отдельности'),
    ('loyalty_bonus', 'Бонус за продление', 'Скидка или бонус при продлении договора'),
    ('referral_bonus', 'Бонус за рекомендацию', 'Бонус клиенту, который привёл друга'),
    ('consultation', 'Бесплатная консультация', 'Консультация с экспертом / менеджером'),
    ('free_setup', 'Бесплатное подключение', 'Подключение и настройка без доплаты'),
    ('seasonal_sale', 'Сезонная акция', 'Специальная цена на ограниченный период');

-- ======================================================
-- Таблица 4: experiments — эксперименты
-- ======================================================
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    ice_impact INTEGER NOT NULL DEFAULT 5,     -- 1-10
    ice_confidence INTEGER NOT NULL DEFAULT 5,  -- 1-10
    ice_ease INTEGER NOT NULL DEFAULT 5,        -- 1-10
    ice_score REAL NOT NULL DEFAULT 0,  -- вычисляется в коде при вставке (старый SQLite (<3.31) не поддерживает GENERATED ALWAYS AS)
    status TEXT NOT NULL DEFAULT 'draft',  -- draft/running/completed/cancelled
    outcome TEXT,                            -- win/loss/inconclusive
    learnings_text TEXT,                    -- что узнали
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);
CREATE INDEX IF NOT EXISTS idx_experiments_ice ON experiments(ice_score);

-- ======================================================
-- Таблица 5: learnings — выводы из KB
-- ======================================================
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement TEXT NOT NULL,
    evidence_ad_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array of ad_id strings
    confidence TEXT NOT NULL DEFAULT 'hypothesis',  -- hypothesis/probable/confirmed
    source TEXT NOT NULL DEFAULT 'manual',  -- manual/experiment/llm
    experiment_id INTEGER,
    tags TEXT NOT NULL DEFAULT '',  -- comma-separated tags
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (experiment_id) REFERENCES experiments(id)
);

CREATE INDEX IF NOT EXISTS idx_learnings_confidence ON learnings(confidence);

-- ======================================================
-- Таблица 6: prompt_versions — версии промптов
-- (ПЕРЕД ad_drafts — т.к. ad_drafts ссылается на prompt_versions)
-- ======================================================
CREATE TABLE IF NOT EXISTS prompt_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                 -- 'copywriter_v2_system'
    content TEXT NOT NULL,              -- полный текст промпта
    model TEXT NOT NULL DEFAULT '',     -- для какой модели
    is_active INTEGER NOT NULL DEFAULT 1,  -- 0/1
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_prompt_versions_name ON prompt_versions(name);

-- ======================================================
-- Таблица 7: llm_calls — логирование вызовов LLM
-- (ПЕРЕД ad_drafts — т.к. ad_drafts ссылается на llm_calls)
-- ======================================================
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',     -- 'generate_ad_batch' / 'analyze_pair' / etc.
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    prompt_version_id INTEGER,
    error TEXT,                           -- если вызов упал
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (prompt_version_id) REFERENCES prompt_versions(id)
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_calls_model ON llm_calls(model);
CREATE INDEX IF NOT EXISTS idx_llm_calls_purpose ON llm_calls(purpose);

-- ======================================================
-- Таблица 8: ad_drafts — черновики от копирайтера
-- ======================================================
CREATE TABLE IF NOT EXISTS ad_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Structured output поля
    hook TEXT NOT NULL,
    body TEXT NOT NULL,
    cta TEXT NOT NULL,
    angle TEXT NOT NULL DEFAULT '',
    format TEXT NOT NULL DEFAULT '',           -- short_post/long_post/story/carousel
    target_persona TEXT NOT NULL DEFAULT '',   -- client_citya_l1 / client_cityc_l2 / etc.
    primary_language TEXT NOT NULL DEFAULT 'l1',  -- l1/l2
    rationale TEXT NOT NULL DEFAULT '',

    -- Контекст генерации
    brief_json TEXT NOT NULL DEFAULT '{}',      -- JSON: полный brief запроса
    model TEXT NOT NULL DEFAULT '',             -- claude-sonnet-4-20250514
    prompt_version_id INTEGER,
    llm_call_id INTEGER,

    -- Workflow
    status TEXT NOT NULL DEFAULT 'pending_review',  -- pending_review/approved/rejected
    approved_by TEXT,
    feedback TEXT,                                -- feedback при reject
    approved_at TEXT,
    rejected_at TEXT,

    -- Meta
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (prompt_version_id) REFERENCES prompt_versions(id),
    FOREIGN KEY (llm_call_id) REFERENCES llm_calls(id)
);

CREATE INDEX IF NOT EXISTS idx_ad_drafts_status ON ad_drafts(status);
CREATE INDEX IF NOT EXISTS idx_ad_drafts_created ON ad_drafts(created_at);

-- ======================================================
-- Расширение creative_kb: новые колонки
-- ======================================================
-- SQLite не поддерживает IF NOT EXISTS для ALTER TABLE,
-- поэтому эти ALTER вызываются через Python с обработкой duplicate column
ALTER TABLE creative_kb ADD COLUMN hook_type TEXT;
ALTER TABLE creative_kb ADD COLUMN angle TEXT;
ALTER TABLE creative_kb ADD COLUMN offer_type TEXT;
ALTER TABLE creative_kb ADD COLUMN experiment_id INTEGER;
ALTER TABLE creative_kb ADD COLUMN target_product TEXT;         -- продуктовая линия: PRODA/PRODB/...
ALTER TABLE creative_kb ADD COLUMN primary_language TEXT;     -- l1/l2

CREATE INDEX IF NOT EXISTS idx_kb_hook_type ON creative_kb(hook_type);
CREATE INDEX IF NOT EXISTS idx_kb_angle ON creative_kb(angle);
CREATE INDEX IF NOT EXISTS idx_kb_target_product ON creative_kb(target_product);
CREATE INDEX IF NOT EXISTS idx_kb_primary_language ON creative_kb(primary_language);
