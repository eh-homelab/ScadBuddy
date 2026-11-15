-- ScadBuddy's own filament inventory, part 3: copy the native spools into the new tables
-- (#6261, epic #6260, spec 2026-11-14-filament-management-design.md §5.3, §5.5). The old
-- tables stay as they are. A spool takes the id of its physical spool when a `native`
-- record link names one, else its own id. A native spool with no material gets 'Unknown'.

-- Materials a native spool names and the catalog lacks, by case-blind name.
INSERT INTO filament_materials (name, sort_order)
SELECT DISTINCT ON (upper(m.name)) m.name, 1000
FROM (
    SELECT coalesce(nullif(btrim(material), ''), 'Unknown') AS name
    FROM native_spools
) m
WHERE NOT EXISTS (
    SELECT 1 FROM filament_materials f
    WHERE f.archived_at IS NULL AND upper(f.name) = upper(m.name)
)
ORDER BY upper(m.name), m.name;

-- Brands a native spool names and the catalog lacks, by case-blind name.
INSERT INTO filament_brands (name, sort_order)
SELECT DISTINCT ON (upper(b.name)) b.name, 1000
FROM (SELECT btrim(brand) AS name FROM native_spools WHERE nullif(btrim(brand), '') IS NOT NULL) b
WHERE NOT EXISTS (
    SELECT 1 FROM filament_brands f
    WHERE f.archived_at IS NULL AND upper(f.name) = upper(b.name)
)
ORDER BY upper(b.name), b.name;

DO $$
DECLARE
    n record;
    p record;
    v_spool uuid;
    v_tag uuid;
BEGIN
    -- Unarchived spools first, so a tag shared with an archived one binds to the live one.
    FOR n IN
        SELECT s.*, l.physical_spool_id
        FROM native_spools s
        LEFT JOIN spool_record_links l
            ON l.provider = 'native' AND l.record_id = s.id::text
        ORDER BY (s.archived_at IS NOT NULL), s.created_at
    LOOP
        v_spool := coalesce(n.physical_spool_id, n.id);
        INSERT INTO filament_spools
            (id, material_id, brand_id, colour_name, rgba, label_weight_g, note, grams_left,
             archived_at, created_at, updated_at)
        SELECT v_spool, f.id,
               (SELECT br.id FROM filament_brands br
                WHERE br.archived_at IS NULL AND upper(br.name) = upper(btrim(n.brand))),
               n.name,
               CASE WHEN n.colour ~ '^#[0-9A-Fa-f]{6}$' THEN upper(n.colour) || 'FF' END,
               -- What the ledger gives (`filament/ledger.py` `grams_left`): the native
               -- weight less the grams of each print still running, never below 0 (and
               -- unknown while the native weight is; `greatest` alone would give 0).
               n.label_weight_g, n.note,
               CASE WHEN n.remaining_g IS NOT NULL THEN greatest(n.remaining_g - coalesce((
                   SELECT sum(u.grams) FROM native_spool_uses u
                   WHERE u.spool_id = n.id AND u.settled_at IS NULL
               ), 0), 0) END,
               n.archived_at,
               n.created_at, n.updated_at
        FROM filament_materials f
        WHERE f.archived_at IS NULL
          AND upper(f.name) = upper(coalesce(nullif(btrim(n.material), ''), 'Unknown'));

        -- A native preset has no printer model: it is the '*' row (#6262 replaces it).
        IF nullif(btrim(n.preset), '') IS NOT NULL THEN
            INSERT INTO filament_spool_presets
                (spool_id, printer_model, nozzle_diameter, slicer_filament)
            VALUES (v_spool, '*', '*', n.preset);
        END IF;

        IF n.tray_uuid IS NOT NULL OR n.tag_uid IS NOT NULL THEN
            SELECT t.id INTO v_tag FROM filament_tags t
            WHERE (n.tray_uuid IS NOT NULL AND t.tray_uuid = n.tray_uuid)
               OR (n.tag_uid IS NOT NULL AND t.tag_uid = n.tag_uid)
            ORDER BY t.created_at
            LIMIT 1;
            IF v_tag IS NULL THEN
                INSERT INTO filament_tags (kind, tag_uid, tray_uuid)
                VALUES (CASE WHEN n.tray_uuid IS NOT NULL THEN 'bambu_mifare' ELSE 'other' END,
                        n.tag_uid, n.tray_uuid)
                RETURNING id INTO v_tag;
            END IF;
            IF n.archived_at IS NULL AND NOT EXISTS (
                SELECT 1 FROM filament_tag_bindings b
                WHERE b.tag_id = v_tag AND b.unbound_at IS NULL
            ) THEN
                INSERT INTO filament_tag_bindings (tag_id, spool_id, bound_at)
                VALUES (v_tag, v_spool, n.created_at);
            ELSIF n.archived_at IS NOT NULL THEN
                -- An archived spool keeps its tag as a closed binding, as an archive
                -- closes one (`filament/spools.py` `_archive`, `spools/native.py`).
                INSERT INTO filament_tag_bindings
                    (tag_id, spool_id, bound_at, unbound_at, reason)
                VALUES (v_tag, v_spool, least(n.created_at, n.archived_at), n.archived_at,
                        'archived');
            END IF;
            v_tag := NULL;
        END IF;
    END LOOP;

    -- A physical spool no native record names gets its tag only.
    FOR p IN
        SELECT * FROM physical_spools ps
        WHERE (ps.tray_uuid IS NOT NULL OR ps.tag_uid IS NOT NULL)
          AND NOT EXISTS (
              SELECT 1 FROM filament_tags t
              WHERE (ps.tray_uuid IS NOT NULL AND t.tray_uuid = ps.tray_uuid)
                 OR (ps.tag_uid IS NOT NULL AND t.tag_uid = ps.tag_uid)
          )
    LOOP
        INSERT INTO filament_tags (kind, tag_uid, tray_uuid)
        VALUES (CASE WHEN p.tray_uuid IS NOT NULL THEN 'bambu_mifare' ELSE 'other' END,
                p.tag_uid, p.tray_uuid);
    END LOOP;
END
$$;

-- The `created` reading is the weight before any applied use: `remaining_g` already has
-- those uses taken off, and the ledger takes each `print_actual` below off the reading
-- again (`filament/ledger.py` `grams_left`), which then gives `remaining_g` back.
INSERT INTO filament_spool_events (spool_id, at, kind, source, grams_after)
SELECT coalesce(l.physical_spool_id, s.id), s.created_at, 'created', 'person',
       s.remaining_g + coalesce((
           SELECT sum(u.grams) FROM native_spool_uses u
           WHERE u.spool_id = s.id AND u.settled_at IS NOT NULL AND u.applied IS TRUE
       ), 0)
FROM native_spools s
LEFT JOIN spool_record_links l ON l.provider = 'native' AND l.record_id = s.id::text;

INSERT INTO filament_spool_events
    (spool_id, at, kind, source, delta_g, ref_kind, ref_id, data)
SELECT coalesce(l.physical_spool_id, u.spool_id),
       coalesce(u.settled_at, u.created_at),
       CASE WHEN u.settled_at IS NULL THEN 'print_started' ELSE 'print_ended' END,
       CASE WHEN u.settled_at IS NULL THEN 'print_estimate' ELSE 'print_actual' END,
       CASE WHEN u.settled_at IS NULL OR u.applied IS TRUE THEN -u.grams ELSE 0 END,
       'print_run',
       u.run_id || '/' || u.subtask_name || '/' || u.slot_id,
       CASE WHEN u.settled_at IS NULL THEN '{}'::jsonb
            WHEN u.applied IS TRUE THEN '{"basis": "sliced"}'::jsonb
            ELSE '{"basis": "not_applied"}'::jsonb END
FROM native_spool_uses u
LEFT JOIN spool_record_links l ON l.provider = 'native' AND l.record_id = u.spool_id::text;

-- A native record linked to a physical spool is now that spool's id. Its cached copy and
-- its link take the new id, so the overlay keeps a person's tray link until the next
-- refresh rewrites the cache (`SpoolStore.replace_records`). Last: the copies above
-- look the links up by the old id.
UPDATE spool_provider_records r
SET record_id = l.physical_spool_id::text,
    snapshot = jsonb_set(r.snapshot, '{record_id}', to_jsonb(l.physical_spool_id::text))
FROM spool_record_links l
WHERE r.provider = 'native' AND l.provider = 'native' AND l.record_id = r.record_id;

UPDATE spool_record_links SET record_id = physical_spool_id::text WHERE provider = 'native';
