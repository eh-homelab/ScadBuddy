-- #7419 review: `expire_jobs`, `cancel_job` and each NFC job read look up a job's
-- `agent_write` backups by `job_id`; only those backups name a job.
CREATE INDEX filament_tag_backups_job ON filament_tag_backups (job_id, at DESC)
    WHERE job_id IS NOT NULL;
