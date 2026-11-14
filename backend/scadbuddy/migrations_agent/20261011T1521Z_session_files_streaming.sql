-- #2329 part 2: an upload from the workspace streams a file to the backend a
-- slice at a time (src/workspace/store.ts `SessionFiles.chunks`, `substring` on
-- `data`). Stored uncompressed (EXTERNAL: out of line, never compressed), a
-- slice reads only its own bytes; compressed, every slice would decompress the
-- value from its start. Applies to rows written from now on.
ALTER TABLE ai_session_files ALTER COLUMN data SET STORAGE EXTERNAL;
