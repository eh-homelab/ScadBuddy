-- Flow scripts in git (#2503): a flow's script is a file in its own repository,
-- `data/flows/<name>/flow.py`, and every version is a commit there. Postgres keeps
-- state, not source.
--
-- The old one-row-per-version table becomes `workflow_definition_versions`: what a
-- version id (the definition id the routes take, and each run's `definition_id`)
-- names, now a commit. `script` is only what a version registered before this
-- migration held; the backend's start imports each such version into the repository
-- as one commit, oldest first with its author and date, sets `commit_sha` and clears
-- `script` (flows/definitions.py `import_legacy`). That step is git, so it is not SQL.
-- `version` is the version's place in its flow's commit order, which the import
-- recounts: two consecutive versions with one script are one commit.
ALTER TABLE workflow_definitions RENAME TO workflow_definition_versions;
ALTER TABLE workflow_definition_versions
    RENAME CONSTRAINT workflow_definitions_pkey TO workflow_definition_versions_pkey;
ALTER TABLE workflow_definition_versions
    DROP CONSTRAINT workflow_definitions_name_version_key;
ALTER TABLE workflow_definition_versions ALTER COLUMN script DROP NOT NULL;
ALTER TABLE workflow_definition_versions ADD COLUMN commit_sha text;
CREATE INDEX workflow_definition_versions_commit
    ON workflow_definition_versions (name, commit_sha);

-- The index of flows, one row per name: its head version (`id`), the head commit, and
-- the approval timeout of its runs (NULL takes the global setting, 0 is never). Before
-- the import a migrated flow has no head commit yet.
CREATE TABLE workflow_definitions (
    name               text PRIMARY KEY CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    id                 text NOT NULL REFERENCES workflow_definition_versions (id),
    head_commit        text,
    approval_timeout_s integer CHECK (approval_timeout_s >= 0),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
INSERT INTO workflow_definitions (name, id, approval_timeout_s)
SELECT DISTINCT ON (name) name, id, approval_timeout_s
FROM workflow_definition_versions
ORDER BY name, version DESC;

-- The commit a run ran: its version's, set by the run's first activity (or, for a run
-- started before this migration, by the import).
ALTER TABLE workflow_runs ADD COLUMN commit_sha text;
