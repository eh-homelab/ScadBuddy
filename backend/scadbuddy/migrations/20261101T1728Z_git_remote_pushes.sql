-- #4974: the off-box copy of the model and flow history. One row per repository
-- (`models`, `flows`): the last commit a push of it landed at the remote `url`, and how
-- the last attempt went. A row for another URL than the one set says nothing about it.
-- Written by every API replica (`git_remote/store.py`); `error` never holds the token.
CREATE TABLE git_remote_pushes (
    repository text PRIMARY KEY,
    url text NOT NULL,
    pushed_commit text,
    pushed_at timestamptz,
    attempted_at timestamptz NOT NULL DEFAULT now(),
    error text
);
