-- #7191 (epic #6931): the strict pending pin CHECKs. 20261122T0323Z_plugin_package_pins.sql
-- (#6951) let the build before it write during the roll: a commit re-pin without
-- pending_pin_kind, and a discard or approve that left pending_pin_kind set. Every pod now
-- runs a build with #6951 (sha-420a83a, measured 2026-11-23), and that build writes
-- pending_pin_kind with every re-pin and clears it with every discard and approve
-- (api/crud_agent_packages.py). So pending_pin_kind alone is now the re-pin's kind.

-- 1. A re-pin that an old pod stored has no kind: it is a commit re-pin.
UPDATE ai_plugin_packages SET pending_pin_kind = 'commit'
WHERE pending_content_hash IS NOT NULL AND pending_pin_kind IS NULL
  AND pending_commit_sha IS NOT NULL;

-- 2. A kind that an old pod left behind with no re-pin means nothing.
UPDATE ai_plugin_packages SET pending_pin_kind = NULL
WHERE pending_content_hash IS NULL AND pending_pin_kind IS NOT NULL;

-- 3. A re-pin that still does not fit the CHECKs below (no kind and no commit, a pin column
-- that is not its kind's, or a kind its source cannot have) is discarded: all of its
-- pending_* columns are cleared. The old CHECKs refused each of these, so no row is
-- expected here. A re-pin is only a proposal that waits for an admin's approval, and the
-- current pin stays as it is, so to discard one is safe: the agent proposes it again at
-- its next re-pin. The next package write prunes any access row or mirror hold that only
-- the discarded proposal kept.
UPDATE ai_plugin_packages SET pending_ref = NULL, pending_fetch_url = NULL,
  pending_fetch_path = NULL, pending_pin_kind = NULL, pending_commit_sha = NULL,
  pending_bytes_sha256 = NULL, pending_version = NULL, pending_content_hash = NULL,
  pending_files = NULL, pending_review = NULL, updated_at = now()
WHERE pending_content_hash IS NOT NULL AND NOT coalesce(
  pending_pin_kind IS NOT NULL AND
  (pending_pin_kind = 'commit') = (pending_commit_sha IS NOT NULL) AND
  (pending_pin_kind = 'bytes') = (pending_bytes_sha256 IS NOT NULL) AND
  (pending_pin_kind = 'version') = (pending_version IS NOT NULL) AND
  CASE source_kind
    WHEN 'git' THEN pending_pin_kind = 'commit'
    WHEN 'archive' THEN pending_pin_kind = 'bytes'
    WHEN 'npm' THEN pending_pin_kind = 'bytes'
    WHEN 'hosted' THEN pending_pin_kind = 'version'
    ELSE pending_pin_kind IN ('commit', 'bytes')
  END,
  false);

ALTER TABLE ai_plugin_packages
  DROP CONSTRAINT ai_plugin_packages_pending_check,
  DROP CONSTRAINT ai_plugin_packages_pending_source_pin_check;

ALTER TABLE ai_plugin_packages
  -- A re-pin waiting for approval: all of its columns, or none. pending_content_hash says
  -- whether there is one, and pending_pin_kind is set exactly when it is. The re-pin's one
  -- pin column is the column of its kind, as ai_plugin_packages_pin_check requires of the
  -- pin. With no re-pin, no pending pin column is set.
  ADD CONSTRAINT ai_plugin_packages_pending_check CHECK (
    (pending_content_hash IS NULL) = (pending_pin_kind IS NULL) AND
    (pending_content_hash IS NULL) = (pending_files IS NULL) AND
    (pending_content_hash IS NULL) = (pending_review IS NULL) AND
    (pending_content_hash IS NULL) = (pending_ref IS NULL) AND
    (pending_content_hash IS NULL) = (pending_fetch_url IS NULL) AND
    (pending_content_hash IS NULL) = (pending_fetch_path IS NULL) AND
    (pending_pin_kind IS NOT DISTINCT FROM 'commit') = (pending_commit_sha IS NOT NULL) AND
    (pending_pin_kind IS NOT DISTINCT FROM 'bytes') = (pending_bytes_sha256 IS NOT NULL) AND
    (pending_pin_kind IS NOT DISTINCT FROM 'version') = (pending_version IS NOT NULL)
  ),
  -- Which re-pin a source can have, as ai_plugin_packages_source_pin_check says for the pin.
  ADD CONSTRAINT ai_plugin_packages_pending_source_pin_check CHECK (
    pending_pin_kind IS NULL OR
    CASE source_kind
      WHEN 'git' THEN pending_pin_kind = 'commit'
      WHEN 'archive' THEN pending_pin_kind = 'bytes'
      WHEN 'npm' THEN pending_pin_kind = 'bytes'
      WHEN 'hosted' THEN pending_pin_kind = 'version'
      ELSE pending_pin_kind IN ('commit', 'bytes')
    END
  );
