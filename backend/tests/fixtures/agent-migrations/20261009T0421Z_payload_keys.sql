-- One data key per durable subject (spec 2026-10-01 §6.5): every payload of the
-- workflow `session-<id>` or `flow-<id>` is sealed under its subject's key by the
-- payload codec (agent/src/temporal/payloadCodec.ts, agent-durable codec.py).
-- Deleting the row is how forgetSubject crypto-shreds a subject: every copy of its
-- payloads (history, Visibility, Archival) is then undecryptable. The key is sealed
-- under the KEK with the context `dek:ai_payload_keys:<subject>`, and re-wrapped on
-- rotation like ai_credentials.
CREATE TABLE ai_payload_keys (
  subject     text PRIMARY KEY
              CHECK (subject ~ '^(session|flow)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
  dek_sealed  bytea NOT NULL,
  kek_id      text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Subjects forgetSubject shredded. A key is never made again for one, so an encoder
-- still running for it (a workflow task, an activity between the key's deletion and
-- the workflow's termination) fails instead of sealing new payloads under a new key.
CREATE TABLE ai_forgotten_subjects (
  subject       text PRIMARY KEY,
  forgotten_at  timestamptz NOT NULL DEFAULT now()
);
