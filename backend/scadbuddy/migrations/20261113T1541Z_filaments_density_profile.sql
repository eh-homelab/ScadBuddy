-- #6136: where a filament record's density came from. NULL is a density a person set
-- (or none); a preset name (`Bambu PLA Basic @base`) is a density the spool import or
-- "Fill densities" read from the slicer's profiles (`orders/density.py`). No value is
-- guessed here: the empty densities are filled by those two actions, never by a migration.
ALTER TABLE filaments ADD COLUMN density_profile text;
