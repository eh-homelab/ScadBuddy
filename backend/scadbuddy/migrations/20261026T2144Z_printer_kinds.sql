-- The Bambuddy printer sync (#4439, #4747; plan 2026-10-26-bambuddy-printer-sync).
-- `kind` tells a physical printer from a Bambuddy virtual printer, which is never counted
-- as hardware. A virtual printer's row is keyed by `bambuddy_vp_id`; `forwards_to` is the
-- registry printer it targets (NULL: Bambuddy's queue picks any free printer of its
-- model). `bambuddy_view` is what Bambuddy reported at the last sync (never an access
-- code), and `lan_sync_error` why the sync last could not fill or move the LAN link.
ALTER TABLE printers
    ADD COLUMN kind text NOT NULL DEFAULT 'physical'
        CHECK (kind IN ('physical', 'bambuddy_virtual')),
    ADD COLUMN bambuddy_vp_id integer UNIQUE,
    ADD COLUMN forwards_to uuid REFERENCES printers (id) ON DELETE SET NULL,
    ADD COLUMN vp_mode text,
    ADD COLUMN bambuddy_view jsonb,
    ADD COLUMN lan_sync_error text,
    ADD CONSTRAINT printers_vp_kind CHECK ((bambuddy_vp_id IS NULL) = (kind = 'physical')),
    ADD CONSTRAINT printers_vp_unlinked CHECK (kind = 'physical' OR bambuddy_printer_id IS NULL);
