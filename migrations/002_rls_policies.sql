-- Включаем RLS на всех таблицах
ALTER TABLE decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE launch_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE amo_data ENABLE ROW LEVEL SECURITY;
ALTER TABLE competitors ENABLE ROW LEVEL SECURITY;
ALTER TABLE learner_cache ENABLE ROW LEVEL SECURITY;

-- Политики: пользователь видит только свои данные
-- auth.uid() = UUID из Supabase Auth JWT

-- decisions
CREATE POLICY "decisions_select" ON decisions FOR SELECT
    USING (tenant_id = auth.uid());
CREATE POLICY "decisions_insert" ON decisions FOR INSERT
    WITH CHECK (tenant_id = auth.uid());
CREATE POLICY "decisions_update" ON decisions FOR UPDATE
    USING (tenant_id = auth.uid());
CREATE POLICY "decisions_delete" ON decisions FOR DELETE
    USING (tenant_id = auth.uid());

-- launch_history
CREATE POLICY "launch_history_select" ON launch_history FOR SELECT
    USING (tenant_id = auth.uid());
CREATE POLICY "launch_history_insert" ON launch_history FOR INSERT
    WITH CHECK (tenant_id = auth.uid());

-- amo_data
CREATE POLICY "amo_data_select" ON amo_data FOR SELECT
    USING (tenant_id = auth.uid());
CREATE POLICY "amo_data_insert" ON amo_data FOR INSERT
    WITH CHECK (tenant_id = auth.uid());
CREATE POLICY "amo_data_update" ON amo_data FOR UPDATE
    USING (tenant_id = auth.uid());

-- competitors
CREATE POLICY "competitors_select" ON competitors FOR SELECT
    USING (tenant_id = auth.uid());
CREATE POLICY "competitors_insert" ON competitors FOR INSERT
    WITH CHECK (tenant_id = auth.uid());
CREATE POLICY "competitors_delete" ON competitors FOR DELETE
    USING (tenant_id = auth.uid());

-- learner_cache
CREATE POLICY "learner_cache_select" ON learner_cache FOR SELECT
    USING (tenant_id = auth.uid());
CREATE POLICY "learner_cache_insert" ON learner_cache FOR INSERT
    WITH CHECK (tenant_id = auth.uid());
CREATE POLICY "learner_cache_update" ON learner_cache FOR UPDATE
    USING (tenant_id = auth.uid());
