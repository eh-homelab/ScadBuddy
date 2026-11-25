-- Saved outputs' bytes in the blob store (#7256, outputs-in-Postgres spec 2026-11-23
-- §4.2, §4.3). Each byte role of an output (`model.3mf`, `preview.glb`,
-- `thumbnail.png`) is a blob of kind `output`, keyed `output-<output_id>-<role>`, and
-- each file under `files/` is one keyed `output-<output_id>-file-<hash>`. The row holds the
-- keys; `blob_refs` holds the blobs for the row. The files keep the bytes as the
-- fallback until #7257.
ALTER TABLE outputs
    -- The blob keys. NULL when the role has no blob yet.
    ADD COLUMN model_blob text,
    ADD COLUMN preview_blob text,
    ADD COLUMN thumbnail_blob text,
    -- The byte copy's stamp of the byte files it read. NULL for a save.
    ADD COLUMN bytes_stamp text,
    -- Set by a save that put its blobs, or by the copy that wrote and verified them.
    -- NULL: the files still hold the bytes.
    ADD COLUMN bytes_at timestamptz,
    -- `[{role, error}]` for the byte roles whose file was missing. Empty when none.
    ADD COLUMN damaged jsonb NOT NULL DEFAULT '[]';

CREATE INDEX outputs_bytes_pending ON outputs (output_id) WHERE bytes_at IS NULL;

-- An output's `files/`: one row per file, by name.
CREATE TABLE output_files (
    output_id text NOT NULL REFERENCES outputs ON DELETE CASCADE,
    name text NOT NULL,
    blob_key text NOT NULL,
    size bigint NOT NULL,
    PRIMARY KEY (output_id, name)
);

-- What a reader derives from an output's model, per plate (the geometry analysis, the
-- attributed preview), and the version of the code that derived it. Plate 0 is the whole
-- output: the asset ids its 3MF names (`asset_ids`), its plate names (`plate_names`), and
-- its plate cover (`plate_cover`: the key of the cover's blob, or none when the 3MF has no
-- cover; the image itself is a blob, never a row).
CREATE TABLE output_derived (
    output_id text NOT NULL REFERENCES outputs ON DELETE CASCADE,
    kind text NOT NULL
        CHECK (kind IN ('geometry', 'attributed', 'asset_ids', 'plate_names', 'plate_cover')),
    plate int NOT NULL,
    version text NOT NULL,
    data jsonb NOT NULL,
    PRIMARY KEY (output_id, kind, plate)
);

-- One row per run of the byte copy from the files into the blob store.
CREATE TABLE output_byte_runs (
    id bigserial PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- Set only by a run that copied every output's bytes.
    finished_at timestamptz,
    -- True for a run whose walk may have missed folders. It has no `finished_at`.
    incomplete boolean NOT NULL DEFAULT false,
    -- Why the run copied nothing (a `bambuddy` or `local` store, a store past its cap).
    refused text,
    copied int NOT NULL DEFAULT 0,
    verified int NOT NULL DEFAULT 0,
    skipped int NOT NULL DEFAULT 0,
    -- What the copy needed to upload, checked against the store's quota first.
    needed_bytes bigint NOT NULL DEFAULT 0,
    needed_count int NOT NULL DEFAULT 0,
    failed jsonb NOT NULL DEFAULT '[]',
    damaged jsonb NOT NULL DEFAULT '[]'
);
