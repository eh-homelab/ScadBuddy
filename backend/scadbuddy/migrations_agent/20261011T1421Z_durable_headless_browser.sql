-- #2320: the headless browser in durable sessions. A durable turn holds no classic
-- claim (ai_sessions.turn_id stays NULL, plan 5c Ruling 7) and its approvals are
-- ai_input_responses rows, not ai_approvals. So:
--
-- `durable_turn_id`: the turn a durable session is running now, set by
-- DurableTurns.send with its claim and cleared when the turn ends (finish_turn, the
-- durable sweep). A grant dies with it, as a classic grant dies with turn_id.
ALTER TABLE ai_sessions ADD COLUMN durable_turn_id uuid;

-- A grant (src/harness/headlessGrants.ts) and an approved origin
-- (src/harness/browserOrigins.ts) name the approval they ran under: a classic
-- ai_approvals row, or a durable session's approved ai_input_responses row.
ALTER TABLE ai_headless_grants ALTER COLUMN approval_id DROP NOT NULL;
ALTER TABLE ai_headless_grants ADD COLUMN request_id text UNIQUE REFERENCES ai_input_responses (request_id) ON DELETE CASCADE;
ALTER TABLE ai_headless_grants ADD CONSTRAINT ai_headless_grants_one_approval
  CHECK ((approval_id IS NULL) <> (request_id IS NULL));

ALTER TABLE ai_browser_origins ALTER COLUMN approval_id DROP NOT NULL;
ALTER TABLE ai_browser_origins ADD COLUMN request_id text REFERENCES ai_input_responses (request_id) ON DELETE CASCADE;
ALTER TABLE ai_browser_origins ADD CONSTRAINT ai_browser_origins_one_approval
  CHECK ((approval_id IS NULL) <> (request_id IS NULL));
