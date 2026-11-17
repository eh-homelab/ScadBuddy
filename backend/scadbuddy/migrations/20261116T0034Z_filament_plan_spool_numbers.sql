-- Stored spool ids name SB spools (#6262 Task 8, preflight F14, F15, F30). Until now a
-- positive spool id in a remembered or quoted plan named a Bambuddy spool; from #6262
-- Task 7 it names an SB spool's `number` (`bambuddy/filaments.py` `FilamentPlan.ids`).
-- Each stored id is mapped through `filament_bb_links`. An id with no link is dropped
-- (from a plan, a slice profile or `spool_ids`) or set NULL (a rack pick, whose label
-- stays, as an untracked spool's row does, `rack/usage.py`), never read as SB spool #N.
-- A negative id is a tray chosen for itself, never stored as a spool
-- (`bambuddy/trays.py`), and is left alone.
--
-- Every rewrite is guarded by shape, so data of an unexpected shape (a null plan, a plan
-- with no slots, a non-numeric id) is left as it is and never stops the API's start. A
-- plan already marked `"ids": "sb"` is left alone. The helpers are temporary functions,
-- dropped at the end.

-- First (F15, F30): a Bambuddy record link whose physical spool is an SB spool (the
-- native migration kept the ids, `20261114T1302Z_filament_from_native.sql`) becomes a
-- `filament_bb_links` row, so its id is mapped, not dropped. `bb_updated_at` stays NULL
-- so the next pull refreshes the spool, and the preset columns of
-- `20261115T2331Z_filament_bb_link_preset.sql` stay NULL, so that pull fills its empty
-- preset keys once (`filament/bambuddy_pull.py`). A spool already linked, or a Bambuddy
-- id already linked to another spool, keeps its link. At most 9 digits fits an integer.
INSERT INTO filament_bb_links (spool_id, bb_spool_id)
SELECT s.id, l.record_id::integer
FROM spool_record_links l
JOIN filament_spools s ON s.id = l.physical_spool_id
WHERE l.provider = 'bambuddy' AND l.record_id ~ '^[0-9]{1,9}$'
ORDER BY l.linked_at, l.record_id
ON CONFLICT DO NOTHING;

-- The SB number of a linked Bambuddy spool, else NULL.
CREATE FUNCTION pg_temp.sb_number(bb integer) RETURNS integer LANGUAGE sql STABLE AS $$
    SELECT s.number FROM filament_bb_links l JOIN filament_spools s ON s.id = l.spool_id
    WHERE l.bb_spool_id = bb
$$;

-- A JSON spool id as an integer (a number, or a string Pydantic would read as one), else
-- NULL: an id of any other shape is not rewritten.
CREATE FUNCTION pg_temp.spool_int(v jsonb) RETURNS integer LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(v) IN ('number', 'string') AND (v #>> '{}') ~ '^-?[0-9]{1,9}$'
                THEN (v #>> '{}')::integer END
$$;

-- A list of `{slot_id, spool_id}` with each positive id mapped and each unlinked one's
-- slot dropped; an element of another shape is kept as it is.
CREATE FUNCTION pg_temp.sb_slots(slots jsonb) RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT coalesce(jsonb_agg(
               CASE WHEN i.id IS NULL OR i.id <= 0 THEN e.slot
                    ELSE jsonb_set(e.slot, '{spool_id}', to_jsonb(pg_temp.sb_number(i.id)))
               END ORDER BY e.n), '[]'::jsonb)
    FROM jsonb_array_elements(slots) WITH ORDINALITY AS e(slot, n)
    CROSS JOIN LATERAL (
        SELECT CASE WHEN jsonb_typeof(e.slot) = 'object'
                    THEN pg_temp.spool_int(e.slot -> 'spool_id') END AS id
    ) AS i
    WHERE i.id IS NULL OR i.id <= 0 OR pg_temp.sb_number(i.id) IS NOT NULL
$$;

-- A `FilamentPlan` (`{slots, force_colour_match, ids}`) not yet marked `"sb"`, with its
-- slots mapped and the mark set; any other value as it is.
CREATE FUNCTION pg_temp.sb_plan(plan jsonb) RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN jsonb_typeof(plan) = 'object'
                 AND jsonb_typeof(plan -> 'slots') = 'array'
                 AND coalesce(plan ->> 'ids', 'bb') <> 'sb'
                THEN plan || jsonb_build_object(
                    'slots', pg_temp.sb_slots(plan -> 'slots'), 'ids', 'sb')
                ELSE plan END
$$;

-- A stored request with its `filament_plan` mapped, and the one a level down too (the
-- preview slice's `request`, as `operations/kinds.py` `without_bb_ids` reads it).
CREATE FUNCTION pg_temp.sb_request(req jsonb) RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
DECLARE
    result jsonb := req;
BEGIN
    IF jsonb_typeof(req) IS DISTINCT FROM 'object' THEN
        RETURN req;
    END IF;
    IF req ? 'filament_plan' THEN
        result := jsonb_set(result, '{filament_plan}', pg_temp.sb_plan(req -> 'filament_plan'));
    END IF;
    IF jsonb_typeof(req -> 'request') = 'object' AND req -> 'request' ? 'filament_plan' THEN
        result := jsonb_set(result, '{request,filament_plan}',
                            pg_temp.sb_plan(req #> '{request,filament_plan}'));
    END IF;
    RETURN result;
END
$$;

-- Remembered choices: `filament_plan` is a bare list of slots (`library/settings_store.py`
-- `ModelPrintChoices`), with no place for a mark; the picker reads it as SB numbers.
UPDATE model_print_choices
SET choices = jsonb_set(choices, '{filament_plan}', pg_temp.sb_slots(choices -> 'filament_plan'))
WHERE jsonb_typeof(choices) = 'object' AND jsonb_typeof(choices -> 'filament_plan') = 'array';

UPDATE order_item_quotes SET filament_plan = pg_temp.sb_plan(filament_plan)
WHERE jsonb_typeof(filament_plan) = 'object';

-- A file whose run may have started (outcome `pending` or `started`, `batches/store.py`)
-- keeps its request byte for byte: Queue all resends it, and its batch request id and
-- run key hash it, so a changed body would start a second print. Its unmarked plan is
-- mapped where it is read (`bambuddy/filaments.py` `sb_plan`).
UPDATE print_batch_items
SET request = pg_temp.sb_request(request),
    refused_request = pg_temp.sb_request(refused_request)
WHERE (request IS NOT NULL OR refused_request IS NOT NULL)
  AND (outcome ->> 'status') IS DISTINCT FROM 'pending'
  AND (outcome ->> 'status') IS DISTINCT FROM 'started';

-- Only operations not yet ended (F14): an ended one's request is history.
UPDATE operations SET request = pg_temp.sb_request(request) WHERE status = 'running';

-- The storefront slice profile (`orders/storefront/slice_profile.py`, a `settings` row:
-- `library/settings_store.py`): material to spool id, with no mark, so always rewritten.
UPDATE settings
SET value = jsonb_set(value, '{spools}', (
    SELECT coalesce(jsonb_object_agg(
               e.key,
               CASE WHEN i.id IS NULL OR i.id <= 0 THEN e.value
                    ELSE to_jsonb(pg_temp.sb_number(i.id)) END), '{}'::jsonb)
    FROM jsonb_each(value -> 'spools') AS e(key, value)
    CROSS JOIN LATERAL (SELECT pg_temp.spool_int(e.value) AS id) AS i
    WHERE i.id IS NULL OR i.id <= 0 OR pg_temp.sb_number(i.id) IS NOT NULL
))
WHERE name = 'storefront_slice_profile'
  AND jsonb_typeof(value) = 'object'
  AND jsonb_typeof(value -> 'spools') = 'object';

-- The price book's spools at its last import (`20261114T1607Z_filaments_alpha_effect.sql`).
UPDATE filaments AS f
SET spool_ids = (
    SELECT coalesce(array_agg(CASE WHEN u.x <= 0 THEN u.x ELSE pg_temp.sb_number(u.x) END
                              ORDER BY u.n), '{}')
    FROM unnest(f.spool_ids) WITH ORDINALITY AS u(x, n)
    WHERE u.x <= 0 OR pg_temp.sb_number(u.x) IS NOT NULL
)
WHERE cardinality(f.spool_ids) > 0;

UPDATE rack_nozzle_pick_spools SET spool_id = pg_temp.sb_number(spool_id) WHERE spool_id > 0;

DROP FUNCTION pg_temp.sb_request(jsonb);
DROP FUNCTION pg_temp.sb_plan(jsonb);
DROP FUNCTION pg_temp.sb_slots(jsonb);
DROP FUNCTION pg_temp.spool_int(jsonb);
DROP FUNCTION pg_temp.sb_number(integer);
