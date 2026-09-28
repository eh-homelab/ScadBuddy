-- #297: Claude plugin PACKAGES (skills, subagents, hooks, .mcp.json) installed
-- from a git URL or a marketplace entry at a pinned commit. Postgres holds
-- the pin; the files on disk are only a cache, re-fetched from the pin and
-- verified against content_hash before every load. See src/plugins/packages/.
CREATE TABLE ai_plugin_packages (
  -- The plugin's name: its manifest `name`, which namespaces its skills
  -- (/<name>:<skill>). Same alphabet as ai_plugins.name.
  name              text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9-]{0,30}[a-z0-9]$' AND name !~ '--'),
  -- Where it comes from: a git repository, or an entry of a marketplace
  -- (a git repository with .claude-plugin/marketplace.json).
  source_kind       text NOT NULL CHECK (source_kind IN ('git', 'marketplace')),
  -- The repository the user named (https; http to loopback only).
  source_url        text NOT NULL,
  -- The ref the user asked for (branch, tag or commit); informational, the
  -- pin below is what is loaded.
  source_ref        text NOT NULL,
  -- The marketplace entry's name, for source_kind 'marketplace'.
  marketplace_entry text,
  -- Where the plugin's files are fetched from: for a git source the same as
  -- source_url; for a marketplace entry, the entry's own repository.
  fetch_url         text NOT NULL,
  -- The plugin's directory inside that repository ('' for the root).
  fetch_path        text NOT NULL DEFAULT '',
  -- The pin: the commit the files come from, and the SHA-256 of the file
  -- list (every path, mode and content hash) under fetch_path at it.
  commit_sha        text NOT NULL CHECK (commit_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
  content_hash      text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
  -- { "<path>": { "sha256": ..., "size": ..., "executable": bool } }, for
  -- the re-pin diff.
  files             jsonb NOT NULL,
  -- What the vetting found and the admin reviewed: manifest, skills, agents,
  -- commands, hooks and MCP servers (src/plugins/packages/vet.ts).
  review            jsonb NOT NULL,
  -- Set when an admin approved exactly this pin in the UI (spec §8.2).
  approved_at       timestamptz,
  enabled           boolean NOT NULL DEFAULT false,
  -- A re-pin waiting for approval: the same columns for the new commit,
  -- including where it is fetched from (a marketplace entry may have moved to
  -- another repository or path since the current pin).
  pending_ref          text,
  pending_fetch_url    text,
  pending_fetch_path   text,
  pending_commit_sha   text CHECK (pending_commit_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
  pending_content_hash text CHECK (pending_content_hash ~ '^sha256:[0-9a-f]{64}$'),
  pending_files        jsonb,
  pending_review       jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  -- Only an approved pin may be enabled.
  CHECK (NOT enabled OR approved_at IS NOT NULL),
  CHECK ((source_kind = 'marketplace') = (marketplace_entry IS NOT NULL)),
  CHECK (jsonb_typeof(files) = 'object'),
  CHECK (jsonb_typeof(review) = 'object'),
  CHECK (
    (pending_commit_sha IS NULL) = (pending_content_hash IS NULL) AND
    (pending_commit_sha IS NULL) = (pending_files IS NULL) AND
    (pending_commit_sha IS NULL) = (pending_review IS NULL) AND
    (pending_commit_sha IS NULL) = (pending_ref IS NULL) AND
    (pending_commit_sha IS NULL) = (pending_fetch_url IS NULL) AND
    (pending_commit_sha IS NULL) = (pending_fetch_path IS NULL)
  )
);
