-- #6136: where a filament record's density came from. NULL is a density a person set
-- (or none); a text such as `Bambu PLA Silk @base (spool preset GFA05)` is a density the
-- spool import, "Fill densities" or a manual add with no density read through Bambuddy's
-- link to the slicer preset (`orders/density.py`). No value is guessed here: the empty
-- densities are filled by those actions, never by a migration.
ALTER TABLE filaments ADD COLUMN density_profile text;
