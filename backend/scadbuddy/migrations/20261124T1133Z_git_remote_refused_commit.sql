-- #7388: the head the remote last refused for a reason no retry can fix (a size limit,
-- a pre-receive hook, a non-fast-forward, an auth refusal). That head is not pushed again
-- until it moves or `git_remote/push.py` `REFUSED_BACKOFF` has passed since
-- `attempted_at`. NULL after a push lands or a transport failure (timeout, connection).
ALTER TABLE git_remote_pushes ADD COLUMN refused_commit text;
