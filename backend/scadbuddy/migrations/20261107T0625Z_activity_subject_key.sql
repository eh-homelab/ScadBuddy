-- #5596: the activity list's subject filter, and its per-subject read (`GET /activity`
-- with several `subject`), compare a subject's key (activity/index.py `_SUBJECT_KEY`),
-- not the column, so only an expression index on that CASE serves them. Each subject's
-- read is then a keyset walk of this index, newest first. The CASE must stay the text
-- `_SUBJECT_KEY` holds, or the planner cannot match it.
CREATE INDEX IF NOT EXISTS activity_subject_key ON activity (
    (CASE WHEN subject ~ '^library-[0-9]+$' THEN 'library:' || substr(subject, 9)
     WHEN subject ~ '^[0-9a-f]{32}$' THEN 'output:' || subject ELSE subject END),
    created_at DESC,
    id DESC
);
