-- #2477: the images a flow run's tool calls returned (src/temporal/flowBlobs.ts).
-- A flow call has no session, so these are kept by the run's subject `flow-<run id>`,
-- named by the bytes' sha256 and the type's extension as ai_session_blobs are, and
-- sealed under the subject's payload key (ai_payload_keys) in the context
-- `flow-blob:<subject>:<name>`. Forgetting the run (backend flows/forget.py) deletes
-- them in the transaction that deletes the key. `size` is the image's byte count,
-- which the run page lists without opening the blob.
CREATE TABLE ai_flow_blobs (
  subject     text NOT NULL
              CHECK (subject ~ '^flow-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
  name        text NOT NULL CHECK (name ~ '^[0-9a-f]{64}\.(png|jpg|gif|webp)$'),
  media_type  text NOT NULL CHECK (media_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp')),
  size        integer NOT NULL CHECK (size > 0),
  data        bytea NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (subject, name)
);
