-- #2171, spec §4.3: copy the old tool settings into ai_access. Nothing is deleted or
-- altered. Disabled tools go first as 'off', so ON CONFLICT DO NOTHING keeps them over a tier.

-- During the rolling deploy an older replica writes only ai_plugins'
-- enabled, tool_tiers and disabled_tools. This trigger mirrors each such write into
-- ai_access (`remote:<name>`), so a plugin it creates or switches on reads the same here.
-- The new code writes the same values itself, so the mirror is idempotent with it. A
-- delete is not mirrored: the new code forgets the source, and its create forgets first.
-- No NOTIFY: the old code's writes already send the warm eviction.
-- Created before the copy below, so no such write falls between them. Drop with the old
-- columns next release (#4828).
CREATE FUNCTION ai_plugins_mirror_access() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  src text := 'remote:' || NEW.name;
BEGIN
  IF TG_OP = 'INSERT' OR OLD.enabled IS DISTINCT FROM NEW.enabled THEN
    INSERT INTO ai_access (source, item, access, updated_by)
    VALUES (src, '*', CASE WHEN NEW.enabled THEN 'on' ELSE 'off' END, 'migration:2171')
    ON CONFLICT (source, item) DO UPDATE SET access = EXCLUDED.access, updated_by = EXCLUDED.updated_by, updated_at = now()
      WHERE ai_access.access IS DISTINCT FROM EXCLUDED.access;
  END IF;

  IF TG_OP = 'INSERT' OR OLD.tool_tiers IS DISTINCT FROM NEW.tool_tiers OR OLD.disabled_tools IS DISTINCT FROM NEW.disabled_tools THEN
    DELETE FROM ai_access WHERE source = src AND item <> '*';
    -- Disabled tools first, so ON CONFLICT DO NOTHING keeps them over a tier.
    INSERT INTO ai_access (source, item, access, updated_by)
    SELECT src, d, 'off', 'migration:2171'
    FROM unnest(coalesce(NEW.disabled_tools, '{}'::text[])) AS d
    WHERE d <> '' AND d <> '*'
    ON CONFLICT (source, item) DO NOTHING;

    INSERT INTO ai_access (source, item, access, updated_by)
    SELECT src, j.key, j.val #>> '{}', 'migration:2171'
    FROM jsonb_each(CASE WHEN jsonb_typeof(NEW.tool_tiers) = 'object' THEN NEW.tool_tiers ELSE '{}'::jsonb END) AS j(key, val)
    WHERE jsonb_typeof(j.val) = 'string' AND j.val #>> '{}' IN ('read', 'write', 'outward')
      AND j.key <> '' AND j.key <> '*'
    ON CONFLICT (source, item) DO NOTHING;
  END IF;
  RETURN NULL;
END
$$;

CREATE TRIGGER ai_plugins_mirror_access AFTER INSERT OR UPDATE OF enabled, tool_tiers, disabled_tools ON ai_plugins
  FOR EACH ROW EXECUTE FUNCTION ai_plugins_mirror_access();

-- During the rolling deploy an older replica writes only the ai_settings keys
-- builtin_tools.scadbuddy, printer_camera_enabled, http_request_enabled and
-- scadbuddy_plugin_enabled. This trigger mirrors each such write into ai_access, with the
-- mappings of 20261026T2201Z_access_copy.sql and its `updated_by` ('migration:2171', so a
-- mirrored tier counts only where it raises). It mirrors what changed, item by item, so a
-- row the old key does not hold (a lowered tier) is left alone. The new code writes the old
-- key first and then ai_access in the same transaction (access/store.ts), so its own rows
-- win. No NOTIFY: the old code's writes already send the warm eviction.
-- Created before the copy below, as the ai_plugins mirror is. Drop next release, with the
-- new code's dual write (#4828).

-- The registry rows an old builtin_tools value and camera switch stand for: disabled tools
-- (and the camera, when off) as 'off', which wins over a tier; browser tools in builtin:browser.
CREATE FUNCTION ai_legacy_registry_rows(tools jsonb, camera jsonb)
RETURNS TABLE (row_source text, row_item text, row_access text) LANGUAGE sql IMMUTABLE AS $$
  WITH disabled AS (
    SELECT e.v #>> '{}' AS item
    FROM jsonb_array_elements(CASE WHEN jsonb_typeof(tools) = 'object' AND jsonb_typeof(tools -> 'disabled_tools') = 'array'
                                   THEN tools -> 'disabled_tools' ELSE '[]'::jsonb END) AS e(v)
    WHERE jsonb_typeof(e.v) = 'string'
    UNION
    SELECT 'get_printer_camera' WHERE camera = 'false'::jsonb
  ),
  tiers AS (
    SELECT j.key AS item, j.val #>> '{}' AS access
    FROM jsonb_each(CASE WHEN jsonb_typeof(tools) = 'object' AND jsonb_typeof(tools -> 'tool_tiers') = 'object'
                         THEN tools -> 'tool_tiers' ELSE '{}'::jsonb END) AS j(key, val)
    WHERE jsonb_typeof(j.val) = 'string' AND j.val #>> '{}' IN ('read', 'write', 'outward')
  ),
  merged AS (
    SELECT item, 'off' AS access FROM disabled
    UNION ALL
    SELECT t.item, t.access FROM tiers t WHERE NOT EXISTS (SELECT 1 FROM disabled d WHERE d.item = t.item)
  )
  SELECT CASE WHEN item LIKE 'browser\_%' THEN 'builtin:browser' ELSE 'builtin:scadbuddy' END, item, access
  FROM merged WHERE item NOT IN ('*', '')
$$;

CREATE FUNCTION ai_settings_mirror_access() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  old_value jsonb := CASE WHEN TG_OP = 'UPDATE' THEN OLD.value END;
  src text;
  old_tools jsonb;
  new_tools jsonb;
  old_camera jsonb;
  new_camera jsonb;
BEGIN
  IF old_value IS NOT DISTINCT FROM NEW.value THEN
    RETURN NULL;
  END IF;

  IF NEW.key IN ('http_request_enabled', 'scadbuddy_plugin_enabled') THEN
    -- On unless stored false (the old code's rule), as the source's '*' row.
    IF TG_OP = 'INSERT' OR (old_value = 'false'::jsonb) IS DISTINCT FROM (NEW.value = 'false'::jsonb) THEN
      src := CASE NEW.key WHEN 'http_request_enabled' THEN 'builtin:http' ELSE 'builtin:own-plugin' END;
      INSERT INTO ai_access (source, item, access, updated_by)
      VALUES (src, '*', CASE WHEN NEW.value = 'false'::jsonb THEN 'off' ELSE 'on' END, 'migration:2171')
      ON CONFLICT (source, item) DO UPDATE SET access = EXCLUDED.access, updated_by = EXCLUDED.updated_by, updated_at = now()
        WHERE ai_access.access IS DISTINCT FROM EXCLUDED.access;
    END IF;
    RETURN NULL;
  END IF;

  -- The sibling key is read as it stands now. Safe: the new code's dual writes hold one
  -- advisory lock (access/store.ts), and the old code writes one key per INSERT statement
  -- (SettingsStore.set and setMany), so this row's sibling is as earlier statements left it.
  IF NEW.key = 'builtin_tools.scadbuddy' THEN
    old_tools := old_value;
    new_tools := NEW.value;
    SELECT value INTO old_camera FROM ai_settings WHERE key = 'printer_camera_enabled';
    new_camera := old_camera;
  ELSE
    old_camera := old_value;
    new_camera := NEW.value;
    SELECT value INTO old_tools FROM ai_settings WHERE key = 'builtin_tools.scadbuddy';
    new_tools := old_tools;
  END IF;

  -- An item the old value held and the new one does not: its row goes, if it is still that.
  DELETE FROM ai_access a
  USING ai_legacy_registry_rows(old_tools, old_camera) o
  WHERE a.source = o.row_source AND a.item = o.row_item AND a.access = o.row_access
    AND NOT EXISTS (SELECT 1 FROM ai_legacy_registry_rows(new_tools, new_camera) n WHERE n.row_item = o.row_item);

  -- An item the new value holds differently: its row is written.
  INSERT INTO ai_access (source, item, access, updated_by)
  SELECT n.row_source, n.row_item, n.row_access, 'migration:2171'
  FROM ai_legacy_registry_rows(new_tools, new_camera) n
  WHERE NOT EXISTS (
    SELECT 1 FROM ai_legacy_registry_rows(old_tools, old_camera) o WHERE o.row_item = n.row_item AND o.row_access = n.row_access)
  ON CONFLICT (source, item) DO UPDATE SET access = EXCLUDED.access, updated_by = EXCLUDED.updated_by, updated_at = now()
    WHERE ai_access.access IS DISTINCT FROM EXCLUDED.access;
  RETURN NULL;
END
$$;

CREATE TRIGGER ai_settings_mirror_access AFTER INSERT OR UPDATE OF value ON ai_settings
  FOR EACH ROW
  WHEN (NEW.key IN ('builtin_tools.scadbuddy', 'printer_camera_enabled', 'http_request_enabled', 'scadbuddy_plugin_enabled'))
  EXECUTE FUNCTION ai_settings_mirror_access();

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
