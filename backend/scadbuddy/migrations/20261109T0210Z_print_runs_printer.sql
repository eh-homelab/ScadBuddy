-- #3741: the Prints page strip, polled, asks which runs on its printers started the job
-- each printer runs now (`bambuddy/print_links.py` `_LAN_JOBS`). This index reads only
-- those printers' runs. The query has no ORDER BY, so the key is the printer alone. The
-- other printer index, `print_runs_lan_waiting`, holds only the runs with prints still
-- to start.
-- A plain CREATE INDEX inside `migrate`'s one transaction: it holds a SHARE lock on
-- `print_runs` (writes wait, reads go on) until every pending file commits. That is
-- short at today's size: one row per print a person starts.
CREATE INDEX IF NOT EXISTS print_runs_printer ON print_runs (printer)
    WHERE printer IS NOT NULL;
