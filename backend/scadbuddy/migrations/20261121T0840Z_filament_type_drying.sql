-- #6720: a spool type's drying temperature and hours, as materials and subtypes already
-- hold them (`20261114T1300Z_filament_catalogs.sql`). The slicer profile fill
-- (`filament/profile_fill.py`) writes them while they are empty. A spool may override
-- both (R-6714-18), and a spool's drying takes them as its default (spool, else type, else
-- subtype, else material). Null is none.
ALTER TABLE filament_spool_types
    ADD COLUMN drying_temp_c integer CHECK (drying_temp_c BETWEEN 0 AND 1000),
    ADD COLUMN drying_hours numeric(5,2) CHECK (drying_hours >= 0);

ALTER TABLE filament_spools
    ADD COLUMN drying_temp_c integer CHECK (drying_temp_c BETWEEN 0 AND 1000),
    ADD COLUMN drying_hours numeric(5,2) CHECK (drying_hours >= 0);

-- Who set each of a type's K values (R-6714-19): a person, the profile fill (from its
-- linked Bambuddy spools), a printer's calibration, or the 3DFP profile fetch (#6625). A
-- printer calibration replaces only a `printer_calibration` row of its key, never a
-- person's. Every row made before this file is counted as a person's.
ALTER TABLE filament_spool_type_k_values
    ADD COLUMN source text NOT NULL DEFAULT 'person'
        CHECK (source IN ('person', 'profile', 'printer_calibration', 'profile_fetch'));
