-- The ScadBuddy-to-Bambuddy sync (#6265, plan 2026-11-18-filament-bb-sync.md Task 3).
--
-- What a link pushed and saw. `pushed_fields` is field -> `field_hash(value)` of what the
-- last write sent (Decision 7). It supersedes `pushed_hash` of
-- `20261114T1301Z_filament_spools.sql`, which stays, unused: that file is merged (#491)
-- and `filament/models.py` still exposes the column. `bb_seen` is `seen_row(answer)`:
-- Bambuddy's row in the fields Decision 14 names, with BB's names and canonical colours
-- (F5). `bb_k_profiles` is the spool's K-profiles as Bambuddy last answered them. Every
-- `bb_seen_at` in this file is the time `results` or the pull last wrote that row's
-- `bb_seen` (F11).
ALTER TABLE filament_bb_links
    ADD COLUMN pushed_fields jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN bb_seen jsonb,
    ADD COLUMN bb_seen_at timestamptz,
    ADD COLUMN bb_k_profiles jsonb,
    -- `bb_seen` is a full copy (seeded, or stored from a whole row's answer), not only the
    -- one field a list PUT answered: until then a full pass seeds the link (R-T5-10).
    ADD COLUMN bb_seeded boolean NOT NULL DEFAULT false;

-- Each SB row the sync makes or links in Bambuddy names its BB row, and keeps the hashes
-- of what it pushed and what it last saw there.
ALTER TABLE filament_suppliers
    ADD COLUMN bb_supplier_id integer UNIQUE,
    ADD COLUMN bb_pushed jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN bb_seen jsonb,
    ADD COLUMN bb_seen_at timestamptz;
ALTER TABLE filament_locations
    ADD COLUMN bb_location_id integer UNIQUE,
    ADD COLUMN bb_pushed jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN bb_seen jsonb,
    ADD COLUMN bb_seen_at timestamptz;
-- An insert type's BB row is its `bb_catalog_id` (`20261116T0327Z_filament_insert_bb_catalog.sql`).
ALTER TABLE filament_insert_types
    ADD COLUMN bb_pushed jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN bb_seen jsonb,
    ADD COLUMN bb_seen_at timestamptz;
-- A spool type's entry in Bambuddy's filament catalog (`/filament-catalog/`, Decision 14),
-- with its seen copy as the other rows keep one (R-T4-25).
ALTER TABLE filament_spool_types
    ADD COLUMN bb_filament_id integer UNIQUE,
    ADD COLUMN bb_pushed jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN bb_seen jsonb,
    ADD COLUMN bb_seen_at timestamptz;

-- Decision 10: a create's intent, written before its POST and deleted when `results`
-- links Bambuddy's answer. A later plan adopts the row a lost answer made. `sent` is the
-- field hashes the create sent (R-T4-35): the adoption pushes a field only where
-- Bambuddy's row still holds what SB sent, so a person's later edit there stays.
CREATE TABLE filament_bb_creates (
    kind text CHECK (kind IN ('spool', 'supplier', 'location', 'insert_type', 'filament')),
    sb_id uuid,
    started_at timestamptz NOT NULL DEFAULT now(),
    sent jsonb,
    PRIMARY KEY (kind, sb_id)
);

-- Decision 19: one row per item whose last write failed, deleted on success. `error` is
-- `rfid_push.error_label(exc)`, never a message or a class name.
CREATE TABLE filament_bb_push_errors (
    kind text CHECK (kind IN ('spool', 'supplier', 'location', 'insert_type', 'filament')),
    sb_id uuid,
    error text NOT NULL,
    attempts integer NOT NULL DEFAULT 1,
    at timestamptz NOT NULL DEFAULT now(),
    -- The plan round whose results last wrote this row (R-T6-5): a result of the same
    -- round adds no attempt, so a resend after a lost answer counts once.
    last_pass_id text,
    PRIMARY KEY (kind, sb_id)
);

-- Decisions 12 and 19: the one state row. `instance` is the Bambuddy the links were made
-- against; it starts null and is set by the API's `adopt_instance` for links made before
-- this row, or by the first `results` or pull that links anything (F6, plan decision 155). `last_ok_at` moves only
-- on a pass with no `error` (F7).
CREATE TABLE filament_bb_sync_state (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    instance text,
    last_pass_at timestamptz,
    last_ok_at timestamptz,
    last_error text,
    counts jsonb NOT NULL DEFAULT '{}'
);
INSERT INTO filament_bb_sync_state DEFAULT VALUES;
