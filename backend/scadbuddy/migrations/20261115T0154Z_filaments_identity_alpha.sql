-- #6261: a price book record's identity holds its alpha, so an opaque and a translucent
-- spool type of one hue (`filament_spool_types.rgba` `#RRGGBBFF` and `#RRGGBB80`) each
-- link to a record of their own (`filament/types.py` `_link_price_book`). `colour` stays
-- `#RRGGBB` (#6209, `20261114T1607Z_filaments_alpha_effect.sql`). Every record so far
-- keeps its identity: the old index already kept two records from sharing the other four.
DROP INDEX filaments_identity;
CREATE UNIQUE INDEX filaments_identity ON filaments (
  upper(material), upper(coalesce(subtype, '')), upper(coalesce(brand, '')), upper(colour),
  alpha
) WHERE archived_at IS NULL;
