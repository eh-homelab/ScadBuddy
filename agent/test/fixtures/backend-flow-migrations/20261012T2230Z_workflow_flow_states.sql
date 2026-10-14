-- #2485: a flow archived or deleted, by name (every version of it). A row only for a
-- flow that is or was either. Archived: hidden from the Flows tab by default and never
-- started, until unarchived. Deleted: `deleted_commit` removed `<name>/` from
-- `data/flows`; its versions stay in the history, so their rows stay and finished runs
-- still read the script they ran. Registering or restoring a version clears `deleted_*`.
-- `*_by` is who did it, as `workflow_definitions.created_by` records a registration.
CREATE TABLE workflow_flow_states (
    name           text PRIMARY KEY CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    archived_at    timestamptz,
    archived_by    jsonb,
    deleted_at     timestamptz,
    deleted_by     jsonb,
    deleted_commit text
);
