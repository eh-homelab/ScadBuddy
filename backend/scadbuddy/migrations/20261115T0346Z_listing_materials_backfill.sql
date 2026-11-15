-- #6258, #6302 review: a listing with no materials of its own takes the shop's default
-- (`storefront_materials`, PETG and PLA). A listing made before #6258 can offer filaments
-- of other materials (ABS, TPU): on deploy those colours would go, and the listing could
-- become unavailable. So each such listing gets the materials of the filaments it offers:
-- compared casefolded, the spelling of the first filament in the listing's `colours`
-- kept (as the agent's `storefrontDraft.ts` `draftMaterials` does), support materials
-- left out (the pattern of `orders/storefront/wire.py` `SUPPORT_MATERIAL`), PETG, then
-- PLA, then the rest by name (byte order, as `draftMaterials` compares), at most 16
-- (`core/settings.py` `MAX_MATERIALS`), each at most 64 characters
-- (`MAX_MATERIAL_LENGTH`). Postgres `lower()` stands in for Python's
-- `casefold()`: they differ only for a few non-ASCII names (`ß`, say), and
-- `ListingInput` drops repeats casefolded at the listing's next save. A listing with
-- materials, or with no such filament, is left alone. `updated_at` stays, so an open
-- page does not read `listing_changed`.
UPDATE storefront_listings AS l
SET materials = found.materials
FROM (
  SELECT
    listing_id,
    (array_agg(spelling ORDER BY
      CASE folded WHEN 'petg' THEN 0 WHEN 'pla' THEN 1 ELSE 2 END, folded COLLATE "C"))[1:16] AS materials
  FROM (
    SELECT DISTINCT ON (l.id, lower(btrim(f.material)))
      l.id AS listing_id,
      lower(btrim(f.material)) AS folded,
      btrim(f.material) AS spelling
    FROM storefront_listings l
    CROSS JOIN LATERAL unnest(l.colours) WITH ORDINALITY AS c(filament_id, place)
    JOIN filaments f ON f.id = c.filament_id
    WHERE l.materials = '{}'
      AND btrim(f.material) <> ''
      AND length(btrim(f.material)) <= 64
      AND f.material !~* '(support|pva|bvoh|hips)'
    ORDER BY l.id, lower(btrim(f.material)), c.place
  ) AS each_material
  GROUP BY listing_id
) AS found
WHERE l.id = found.listing_id
  AND l.materials = '{}';
