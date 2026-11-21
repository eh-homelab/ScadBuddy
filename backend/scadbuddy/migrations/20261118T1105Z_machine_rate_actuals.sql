-- #5774, spec 2026-11-17-machine-rate-actuals §4: logged maintenance and replaced wear
-- parts, whose actual per-hour figures may price a printer's machine rate. Every table is
-- the CRUD service's own (#5295). Each goes with its printer; a merge moves the gone
-- printer's events onto the kept one (`printers/store.py` `PRINTER_REFERENCES`).

-- A maintenance task done on a printer: imported from Bambuddy's log, or logged in
-- ScadBuddy (a printer Bambuddy does not track).
CREATE TABLE printer_maintenance_events (
  id uuid PRIMARY KEY,
  printer uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
  task text NOT NULL CHECK (length(task) BETWEEN 1 AND 100),
  performed_at timestamptz NOT NULL,
  -- The printer's print-hour counter when it was done. Null: not known (no gap).
  hours_at numeric(10, 1) CHECK (hours_at >= 0),
  -- Which counter `hours_at` is on (§3.2). A gap across two sources is not usable.
  hours_source text CHECK (hours_source IN ('bambuddy', 'lan_watch', 'typed')),
  -- Null: the task's typed figure prices this event (`partly_typed`).
  minutes numeric(8, 1) CHECK (minutes >= 0),
  consumables numeric(14, 4) CHECK (consumables >= 0),
  notes text CHECK (length(notes) <= 1000),
  origin text NOT NULL CHECK (origin IN ('bambuddy', 'scadbuddy')),
  bambuddy_history_id integer,
  -- A ScadBuddy event's writer key, so a retry writes once (as job cost's `source`);
  -- `bambuddy:<history id>` for an imported one.
  source text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL,
  UNIQUE (printer, source),
  UNIQUE (printer, bambuddy_history_id),
  CHECK ((hours_at IS NULL) = (hours_source IS NULL))
);
CREATE INDEX printer_maintenance_events_task
  ON printer_maintenance_events (printer, task, hours_at DESC);

-- A wear part replaced, logged in ScadBuddy only (§3.1): its real cost, and the hours
-- the removed part ran.
CREATE TABLE printer_wear_replacements (
  id uuid PRIMARY KEY,
  printer uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
  part text NOT NULL CHECK (length(part) BETWEEN 1 AND 100),
  replaced_at timestamptz NOT NULL,
  hours_at numeric(10, 1) CHECK (hours_at >= 0),
  hours_source text CHECK (hours_source IN ('bambuddy', 'lan_watch', 'typed')),
  -- Typed (a correction): the removed part's hours. Null: from `hours_at` and the
  -- replacement before.
  hours_used numeric(10, 1) CHECK (hours_used > 0),
  cost numeric(14, 4) NOT NULL CHECK (cost >= 0),
  notes text CHECK (length(notes) <= 1000),
  source text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL,
  UNIQUE (printer, source),
  CHECK ((hours_at IS NULL) = (hours_source IS NULL))
);
CREATE INDEX printer_wear_replacements_part
  ON printer_wear_replacements (printer, part, hours_at DESC);

ALTER TABLE printer_cost_profiles
  ADD COLUMN actuals_window integer NOT NULL DEFAULT 3
    CHECK (actuals_window BETWEEN 1 AND 20),
  ADD COLUMN lan_hours_offset numeric(10, 1) NOT NULL DEFAULT 0
    CHECK (lan_hours_offset >= 0);
ALTER TABLE printer_wear_parts
  ADD COLUMN priced_by text NOT NULL DEFAULT 'estimate'
    CHECK (priced_by IN ('estimate', 'actual'));
ALTER TABLE printer_maintenance_costs
  ADD COLUMN priced_by text NOT NULL DEFAULT 'estimate'
    CHECK (priced_by IN ('estimate', 'actual'));
