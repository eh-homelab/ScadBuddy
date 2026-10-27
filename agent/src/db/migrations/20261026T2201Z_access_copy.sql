-- #2171, spec §4.3: copy the old tool settings into ai_access. Nothing is deleted or
-- altered. Disabled tools go first as 'off', so ON CONFLICT DO NOTHING keeps them over a tier.

-- builtin_tools.scadbuddy: disabled tools, then tiers. Browser tools belong to builtin:browser.
INSERT INTO ai_access (source, item, access, updated_by)
SELECT CASE WHEN t.item LIKE 'browser\_%' THEN 'builtin:browser' ELSE 'builtin:scadbuddy' END,
       t.item, 'off', 'migration:2171'
FROM ai_settings s,
     LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(s.value) = 'object' AND jsonb_typeof(s.value -> 'disabled_tools') = 'array'
                                       THEN s.value -> 'disabled_tools' ELSE '[]'::jsonb END) AS e(v),
     LATERAL (SELECT e.v #>> '{}' AS item WHERE jsonb_typeof(e.v) = 'string') t
WHERE s.key = 'builtin_tools.scadbuddy' AND t.item NOT IN ('*', '')
ON CONFLICT (source, item) DO NOTHING;

INSERT INTO ai_access (source, item, access, updated_by)
SELECT CASE WHEN k.key LIKE 'browser\_%' THEN 'builtin:browser' ELSE 'builtin:scadbuddy' END,
       k.key, k.value, 'migration:2171'
FROM ai_settings s,
     LATERAL jsonb_each(CASE WHEN jsonb_typeof(s.value) = 'object' AND jsonb_typeof(s.value -> 'tool_tiers') = 'object'
                             THEN s.value -> 'tool_tiers' ELSE '{}'::jsonb END) AS j(key, val),
     LATERAL (SELECT j.key AS key, j.val #>> '{}' AS value WHERE jsonb_typeof(j.val) = 'string') k
WHERE s.key = 'builtin_tools.scadbuddy' AND k.value IN ('read', 'write', 'outward') AND k.key NOT IN ('*', '')
ON CONFLICT (source, item) DO NOTHING;

-- ai_plugins: the plugin as '*', its disabled tools as 'off', then its tiers.
INSERT INTO ai_access (source, item, access, updated_by)
SELECT 'remote:' || name, '*', CASE WHEN enabled THEN 'on' ELSE 'off' END, 'migration:2171'
FROM ai_plugins
ON CONFLICT (source, item) DO NOTHING;

INSERT INTO ai_access (source, item, access, updated_by)
SELECT 'remote:' || name, d, 'off', 'migration:2171'
FROM ai_plugins, LATERAL unnest(disabled_tools) AS d
WHERE d NOT IN ('*', '')
ON CONFLICT (source, item) DO NOTHING;

INSERT INTO ai_access (source, item, access, updated_by)
SELECT 'remote:' || p.name, j.key, j.val #>> '{}', 'migration:2171'
FROM ai_plugins p, LATERAL jsonb_each(p.tool_tiers) AS j(key, val)
WHERE jsonb_typeof(p.tool_tiers) = 'object' AND jsonb_typeof(j.val) = 'string'
  AND j.val #>> '{}' IN ('read', 'write', 'outward') AND j.key NOT IN ('*', '')
ON CONFLICT (source, item) DO NOTHING;

-- Switches, copied only when stored false. The camera switch wins over a stored tier.
INSERT INTO ai_access (source, item, access, updated_by)
SELECT 'builtin:http', '*', 'off', 'migration:2171' FROM ai_settings
WHERE key = 'http_request_enabled' AND value = 'false'::jsonb
ON CONFLICT (source, item) DO NOTHING;

INSERT INTO ai_access (source, item, access, updated_by)
SELECT 'builtin:scadbuddy', 'get_printer_camera', 'off', 'migration:2171' FROM ai_settings
WHERE key = 'printer_camera_enabled' AND value = 'false'::jsonb
ON CONFLICT (source, item) DO UPDATE SET access = 'off', updated_by = 'migration:2171';

INSERT INTO ai_access (source, item, access, updated_by)
SELECT 'builtin:own-plugin', '*', 'off', 'migration:2171' FROM ai_settings
WHERE key = 'scadbuddy_plugin_enabled' AND value = 'false'::jsonb
ON CONFLICT (source, item) DO NOTHING;
