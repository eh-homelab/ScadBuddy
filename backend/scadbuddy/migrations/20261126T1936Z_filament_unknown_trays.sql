-- #7143: a tray a printer reports as loaded that no spool holds. A row exists while the
-- tray is loaded and unmatched.
CREATE TABLE filament_unknown_trays (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
    ams_id integer NOT NULL CHECK (ams_id BETWEEN 0 AND 255),
    tray_id integer NOT NULL CHECK (tray_id BETWEEN 0 AND 15),
    tray_uuid text,
    tag_uid text,
    material text NOT NULL,
    colour_rgba text,
    remain_pct integer CHECK (remain_pct BETWEEN 0 AND 100),
    first_seen_at timestamptz NOT NULL,
    seen_at timestamptz NOT NULL,
    UNIQUE (printer_id, ams_id, tray_id)
);
