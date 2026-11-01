-- #5014: drops the ai_plugins columns that #4828 left behind. Since #4828 the agent reads and
-- writes a remote plugin's enabled flag, tool tiers and disabled tools only in ai_access
-- (20261026T2201Z_access_copy.sql, 20261030T1020Z_access_drop_mirrors.sql). The previous build
-- never names these columns, so the drop is safe in a rolling deploy. The DELETE repeats the
-- one in 20261030T1020Z_access_drop_mirrors.sql, for the keys that replicas of the builds
-- before #4828 wrote after it ran.
ALTER TABLE ai_plugins DROP COLUMN enabled, DROP COLUMN tool_tiers, DROP COLUMN disabled_tools;
DELETE FROM ai_settings
WHERE key IN ('builtin_tools.scadbuddy', 'printer_camera_enabled', 'http_request_enabled', 'scadbuddy_plugin_enabled');
