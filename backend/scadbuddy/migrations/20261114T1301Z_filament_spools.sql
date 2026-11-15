-- ScadBuddy's own filament inventory, part 2: spool types, spools, tags and the ledger
-- (#6261, epic #6260, spec 2026-11-14-filament-management-design.md §5). Needs the
-- catalogs of `20261114T1300Z_filament_catalogs.sql`.

-- A spool type is a product: what a maker sells under one code. A spool takes its
-- values from its type and may override each one.
CREATE TABLE filament_spool_types (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL,
    brand_id uuid REFERENCES filament_brands (id),
    material_id uuid NOT NULL REFERENCES filament_materials (id),
    subtype_id uuid REFERENCES filament_subtypes (id),
    category_id uuid REFERENCES filament_categories (id),
    effect_id uuid REFERENCES filament_effects (id),
    colour_name text,
    rgba text NOT NULL CHECK (rgba ~ '^#[0-9A-F]{8}$'),
    extra_colours text[],
    diameter_mm numeric(4,2) NOT NULL DEFAULT 1.75,
    density_g_cm3 numeric(6,3) CHECK (density_g_cm3 > 0),
    nozzle_temp_min integer,
    nozzle_temp_max integer,
    bed_temp_min integer,
    bed_temp_max integer,
    label_weight_g integer NOT NULL CHECK (label_weight_g > 0),
    insert_type_id uuid REFERENCES filament_insert_types (id),
    low_stock_pct integer CHECK (low_stock_pct BETWEEN 0 AND 100),
    cost_per_kg numeric(12,4) CHECK (cost_per_kg >= 0),
    filament_id text REFERENCES filaments (id),
    note text,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_spool_types_code ON filament_spool_types (upper(code))
    WHERE archived_at IS NULL;

-- Slicer filament presets per printer model and nozzle, and pressure-advance (K)
-- values per printer, for a type.
CREATE TABLE filament_spool_type_presets (
    type_id uuid NOT NULL REFERENCES filament_spool_types (id) ON DELETE CASCADE,
    printer_model text NOT NULL,
    nozzle_diameter text NOT NULL,
    slicer_filament text NOT NULL,
    slicer_filament_name text,
    PRIMARY KEY (type_id, printer_model, nozzle_diameter)
);

CREATE TABLE filament_spool_type_k_values (
    type_id uuid NOT NULL REFERENCES filament_spool_types (id) ON DELETE CASCADE,
    printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
    extruder integer NOT NULL,
    nozzle_diameter text NOT NULL,
    nozzle_type text,
    k_value numeric(6,4) NOT NULL,
    name text,
    cali_idx integer,
    setting_id text,
    PRIMARY KEY (type_id, printer_id, extruder, nozzle_diameter)
);

-- A spool is one physical reel. `type_id` is null for a spool that still needs a type;
-- it then names its own material. Every overridable column is null until set.
-- `number` is the label a person reads. The columns from `grams_left` on are derived
-- from the ledger and the scale; the ledger is the record.
CREATE TABLE filament_spools (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    number integer GENERATED ALWAYS AS IDENTITY UNIQUE,
    type_id uuid REFERENCES filament_spool_types (id),
    brand_id uuid REFERENCES filament_brands (id),
    material_id uuid REFERENCES filament_materials (id),
    subtype_id uuid REFERENCES filament_subtypes (id),
    category_id uuid REFERENCES filament_categories (id),
    effect_id uuid REFERENCES filament_effects (id),
    colour_name text,
    rgba text CHECK (rgba ~ '^#[0-9A-F]{8}$'),
    extra_colours text[],
    diameter_mm numeric(4,2),
    density_g_cm3 numeric(6,3) CHECK (density_g_cm3 > 0),
    nozzle_temp_min integer,
    nozzle_temp_max integer,
    bed_temp_min integer,
    bed_temp_max integer,
    label_weight_g integer CHECK (label_weight_g > 0),
    insert_type_id uuid REFERENCES filament_insert_types (id),
    low_stock_pct integer CHECK (low_stock_pct BETWEEN 0 AND 100),
    cost_per_kg numeric(12,4) CHECK (cost_per_kg >= 0),
    location_id uuid REFERENCES filament_locations (id),
    weight_locked boolean NOT NULL DEFAULT false,
    note text,
    grams_left double precision,
    last_weighed_g double precision,
    last_weighed_at timestamptz,
    last_dried_at timestamptz,
    last_dried_temp_c integer,
    last_dried_hours numeric(5,2),
    last_used_at timestamptz,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT filament_spools_typed CHECK (type_id IS NOT NULL OR material_id IS NOT NULL)
);

CREATE TABLE filament_spool_presets (
    spool_id uuid NOT NULL REFERENCES filament_spools (id) ON DELETE CASCADE,
    printer_model text NOT NULL,
    nozzle_diameter text NOT NULL,
    slicer_filament text NOT NULL,
    slicer_filament_name text,
    PRIMARY KEY (spool_id, printer_model, nozzle_diameter)
);

CREATE TABLE filament_spool_k_values (
    spool_id uuid NOT NULL REFERENCES filament_spools (id) ON DELETE CASCADE,
    printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
    extruder integer NOT NULL,
    nozzle_diameter text NOT NULL,
    nozzle_type text,
    k_value numeric(6,4) NOT NULL,
    name text,
    cali_idx integer,
    setting_id text,
    PRIMARY KEY (spool_id, printer_id, extruder, nozzle_diameter)
);

-- A tag (a Bambu RFID tag, an OpenSpool or OpenTag NTAG) is read from a spool. It names
-- itself by its UID, its tray UUID, or both.
CREATE TABLE filament_tags (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL
        CHECK (kind IN ('bambu_mifare', 'ntag_openspool', 'ntag_opentag', 'other')),
    tag_uid text,
    tray_uuid text,
    raw_blob text,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (tag_uid IS NOT NULL OR tray_uuid IS NOT NULL)
);
CREATE UNIQUE INDEX filament_tags_tag_uid ON filament_tags (tag_uid) WHERE tag_uid IS NOT NULL;
CREATE UNIQUE INDEX filament_tags_tray_uuid ON filament_tags (tray_uuid)
    WHERE tray_uuid IS NOT NULL;

-- A tag is bound to one spool at a time and a spool holds one tag at a time. A binding
-- ends by `unbound_at`; the row stays as history.
CREATE TABLE filament_tag_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tag_id uuid NOT NULL REFERENCES filament_tags (id),
    spool_id uuid NOT NULL REFERENCES filament_spools (id),
    override boolean NOT NULL DEFAULT false,
    bound_at timestamptz NOT NULL DEFAULT now(),
    unbound_at timestamptz,
    reason text
);
CREATE UNIQUE INDEX filament_tag_bindings_open_tag ON filament_tag_bindings (tag_id)
    WHERE unbound_at IS NULL;
CREATE UNIQUE INDEX filament_tag_bindings_open_spool ON filament_tag_bindings (spool_id)
    WHERE unbound_at IS NULL;

-- The ledger: one row per thing that happened to a spool. No cascade: a spool with
-- events is archived, never deleted. `printer_id` is a plain uuid so that deleting a
-- printer leaves the record whole. A row with a `ref_id` (a print run, a sync) is
-- recorded once per `kind`, so a retried activity cannot book grams twice. `at` is when
-- it physically happened, and `seq` the order it was booked in, which orders two events
-- of one time (`filament/ledger.py`).
CREATE TABLE filament_spool_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seq bigint GENERATED ALWAYS AS IDENTITY,
    spool_id uuid NOT NULL REFERENCES filament_spools (id),
    at timestamptz NOT NULL DEFAULT now(),
    kind text NOT NULL CHECK (kind IN (
        'created', 'loaded', 'unloaded', 'moved', 'print_started', 'print_ended', 'weighed',
        'ams_reading', 'dried', 'edited', 'tag_bound', 'tag_unbound', 'archived', 'restored',
        'synced_from_bb'
    )),
    source text CHECK (source IN (
        'ams_remain', 'scale', 'print_estimate', 'print_actual', 'person', 'bb'
    )),
    printer_id uuid,
    ams_id integer,
    tray_id integer,
    grams_before double precision,
    grams_after double precision,
    delta_g double precision,
    ref_kind text,
    ref_id text,
    actor text,
    data jsonb NOT NULL DEFAULT '{}',
    CHECK (ref_id IS NULL OR ref_kind IS NOT NULL)
);
CREATE INDEX filament_spool_events_spool_at ON filament_spool_events (spool_id, at);
CREATE UNIQUE INDEX filament_spool_events_once
    ON filament_spool_events (spool_id, ref_kind, ref_id, kind) WHERE ref_id IS NOT NULL;

-- The link of a spool to its record in Bambuddy, for the sync.
CREATE TABLE filament_bb_links (
    spool_id uuid PRIMARY KEY REFERENCES filament_spools (id),
    bb_spool_id integer NOT NULL UNIQUE,
    bb_updated_at timestamptz,
    pushed_hash text,
    linked_at timestamptz NOT NULL DEFAULT now()
);
