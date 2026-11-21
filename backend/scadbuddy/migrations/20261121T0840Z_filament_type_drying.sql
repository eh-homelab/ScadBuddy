-- #6720: a spool type's drying temperature and hours, as materials and subtypes already
-- hold them (`20261114T1300Z_filament_catalogs.sql`). The slicer profile fill
-- (`filament/profile_fill.py`) writes them while they are empty, and a spool's drying takes
-- them as its default (type, else subtype, else material). Null is none.
ALTER TABLE filament_spool_types
    ADD COLUMN drying_temp_c integer CHECK (drying_temp_c BETWEEN 0 AND 1000),
    ADD COLUMN drying_hours numeric(5,2) CHECK (drying_hours >= 0);
