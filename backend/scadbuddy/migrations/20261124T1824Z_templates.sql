-- Model records in Postgres, phase 1 (#7317; spec
-- docs/superpowers/specs/2026-11-25-model-records-in-postgres-design.md §5.1).
-- `templates` is one derived row per template folder that has a `model.scad`.
-- `model.json` in git stays the record: any row can be rebuilt from the tree.
-- `template_copy_runs` is one row per copy run, as `output_copy_runs` (#7360).

CREATE TABLE templates (
    template_id text PRIMARY KEY,
    origin text NOT NULL CHECK (origin IN ('mine', 'builtin')),
    meta jsonb,
    meta_error text,
    meta_text text,
    name text NOT NULL,
    version text,
    updated_at timestamptz NOT NULL,
    has_readme boolean NOT NULL,
    inputs_version bigint NOT NULL DEFAULT 0,
    colour_params text[],
    files jsonb NOT NULL DEFAULT '{}',
    cover jsonb,
    files_stamp text NOT NULL,
    refreshed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    refreshed_head text
);

CREATE TABLE template_copy_runs (
    id bigserial PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    incomplete boolean NOT NULL DEFAULT false,
    added int NOT NULL DEFAULT 0,
    refreshed int NOT NULL DEFAULT 0,
    dropped int NOT NULL DEFAULT 0,
    unreadable jsonb NOT NULL DEFAULT '[]'
);
