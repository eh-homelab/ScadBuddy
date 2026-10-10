-- #601: the settings a render worker may read (spec 2026-09-27 §9: render workers do
-- not hold the API's settings store). The worker's own role is granted SELECT on this
-- view and nothing on `settings` (`render/worker_role.py`), so it reads the store's
-- settings and never the rest. The full Bambuddy key is in it only while no render
-- key is stored: that is the fallback the store already takes (and reports as
-- `render_key_fallback`); once a render key is saved, the worker cannot read the full
-- key at all. security_barrier: the filter runs before anything a caller adds.
CREATE VIEW render_settings WITH (security_barrier) AS
SELECT name, value
FROM settings
WHERE name IN ('store_backend', 'bambuddy_url', 'bambuddy_render_api_key', 'library_folder_id')
   OR (
       name = 'bambuddy_api_key'
       AND NOT EXISTS (
           SELECT 1
           FROM settings AS render
           WHERE render.name = 'bambuddy_render_api_key'
             AND render.value NOT IN ('null'::jsonb, '""'::jsonb)
       )
   );
