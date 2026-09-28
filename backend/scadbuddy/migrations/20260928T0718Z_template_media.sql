-- #274: a template's images and videos, in order; the files are in
-- `models/<slug>/media/`. Built-ins read theirs from the bundled model.json.
CREATE TABLE template_media (
    template_id text        NOT NULL,
    id          text        NOT NULL,
    position    integer     NOT NULL,
    file        text        NOT NULL,
    kind        text        NOT NULL CHECK (kind IN ('image', 'video')),
    caption     text        NOT NULL DEFAULT '' CHECK (char_length(caption) <= 1000),
    poster      text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (template_id, id),
    UNIQUE (template_id, position) DEFERRABLE INITIALLY DEFERRED
);
