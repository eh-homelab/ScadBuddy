-- #2385: the HEAD the last writer left the models repository at. Every API replica
-- commits to the one repository on the NFS share; a writer holds the advisory lock
-- `library/repo_lock.py` takes, waits until git shows this HEAD, commits, and records
-- its own commit here before releasing. One row.
CREATE TABLE models_repo_head (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    head text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
