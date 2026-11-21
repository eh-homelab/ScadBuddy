-- A person's tray links carried to SB spool places (#6262 Task 12, plan F8, F30). The
-- spool overlay is gone, and with it the last reader of `tray_spool_links` (a person's
-- link of an untagged tray to a spool, `20261018T1744Z_physical_spools.sql`). Each link
-- becomes what the SB inventory reads for an untagged tray (`filament/inventory.py`,
-- `20261115T0741Z_filament_tray_place.sql`): the spool's place is the tray's, with the
-- link's fingerprint, and one `moved` event, as `filament/locations.py` `move_on` writes.
--
-- A link's SB spool is the one with its `physical_spool_id` (the native migration kept
-- the ids, `20261114T1302Z_filament_from_native.sql`), else the SB spool `filament_bb_links`
-- names for that physical spool's Bambuddy record link (a spool only Bambuddy knew, which
-- the pull made with a new id). The old fingerprint `MATERIAL|RRGGBB` becomes
-- `MATERIAL|#RRGGBBFF`, and `MATERIAL|` stays (`inventory.py` `tray_fingerprint`): the old
-- one kept no alpha, so a translucent tray asks once more (F8). The tray's place is made
-- as `LocationStore.ensure_ams` makes it (the first unarchived kind flagged `ams`, named
-- `<printer> <tray label>`, `bambuddy/trays.py` `tray_label`).
--
-- Every row is guarded, so this is a no-op elsewhere and never stops the API's start: a
-- link with no SB spool, an archived spool, a fingerprint of another shape, a spool
-- already in a tray, and a tray an unarchived spool already holds are skipped (the
-- earliest link wins between two of one spool). No table is dropped: the old ones stay,
-- unread, for the PR that drops them (plan Decision 6).
DO $$
DECLARE
    link record;
    kind uuid;
    spool uuid;
    place uuid;
    before uuid;
    material text;
    part text;
BEGIN
    SELECT id INTO kind FROM filament_location_kinds WHERE ams AND archived_at IS NULL
        ORDER BY sort_order, upper(name), id LIMIT 1;
    FOR link IN
        SELECT t.printer_id, t.ams_id, t.tray_id, t.physical_spool_id, t.fingerprint,
               p.name AS printer_name
        FROM tray_spool_links t JOIN printers p ON p.id = t.printer_id
        ORDER BY t.linked_at, t.printer_id, t.ams_id, t.tray_id
    LOOP
        CONTINUE WHEN link.fingerprint !~ '^[^|]*\|([0-9A-F]{6})?$';
        material := split_part(link.fingerprint, '|', 1);
        part := split_part(link.fingerprint, '|', 2);

        spool := NULL;
        SELECT s.id INTO spool FROM filament_spools s
            WHERE s.id = link.physical_spool_id AND s.archived_at IS NULL;
        IF spool IS NULL THEN
            SELECT s.id INTO spool FROM spool_record_links r
                JOIN filament_bb_links b ON b.bb_spool_id::text = r.record_id
                JOIN filament_spools s ON s.id = b.spool_id
                WHERE r.provider = 'bambuddy' AND r.physical_spool_id = link.physical_spool_id
                  AND r.record_id ~ '^[0-9]{1,9}$' AND s.archived_at IS NULL
                ORDER BY r.linked_at, r.record_id
                LIMIT 1;
        END IF;
        CONTINUE WHEN spool IS NULL;
        -- Already in a tray, this one or another: it stays there.
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM filament_spools s JOIN filament_locations l ON l.id = s.location_id
            WHERE s.id = spool AND l.printer_id IS NOT NULL
        );

        place := NULL;
        SELECT id INTO place FROM filament_locations
            WHERE printer_id = link.printer_id AND ams_id = link.ams_id
              AND tray_id = link.tray_id AND archived_at IS NULL;
        IF place IS NULL THEN
            CONTINUE WHEN kind IS NULL;
            INSERT INTO filament_locations (name, kind_id, printer_id, ams_id, tray_id)
            VALUES (
                link.printer_name || ' ' || CASE
                    WHEN link.ams_id = 255 THEN 'External slot ' || (link.tray_id + 1)
                    WHEN link.ams_id >= 128 THEN 'HT-' || chr(65 + link.ams_id - 128)
                    ELSE 'AMS-' || chr(65 + link.ams_id) || ' slot ' || (link.tray_id + 1)
                END,
                kind, link.printer_id, link.ams_id, link.tray_id
            )
            ON CONFLICT (printer_id, ams_id, tray_id)
                WHERE printer_id IS NOT NULL AND archived_at IS NULL DO NOTHING
            RETURNING id INTO place;
            CONTINUE WHEN place IS NULL;
        END IF;
        -- Another spool holds the tray: it keeps it.
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM filament_spools
            WHERE location_id = place AND id <> spool AND archived_at IS NULL
        );

        SELECT location_id INTO before FROM filament_spools WHERE id = spool FOR UPDATE;
        UPDATE filament_spools
            SET location_id = place,
                tray_fingerprint = material || '|'
                    || CASE WHEN part = '' THEN '' ELSE '#' || part || 'FF' END,
                updated_at = now()
            WHERE id = spool;
        INSERT INTO filament_spool_events (spool_id, at, kind, grams_before, actor, data)
        VALUES (
            spool,
            greatest(clock_timestamp(), (SELECT max(at) + interval '1 microsecond'
                                         FROM filament_spool_events WHERE spool_id = spool)),
            'moved',
            (SELECT grams_left FROM filament_spools WHERE id = spool),
            'system:migration',
            jsonb_build_object('from', before, 'to', place)
        );
    END LOOP;
END
$$;
