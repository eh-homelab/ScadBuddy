-- #4337: when a recorded copy was last laid out for a run (recorded, or reused by
-- `print_library/copies.py` `ensure_native_copy`). The per-print copies sweep keeps each
-- subject's newest native copy per folder and trashes the rest unused past its TTL.
-- Rows from before it count from now: none is trashed before a full TTL has passed.
ALTER TABLE output_bambuddy_uploads ADD COLUMN used_at timestamptz NOT NULL DEFAULT now();
