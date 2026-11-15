-- #4212: a fork shares its parent's workspace files where the backend keeps no blobs
-- (store_backend local), as it already does for blobs: no byte is copied.
--
-- The bytes of such a file are one row of `ai_session_file_bytes`, keyed by their sha256,
-- and every file row with those bytes names it (`bytes_sha256`). A fork (SessionFiles.copy)
-- writes rows that name the same bytes. The CHECK makes a key always the sha256 of its
-- bytes, so two files share a row only when their bytes are the same. The foreign key keeps
-- a row while a file names it; SessionFiles deletes the rows no file names (after a write or
-- a delete, on its sweep, and at forgetSubject). EXTERNAL storage, as `data` has, so
-- SessionFiles.chunks reads one slice at a time.
--
-- Rows kept in `data` stay there: this migration moves none, so an older replica still
-- serving during the rolling update reads, writes and forks the files that were there
-- before it. Nothing here writes `data` any more; reads take either place, and
-- SessionFiles.copy moves a parent's `data` rows here before a fork shares them. Until the
-- last older replica stops, it cannot read, overwrite or fork a file a newer one wrote or
-- shared at a fork: that row names its bytes, which an older agent does not know (its read
-- fails, its write is refused by the CHECK below, and its fork leaves that file out).
CREATE TABLE ai_session_file_bytes (
  sha256     text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  data       bytea NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (sha256 = encode(sha256(data), 'hex'))
);
ALTER TABLE ai_session_file_bytes ALTER COLUMN data SET STORAGE EXTERNAL;

ALTER TABLE ai_session_files ADD COLUMN bytes_sha256 text REFERENCES ai_session_file_bytes (sha256);
ALTER TABLE ai_session_files DROP CONSTRAINT ai_session_files_bytes_check;
ALTER TABLE ai_session_files ADD CONSTRAINT ai_session_files_bytes_check CHECK (
  (data IS NOT NULL AND bytes_sha256 IS NULL AND blob_key IS NULL AND key_sealed IS NULL AND key_kek_id IS NULL)
  OR (data IS NULL AND bytes_sha256 = sha256 AND blob_key IS NULL AND key_sealed IS NULL AND key_kek_id IS NULL)
  OR (data IS NULL AND bytes_sha256 IS NULL AND blob_key IS NOT NULL AND key_sealed IS NOT NULL
      AND (key_kek_id IS NULL) = (session_id IS NOT NULL))
);

-- The check that a row of bytes is still named, and moveToBlobs's pass over them.
CREATE INDEX ai_session_files_bytes ON ai_session_files (bytes_sha256) WHERE bytes_sha256 IS NOT NULL;
CREATE INDEX ai_session_files_shared ON ai_session_files (workspace, path) WHERE bytes_sha256 IS NOT NULL;
