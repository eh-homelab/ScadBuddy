-- Per-subject data keys for the payload codec (spec 2026-10-01 §6.5). A subject is
-- a workflow id, session-<uuid> or flow-<uuid>. Deleting the row makes every copy of
-- that workflow's payloads (history, Visibility, Archival) undecryptable.
CREATE TABLE ai_payload_keys (
  subject    text PRIMARY KEY CHECK (subject ~ '^(session|flow)-[0-9a-f-]{36}$'),
  dek_sealed bytea NOT NULL,
  kek_id     text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
