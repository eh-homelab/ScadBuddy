-- #7276 Task 5: ScadBuddy's own K-profile notes and AMS slot presets, per printer.
-- A printer's delete takes them; a printer merge keeps the kept printer's row on a clash
-- (`printers/store.py` `PRINTER_REFERENCES`).

CREATE TABLE printer_kprofile_notes (
    printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
    note_key text NOT NULL
        CHECK (length(note_key) BETWEEN 1 AND 256 AND strpos(note_key, '/') = 0),
    note text NOT NULL CHECK (length(note) BETWEEN 1 AND 2000),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (printer_id, note_key)
);

CREATE TABLE printer_slot_presets (
    printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
    ams_id smallint NOT NULL,
    tray_id smallint NOT NULL,
    preset_id text NOT NULL CHECK (length(preset_id) BETWEEN 1 AND 64),
    preset_name text NOT NULL CHECK (length(preset_name) BETWEEN 1 AND 128),
    tray_info_idx text CHECK (length(tray_info_idx) <= 64),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (printer_id, ams_id, tray_id)
);
