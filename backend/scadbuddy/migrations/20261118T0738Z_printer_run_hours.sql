-- ScadBuddy's own print-hour counter for a LAN-linked printer (#5774, spec
-- 2026-11-17-machine-rate-actuals §3.6), advanced by the CRUD service inside the LAN
-- watch's report writes and touches (`printers/run_hours.py`).
CREATE TABLE printer_run_hours (
  printer uuid PRIMARY KEY REFERENCES printers (id) ON DELETE CASCADE,
  run_seconds numeric(16, 6) NOT NULL DEFAULT 0 CHECK (run_seconds >= 0),
  -- Gaps over the limit that ended in another state or print: not counted.
  unseen_seconds numeric(16, 6) NOT NULL DEFAULT 0 CHECK (unseen_seconds >= 0),
  -- Counted up to here. Never moves back: a write stamped before it counts nothing.
  counted_to timestamptz NOT NULL,
  -- The last report's gcode_state and subtask_name. Null state: nothing to count
  -- (also set by an unlink, so no gap spans it).
  state text,
  subtask text,
  started_at timestamptz NOT NULL DEFAULT now()
);
