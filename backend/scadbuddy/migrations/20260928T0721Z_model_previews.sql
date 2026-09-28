-- #454: default-render previews (`library.previews.PreviewStore`). One row per
-- model id, the record and its image together: a rendered row always has its PNG
-- and a failed one never does, so the two cannot disagree.
CREATE TABLE model_previews (
    model_id    text PRIMARY KEY,
    source_key  text NOT NULL,
    ok          boolean NOT NULL,
    error       text,
    png         bytea,
    rendered_at timestamptz NOT NULL,
    CONSTRAINT model_previews_image_iff_ok CHECK (ok = (png IS NOT NULL))
);
