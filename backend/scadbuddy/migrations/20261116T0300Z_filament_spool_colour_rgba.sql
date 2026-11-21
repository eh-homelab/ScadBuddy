-- #6262 Task 9 review (fix round 2, I3): a record's `spool_colour` is its spools' own
-- `#RRGGBBAA` at the last import, not their `#RRGGBB`. The import's colour fallback
-- (`orders/spool_import.py` `_link_rgba`) and an item print's (`orders/spools.py` `_of`)
-- compare it with a spool's `rgba` as it is, so an opaque record of clear spools (the two
-- prod stand-ins below) still finds a new clear spool once its linked ones are archived.
--
-- The check takes both forms for one rollout window: an API still running the old code
-- writes `#RRGGBB`, which the new code reads with the record's alpha (`orders/spools.py`
-- `link_rgba`), as the conversion below does.
ALTER TABLE filaments DROP CONSTRAINT IF EXISTS filaments_spool_colour_check;
ALTER TABLE filaments ADD CONSTRAINT filaments_spool_colour_check
  CHECK (spool_colour ~ '^#[0-9A-F]{6}([0-9A-F]{2})?$');

-- The two stand-ins `20261114T1607Z_filaments_alpha_effect.sql` pinned (opaque records a
-- person set for clear spools): their spools are `#00000000`. Each guard is its id, its
-- stand-in colour and the old 7-char `#000000`, so elsewhere, or run again, this is a
-- no-op.
UPDATE filaments SET spool_colour = '#00000000'
WHERE spool_colour = '#000000'
  AND ((id = 'a324599b-86dd-45fe-8408-579491da2f59' AND upper(colour) = '#EEF3F4')
    OR (id = 'c7c22e72-76a2-4cca-b74a-793ba5e87c29' AND upper(colour) = '#E3ECEF'));

-- Every other link still in the old form takes the record's alpha: the import that wrote
-- it gave an `import` record its spools' alpha. A person may have changed a `manual`
-- record's alpha since; the two stand-ins above are the known ones (prod, 2026-11-14).
UPDATE filaments
SET spool_colour = spool_colour || upper(lpad(to_hex(alpha::integer), 2, '0'))
WHERE spool_colour ~ '^#[0-9A-F]{6}$';
