-- The Settings page's "Colours rendered at once, per job" joins what a render worker
-- may read: the worker runs the per-colour solids, and its role reads settings only
-- through this view (#601), so without the name a standalone worker sized its pool from
-- its own CPUs whatever was saved. The body is `20261016T1759Z_render_settings_s3.sql`'s
-- with `solid_concurrency` added.
CREATE OR REPLACE VIEW render_settings WITH (security_barrier) AS
SELECT name, value
FROM settings
WHERE name IN (
        'store_backend', 'bambuddy_url', 'bambuddy_render_api_key', 'library_folder_id',
        'store_s3_endpoint', 'store_s3_region', 'store_s3_bucket', 'store_s3_prefix',
        'store_s3_access_key_id', 'store_s3_secret_access_key', 'store_s3_path_style',
        'solid_concurrency'
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
