-- #2243: a session's large Temporal payloads, out of history. Every payload of a
-- `session-<uuid>` workflow whose sealed bytes reach agent-durable's
-- payload_store.OFFLOAD_BYTES (128 KiB) is stored here by Temporal's External
-- Storage (agent-durable payload_store.py) or by the agent's codec
-- (src/temporal/payloadCodec.ts, PgPayloadStore), and history holds only a
-- reference: the subject and the SHA-256 of the sealed bytes. The bytes are the
-- codec's output, sealed under the subject's key, and the rows go with that key, so
-- forgetSubject removes them with every other copy of the subject's payloads.
CREATE TABLE ai_payload_blobs (
  subject     text NOT NULL REFERENCES ai_payload_keys (subject) ON DELETE CASCADE,
  digest      text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
  data        bytea NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (subject, digest)
);
