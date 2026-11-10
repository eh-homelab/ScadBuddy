-- #5298 Task 27: the copy lease (`bambuddy/copy_leases.py`). One row per library copy
-- section (#317), keyed as `BambuddyUploadStore.copy_lock` names it: held by ``token``
-- until ``expires_at``, renewed while the section runs. It replaces the session advisory
-- lock, which a worker without a database cannot take. The API's table alone: the print
-- worker takes, renews and releases a lease through `…/crud/print/copy-leases/{key}`.
CREATE TABLE IF NOT EXISTS copy_leases (
    key        text PRIMARY KEY,
    token      uuid NOT NULL,
    expires_at timestamptz NOT NULL
);
