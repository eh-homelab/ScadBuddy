-- #1093: several Claude credentials in priority order, each with its own
-- health (src/credentials.ts CredentialStore, src/harness/fallback.ts).
-- The existing row (id 'default') keeps its id, so its sealed secret still
-- opens (the AAD names the row id), and becomes priority 0, active.
--
--   priority        0 is tried first; unique, deferred so a reorder can
--                   permute every row in one transaction
--   status          active, cooling_down (rate limited until cooldown_until;
--                   usable again once that passes, with no write needed) or
--                   disabled (refused for good, until a person resets it or
--                   saves a new secret)
--   epoch           bumped by every new secret and every reset: a failure is
--                   recorded only against the epoch the query read, so a query
--                   that started with the old secret cannot disable the new one
--   last_error      why it was last refused, redacted of the secret
--   last_used_at    the last query that got an answer with it
ALTER TABLE ai_credentials
  ADD COLUMN priority       integer,
  ADD COLUMN status         text NOT NULL DEFAULT 'active',
  ADD COLUMN cooldown_until timestamptz,
  ADD COLUMN last_error     text,
  ADD COLUMN last_error_at  timestamptz,
  ADD COLUMN last_used_at   timestamptz,
  ADD COLUMN epoch          integer NOT NULL DEFAULT 0;

UPDATE ai_credentials SET priority = 0 WHERE id = 'default';
UPDATE ai_credentials c SET priority = n.rn
  FROM (SELECT id, row_number() OVER (ORDER BY created_at, id) AS rn FROM ai_credentials WHERE id <> 'default') n
  WHERE c.id = n.id;

ALTER TABLE ai_credentials
  ALTER COLUMN priority SET NOT NULL,
  ADD CONSTRAINT ai_credentials_priority_unique UNIQUE (priority) DEFERRABLE INITIALLY DEFERRED,
  ADD CONSTRAINT ai_credentials_status_check CHECK (status IN ('active', 'cooling_down', 'disabled')),
  ADD CONSTRAINT ai_credentials_cooldown_check CHECK ((status = 'cooling_down') = (cooldown_until IS NOT NULL));
