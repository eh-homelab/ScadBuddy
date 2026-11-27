-- #8042 (#7905): one form for a 4-byte MIFARE tag UID, the AMS's `<uid>00000100`
-- (`spools/models.py` `clean_tag_uid`, `MIFARE_UID_SUFFIX`).
-- - A phone (Web NFC, Android NFC) reads a Bambu tag's UID as 4 bytes, `7E7482BB`; the AMS
--   reports the same tag as `7E7482BB00000100` (prod tags `a167982e…` and `becfa1bf…`,
--   read 2026-11-26).
-- - All 38 `bambu_mifare` rows on prod end `00000100` (read 2026-11-26).
-- - Prod held 0 short UIDs after the owner's hand fix of 2026-11-26, so this is a no-op
--   there and wherever no short UID is stored. It catches a short row that a phone scan
--   makes before this code runs.
-- A short row whose twin exists is merged into the twin: its bindings, the ledger's copies
-- of its id, its Bambuddy queue row and its newer last-seen fields move, then it is deleted.
-- The twin keeps its own kind and tray UUID (a Bambu tag stays Bambu only, #7905). A short
-- row with no twin is re-keyed. A short row with backups is left alone: backups refuse
-- UPDATE and DELETE (`20261124T0258Z_filament_tag_backups.sql`). So is a pair where both
-- rows hold an open binding (the one-open-binding-per-tag index). Bindings and backups
-- reference `filament_tags` with no ON DELETE, so a reference missed here fails the
-- delete and rolls back the whole file, rather than losing data.
DO $$
DECLARE
    s record;
    twin record;
    v_bindings integer;
    v_events integer;
    v_queued integer;
BEGIN
    FOR s IN
        SELECT * FROM filament_tags WHERE tag_uid ~ '^[0-9A-F]{8}$' ORDER BY id FOR UPDATE
    LOOP
        SELECT * INTO twin FROM filament_tags WHERE tag_uid = s.tag_uid || '00000100' FOR UPDATE;
        IF NOT FOUND THEN
            UPDATE filament_tags SET tag_uid = s.tag_uid || '00000100' WHERE id = s.id;
            RAISE NOTICE 'filament tag % re-keyed from % to %00000100', s.id, s.tag_uid, s.tag_uid;
            CONTINUE;
        END IF;
        IF EXISTS (SELECT 1 FROM filament_tag_backups WHERE tag_id = s.id) THEN
            RAISE NOTICE 'filament tag % (%) has backups, which cannot move; not merged into %',
                s.id, s.tag_uid, twin.id;
            CONTINUE;
        END IF;
        IF EXISTS (SELECT 1 FROM filament_tag_bindings WHERE tag_id = s.id AND unbound_at IS NULL)
            AND EXISTS (
                SELECT 1 FROM filament_tag_bindings WHERE tag_id = twin.id AND unbound_at IS NULL
            )
        THEN
            RAISE NOTICE 'filament tags % (%) and % both hold an open binding; not merged',
                s.id, s.tag_uid, twin.id;
            CONTINUE;
        END IF;

        UPDATE filament_tag_bindings SET tag_id = twin.id WHERE tag_id = s.id;
        GET DIAGNOSTICS v_bindings = ROW_COUNT;

        -- `filament/spools.py` writes `data.tag_id` on both kinds; no foreign key guards it.
        UPDATE filament_spool_events
            SET data = jsonb_set(data, '{tag_id}', to_jsonb(twin.id::text))
            WHERE kind IN ('tag_bound', 'tag_unbound') AND data ->> 'tag_id' = s.id::text;
        GET DIAGNOSTICS v_events = ROW_COUNT;

        DELETE FROM filament_bb_tag_pending WHERE tag_id = s.id;
        GET DIAGNOSTICS v_queued = ROW_COUNT;
        IF v_queued > 0 THEN
            INSERT INTO filament_bb_tag_pending (tag_id) VALUES (twin.id) ON CONFLICT DO NOTHING;
        END IF;

        UPDATE filament_tags SET
            last_seen_at = CASE WHEN newer THEN s.last_seen_at ELSE last_seen_at END,
            last_seen_source = CASE WHEN newer THEN s.last_seen_source ELSE last_seen_source END,
            last_seen_records = CASE WHEN newer THEN s.last_seen_records ELSE last_seen_records END,
            raw_blob = COALESCE(raw_blob, s.raw_blob),
            note = COALESCE(note, s.note),
            kind = CASE WHEN s.kind = 'bambu_mifare' THEN 'bambu_mifare' ELSE kind END
        FROM (
            SELECT s.last_seen_at IS NOT NULL
                AND (twin.last_seen_at IS NULL OR s.last_seen_at > twin.last_seen_at) AS newer
        ) AS cmp
        WHERE id = twin.id;

        RAISE NOTICE 'filament tag % (%) merged into % (%): % bindings, % ledger events, % queued',
            s.id, s.tag_uid, twin.id, twin.tag_uid, v_bindings, v_events, v_queued;
        DELETE FROM filament_tags WHERE id = s.id;
    END LOOP;
END $$;
