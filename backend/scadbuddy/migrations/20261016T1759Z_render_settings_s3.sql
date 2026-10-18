-- The S3 store's settings join what a render worker may read (spec 2026-10-16 §4.2):
-- a worker opens the store it is configured for, and its role reads settings only
-- through this view (#601). The body is `20261010T1517Z_render_settings.sql`'s with the
-- seven `store_s3_*` names added; the secret access key is sealed in its row, as the
-- Bambuddy keys are.
CREATE OR REPLACE VIEW render_settings WITH (security_barrier) AS
SELECT name, value
FROM settings
WHERE name IN (
        'store_backend', 'bambuddy_url', 'bambuddy_render_api_key', 'library_folder_id',
        'store_s3_endpoint', 'store_s3_region', 'store_s3_bucket', 'store_s3_prefix',
        'store_s3_access_key_id', 'store_s3_secret_access_key', 'store_s3_path_style'
    )
   OR (
       name = 'bambuddy_api_key'
       AND NOT EXISTS (
           SELECT 1
           FROM settings AS render
           WHERE render.name = 'bambuddy_render_api_key'
             AND render.value NOT IN ('null'::jsonb, '""'::jsonb)
       )
   );
