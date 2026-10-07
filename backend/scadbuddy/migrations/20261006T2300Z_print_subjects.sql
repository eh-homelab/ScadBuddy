-- #1750 (epic #1749): a library-file print is recorded exactly as an output's is, keyed
-- by its print subject (`bambuddy.subject.PrintSubject`): 'output:<output id>' or
-- 'library:<library file id>'.
--
-- `print_sends`: every queue item a run queued, written by both sources' `record()` as
-- each plate is queued. `gone` marks one whose archive will never be known (Bambuddy
-- dropped or settled it first), so it is not read again.
CREATE TABLE print_sends (
    queue_item_id bigint PRIMARY KEY,
    subject       text NOT NULL CHECK (subject ~ '^(output:.+|library:[0-9]+)$'),
    plate_id      integer,
    printer_id    bigint,
    project_id    bigint,
    slice_job_id  bigint,
    gone          boolean NOT NULL DEFAULT false,
    first_seen    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX print_sends_subject ON print_sends (subject, first_seen);
CREATE INDEX print_sends_pending ON print_sends (first_seen)
    WHERE NOT gone AND subject LIKE 'library:%';

-- `print_links`: `output_bambuddy_prints` and the linked rows of
-- `library_bambuddy_prints` in one table. `name` is a library file's name as its queue
-- item reported it, for an archive Bambuddy deletes.
CREATE TABLE print_links (
    subject       text NOT NULL CHECK (subject ~ '^(output:.+|library:[0-9]+)$'),
    archive_id    bigint NOT NULL,
    matched_by    text NOT NULL CHECK (matched_by IN ('queue_item', 'content_hash')),
    queue_item_id bigint,
    plate_id      integer,
    printer_id    bigint,
    name          text,
    first_seen    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subject, archive_id)
);
-- Each archive's owner, as `print_links._OWNER_ORDER` picks it: an output's before a
-- library file's, then the first seen, then the lower subject (#609 review).
CREATE INDEX print_links_owner
    ON print_links (archive_id DESC, (subject LIKE 'library:%'), first_seen, subject);
CREATE INDEX print_links_queue_item ON print_links (queue_item_id)
    WHERE queue_item_id IS NOT NULL;

-- Every existing row, with its first sighting.
INSERT INTO print_links
    (subject, archive_id, matched_by, queue_item_id, plate_id, printer_id, first_seen)
SELECT 'output:' || output_id, archive_id, matched_by, queue_item_id, plate_id,
       printer_id, first_seen
FROM output_bambuddy_prints;
INSERT INTO print_sends (queue_item_id, subject, plate_id, printer_id, gone, first_seen)
SELECT queue_item_id, 'library:' || library_file_id, plate_id, printer_id, gone, first_seen
FROM library_bambuddy_prints;
INSERT INTO print_links
    (subject, archive_id, matched_by, queue_item_id, plate_id, printer_id, name, first_seen)
SELECT 'library:' || library_file_id, archive_id, 'queue_item', queue_item_id, plate_id,
       printer_id, name, first_seen
FROM library_bambuddy_prints WHERE archive_id IS NOT NULL
ON CONFLICT (subject, archive_id) DO NOTHING;

-- For the rollout: a pod of the previous release, still draining, records into the old
-- tables. These forward its writes until a later migration drops the old tables and
-- these functions with them. Its deletes are not forwarded: a link left behind by a
-- deleted output is skipped by the prints list (`api/print_history.py`).
CREATE FUNCTION print_links_forward_output() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO print_links
        (subject, archive_id, matched_by, queue_item_id, plate_id, printer_id, first_seen)
    VALUES ('output:' || NEW.output_id, NEW.archive_id, NEW.matched_by, NEW.queue_item_id,
            NEW.plate_id, NEW.printer_id, NEW.first_seen)
    ON CONFLICT (subject, archive_id) DO NOTHING;
    RETURN NULL;
END
$$;
CREATE TRIGGER output_bambuddy_prints_forward AFTER INSERT ON output_bambuddy_prints
    FOR EACH ROW EXECUTE FUNCTION print_links_forward_output();

CREATE FUNCTION print_links_forward_library() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO print_sends (queue_item_id, subject, plate_id, printer_id, gone, first_seen)
    VALUES (NEW.queue_item_id, 'library:' || NEW.library_file_id, NEW.plate_id,
            NEW.printer_id, NEW.gone, NEW.first_seen)
    ON CONFLICT (queue_item_id) DO UPDATE SET gone = print_sends.gone OR EXCLUDED.gone;
    IF NEW.archive_id IS NOT NULL THEN
        INSERT INTO print_links
            (subject, archive_id, matched_by, queue_item_id, plate_id, printer_id, name,
             first_seen)
        VALUES ('library:' || NEW.library_file_id, NEW.archive_id, 'queue_item',
                NEW.queue_item_id, NEW.plate_id, NEW.printer_id, NEW.name, NEW.first_seen)
        ON CONFLICT (subject, archive_id) DO NOTHING;
    END IF;
    RETURN NULL;
END
$$;
CREATE TRIGGER library_bambuddy_prints_forward AFTER INSERT OR UPDATE
    ON library_bambuddy_prints
    FOR EACH ROW EXECUTE FUNCTION print_links_forward_library();
