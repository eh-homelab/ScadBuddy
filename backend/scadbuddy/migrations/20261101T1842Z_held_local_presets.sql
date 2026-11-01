-- Bambuddy's local presets (`GET /local-presets/`, OrcaSlicer imports) as last kept
-- (#4645, hot-path plan 2026-10-18 D5): what a native run offers and slices them from,
-- with no Bambuddy call. A cache, rewritten only by the refresh on the spool providers'
-- Schedule (`slicing/held_presets.py` `refresh_held_presets`). `preset` is Bambuddy's
-- `GET /local-presets/{id}` answer whole (its row and its `setting` document);
-- `updated_at` is that row's own stamp as Bambuddy sends it, which a refresh compares to
-- read only the presets edited since; `sha256` is the document's, so a reader can tell
-- an edit without reading it.
CREATE TABLE held_local_presets (
    id bigint PRIMARY KEY,
    preset jsonb NOT NULL,
    updated_at text,
    sha256 text NOT NULL,
    kept_at timestamptz NOT NULL DEFAULT now()
);
