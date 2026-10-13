-- #2268: waiting_done keeps at most a fixed number of resolved entries. The oldest are
-- dropped, and reset_floor records the latest history length one of them was resolved
-- at: a Reset to that point or before it would need a dropped entry to park it again,
-- so it is refused. NULL until an entry is dropped.
ALTER TABLE workflow_runs ADD COLUMN reset_floor integer;
