-- #7907: a commit pin's approved files, kept in the blob store as a zip (the agent's CRUD
-- area `package-mirrors`, the blob `plugin-mirror-<sha256>`), so a pod with a cold cache
-- loads the pin from that copy and does not fetch the repository again.
--
--   mirror_sha256          the sha256 of the zip of the pin's files (agent
--                          `plugins/packages/zip.ts` `storedZip`); null until the agent
--                          records one, and for a bytes or version pin, whose own
--                          bytes_sha256 or hosted version is its copy
--   pending_mirror_sha256  the same for the pending re-pin; approving the re-pin moves it
--                          into mirror_sha256, as the other pending columns move
--
-- Only a record route sets them, and only for the commit and content hash that the row
-- names. The agent hashes the unpacked files against content_hash before it uses them, so
-- a value that names other files is never loaded.
ALTER TABLE ai_plugin_packages
  ADD COLUMN mirror_sha256 text
    CONSTRAINT ai_plugin_packages_mirror_sha256_check CHECK (mirror_sha256 ~ '^[0-9a-f]{64}$'),
  ADD COLUMN pending_mirror_sha256 text
    CONSTRAINT ai_plugin_packages_pending_mirror_sha256_check CHECK (pending_mirror_sha256 ~ '^[0-9a-f]{64}$');
