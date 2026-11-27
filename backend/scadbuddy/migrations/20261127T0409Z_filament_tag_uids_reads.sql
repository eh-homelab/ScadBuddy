-- #8148 (#7905, spec `2026-11-26-bambu-tag-scan-design.md` G3 and G4).
--
-- `filament_tag_uids`: every UID of a tag. A Bambu spool has two tags, one on each side,
-- with two UIDs and one tray UUID (block 9), so one tag row can have more than one UID.
-- `filament_tags.tag_uid` stays the tag's first UID, which Bambuddy's sync and the AMS
-- read. A trigger keeps that first UID in this table: a write of `tag_uid` that names a UID
-- another tag holds here is a unique violation, as the unique index on `tag_uid` was. Every
-- lookup by UID goes through this table (`filament/tags.py` `_find`, `holder_by_keys`,
-- `filament/spools.py` `_tag`, and core's `internal/tagreads`).
--
-- `filament_tag_reads`: the scan history, one row per read, never changed but by a tag
-- merge (which moves `tag_id`) and never deleted. A MIFARE read's raw dump is a blob of
-- kind `tag_dump` (`dump_key`, the content index's key), held in `blob_refs` by holder
-- `tag_read`; key trailers are never stored. `decoded` is the decoder's JSON with its
-- `decoder_version`. Core writes the rows of the phone's reads (`POST /filament/tags/reads`,
-- NDEF and MIFARE); the agent's NFC jobs and the AMS write none yet.
--
-- No row is merged here. #8056's `20261126T1817Z_filament_tag_uid_canonical.sql` covers a
-- tag stored in two UID forms; prod held no such pair on 2026-11-27 (`filament_tag_list`,
-- read 2026-11-27: the owner had merged `a167982e…` into `becfa1bf…` by hand). The three
-- UID-only `other` rows that phone scans made (`9CC69CE300000100`, `DC999BE300000100`,
-- `DCAE1CE100000100`) are folded into their tray's tag by the first MIFARE read that names
-- the tray, in core's route.
CREATE TABLE filament_tag_uids (
    uid text PRIMARY KEY CHECK (uid <> ''),
    tag_id uuid NOT NULL REFERENCES filament_tags (id) ON DELETE CASCADE,
    -- `tag` is a UID a Python tag write gave the tag (its own `tag_uid`, or a UID added to
    -- the tag its tray UUID found, `filament/spools.py` `_tag`); the others name the read
    -- that added it in core (`app` for a MIFARE read from the ScadBuddy app).
    source text NOT NULL CHECK (source IN ('tag', 'phone', 'app', 'agent', 'ams')),
    added_at timestamptz NOT NULL DEFAULT now(),
    added_by text
);
CREATE INDEX filament_tag_uids_tag ON filament_tag_uids (tag_id);

INSERT INTO filament_tag_uids (uid, tag_id, source, added_at)
SELECT tag_uid, id, 'tag', created_at FROM filament_tags WHERE tag_uid IS NOT NULL;

CREATE FUNCTION filament_tag_uids_keep_first() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    holder uuid;
BEGIN
    IF NEW.tag_uid IS NULL THEN
        RETURN NEW;
    END IF;
    INSERT INTO filament_tag_uids (uid, tag_id, source) VALUES (NEW.tag_uid, NEW.id, 'tag')
        ON CONFLICT (uid) DO NOTHING;
    SELECT tag_id INTO holder FROM filament_tag_uids WHERE uid = NEW.tag_uid;
    IF holder <> NEW.id THEN
        RAISE EXCEPTION 'tag UID % belongs to another tag', NEW.tag_uid
            USING ERRCODE = 'unique_violation', CONSTRAINT = 'filament_tag_uids_pkey';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER filament_tag_uids_keep_first AFTER INSERT OR UPDATE OF tag_uid ON filament_tags
    FOR EACH ROW EXECUTE FUNCTION filament_tag_uids_keep_first();

CREATE TABLE filament_tag_reads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tag_id uuid NOT NULL REFERENCES filament_tags (id),
    at timestamptz NOT NULL DEFAULT now(),
    source text NOT NULL CHECK (source IN ('phone', 'app', 'agent', 'ams')),
    actor text,
    uid text,
    tray_uuid text,
    tag_type text CHECK (length(tag_type) <= 32),
    -- An NDEF read's records, as `filament_tags.last_seen_records` holds them.
    records jsonb,
    -- A MIFARE read's raw dump: the content index's key of its `tag_dump` blob.
    dump_key text,
    decoded jsonb,
    -- The photo taken with the scan (the owner, 2026-11-27): a `scan_image` blob's content
    -- index key, held in `blob_refs` by holder `tag_read`, and its media type.
    image_key text,
    image_media_type text CHECK (length(image_media_type) <= 127),
    CHECK ((image_key IS NULL) = (image_media_type IS NULL)),
    -- Where the scan was made (the owner, 2026-11-27): the phone's position, its accuracy
    -- and when the phone took it; all null when the person refused it.
    location_lat double precision CHECK (location_lat BETWEEN -90 AND 90),
    location_lon double precision CHECK (location_lon BETWEEN -180 AND 180),
    location_accuracy_m double precision CHECK (location_accuracy_m BETWEEN 0 AND 100000),
    location_at timestamptz,
    CHECK ((location_lat IS NULL) = (location_lon IS NULL)
        AND (location_lat IS NULL) = (location_at IS NULL)),
    printer_id uuid,
    ams_id integer,
    tray_id integer,
    job_id uuid
);
CREATE INDEX filament_tag_reads_tag_at ON filament_tag_reads (tag_id, at DESC, id DESC);
CREATE INDEX filament_tag_reads_at ON filament_tag_reads (at DESC, id DESC);

CREATE FUNCTION filament_tag_reads_keep() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
        AND (to_jsonb(NEW) - 'tag_id') = (to_jsonb(OLD) - 'tag_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'filament_tag_reads rows are never changed or deleted; a tag merge moves tag_id only';
END $$;
CREATE TRIGGER filament_tag_reads_keep BEFORE UPDATE OR DELETE ON filament_tag_reads
    FOR EACH ROW EXECUTE FUNCTION filament_tag_reads_keep();
CREATE TRIGGER filament_tag_reads_keep_truncate BEFORE TRUNCATE ON filament_tag_reads
    FOR EACH STATEMENT EXECUTE FUNCTION filament_tag_reads_keep();
