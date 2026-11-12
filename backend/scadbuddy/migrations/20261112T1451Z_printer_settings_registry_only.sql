-- #5167, the contract step of #4971 part 2 (#5170): every pod runs a build that reads
-- each per-printer setting by the registry printer (`printers.id`) when the Bambuddy key
-- is missing. This file rewrites each Bambuddy key to the registry id that links that
-- Bambuddy printer, and drops what the registry does not hold. From this build on, the
-- code keys these settings by the registry id only.
--
-- The expand build read the Bambuddy key first, so where a printer has both keys, the
-- Bambuddy key's entry wins here too.

-- 1. The per-printer maps in `settings` rows. A key of digits is a Bambuddy printer id;
-- any other key is a registry id. A key that no registry printer matches is dropped.
UPDATE settings AS s
    SET value = (
        SELECT COALESCE(jsonb_object_agg(k.printer, k.value), '{}'::jsonb)
        FROM (
            SELECT DISTINCT ON (p.id) p.id::text AS printer, e.value
            FROM jsonb_each(s.value) AS e
            JOIN printers AS p
                ON (e.key ~ '^[0-9]{1,10}$' AND p.bambuddy_printer_id = e.key::bigint)
                    OR p.id::text = e.key
            ORDER BY p.id, (e.key ~ '^[0-9]{1,10}$') DESC
        ) AS k
    ),
    updated_at = now()
    WHERE s.name IN (
            'printer_rack_algorithms', 'printer_rack_algorithm_versions', 'printer_print_options'
        )
        AND jsonb_typeof(s.value) = 'object';

-- 2. The default printer. `PUT /settings` kept one of `printer_id` and `default_printer`.
-- The expand build read `printer_id` first, so it wins.
INSERT INTO settings (name, value, updated_at)
    SELECT 'default_printer', to_jsonb(p.id::text), now()
    FROM settings AS s
    JOIN printers AS p ON p.bambuddy_printer_id = (s.value #>> '{}')::bigint
    WHERE s.name = 'printer_id' AND jsonb_typeof(s.value) = 'number'
    ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
DELETE FROM settings WHERE name = 'printer_id';
-- A default that names no registry printer is no default.
DELETE FROM settings AS s
    WHERE s.name = 'default_printer'
        AND NOT EXISTS (SELECT 1 FROM printers AS p WHERE p.id::text = s.value #>> '{}');

-- 3. The printer each subject last printed on: `choices.printer_id` becomes
-- `choices.printer`. A Bambuddy printer the registry does not link is dropped, and so is
-- a registry id the registry no longer holds.
UPDATE model_print_choices AS c
    SET choices = (c.choices - 'printer_id') || jsonb_build_object('printer', p.id::text)
    FROM printers AS p
    WHERE jsonb_typeof(c.choices -> 'printer_id') = 'number'
        AND p.bambuddy_printer_id = (c.choices ->> 'printer_id')::bigint;
UPDATE model_print_choices SET choices = choices - 'printer_id' WHERE choices ? 'printer_id';
UPDATE model_print_choices AS c
    SET choices = c.choices - 'printer'
    WHERE c.choices ? 'printer'
        AND NOT EXISTS (SELECT 1 FROM printers AS p WHERE p.id::text = c.choices ->> 'printer');

-- 4. The printer each Bambuddy project last printed on. A `printer_id` other than 0 names
-- the Bambuddy printer, and the expand build wrote `printer` only beside a 0.
UPDATE project_print_targets AS t SET printer = p.id
    FROM printers AS p
    WHERE t.printer_id <> 0 AND p.bambuddy_printer_id = t.printer_id;
UPDATE project_print_targets AS t SET printer = NULL
    WHERE t.printer_id <> 0
        AND NOT EXISTS (SELECT 1 FROM printers AS p WHERE p.bambuddy_printer_id = t.printer_id);
DELETE FROM project_print_targets WHERE printer IS NULL;
-- A target without a printer means nothing, so a deleted printer takes its targets with
-- it (it was `ON DELETE SET NULL`).
ALTER TABLE project_print_targets
    DROP CONSTRAINT IF EXISTS project_print_targets_printer_fkey,
    DROP COLUMN printer_id,
    ALTER COLUMN printer SET NOT NULL,
    ADD CONSTRAINT project_print_targets_printer_fkey
        FOREIGN KEY (printer) REFERENCES printers (id) ON DELETE CASCADE;

-- 5. The rack tables: fill `printer` once more from the registry, as
-- `20261101T1911Z_rack_nozzle_registry_printer.sql` did, for rows the expand build's
-- rollout wrote. Then drop `printer_id`. `printer` stays nullable (`ON DELETE SET NULL`):
-- a hotend's first sighting and a pick outlive the printer.
UPDATE rack_nozzle_seen AS s SET printer = p.id
    FROM printers AS p
    WHERE p.bambuddy_printer_id = s.printer_id AND s.printer IS NULL;
UPDATE rack_nozzle_picks AS k SET printer = p.id
    FROM printers AS p
    WHERE p.bambuddy_printer_id = k.printer_id AND k.printer IS NULL;
ALTER TABLE rack_nozzle_seen DROP COLUMN printer_id;
ALTER TABLE rack_nozzle_picks DROP COLUMN printer_id;
