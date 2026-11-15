-- #2171, spec §4.3: one tool-access model. `ai_access` holds one row per (source, item)
-- an admin set; `ai_tool_listings` caches the tools a remote or package source listed.
-- The next file copies the old settings in; the old keys and columns stay.
CREATE TABLE ai_access (
  source     text NOT NULL CHECK (source ~ '^(builtin|remote|package):.+$'),
  item       text NOT NULL,
  access     text NOT NULL CHECK (access IN ('off', 'on', 'read', 'write', 'outward')),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by text NOT NULL,
  PRIMARY KEY (source, item),
  -- A whole source is on or off, never a tier.
  CHECK (item <> '*' OR access IN ('on', 'off')),
  -- 'on' on a single item exists only for a package's or the own plugin's items.
  CHECK (item = '*' OR access <> 'on' OR source LIKE 'package:%' OR source = 'builtin:own-plugin')
);

CREATE TABLE ai_tool_listings (
  source    text PRIMARY KEY CHECK (source ~ '^(remote|package):.+$'),
  tools     jsonb NOT NULL CHECK (jsonb_typeof(tools) = 'array'),
  listed_at timestamptz NOT NULL DEFAULT now()
);
