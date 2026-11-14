-- #6209: a filament's whole colour as Bambuddy keeps it beside `rgba`
-- (backend/app/models/spool.py at
-- https://github.com/maziggy/bambuddy/blob/1aa7bce166682fde929fec0d535bdaaa22685aad/backend/app/models/spool.py).
-- `colour` stays `#RRGGBB`, so every reader and the render's colour split stay as they
-- are; a clear filament keeps its RGB (`#000000`) and gets `alpha` 0.
--
-- `alpha`: the `rgba` alpha byte, 0 to 255; 255 is opaque.
-- `extra_colours`: Bambuddy's `extra_colors` stops (`normalize_extra_colors`: at most 8,
--   each 6 or 8 hex digits), as `#RRGGBB` or `#RRGGBBAA`.
-- `effect`: Bambuddy's `effect_type` (`ALLOWED_EFFECT_TYPES` in
--   backend/app/schemas/spool.py at the same commit).
-- `colour_source`: `import` when the spool import set the colour (and may set it again),
--   `manual` when a person set it (the import never overwrites it).
-- `spool_colour` and `spool_ids`: the spools' `#RRGGBB` and Bambuddy ids at the last
--   import, which match a record to its spools after a person changes its colour.
ALTER TABLE filaments
  ADD COLUMN alpha smallint NOT NULL DEFAULT 255 CHECK (alpha BETWEEN 0 AND 255),
  ADD COLUMN extra_colours text[] NOT NULL DEFAULT '{}' CHECK (
    cardinality(extra_colours) <= 8
    AND array_to_string(extra_colours, ',') ~ '^(#[0-9A-F]{6}([0-9A-F]{2})?(,|$))*$'
  ),
  ADD COLUMN effect text CHECK (effect IN (
    'sparkle', 'wood', 'marble', 'glow', 'matte', 'silk', 'galaxy', 'rainbow', 'metal',
    'translucent', 'gradient', 'dual-color', 'tri-color', 'multicolor'
  )),
  ADD COLUMN colour_source text NOT NULL DEFAULT 'import'
    CHECK (colour_source IN ('import', 'manual')),
  ADD COLUMN spool_colour text CHECK (spool_colour ~ '^#[0-9A-F]{6}$'),
  ADD COLUMN spool_ids integer[] NOT NULL DEFAULT '{}';

-- Every record so far but the two below keeps `colour_source` `import` and no link
-- (`spool_colour` NULL, `spool_ids` empty): the next import finds it by its own `colour`,
-- links it and fills its alpha, stops and effect. A record a person added by hand has no spools, so a link copied from its colour
-- would keep its old colour after a later colour fix (#6211 review).
--
-- Two records a person set by hand to an opaque stand-in for a clear spool, which the
-- import had made `#000000` (prod, 2026-11-14): their colour stays, and the import still
-- finds them by the spools' `#000000`: they get `colour_source` `manual` and
-- `spool_colour` `#000000` (`spool_ids` stays empty). This is in the migration, not a manual UPDATE or a
-- PATCH, because no route sets `spool_colour`, and without it the next import would add a
-- second, `#000000` record for each. Each guard is its id and its stand-in colour, so
-- elsewhere this is a no-op.
UPDATE filaments SET colour_source = 'manual', spool_colour = '#000000'
WHERE (id = 'a324599b-86dd-45fe-8408-579491da2f59' AND upper(colour) = '#EEF3F4')
   OR (id = 'c7c22e72-76a2-4cca-b74a-793ba5e87c29' AND upper(colour) = '#E3ECEF');

ALTER TABLE filaments ALTER COLUMN colour_source SET DEFAULT 'manual';
