-- #3823 (plan 2026-10-16 Task 8): the registry printer a run prints on, by identity,
-- whichever field of the request named it. Null for a run accepted before the registry
-- or on a process without it. A printer deleted later leaves its runs unnamed.
ALTER TABLE print_runs
    ADD COLUMN printer uuid REFERENCES printers (id) ON DELETE SET NULL;
