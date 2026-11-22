-- #6932 (spec 2026-11-21 §6.1): hosted plugins, a Claude plugin written in ScadBuddy.
-- The API's /api/v1/hosted-plugins routes write these tables; the agent reads them only
-- through its CRUD routes (backend api/crud_agent_packages.py). File bytes and version zips
-- are blobs of the store, held in `blob_refs` by holder `hosted_plugin` (the plugin's id),
-- never bytea here.

-- The marketplace feeds ScadBuddy serves (Task 2, #6933). One, `default`, exists from here.
CREATE TABLE ai_plugin_feeds (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  -- In the feed's URL.
  slug       text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  -- The marketplace's `name` (kebab-case, as Claude Code takes it).
  name       text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  -- The marketplace's `owner.name`.
  owner      text NOT NULL CHECK (length(owner) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO ai_plugin_feeds (slug, name, owner) VALUES ('default', 'scadbuddy', 'ScadBuddy');

CREATE TABLE ai_hosted_plugins (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  -- The package's name: the alphabet of ai_plugin_packages.name (the route takes only the
  -- kebab names the package vetting allows without an allowance).
  name           text NOT NULL UNIQUE CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  description    text NOT NULL DEFAULT '' CHECK (length(description) <= 1024),
  category       text CHECK (length(category) BETWEEN 1 AND 64),
  tags           text[] NOT NULL DEFAULT '{}' CHECK (cardinality(tags) <= 20),
  -- Null until the first publish.
  latest_version integer CHECK (latest_version >= 1),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- The draft: what the next publish takes.
CREATE TABLE ai_hosted_plugin_files (
  plugin_id  bigint NOT NULL REFERENCES ai_hosted_plugins (id) ON DELETE CASCADE,
  path       text NOT NULL CHECK (length(path) BETWEEN 1 AND 1024),
  -- The bytes' blob (`attachment-<sha256>`).
  sha256     text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  size       integer NOT NULL CHECK (size BETWEEN 0 AND 2097152),
  executable boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (plugin_id, path),
  CHECK (NOT executable OR path LIKE 'skills/%')
);

-- The published versions: never updated, and deleted only with their plugin.
CREATE TABLE ai_hosted_plugin_versions (
  plugin_id    bigint NOT NULL REFERENCES ai_hosted_plugins (id) ON DELETE CASCADE,
  version      integer NOT NULL CHECK (version >= 1),
  -- The zip's blob (`attachment-<sha256>`).
  zip_sha256   text NOT NULL CHECK (zip_sha256 ~ '^[0-9a-f]{64}$'),
  -- The file-list hash of the unpacked zip (agent src/plugins/packages/hash.ts), and the
  -- list itself, as ai_plugin_packages keeps them.
  content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  files        jsonb NOT NULL CHECK (jsonb_typeof(files) = 'object'),
  published_at timestamptz NOT NULL DEFAULT now(),
  published_by text NOT NULL,
  PRIMARY KEY (plugin_id, version)
);

CREATE FUNCTION ai_hosted_plugin_versions_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND NOT EXISTS (SELECT 1 FROM ai_hosted_plugins WHERE id = OLD.plugin_id) THEN
    RETURN OLD;  -- the plugin's own delete, cascading
  END IF;
  RAISE EXCEPTION 'a published hosted plugin version is immutable' USING ERRCODE = 'restrict_violation';
END
$$;
CREATE TRIGGER ai_hosted_plugin_versions_immutable
  BEFORE UPDATE OR DELETE ON ai_hosted_plugin_versions
  FOR EACH ROW EXECUTE FUNCTION ai_hosted_plugin_versions_immutable();
