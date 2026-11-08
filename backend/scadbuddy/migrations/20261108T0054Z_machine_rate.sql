-- #4625, order operations spec 2026-11-07 §6: a machine rate per registry printer, derived
-- from what a print-hour costs. Every amount is in the shop's `orders_currency`, as the
-- price book's rates are. A printer with no profile row is priced at the book's
-- `pricing_machine_rate_per_hour`. Each table goes with its printer; a merge keeps the
-- kept printer's own rows (`printers/store.py` `PRINTER_REFERENCES`).
CREATE TABLE printer_cost_profiles (
  printer uuid PRIMARY KEY REFERENCES printers (id) ON DELETE CASCADE,
  purchase_price numeric(14, 4) NOT NULL DEFAULT 0 CHECK (purchase_price >= 0),
  -- Null: no depreciation part.
  life_hours numeric(10, 1) CHECK (life_hours > 0),
  watts numeric(8, 1) NOT NULL DEFAULT 0 CHECK (watts >= 0),
  kwh_price numeric(10, 4) NOT NULL DEFAULT 0 CHECK (kwh_price >= 0),
  -- Null: `pricing_labour_rate_per_hour` when the rate is computed.
  labour_rate_per_hour numeric(12, 4) CHECK (labour_rate_per_hour >= 0),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Parts that wear out with print-hours (a nozzle, a plate, PTFE tube, filters).
CREATE TABLE printer_wear_parts (
  printer uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
  position integer NOT NULL,
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  cost numeric(14, 4) NOT NULL CHECK (cost >= 0),
  life_hours numeric(10, 1) NOT NULL CHECK (life_hours > 0),
  PRIMARY KEY (printer, position)
);

-- What one occurrence of a maintenance task costs: the minutes it takes and its
-- consumables. A printer linked to Bambuddy reads each task's interval from Bambuddy's
-- maintenance tracker, keyed by the task's name there, and keeps the last interval read
-- here (`interval_read_at`) for when Bambuddy does not answer. A printer only the LAN
-- reaches has its tasks and intervals typed here.
CREATE TABLE printer_maintenance_costs (
  printer uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
  task text NOT NULL CHECK (length(task) BETWEEN 1 AND 100),
  minutes numeric(8, 1) NOT NULL DEFAULT 0 CHECK (minutes >= 0),
  consumables numeric(14, 4) NOT NULL DEFAULT 0 CHECK (consumables >= 0),
  -- In print-hours. Null: no interval known, so the task adds nothing.
  interval_hours numeric(10, 1) CHECK (interval_hours > 0),
  -- When `interval_hours` was last read from Bambuddy; null for one typed here.
  interval_read_at timestamptz,
  PRIMARY KEY (printer, task)
);

-- The registry printer a slice quote was priced for (no foreign key: a quote is a
-- snapshot, and keeps naming a printer deleted since) and each part of the machine rate
-- it was priced at, so the price can be explained and never moves later. Null on a
-- manual quote and on every quote recorded before.
ALTER TABLE order_item_quotes ADD COLUMN printer text;
ALTER TABLE order_item_quotes ADD COLUMN machine_rate_breakdown jsonb;
