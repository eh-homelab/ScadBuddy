-- #2171 (#4756): during the rolling deploy an older replica writes only ai_plugins'
-- enabled, tool_tiers and disabled_tools. This trigger mirrors each such write into
-- ai_access (`remote:<name>`), so a plugin it creates or switches on reads the same here.
-- The new code writes the same values itself, so the mirror is idempotent with it. A
-- delete is not mirrored: the new code forgets the source, and its create forgets first.
-- No NOTIFY: the old code's writes already send the warm eviction.
-- Drop with the old columns next release.
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
