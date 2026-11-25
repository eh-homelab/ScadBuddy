-- #7273 G7: what a tag held just before each write. One row per write, never updated or
-- deleted; a spool delete or an unbind keeps them. `job_id` names the agent's NFC job
-- (filament_nfc_jobs, a later migration) for an agent's write, as a plain uuid.
CREATE TABLE filament_tag_backups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tag_id uuid NOT NULL REFERENCES filament_tags (id),
    uid text NOT NULL,
    records jsonb NOT NULL,
    reason text NOT NULL CHECK (reason IN ('spool_write', 'agent_write', 'restore')),
    job_id uuid,
    actor text NOT NULL,
    at timestamptz NOT NULL DEFAULT now(),
    CHECK ((reason = 'agent_write') = (job_id IS NOT NULL))
);
CREATE INDEX filament_tag_backups_tag_at ON filament_tag_backups (tag_id, at DESC);
CREATE FUNCTION filament_tag_backups_keep() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'filament_tag_backups rows are never changed or deleted';
END $$;
CREATE TRIGGER filament_tag_backups_keep BEFORE UPDATE OR DELETE ON filament_tag_backups
    FOR EACH ROW EXECUTE FUNCTION filament_tag_backups_keep();
CREATE TRIGGER filament_tag_backups_keep_truncate BEFORE TRUNCATE ON filament_tag_backups
    FOR EACH STATEMENT EXECUTE FUNCTION filament_tag_backups_keep();
