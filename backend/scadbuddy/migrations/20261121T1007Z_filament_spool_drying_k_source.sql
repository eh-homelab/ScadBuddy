-- #6720, R-6714-18: a spool may override its type's drying temperature and hours, as any
-- other value of its type (`20261121T0840Z_filament_type_drying.sql` gave the type them).
-- A spool's drying takes them as its default: spool, else type, else subtype, else
-- material. Null is none.
ALTER TABLE filament_spools
    ADD COLUMN drying_temp_c integer CHECK (drying_temp_c BETWEEN 0 AND 1000),
    ADD COLUMN drying_hours numeric(5,2) CHECK (drying_hours >= 0);

-- R-6714-19: who set each of a type's K values: a person, the profile fill (from its
-- linked Bambuddy spools), a printer's calibration, or the 3DFP profile fetch (#6625). A
-- printer calibration replaces only a `printer_calibration` row of its key, never a
-- person's. Every row made before this file is counted as a person's.
ALTER TABLE filament_spool_type_k_values
    ADD COLUMN source text NOT NULL DEFAULT 'person'
        CHECK (source IN ('person', 'profile', 'printer_calibration', 'profile_fetch'));
