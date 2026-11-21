-- #5774 PR 3, spec 2026-11-17-machine-rate-actuals §3.7: when a printer's maintenance
-- log was last read from Bambuddy in full (`events_read_at` in the machine-rate view). A
-- read that failed leaves it as it was. The CRUD service's own (#5295); a merge keeps the
-- kept printer's row (`printers/store.py` `PRINTER_REFERENCES`).
CREATE TABLE printer_maintenance_reads (
  printer uuid PRIMARY KEY REFERENCES printers (id) ON DELETE CASCADE,
  read_at timestamptz NOT NULL
);
