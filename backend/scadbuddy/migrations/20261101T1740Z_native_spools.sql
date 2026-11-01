-- ScadBuddy's own spool inventory, the native spool provider (#4643, tier B of #3818;
-- plan 2026-10-18-hot-path-without-bambuddy D1 names it the second provider). The
-- records a person keeps, not a cache: `spools/spool_providers.py` copies the unarchived
-- ones into `spool_provider_records` as provider `native`, as every provider's refresh
-- does, so the overlay reads them like Bambuddy's. Grams are a person's (and, later, a
-- finished print's `used_g`); colour is `#RRGGBB`; the RFID keys are stored cleaned as
-- `spools/models.py` cleans them, never all zeros.
CREATE TABLE native_spools (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    material text,
    colour text,
    brand text,
    name text,
    label_weight_g integer CHECK (label_weight_g > 0),
    remaining_g double precision CHECK (remaining_g >= 0),
    tray_uuid text,
    tag_uid text,
    preset text,
    note text,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One unarchived record per tag: a tag names one spool. An archived record (a spool used
-- up) keeps its keys without holding them.
CREATE UNIQUE INDEX native_spools_tray_uuid ON native_spools (tray_uuid)
    WHERE archived_at IS NULL AND tray_uuid IS NOT NULL;
CREATE UNIQUE INDEX native_spools_tag_uid ON native_spools (tag_uid)
    WHERE archived_at IS NULL AND tag_uid IS NOT NULL;
