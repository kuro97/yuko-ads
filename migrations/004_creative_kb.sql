-- migrations/004_creative_kb.sql
-- Creative Knowledge Base — единая таблица со ВСЕМИ данными по креативам

CREATE TABLE IF NOT EXISTS creative_kb (
    ad_id TEXT PRIMARY KEY,
    ad_name TEXT NOT NULL DEFAULT '',
    city TEXT DEFAULT '',
    adset_type TEXT DEFAULT '',  -- L2/L1
    status TEXT DEFAULT '',      -- ACTIVE/PAUSED

    -- FB метрики
    spend REAL DEFAULT 0,
    leads INTEGER DEFAULT 0,
    cpl REAL DEFAULT 0,
    ctr REAL DEFAULT 0,
    cpm REAL DEFAULT 0,
    impressions INTEGER DEFAULT 0,
    clicks INTEGER DEFAULT 0,
    frequency REAL DEFAULT 0,
    hook_rate REAL DEFAULT 0,
    hold_rate REAL DEFAULT 0,
    video_views_3s INTEGER DEFAULT 0,
    thruplay INTEGER DEFAULT 0,
    video_p25 INTEGER DEFAULT 0,
    video_p50 INTEGER DEFAULT 0,
    video_p75 INTEGER DEFAULT 0,
    video_p100 INTEGER DEFAULT 0,

    -- AMO метрики
    qual_pct REAL,
    romi REAL,
    payments INTEGER DEFAULT 0,
    cpql REAL,
    revenue REAL DEFAULT 0,
    qual_leads INTEGER DEFAULT 0,

    -- Классификация
    creative_class TEXT DEFAULT '',       -- Winner/Clickbait/Hidden Gem/Dead
    business_class TEXT DEFAULT '',       -- Прибыльный/Окупается/Убыточный/Перспективный/Низкая квал/Нет данных

    -- Gemini Vision теги (JSON строка)
    vision_tags TEXT,  -- JSON: {first_frame_type, has_person, has_subtitles, emotion, text_overlay, hook_description, summary}

    -- Текст объявления
    ad_body TEXT DEFAULT '',
    ad_headline TEXT DEFAULT '',

    -- Мета
    content_type TEXT DEFAULT 'video',  -- video/image/carousel
    video_duration REAL,                -- секунды
    days_running INTEGER DEFAULT 0,
    created_at TEXT,                     -- дата создания в FB
    synced_at TEXT NOT NULL DEFAULT (datetime('now')),  -- последняя синхронизация

    -- Разбор креатива (последний)
    diagnosis_level TEXT,     -- hook/hold/ctr/cvr/fatigue/healthy
    diagnosis_message TEXT,   -- конкретная рекомендация
    diagnosed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_kb_creative_class ON creative_kb(creative_class);
CREATE INDEX IF NOT EXISTS idx_kb_city ON creative_kb(city);
CREATE INDEX IF NOT EXISTS idx_kb_business_class ON creative_kb(business_class);
CREATE INDEX IF NOT EXISTS idx_kb_synced_at ON creative_kb(synced_at);
