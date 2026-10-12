-- #2503: the HEAD the last writer left the flows repository (`data/flows`) at, as
-- `models_repo_head` is for models (#2385). Every API replica commits to the one
-- repository on the NFS share; a writer holds the advisory lock
-- `library/repo_lock.py` `flows_coordinator` takes, waits until git shows this HEAD,
-- commits, and records its own commit here before releasing. One row.
CREATE TABLE flows_repo_head (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    head text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
