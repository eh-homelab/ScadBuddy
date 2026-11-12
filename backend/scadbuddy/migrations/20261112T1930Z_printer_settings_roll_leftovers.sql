-- #6106, after #5167 (`20261112T1451Z_printer_settings_registry_only.sql`): during that
-- roll, an old API pod could still write a per-printer setting under a Bambuddy key
-- after the contract migration had run. The current build never reads or removes such
-- a key. The roll is complete before this file runs, so no pod writes them any more.
--
-- Each leftover Bambuddy key goes to the registry printer that links it, but only where
-- that printer has no registry key yet: the current build wrote the registry key, so it
-- is the newer choice. Then every Bambuddy key goes.

-- 1. The per-printer maps. A key of digits is a Bambuddy printer id.
UPDATE settings AS s
    SET value = (
        SELECT COALESCE(jsonb_object_agg(k.key, k.value), '{}'::jsonb)
        FROM (
            SELECT DISTINCT ON (c.key) c.key, c.value
            FROM (
                SELECT e.key, e.value, 0 AS rank
                FROM jsonb_each(s.value) AS e
                WHERE e.key !~ '^[0-9]+$'
                UNION ALL
                SELECT p.id::text, e.value, 1 AS rank
                FROM jsonb_each(s.value) AS e
                JOIN printers AS p
                    ON p.bambuddy_printer_id = CASE
                        WHEN e.key ~ '^[0-9]{1,10}$' THEN e.key::bigint
                    END
            ) AS c
            ORDER BY c.key, c.rank
        ) AS k
    ),
    updated_at = now()
    WHERE s.name IN (
            'printer_rack_algorithms', 'printer_rack_algorithm_versions', 'printer_print_options'
        )
        AND jsonb_typeof(s.value) = 'object'
        AND EXISTS (SELECT 1 FROM jsonb_object_keys(s.value) AS key WHERE key ~ '^[0-9]+$');

-- 2. The default printer. An old pod's `printer_id` row names a Bambuddy printer. It
-- becomes `default_printer` only when no default is stored.
INSERT INTO settings (name, value, updated_at)
    SELECT 'default_printer', to_jsonb(p.id::text), now()
    FROM settings AS s
    JOIN printers AS p
        ON p.bambuddy_printer_id = CASE
            WHEN jsonb_typeof(s.value) = 'number' AND (s.value #>> '{}') ~ '^[0-9]{1,10}$'
                THEN (s.value #>> '{}')::bigint
        END
    WHERE s.name = 'printer_id'
    ON CONFLICT (name) DO NOTHING;
DELETE FROM settings WHERE name = 'printer_id';

-- 3. The printer each subject last printed on. An old pod wrote `{printer_id: N}` with
-- no `printer`: `printer` becomes the registry printer that links N. Then every
-- `printer_id` goes.
UPDATE model_print_choices AS c
    SET choices = (c.choices - 'printer_id') || jsonb_build_object('printer', p.id::text),
        updated_at = now()
    FROM printers AS p
    WHERE jsonb_typeof(c.choices -> 'printer_id') = 'number'
        AND c.choices ->> 'printer' IS NULL
        AND p.bambuddy_printer_id = CASE
            WHEN (c.choices ->> 'printer_id') ~ '^[0-9]{1,10}$'
                THEN (c.choices ->> 'printer_id')::bigint
        END;
UPDATE model_print_choices
    SET choices = choices - 'printer_id', updated_at = now()
    WHERE choices ? 'printer_id';
