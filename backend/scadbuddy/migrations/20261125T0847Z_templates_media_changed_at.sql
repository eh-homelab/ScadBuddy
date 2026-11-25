-- #7317 final review M3: when a template's media last changed. Every media write
-- (an add, a removal, a reorder, a built-in's cover choice) sets it in its own
-- transaction, so a read sees that the row lags its media even when the list is now
-- empty or only the cover choice moved. A refresh leaves it alone.
ALTER TABLE templates ADD COLUMN media_changed_at timestamptz;

-- The rows from before: the time the media list was last written, as the reads used.
UPDATE templates t
SET media_changed_at = m.changed_at
FROM (
    SELECT template_id, max(created_at) AS changed_at FROM template_media GROUP BY template_id
) m
WHERE m.template_id = t.template_id;
