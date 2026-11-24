-- Saved outputs as rows (#7255, outputs-in-Postgres spec 2026-11-23 §4.2): one row per
-- output, holding what its `meta.json`, `params.json`, `inputs.json`, `record.json`,
-- `manifest.json`, `bom.json` and `backfill.json` hold today. The files still keep the
-- bytes until #7256. The last print is not here: it stays in `output_last_prints`.
CREATE TABLE outputs (
    output_id text PRIMARY KEY,
    slug text NOT NULL,
    -- The `OutputMeta` JSON, without the last-print overlay.
    meta jsonb NOT NULL,
    params jsonb NOT NULL DEFAULT '{}',
    -- NULL is a record from before inputs.
    inputs jsonb,
    record jsonb,
    manifest jsonb,
    bom jsonb,
    arranged_from jsonb,
    backfill jsonb,
    has_plate_cover boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL,
    -- When a writer last wrote the row: the copy from the files, or a save, rename or
    -- backfill. The copy writes a row only where this is older than its own mark.
    copied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- The copy's stamp of the record files it read (names, sizes, mtimes). NULL for a
    -- row a save wrote. The copy reads a folder again when its stamp differs.
    files_stamp text
);

CREATE INDEX outputs_slug_created ON outputs (slug, created_at);
CREATE INDEX outputs_backfill ON outputs (output_id) WHERE backfill IS NOT NULL;

-- One row per run of the copy from the files into `outputs`.
CREATE TABLE output_copy_runs (
    id bigserial PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- Set only by a run whose walk saw every folder: the first one ends the fallback
    -- to the files.
    finished_at timestamptz,
    -- True for a run that walked to its end but may have missed folders (a listing
    -- failed, or the outputs folder read empty while rows exist). It has no
    -- `finished_at`.
    incomplete boolean NOT NULL DEFAULT false,
    copied int NOT NULL DEFAULT 0,
    refreshed int NOT NULL DEFAULT 0,
    dropped int NOT NULL DEFAULT 0,
    not_copied jsonb NOT NULL DEFAULT '[]',
    damaged jsonb NOT NULL DEFAULT '[]'
);
