-- #959: what an asset is beyond its bytes. An SVG's viewBox (min-x, min-y, width,
-- height) and its count of <path> elements; for a fetched asset, the URL it came from
-- (never its query) and the licence the fetch named ({spdx, name, url, author}). Rows
-- stored before this have none of them.
ALTER TABLE assets
    ADD COLUMN view_box double precision[]
        CHECK (view_box IS NULL OR cardinality(view_box) = 4),
    ADD COLUMN path_count integer CHECK (path_count IS NULL OR path_count >= 0),
    ADD COLUMN source_url text,
    ADD COLUMN licence jsonb CHECK (licence IS NULL OR jsonb_typeof(licence) = 'object');
