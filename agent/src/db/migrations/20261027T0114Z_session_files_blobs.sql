-- #4789: a workspace file's bytes leave Postgres for the backend's blob store (its
-- content store, store_backend s3 or bambuddy), sealed under a file key of their own
-- (src/workspace/sealing.ts). A row then holds `blob_key` (`workspace-<sha256 of the
-- sealed bytes>`) and `key_sealed`, the file key wrapped under the session's data key
-- (`ai_payload_keys`), or under the KEK for a principal's file (`key_kek_id` names
-- it), and `data` is NULL. A row written where the backend keeps no blobs
-- (store_backend local) keeps its bytes in `data`, as before, and no blob. The size
-- check (`size = octet_length(data)`) passes a NULL `data` as it stands.
--
-- The start-time backfill (SessionFiles.moveToBlobs) moves the existing rows' `data` to
-- blobs; a later migration drops `data` once no row needs it.
ALTER TABLE ai_session_files ALTER COLUMN data DROP NOT NULL;
ALTER TABLE ai_session_files ADD COLUMN blob_key text CHECK (blob_key ~ '^workspace-[0-9a-f]{64}$');
ALTER TABLE ai_session_files ADD COLUMN key_sealed bytea;
ALTER TABLE ai_session_files ADD COLUMN key_kek_id text;
ALTER TABLE ai_session_files ADD CONSTRAINT ai_session_files_bytes_check CHECK (
  (data IS NOT NULL AND blob_key IS NULL AND key_sealed IS NULL AND key_kek_id IS NULL)
  OR (data IS NULL AND blob_key IS NOT NULL AND key_sealed IS NOT NULL
      AND (key_kek_id IS NULL) = (session_id IS NOT NULL))
);

-- The backfill's pass over what is still inline.
CREATE INDEX ai_session_files_inline ON ai_session_files (workspace, path) WHERE data IS NOT NULL;

-- A blob is kept while a row names it: the row's holder in the backend's `blob_refs`
-- (`(ai_session_file, <workspace>/<path>)`), written and removed in the transaction
-- that writes or removes the row, whatever removes it (a delete, the principals' sweep,
-- a session's cascade, forgetSubject). The backend's blob sweep takes a workspace blob
-- no holder names once its grace has passed. `blob_refs` is the backend's table in the
-- same database; a row without a blob never touches it.
CREATE FUNCTION ai_session_files_hold_blob() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' AND OLD.blob_key IS NOT NULL
     AND (TG_OP = 'DELETE' OR NEW.blob_key IS DISTINCT FROM OLD.blob_key) THEN
    DELETE FROM blob_refs
     WHERE key = OLD.blob_key AND holder_kind = 'ai_session_file' AND holder_id = OLD.workspace || '/' || OLD.path;
  END IF;
  IF TG_OP <> 'DELETE' AND NEW.blob_key IS NOT NULL
     AND (TG_OP = 'INSERT' OR NEW.blob_key IS DISTINCT FROM OLD.blob_key) THEN
    INSERT INTO blob_refs (key, holder_kind, holder_id)
    VALUES (NEW.blob_key, 'ai_session_file', NEW.workspace || '/' || NEW.path)
    ON CONFLICT DO NOTHING;
  END IF;
  RETURN NULL;
END
$$;

CREATE TRIGGER ai_session_files_hold_blob
  AFTER INSERT OR UPDATE OR DELETE ON ai_session_files
  FOR EACH ROW EXECUTE FUNCTION ai_session_files_hold_blob();
