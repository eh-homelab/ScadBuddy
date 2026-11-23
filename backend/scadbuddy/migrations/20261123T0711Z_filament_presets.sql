-- ScadBuddy's own filament presets (#7176, plan 2026-11-23-scadbuddy-filament-presets.md).
-- A preset's document is a blob (`blob_key`, `sha256`: the current version's), held in
-- `blob_refs` by holder `filament_preset`, holder id `<id>:<version>`, one hold per
-- version. `source` says who owns it: `3dfp` (a profile fetch made it: `type_id`,
-- `fetch_run_id`, `tdfp_id`, and the key it was fetched for) and `manual` are
-- ScadBuddy's, and Bambuddy gets only a mirror; `bambuddy` (made in Bambuddy, brought in
-- by the pull) is Bambuddy's, and a refresh takes each new version. `bb_preset_id` is
-- the Bambuddy local preset that is this one: the mirror's, or the origin's.
-- `bb_updated_at` is that preset's `updated_at` as Bambuddy sent it when its document
-- was last taken (`bambuddy` presets). `mirrored_sha256` is the document Bambuddy last
-- took from the mirror; `mirror_error` why the last send failed. `filament_type`,
-- `filament_colour` and `compatible_printers` are the document's, so a catalogue lists
-- the presets without reading a blob.
CREATE TABLE filament_presets (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    source text NOT NULL CHECK (source IN ('3dfp', 'bambuddy', 'manual')),
    type_id uuid REFERENCES filament_spool_types (id) ON DELETE SET NULL,
    printer_model text,
    nozzle_diameter text,
    tdfp_id integer CHECK (tdfp_id > 0),
    fetch_run_id uuid REFERENCES filament_profile_fetches (id) ON DELETE SET NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    blob_key text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    filament_type text,
    filament_colour text,
    compatible_printers text[] NOT NULL DEFAULT '{}',
    bb_preset_id bigint UNIQUE,
    bb_updated_at text,
    mirrored_sha256 text,
    mirror_error text,
    mirror_error_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One fetched preset per type, printer model and nozzle: a re-fetch versions it.
CREATE UNIQUE INDEX filament_presets_fetched ON filament_presets
    (type_id, printer_model, nozzle_diameter) WHERE source = '3dfp';

CREATE TABLE filament_preset_versions (
    preset_id bigint NOT NULL REFERENCES filament_presets (id) ON DELETE CASCADE,
    version integer NOT NULL CHECK (version > 0),
    blob_key text NOT NULL,
    sha256 text NOT NULL,
    fetch_run_id uuid REFERENCES filament_profile_fetches (id) ON DELETE SET NULL,
    bb_updated_at text,
    made_by text,
    made_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (preset_id, version)
);

-- A binding names a ScadBuddy preset by the ref `sb-<id>` in `slicer_filament` (text, as
-- every other ref is); `preset_id` is that id, and its foreign key keeps the ref real.
ALTER TABLE filament_spool_type_presets ADD COLUMN preset_id bigint
    GENERATED ALWAYS AS (
        CASE WHEN slicer_filament ~ '^sb-[1-9][0-9]{0,17}$'
             THEN substr(slicer_filament, 4)::bigint END
    ) STORED REFERENCES filament_presets (id);
ALTER TABLE filament_spool_presets ADD COLUMN preset_id bigint
    GENERATED ALWAYS AS (
        CASE WHEN slicer_filament ~ '^sb-[1-9][0-9]{0,17}$'
             THEN substr(slicer_filament, 4)::bigint END
    ) STORED REFERENCES filament_presets (id);
CREATE INDEX filament_spool_type_presets_preset ON filament_spool_type_presets (preset_id)
    WHERE preset_id IS NOT NULL;
CREATE INDEX filament_spool_presets_preset ON filament_spool_presets (preset_id)
    WHERE preset_id IS NOT NULL;
