-- What the slicer worker PUT to a pending key through the internal blob route
-- (plan 2026-10-16-native-slicer-rust-worker Task 10): the key the body was stored under,
-- its sha256 and size, which `Transfers.finish_put` checks against what the worker
-- reports. Pruned a day after the PUT.
CREATE TABLE transfer_puts (
  pending_key text PRIMARY KEY,
  key text NOT NULL,
  sha256 text NOT NULL,
  size bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX transfer_puts_created ON transfer_puts (created_at);
