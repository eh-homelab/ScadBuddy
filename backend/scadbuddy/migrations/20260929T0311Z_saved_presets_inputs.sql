-- Presets store template inputs (spec 2026-09-27 §4.3, §10). `params` stays and is
-- still written as the inputs' `params`, so a rollback reads every preset.
ALTER TABLE saved_presets ADD COLUMN inputs jsonb NOT NULL DEFAULT '{}'::jsonb;
UPDATE saved_presets SET inputs = jsonb_build_object('params', params, 'v', 0);
