-- #332: the presets saved on a template, which were one JSON file per template
-- under data/presets/ (left there: nothing is copied over). `position` keeps them
-- in the order they were saved. Names are unique per template ignoring case,
-- checked by the store under the template's advisory lock -- against the
-- template's own presets too, which no index here can see.
CREATE TABLE saved_presets (
    model_id   text NOT NULL,
    id         text NOT NULL,
    position   bigint GENERATED ALWAYS AS IDENTITY,
    name       text NOT NULL,
    params     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (model_id, id)
);
CREATE INDEX saved_presets_order ON saved_presets (model_id, position);
