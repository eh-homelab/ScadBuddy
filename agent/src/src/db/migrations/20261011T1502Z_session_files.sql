-- #2329: the assistant's workspace, files of its own that it downloads, reads,
-- edits and uploads (src/workspace/store.ts `SessionFiles`, the workspace_*
-- tools in src/tools/workspace.ts). In Postgres, not on a pod's disk, so every
-- replica and both session modes see the same files.
--
-- `workspace` names whose files a row is: `session:<id>` for a session's (the
-- row goes with its session), or `principal:<kind>:<id>` for an /mcp caller's
-- own outside any session, which is swept 24 h after it was last written.
-- `path` is a safe relative path: at most four segments of
-- `[A-Za-z0-9_][A-Za-z0-9_.-]*`, so no `..`, no absolute path and no empty
-- segment. `source` says where a file came from (the tool and its ids, or a
-- URL), for the audit log and the panel. The caps (per file, per workspace) are
-- the settings `workspace_file_max_bytes` and `workspace_max_bytes`, checked by
-- the store under a per-workspace lock.
CREATE TABLE ai_session_files (
  workspace   text NOT NULL,
  session_id  uuid REFERENCES ai_sessions (id) ON DELETE CASCADE,
  path        text NOT NULL CHECK (
    length(path) <= 512
    AND path ~ '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}){0,3}$'
  ),
  media_type  text NOT NULL CHECK (length(media_type) BETWEEN 1 AND 255),
  data        bytea NOT NULL,
  sha256      text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  size        integer NOT NULL CHECK (size = octet_length(data)),
  source      jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace, path),
  CHECK (
    (session_id IS NOT NULL AND workspace = 'session:' || session_id::text)
    OR (session_id IS NULL AND workspace LIKE 'principal:%')
  )
);

-- A session's delete cascades by session_id.
CREATE INDEX ai_session_files_session ON ai_session_files (session_id) WHERE session_id IS NOT NULL;
-- The 24 h sweep of principals' workspaces.
CREATE INDEX ai_session_files_principal_updated ON ai_session_files (updated_at) WHERE session_id IS NULL;
