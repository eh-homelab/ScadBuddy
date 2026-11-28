-- #7984: a fleet login's machine lease is held for one config dir of the machine, and ends.
--
--   lease_dir         the config dir (`~/.claude` or another) the lease is for. A machine holds
--                     one lease per config dir, and a second one only while a rotation replaces
--                     the first (core's POST /leases `replacing`).
--   lease_expires_at  when the lease ends unless the machine renews it or writes the login back
--                     (24 hours on from each; tools/go/core/README.md, "Area `fleet-logins`").
--                     A lease that ended is free to any machine, and its holder's lease_epoch is
--                     stale once another machine takes it.
-- Both are read only with leased_to: a row with no leased_to holds no lease.
ALTER TABLE ai_credentials
  ADD COLUMN lease_dir        text CHECK (char_length(lease_dir) <= 4096),
  ADD COLUMN lease_expires_at timestamptz;

CREATE INDEX ai_credentials_lease_holder ON ai_credentials (leased_to, lease_dir) WHERE leased_to IS NOT NULL;
