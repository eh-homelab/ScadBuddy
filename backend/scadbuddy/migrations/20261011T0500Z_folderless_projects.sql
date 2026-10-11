-- #2263: the Bambuddy projects this ScadBuddy created whose library folder is not
-- linked yet. A row is written the moment Bambuddy answers the create and deleted once
-- the folder is linked, so one left behind is a project whose folder step failed; a
-- create pressed again with the same name and parent gives that project its folder
-- instead of making a second one (`bambuddy.projects.ensure_project`). Keyed by
-- Bambuddy's project id, like project_print_targets: ScadBuddy keeps no project of its own.
CREATE TABLE folderless_projects (
    project_id bigint PRIMARY KEY,
    name       text NOT NULL,
    parent_id  bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);
