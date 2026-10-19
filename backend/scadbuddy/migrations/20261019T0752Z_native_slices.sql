-- One row per native slice (plan 2026-10-16-native-slicer-rust-worker Task 11): the
-- `SliceWorkflow` execution named `workflow_id`, which writes the row through its
-- activities. `input_sha256` is the laid-out copy's digest, set once `slice_prepare` has
-- read it; a completed row is reused by any copy of the same bytes and presets. The
-- sliced blob, the input and the presets are held in `blob_refs` under
-- ('native_slice', id) until the row is swept.
CREATE TABLE native_slices (
  id bigserial PRIMARY KEY,
  workflow_id text NOT NULL UNIQUE,
  subject text NOT NULL,
  library_file_id bigint NOT NULL,
  plate_id integer NOT NULL,
  preset_key text NOT NULL,
  input_sha256 text,
  status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed')),
  failure text,
  sliced_blob_key text,
  warnings jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
CREATE INDEX native_slices_reuse ON native_slices (input_sha256, preset_key) WHERE status = 'completed';
