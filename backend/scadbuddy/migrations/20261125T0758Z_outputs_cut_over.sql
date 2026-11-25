-- The outputs cut over from NFS, release A (#7257, outputs-in-Postgres spec 2026-11-23
-- §4.2, §4.5 phase 3). `outputs` takes the columns the list reads from `output_index`,
-- so `GET /outputs` can read `outputs` alone. `name` and `source` follow `meta`, so a
-- pod of an older build that writes `meta` keeps them right. `params_text` is the
-- parameters that differ from the template's defaults, as `name=value` words, for the
-- `q` filter. The defaults come from the template's cached schema, not from the row, so
-- it is a plain column: a save and `OutputIndex.record` write it. A row that an older
-- build saves during the roll has none until the next release's migration fills it.
ALTER TABLE outputs
    ADD COLUMN name text GENERATED ALWAYS AS (meta->>'name') STORED,
    ADD COLUMN source text GENERATED ALWAYS AS (COALESCE(meta->>'source', 'back_office')) STORED,
    ADD COLUMN params_text text;

UPDATE outputs o SET params_text = x.params_text
FROM output_index x
WHERE x.output_id = o.output_id AND x.slug = o.slug;

CREATE INDEX outputs_created ON outputs (created_at DESC, output_id DESC);

-- The cut-over mark: one row, written once, when a row copy run finished, the last byte
-- run finished with no refusal and no failure, and no row waits for its bytes
-- (`OutputRows.try_cut_over`). After it the copies stop, and the next release's
-- migration refuses to run without it. The key is `one`, since ONLY is a reserved word.
CREATE TABLE output_cutover (
    one boolean PRIMARY KEY DEFAULT true CHECK (one),
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    byte_run_id bigint NOT NULL REFERENCES output_byte_runs
);

-- An output id a save has claimed and not saved yet (a quote's attempts, #5390): the
-- claim of the folder moves here. A save's row deletes its claim in its transaction; the
-- hold reaper deletes claims older than its grace.
CREATE TABLE output_claims (
    output_id text PRIMARY KEY,
    slug text NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
