-- RFID tags, overrides and NTAG (#6264). A tag whose binding changed is queued here for
-- Bambuddy, whatever the sync setting says; the push (`filament/rfid_push.py`) drains it.
CREATE TABLE filament_bb_tag_pending (
    tag_id uuid PRIMARY KEY REFERENCES filament_tags (id) ON DELETE CASCADE,
    queued_at timestamptz NOT NULL DEFAULT now(),
    attempts integer NOT NULL DEFAULT 0,
    last_attempt_at timestamptz,
    last_error text
);

-- One row: whether override mode is on, and Bambuddy's auto-add setting as it stood
-- before the mode turned it off; `blocked` is whether the last reconcile was refused
-- because Bambuddy's auth is on.
CREATE TABLE filament_rfid_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    override_mode boolean NOT NULL DEFAULT false,
    saved_auto_add boolean,
    blocked boolean NOT NULL DEFAULT false,
    changed_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO filament_rfid_state (singleton) VALUES (true);

-- The spool each tray of a printer is configured for, whatever the override mode says.
CREATE TABLE filament_override_slots (
    printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
    ams_id integer NOT NULL,
    tray_id integer NOT NULL,
    spool_id uuid NOT NULL REFERENCES filament_spools (id) ON DELETE CASCADE,
    params_hash text,
    configured_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    PRIMARY KEY (printer_id, ams_id, tray_id)
);

INSERT INTO filament_bb_tag_pending (tag_id)
SELECT tag_id FROM filament_tag_bindings WHERE unbound_at IS NULL;

-- A Bambuddy spool the pull skipped because Bambuddy auto-added it for a tag a linked SB
-- spool holds. The push later clears its tag, so only this row tells the next pull it is
-- no new spool. Only `reason` 'rfid_duplicate' exists.
CREATE TABLE filament_bb_ignored (
    bb_spool_id integer PRIMARY KEY,
    reason text NOT NULL CHECK (reason IN ('rfid_duplicate')),
    tag_uid text,
    tray_uuid text,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

-- The NTAG write queued for a spool: the tag UID Bambuddy's record held when the write was
-- queued (null for none). `confirm` binds only a UID that differs from it.
CREATE TABLE filament_ntag_writes (
    spool_id uuid PRIMARY KEY REFERENCES filament_spools (id) ON DELETE CASCADE,
    baseline_uid text,
    queued_at timestamptz NOT NULL DEFAULT now()
);
