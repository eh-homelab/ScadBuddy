-- #317: the printer and nozzle each Bambuddy project last printed on, so the editable
-- 3MF filed into the project on Generate is laid out for them and a later print on the
-- same printer reuses it (`bambuddy.uploads.BambuddyUploadStore.project_target`).
-- Keyed by Bambuddy's project id; ScadBuddy keeps no project of its own.
CREATE TABLE project_print_targets (
    project_id      bigint PRIMARY KEY,
    printer_id      bigint NOT NULL,
    nozzle_diameter text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
