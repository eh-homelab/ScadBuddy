-- #5299, part of #5295: no trigger writes the backend's `blob_refs` any more. A workspace
-- file's blob is held through the agent's CRUD routes (backend api/crud_agent.py,
-- `/api/v1/internal/crud/agent/workspace-blob-refs`, the `crud.agent` service credential):
-- SessionFiles holds before it writes the row that names the blob and releases once the
-- row is gone, and its reconcile releases what a cascade, forgetSubject or a crash left
-- (src/workspace/store.ts). The holder is the one the trigger wrote,
-- `(ai_session_file, <workspace>/<path>)`, so every existing `blob_refs` row stays valid.
DROP TRIGGER IF EXISTS ai_session_files_hold_blob ON ai_session_files;
DROP FUNCTION IF EXISTS ai_session_files_hold_blob();
