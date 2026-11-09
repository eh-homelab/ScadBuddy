-- #3741: the Prints page strip, polled, asks which runs on its printers started the job
-- each printer runs now (`bambuddy/print_links.py` `_LAN_JOBS`). This index reads only
-- those printers' runs. The other printer index, `print_runs_lan_waiting`, holds only
-- the runs with prints still to start.
CREATE INDEX IF NOT EXISTS print_runs_printer ON print_runs (printer, created_at)
    WHERE printer IS NOT NULL;
