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
    -- When the copy from the files last wrote the row.
    copied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX outputs_slug_created ON outputs (slug, created_at);
CREATE INDEX outputs_backfill ON outputs (output_id) WHERE backfill IS NOT NULL;

-- One row per run of the copy from the files into `outputs`.
CREATE TABLE output_copy_runs (
    id bigserial PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    copied int NOT NULL DEFAULT 0,
    refreshed int NOT NULL DEFAULT 0,
    dropped int NOT NULL DEFAULT 0,
    not_copied jsonb NOT NULL DEFAULT '[]',
    damaged jsonb NOT NULL DEFAULT '[]'
);
