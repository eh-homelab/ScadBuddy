-- The colour catalog's sources (#6907, plan 2026-11-18-filament-bb-sync.md Decisions
-- 206-218): Bambuddy's colour catalog (`GET /inventory/colors`), filamentcolors.xyz and
-- Bambu Studio's colour codes fill `filament_colours`.
--
-- `material_name` is the material as the source names it ("PLA Basic", "Silk PLA"); a row
-- is one maker's colour of one material, so the same name of two materials is two rows
-- (Bambu Lab's "Black" is a PLA Basic, a PLA Matte and a PETG HF colour). `source` is the
-- sync that made or last wrote the row (NULL: a person's). `synced_at` is that write's
-- time: a person's write of a value a sync writes clears `synced_at`, and no sync writes
-- a row whose `synced_at` is NULL (Decision 217). `bb_colour_id` links the row to
-- Bambuddy's colour catalog entry.
ALTER TABLE filament_colours
    ADD COLUMN material_name text,
    ADD COLUMN source text CHECK (source IN ('bambuddy', 'filamentcolors', 'bambu')),
    ADD COLUMN synced_at timestamptz,
    ADD COLUMN bb_colour_id integer UNIQUE;

DROP INDEX filament_colours_name;
CREATE UNIQUE INDEX filament_colours_name ON filament_colours
    (coalesce(upper(manufacturer), ''), upper(name), coalesce(upper(material_name), ''))
    WHERE archived_at IS NULL;

-- One run of a colour sync (filamentcolors.xyz or Bambu's colour codes): its progress while
-- it runs and its counts once it ended. `db_last_modified` is filamentcolors' own
-- (`GET /api/version/`): a run that finds it unchanged since the last finished run reads no
-- page (Decision 211).
CREATE TABLE filament_colour_syncs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (source IN ('filamentcolors', 'bambu')),
    state text NOT NULL DEFAULT 'running'
        CHECK (state IN ('running', 'done', 'unchanged', 'failed')),
    pages_read integer NOT NULL DEFAULT 0,
    pages_total integer,
    total integer,
    added integer NOT NULL DEFAULT 0,
    updated integer NOT NULL DEFAULT 0,
    unchanged integer NOT NULL DEFAULT 0,
    kept integer NOT NULL DEFAULT 0,
    skipped integer NOT NULL DEFAULT 0,
    error text,
    db_last_modified bigint,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    -- The operation run that started it: a retry of that run ends its own dead row.
    operation_id text
);
CREATE INDEX filament_colour_syncs_newest ON filament_colour_syncs (source, started_at DESC);
