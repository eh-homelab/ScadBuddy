-- #2476: what a library copy holds, less the provenance stamp that names the output it
-- was saved as (`render.provenance.content_key`). Two outputs of one identical render
-- differ only in that stamp, so a project folder that already has one's copy for a
-- target reuses it for the other instead of filing "<name> (2).3mf". NULL for copies
-- recorded before it, which are then never matched.
ALTER TABLE output_bambuddy_uploads ADD COLUMN content_key text;
CREATE INDEX output_bambuddy_uploads_content
    ON output_bambuddy_uploads (folder_id, target_key, content_key)
    WHERE content_key IS NOT NULL;
