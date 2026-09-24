-- Таблица решений (миграция из SQLite decisions.db)
CREATE TABLE IF NOT EXISTS decisions (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL,
    ad_id TEXT NOT NULL,
    ad_name TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    confirmed_by TEXT NOT NULL DEFAULT 'user',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    spend DOUBLE PRECISION,
    leads INTEGER,
    cpl DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_decisions_tenant ON decisions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_decisions_action ON decisions(action);
CREATE INDEX IF NOT EXISTS idx_decisions_created_at ON decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_ad_id ON decisions(ad_id);

-- История запусков (миграция из history.json)
CREATE TABLE IF NOT EXISTS launch_history (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_launch_history_tenant ON launch_history(tenant_id);

-- AMO данные (миграция из amo_data.json)
CREATE TABLE IF NOT EXISTS amo_data (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL,
    ad_id TEXT NOT NULL,
    qual_pct DOUBLE PRECISION,
    romi DOUBLE PRECISION,
    payments INTEGER,
    cpql DOUBLE PRECISION,
    revenue DOUBLE PRECISION,
    qual_leads INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, ad_id)
);

CREATE INDEX IF NOT EXISTS idx_amo_data_tenant ON amo_data(tenant_id);

-- Конкуренты (миграция из competitors.json)
CREATE TABLE IF NOT EXISTS competitors (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL,
    page_id TEXT NOT NULL,
    name TEXT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, page_id)
);

CREATE INDEX IF NOT EXISTS idx_competitors_tenant ON competitors(tenant_id);

-- Кеш результатов Learner (миграция из learner_results.json)
CREATE TABLE IF NOT EXISTS learner_cache (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL UNIQUE,
    data JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
