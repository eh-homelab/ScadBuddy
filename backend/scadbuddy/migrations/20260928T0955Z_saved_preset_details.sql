-- #327: a saved preset's description (short Markdown) and tags, as a template's
-- own presets carry in model.json. Empty for every preset saved before this.
ALTER TABLE saved_presets
    ADD COLUMN description text NOT NULL DEFAULT '',
    ADD COLUMN tags text[] NOT NULL DEFAULT '{}';
