-- ScadBuddy's own filament inventory, part 1: the catalogs a spool type or a spool picks
-- from (#6261, epic #6260, spec 2026-11-14-filament-management-design.md §5). Each
-- catalog row is a person's to edit; "removing" one sets `archived_at`, which frees its
-- name (the unique index covers unarchived rows only). Names compare case-blind.

-- Materials carry the print and drying defaults a spool type inherits and the handling
-- flags the print flow checks (enclosure, hardened nozzle).
CREATE TABLE filament_materials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    family text,
    density_g_cm3 numeric(6,3) CHECK (density_g_cm3 > 0),
    nozzle_temp_min integer,
    nozzle_temp_max integer,
    bed_temp_min integer,
    bed_temp_max integer,
    drying_temp_c integer,
    drying_hours numeric(5,2),
    needs_enclosure boolean NOT NULL DEFAULT false,
    abrasive boolean NOT NULL DEFAULT false,
    hygroscopic boolean NOT NULL DEFAULT false,
    flexible boolean NOT NULL DEFAULT false,
    hardened_nozzle boolean NOT NULL DEFAULT false,
    note text,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_materials_name ON filament_materials (upper(name))
    WHERE archived_at IS NULL;

-- A subtype (Basic, Matte, Silk) may belong to one material or stand for all of them.
CREATE TABLE filament_subtypes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    material_id uuid REFERENCES filament_materials (id),
    density_g_cm3 numeric(6,3) CHECK (density_g_cm3 > 0),
    nozzle_temp_min integer,
    nozzle_temp_max integer,
    bed_temp_min integer,
    bed_temp_max integer,
    drying_temp_c integer,
    drying_hours numeric(5,2),
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_subtypes_name ON filament_subtypes (coalesce(material_id, '00000000-0000-0000-0000-000000000000'::uuid), upper(name))
    WHERE archived_at IS NULL;

CREATE TABLE filament_brands (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_brands_name ON filament_brands (upper(name))
    WHERE archived_at IS NULL;

CREATE TABLE filament_categories (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_categories_name ON filament_categories (upper(name))
    WHERE archived_at IS NULL;

CREATE TABLE filament_effects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_effects_name ON filament_effects (upper(name))
    WHERE archived_at IS NULL;

-- The material of a spool's core or reel insert, and the insert types made of it.
CREATE TABLE filament_insert_materials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_insert_materials_name ON filament_insert_materials (upper(name))
    WHERE archived_at IS NULL;

CREATE TABLE filament_insert_types (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    empty_weight_g integer NOT NULL CHECK (empty_weight_g > 0),
    insert_material_id uuid REFERENCES filament_insert_materials (id),
    note text,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_insert_types_name ON filament_insert_types (upper(name))
    WHERE archived_at IS NULL;

-- A named colour of a maker: `rgba` is `#RRGGBBAA`, upper case.
CREATE TABLE filament_colours (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    manufacturer text,
    rgba text NOT NULL CHECK (rgba ~ '^#[0-9A-F]{8}$'),
    material_id uuid REFERENCES filament_materials (id),
    extra_colours text[],
    effect_id uuid REFERENCES filament_effects (id),
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_colours_name ON filament_colours (coalesce(upper(manufacturer), ''), upper(name))
    WHERE archived_at IS NULL;

-- Kinds of place a spool is kept. `ams` marks a printer's AMS; `dry_storage` a dry box.
CREATE TABLE filament_location_kinds (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    dry_storage boolean NOT NULL DEFAULT false,
    ams boolean NOT NULL DEFAULT false,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_location_kinds_name ON filament_location_kinds (upper(name))
    WHERE archived_at IS NULL;

-- Places form a tree (room, shelf, bin). An AMS tray is a place too: it names its printer,
-- AMS unit and tray, once, and its name is only a label: names are unique among the places
-- with no printer. Deleting a printer deletes its trays.
CREATE TABLE filament_locations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    kind_id uuid NOT NULL REFERENCES filament_location_kinds (id),
    parent_id uuid REFERENCES filament_locations (id),
    identifier text,
    capacity integer CHECK (capacity > 0),
    printer_id uuid REFERENCES printers (id) ON DELETE CASCADE,
    ams_id integer,
    tray_id integer,
    note text,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_locations_name ON filament_locations (coalesce(parent_id, '00000000-0000-0000-0000-000000000000'::uuid), upper(name))
    WHERE archived_at IS NULL AND printer_id IS NULL;

CREATE UNIQUE INDEX filament_locations_tray ON filament_locations (printer_id, ams_id, tray_id)
    WHERE printer_id IS NOT NULL AND archived_at IS NULL;

-- Seeds: the starting set. A person edits them.
INSERT INTO filament_materials
    (name, sort_order, hygroscopic, abrasive, hardened_nozzle, needs_enclosure, flexible)
VALUES
    ('PLA', 10, false, false, false, false, false),
    ('PETG', 20, true, false, false, false, false),
    ('PETG-CF', 30, false, true, true, false, false),
    ('ABS', 40, false, false, false, true, false),
    ('ASA', 50, false, false, false, true, false),
    ('TPU', 60, true, false, false, false, true),
    ('PA', 70, true, false, false, false, false),
    ('PA-CF', 75, true, true, true, false, false),
    ('PC', 80, false, false, false, true, false);

INSERT INTO filament_subtypes (name, sort_order) VALUES
    ('Basic', 10),
    ('Matte', 20),
    ('Silk', 30),
    ('HF', 40),
    ('Translucent', 50),
    ('Dual Color', 60),
    ('Tri Color', 70),
    ('Plus', 80);

INSERT INTO filament_effects (name, sort_order) VALUES
    ('Sparkle', 10),
    ('Wood', 20),
    ('Marble', 30),
    ('Glow', 40),
    ('Matte', 50),
    ('Silk', 60),
    ('Galaxy', 70),
    ('Rainbow', 80),
    ('Metal', 90),
    ('Translucent', 100),
    ('Gradient', 110),
    ('Dual Color', 120),
    ('Tri Color', 130),
    ('Multicolor', 140);

INSERT INTO filament_insert_materials (name, sort_order) VALUES
    ('Cardboard', 10),
    ('Plastic', 20),
    ('Bambu reusable', 30);

INSERT INTO filament_location_kinds (name, sort_order, dry_storage, ams) VALUES
    ('Room', 10, false, false),
    ('Shelf', 20, false, false),
    ('Bin', 30, false, false),
    ('Dry box', 40, true, false),
    ('AMS', 50, false, true),
    ('Drawer', 60, false, false);
