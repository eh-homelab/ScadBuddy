-- #7168: a place's tag, written by the swap page, is a backup reason of its own.
ALTER TABLE filament_tag_backups DROP CONSTRAINT filament_tag_backups_reason_check;
ALTER TABLE filament_tag_backups
    ADD CONSTRAINT filament_tag_backups_reason_check
    CHECK (reason IN ('spool_write', 'agent_write', 'restore', 'place_write'));
