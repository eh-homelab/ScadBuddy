-- Location sensors and their readings (#6268). A sensor belongs to a place and is either a
-- Home Assistant entity (read through Bambuddy) or an AMS unit's own humidity or temperature.
-- Its last stored reading is kept on the row, so a read needs no join.
CREATE TABLE filament_location_sensors (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    location_id uuid NOT NULL REFERENCES filament_locations (id) ON DELETE CASCADE,
    source text NOT NULL CHECK (source IN ('home_assistant', 'ams')),
    kind text NOT NULL CHECK (kind IN ('humidity', 'temperature')),
    entity_id text,
    printer_id uuid REFERENCES printers (id) ON DELETE CASCADE,
    ams_id integer,
    name text NOT NULL,
    unit text,
    alert_above double precision,
    alert_below double precision,
    notify boolean NOT NULL DEFAULT false,
    last_value double precision,
    last_at timestamptz,
    reachable boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz,
    CHECK (source <> 'home_assistant' OR entity_id IS NOT NULL),
    CHECK (source <> 'ams' OR (printer_id IS NOT NULL AND ams_id IS NOT NULL))
);
CREATE INDEX filament_location_sensors_location ON filament_location_sensors (location_id);
CREATE UNIQUE INDEX filament_location_sensors_ams
    ON filament_location_sensors (printer_id, ams_id, kind)
    WHERE source = 'ams' AND archived_at IS NULL;

CREATE TABLE filament_sensor_readings (
    sensor_id uuid NOT NULL REFERENCES filament_location_sensors (id) ON DELETE CASCADE,
    at timestamptz NOT NULL,
    value double precision NOT NULL,
    PRIMARY KEY (sensor_id, at)
);

-- Hours a spool of the material may sit outside dry storage; null is no limit.
ALTER TABLE filament_materials ADD COLUMN max_hours_outside_dry numeric(6,1);

-- One place per AMS unit (a tray place has a tray_id; Postgres treats a NULL tray_id as
-- distinct in `filament_locations_tray`).
CREATE UNIQUE INDEX filament_locations_ams_unit ON filament_locations (printer_id, ams_id)
    WHERE tray_id IS NULL AND printer_id IS NOT NULL AND archived_at IS NULL;

-- A tray place hangs under its unit's place where that exists.
UPDATE filament_locations t SET parent_id = u.id, updated_at = now()
FROM filament_locations u
WHERE t.tray_id IS NOT NULL AND t.printer_id IS NOT NULL AND t.archived_at IS NULL
  AND t.parent_id IS NULL
  AND u.printer_id = t.printer_id AND u.ams_id = t.ams_id AND u.tray_id IS NULL
  AND u.archived_at IS NULL;
