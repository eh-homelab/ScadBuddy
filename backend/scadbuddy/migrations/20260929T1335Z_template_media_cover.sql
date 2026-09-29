-- #722: the cover a built-in's media overlay chooses. A built-in's list is the
-- media it ships (its bundled model.json, read-only) followed by the media people
-- added to it (`template_media` rows under the built-in's id, the files in
-- `builtin-media/<slug>/`). The shipped items come first, so the cover is chosen
-- here rather than by position: `item_id` names a shipped or an added item, which
-- is then listed first. One naming no item (a shipped item a newer image dropped)
-- is ignored on read.
CREATE TABLE template_media_cover (
    template_id text        PRIMARY KEY,
    item_id     text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
