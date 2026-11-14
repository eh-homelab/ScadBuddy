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
-- `data` stays for a row an older agent writes while this one rolls out; nothing here
-- writes it any more, and SessionFiles.copy moves such a row's bytes here first.
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

-- The rows kept inline so far. A row whose sha256 does not match its bytes stays inline.
INSERT INTO ai_session_file_bytes (sha256, data)
SELECT DISTINCT ON (sha256) sha256, data FROM ai_session_files
WHERE data IS NOT NULL AND sha256 = encode(sha256(data), 'hex')
ORDER BY sha256
ON CONFLICT (sha256) DO NOTHING;
UPDATE ai_session_files SET bytes_sha256 = sha256, data = NULL
WHERE data IS NOT NULL AND sha256 = encode(sha256(data), 'hex');
