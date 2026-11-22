-- #6935 (epic #6931, spec 2026-11-21 §6.5 and §6.6): a plugin package may be pinned
-- to bytes or to a hosted version, not only to a git commit.
--
--   pin_kind  'commit'   commit_sha is the pin (a git or marketplace source; every row
--                         before this file)
--             'bytes'    bytes_sha256 is the pin: the sha256 of the archive or npm
--                         tarball ScadBuddy fetched once at install and keeps as a blob
--                         (the agent's CRUD area `package-mirrors`); a load unpacks that
--                         copy, never the upstream URL
--             'version'  version is the pin: a hosted plugin's published version
--                         (#6934 builds on it; only the schema allows it here)
--
-- content_hash stays the hash of the unpacked file list for every kind (hash.ts), and
-- the pending_* columns get the same three. New source kinds:
--   marketplace-url  a bare HTTPS marketplace.json URL plus an entry name
--   archive          a zip (or tar) URL
--   npm              a package from an npm registry, or an https tarball link
--   hosted           a ScadBuddy hosted plugin (#6934)
-- (https://code.claude.com/docs/en/plugins/marketplace-reference.md, "Plugin sources").
ALTER TABLE ai_plugin_packages
  ADD COLUMN pin_kind text NOT NULL DEFAULT 'commit'
    CONSTRAINT ai_plugin_packages_pin_kind_check CHECK (pin_kind IN ('commit', 'bytes', 'version')),
  ADD COLUMN version integer CONSTRAINT ai_plugin_packages_version_check CHECK (version >= 1),
  ADD COLUMN bytes_sha256 text CONSTRAINT ai_plugin_packages_bytes_sha256_check CHECK (bytes_sha256 ~ '^[0-9a-f]{64}$'),
  ADD COLUMN pending_pin_kind text
    CONSTRAINT ai_plugin_packages_pending_pin_kind_check CHECK (pending_pin_kind IN ('commit', 'bytes', 'version')),
  ADD COLUMN pending_version integer CONSTRAINT ai_plugin_packages_pending_version_check CHECK (pending_version >= 1),
  ADD COLUMN pending_bytes_sha256 text
    CONSTRAINT ai_plugin_packages_pending_bytes_sha256_check CHECK (pending_bytes_sha256 ~ '^[0-9a-f]{64}$'),
  ALTER COLUMN commit_sha DROP NOT NULL;

-- Every pending re-pin stored before this file is a commit's.
UPDATE ai_plugin_packages SET pending_pin_kind = 'commit' WHERE pending_commit_sha IS NOT NULL;

ALTER TABLE ai_plugin_packages
  DROP CONSTRAINT ai_plugin_packages_source_kind_check,
  DROP CONSTRAINT ai_plugin_packages_check1,
  DROP CONSTRAINT ai_plugin_packages_check2;

ALTER TABLE ai_plugin_packages
  ADD CONSTRAINT ai_plugin_packages_source_kind_check
    CHECK (source_kind IN ('git', 'marketplace', 'marketplace-url', 'archive', 'npm', 'hosted')),
  -- A marketplace kind names its entry; no other kind does.
  ADD CONSTRAINT ai_plugin_packages_entry_check
    CHECK ((source_kind IN ('marketplace', 'marketplace-url')) = (marketplace_entry IS NOT NULL)),
  -- The pin's one column is set, and the others are not. The previous build's approve of
  -- a re-pin copies pending_commit_sha into commit_sha and leaves pin_kind alone: on a
  -- commit-pinned row (every row it can know) that stays a commit pin and passes; on a
  -- bytes-pinned row it would leave two pins, so it is refused and writes nothing.
  ADD CONSTRAINT ai_plugin_packages_pin_check CHECK (
    (pin_kind = 'commit') = (commit_sha IS NOT NULL) AND
    (pin_kind = 'bytes') = (bytes_sha256 IS NOT NULL) AND
    (pin_kind = 'version') = (version IS NOT NULL)
  ),
  -- Which pin a source can have: a git repository a commit, an archive or an npm package
  -- bytes, a hosted plugin a version; a marketplace entry names a git source (a commit)
  -- or an archive or npm one (bytes).
  ADD CONSTRAINT ai_plugin_packages_source_pin_check CHECK (
    CASE source_kind
      WHEN 'git' THEN pin_kind = 'commit'
      WHEN 'archive' THEN pin_kind = 'bytes'
      WHEN 'npm' THEN pin_kind = 'bytes'
      WHEN 'hosted' THEN pin_kind = 'version'
      ELSE pin_kind IN ('commit', 'bytes')
    END
  ),
  -- A re-pin waiting for approval: all of its columns, or none. pending_content_hash
  -- says whether there is one. The previous build's API (pods still serving while this
  -- build rolls out) writes a commit re-pin without pending_pin_kind and clears one
  -- (discard, approve) without clearing it, so a re-pin's kind is pending_pin_kind or, when
  -- that is NULL, 'commit' for a pending_commit_sha; with no re-pin, pending_pin_kind may
  -- be left over and means nothing. The API reads the kind the same way
  -- (crud_agent_packages.py PENDING_KIND).
  ADD CONSTRAINT ai_plugin_packages_pending_check CHECK (
    (pending_content_hash IS NULL) = (pending_files IS NULL) AND
    (pending_content_hash IS NULL) = (pending_review IS NULL) AND
    (pending_content_hash IS NULL) = (pending_ref IS NULL) AND
    (pending_content_hash IS NULL) = (pending_fetch_url IS NULL) AND
    (pending_content_hash IS NULL) = (pending_fetch_path IS NULL) AND
    CASE WHEN pending_content_hash IS NULL THEN
      pending_commit_sha IS NULL AND pending_bytes_sha256 IS NULL AND pending_version IS NULL
    ELSE
      coalesce(pending_pin_kind, CASE WHEN pending_commit_sha IS NOT NULL THEN 'commit' END) IS NOT NULL AND
      (coalesce(pending_pin_kind, CASE WHEN pending_commit_sha IS NOT NULL THEN 'commit' END) = 'commit')
        = (pending_commit_sha IS NOT NULL) AND
      (coalesce(pending_pin_kind, CASE WHEN pending_commit_sha IS NOT NULL THEN 'commit' END) = 'bytes')
        = (pending_bytes_sha256 IS NOT NULL) AND
      (coalesce(pending_pin_kind, CASE WHEN pending_commit_sha IS NOT NULL THEN 'commit' END) = 'version')
        = (pending_version IS NOT NULL)
    END
  ),
  ADD CONSTRAINT ai_plugin_packages_pending_source_pin_check CHECK (
    pending_content_hash IS NULL OR
    CASE source_kind
      WHEN 'git' THEN coalesce(pending_pin_kind, 'commit') = 'commit'
      WHEN 'archive' THEN coalesce(pending_pin_kind, 'commit') = 'bytes'
      WHEN 'npm' THEN coalesce(pending_pin_kind, 'commit') = 'bytes'
      WHEN 'hosted' THEN coalesce(pending_pin_kind, 'commit') = 'version'
      ELSE coalesce(pending_pin_kind, 'commit') IN ('commit', 'bytes')
    END
  );
