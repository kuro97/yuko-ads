-- Рекламные объявления конкурентов (данные из Facebook Ad Library)
CREATE TABLE IF NOT EXISTS competitor_ads (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           UUID NOT NULL,
    page_id             TEXT NOT NULL,
    ad_id               TEXT NOT NULL,
    page_name           TEXT,
    title               TEXT,
    body                TEXT,
    started             TEXT,
    stopped             TEXT,
    snapshot_url        TEXT,
    spend_lower         INTEGER,
    spend_upper         INTEGER,
    impressions_lower   INTEGER,
    impressions_upper   INTEGER,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(tenant_id, ad_id)
);

CREATE INDEX IF NOT EXISTS idx_competitor_ads_tenant    ON competitor_ads(tenant_id);
CREATE INDEX IF NOT EXISTS idx_competitor_ads_page      ON competitor_ads(tenant_id, page_id);
CREATE INDEX IF NOT EXISTS idx_competitor_ads_first_seen ON competitor_ads(first_seen_at);

-- Архив победителей (эталоны для скоринга креативов)
CREATE TABLE IF NOT EXISTS winner_archive (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID NOT NULL,
    ad_name     TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_winner_archive_tenant ON winner_archive(tenant_id);

-- RLS
ALTER TABLE competitor_ads  ENABLE ROW LEVEL SECURITY;
ALTER TABLE winner_archive  ENABLE ROW LEVEL SECURITY;

-- Политики competitor_ads
CREATE POLICY "competitor_ads_tenant_select" ON competitor_ads FOR SELECT
    USING ((SELECT auth.uid()) = tenant_id);
CREATE POLICY "competitor_ads_tenant_insert" ON competitor_ads FOR INSERT
    WITH CHECK ((SELECT auth.uid()) = tenant_id);
CREATE POLICY "competitor_ads_tenant_update" ON competitor_ads FOR UPDATE
    USING ((SELECT auth.uid()) = tenant_id);
CREATE POLICY "competitor_ads_tenant_delete" ON competitor_ads FOR DELETE
    USING ((SELECT auth.uid()) = tenant_id);

-- Политики winner_archive
CREATE POLICY "winner_archive_tenant_select" ON winner_archive FOR SELECT
    USING ((SELECT auth.uid()) = tenant_id);
CREATE POLICY "winner_archive_tenant_insert" ON winner_archive FOR INSERT
    WITH CHECK ((SELECT auth.uid()) = tenant_id);
CREATE POLICY "winner_archive_tenant_delete" ON winner_archive FOR DELETE
    USING ((SELECT auth.uid()) = tenant_id);
