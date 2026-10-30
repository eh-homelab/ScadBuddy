-- #4828: drops #2171's tool-access compat. 20261026T2201Z_access_copy.sql made two mirror
-- triggers (ai_plugins and ai_settings) that carried the writes of pre-#4838 replicas into
-- ai_access, and copied the old state in. No such replica is left. This file drops the
-- triggers and their functions, and deletes the four old ai_settings keys: nothing reads
-- them. The migration:2171 rows in ai_access stay as they are (the "raises only" rule keeps
-- applying to them). The ai_plugins columns enabled, tool_tiers and disabled_tools stay in
-- the schema for this release, because a previous-build replica still selects them. They
-- are dropped in the next release (follow-up issue of #4828).
DROP TRIGGER ai_plugins_mirror_access ON ai_plugins;
DROP TRIGGER ai_settings_mirror_access ON ai_settings;
DROP FUNCTION ai_plugins_mirror_access();
DROP FUNCTION ai_settings_mirror_access();
DROP FUNCTION ai_legacy_registry_rows(jsonb, jsonb);
DELETE FROM ai_settings
WHERE key IN ('builtin_tools.scadbuddy', 'printer_camera_enabled', 'http_request_enabled', 'scadbuddy_plugin_enabled');
