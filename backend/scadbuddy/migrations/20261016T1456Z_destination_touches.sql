-- #3651: when ScadBuddy last put something in, or chose, each Bambuddy project and
-- library folder: printed into, filed into by Generate, or picked as a destination. The
-- destination picker orders by it ("last touched"), together with the folder's own
-- latest activity in Bambuddy. Bambuddy's ids, like project_print_targets: ScadBuddy
-- keeps no project or folder of its own.
CREATE TABLE IF NOT EXISTS destination_touches (
    kind text NOT NULL CHECK (kind IN ('project', 'folder')),
    target_id bigint NOT NULL,
    touched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, target_id)
);
