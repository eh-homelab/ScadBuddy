-- #7389: the `seen_at` of the report a row last wrote. A touch moves `seen_at` and
-- writes no report, so the refresh of an unchanged report (`printers/reports.py`
-- `REFRESH_EVERY`) is timed from this column, which only a written report sets. A row
-- with none (written before this file) writes its next report.
ALTER TABLE printer_reports ADD COLUMN report_seen_at timestamptz;
