-- #5138, #5142: the print a LAN printer's reports last showed running, so the watch can
-- tell when it ends (`printers/reports.py` `advance_print`) and settle its rack picks.
-- A report that says too little (no `subtask_name` or `gcode_state`, such as a delta
-- before the reconnect's `pushall` answer) keeps it, rather than losing the print the
-- next whole report ends. Backfilled from each row's report.
ALTER TABLE printer_reports ADD COLUMN running_subtask text;

UPDATE printer_reports SET running_subtask = report->>'subtask_name'
    WHERE report->>'gcode_state' IN ('PREPARE', 'SLICING', 'RUNNING', 'PAUSE');
