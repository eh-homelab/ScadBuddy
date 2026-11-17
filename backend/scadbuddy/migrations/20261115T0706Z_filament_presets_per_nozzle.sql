-- Slicer presets per printer model and nozzle only (#6262, epic #6260, spec
-- 2026-11-14-filament-management-design.md §5.3). A preset row for any printer or any
-- nozzle (`*`, what the 1302Z migration made of a native spool's preset) becomes one
-- row per printer model and nozzle diameter, marked `migrated`; then `*` is refused.
-- A key that already has a row keeps it (ON CONFLICT DO NOTHING), and of two wild rows
-- the more specific one wins a key: a model's (`H2C`/`*`) over a nozzle's (`*`/`0.4`)
-- over any (`*`/`*`). A spool that needs a type drops its `migrated` rows when it is
-- first given one, so its type's presets show (`filament/presets.py` `drop_migrated`).
ALTER TABLE filament_spool_presets ADD COLUMN migrated boolean NOT NULL DEFAULT false;
ALTER TABLE filament_spool_type_presets ADD COLUMN migrated boolean NOT NULL DEFAULT false;

-- The models: `H2C`, the one the resolver keys presets by (`bambuddy/resolver.py`
-- `PRINTER_MODEL`), and each registry printer's model in that form. Measured on
-- 2026-11-15: Bambuddy's printer bodies carry `"model": "H2C"`
-- (`tests/bambuddy/recordings/printers.json`, `printer.json`), the sync stores that value
-- as it is (`printers/store.py` `record_bambuddy_view`), and the real-stack seed registers
-- `"model": "H2C"` (`e2e-stack/seed.sh`). A model typed by hand (`POST /printers`) is free
-- text, so it is trimmed and upper-cased. No dev database was reachable to measure.
-- The nozzles are the resolver's sizes (`bambuddy/models.py` `NozzleSize`).
INSERT INTO filament_spool_presets
    (spool_id, printer_model, nozzle_diameter, slicer_filament, slicer_filament_name, migrated)
SELECT p.spool_id, k.model, n.nozzle, p.slicer_filament, p.slicer_filament_name, true
FROM filament_spool_presets p
CROSS JOIN LATERAL (
    SELECT 'H2C' AS model WHERE p.printer_model = '*'
    UNION
    SELECT upper(btrim(r.model)) FROM printers r
    WHERE p.printer_model = '*' AND nullif(btrim(r.model), '') IS NOT NULL
    UNION
    SELECT p.printer_model WHERE p.printer_model <> '*'
) k
CROSS JOIN LATERAL (
    SELECT v.nozzle FROM (VALUES ('0.2'), ('0.4'), ('0.6'), ('0.8')) v (nozzle)
    WHERE p.nozzle_diameter = '*'
    UNION ALL
    SELECT p.nozzle_diameter WHERE p.nozzle_diameter <> '*'
) n
WHERE '*' IN (p.printer_model, p.nozzle_diameter)
ORDER BY p.printer_model = '*', p.nozzle_diameter = '*'
ON CONFLICT DO NOTHING;

INSERT INTO filament_spool_type_presets
    (type_id, printer_model, nozzle_diameter, slicer_filament, slicer_filament_name, migrated)
SELECT p.type_id, k.model, n.nozzle, p.slicer_filament, p.slicer_filament_name, true
FROM filament_spool_type_presets p
CROSS JOIN LATERAL (
    SELECT 'H2C' AS model WHERE p.printer_model = '*'
    UNION
    SELECT upper(btrim(r.model)) FROM printers r
    WHERE p.printer_model = '*' AND nullif(btrim(r.model), '') IS NOT NULL
    UNION
    SELECT p.printer_model WHERE p.printer_model <> '*'
) k
CROSS JOIN LATERAL (
    SELECT v.nozzle FROM (VALUES ('0.2'), ('0.4'), ('0.6'), ('0.8')) v (nozzle)
    WHERE p.nozzle_diameter = '*'
    UNION ALL
    SELECT p.nozzle_diameter WHERE p.nozzle_diameter <> '*'
) n
WHERE '*' IN (p.printer_model, p.nozzle_diameter)
ORDER BY p.printer_model = '*', p.nozzle_diameter = '*'
ON CONFLICT DO NOTHING;

DELETE FROM filament_spool_presets WHERE '*' IN (printer_model, nozzle_diameter);
DELETE FROM filament_spool_type_presets WHERE '*' IN (printer_model, nozzle_diameter);

ALTER TABLE filament_spool_presets ADD CONSTRAINT filament_spool_presets_keyed
    CHECK (printer_model <> '*' AND nozzle_diameter <> '*');
ALTER TABLE filament_spool_type_presets ADD CONSTRAINT filament_spool_type_presets_keyed
    CHECK (printer_model <> '*' AND nozzle_diameter <> '*');
