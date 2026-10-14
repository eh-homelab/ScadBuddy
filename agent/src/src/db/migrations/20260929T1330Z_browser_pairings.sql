-- #254: pairing an external agent with the user's open ScadBuddy tab (spec
-- §8.5: "An external agent needs a pairing token that the user accepts in the
-- tab, in every auth mode"). A row starts `pending` when an MCP principal asks
-- to pair (the `browser_pair` tool): the agent is shown a short code, once,
-- and only its SHA-256 is kept here. The user types that code into the tab,
-- which pairs the principal with that tab (`paired`, `tab_id` set) until the
-- user disconnects it, a newer pairing of the same principal replaces it, or
-- `expires_at` passes. A request accepts one code once; wrong codes are
-- counted and too many deny it. See src/bridge/pairings.ts.
CREATE TABLE ai_browser_pairings (
  id              uuid PRIMARY KEY,
  -- The MCP principal (spec §8.1) that asked, as auth/principal.ts names it.
  principal_kind  text NOT NULL CHECK (principal_kind IN ('bearer', 'oidc', 'anonymous', 'flow')),
  principal_id    text NOT NULL,
  -- What the tab shows the user: the token's name, or the principal's kind and id.
  principal_label text NOT NULL,
  code_hash       text NOT NULL,
  status          text NOT NULL CHECK (status IN ('pending', 'paired', 'denied', 'ended')),
  -- The tab (frontend src/agent/link.ts TAB_ID) a paired row drives. The CHECK
  -- reads: `paired` has a tab; `pending` and `denied` have none; `ended` may
  -- have one or not (a pairing ends whether or not it was ever accepted).
  tab_id          text CHECK ((status = 'paired') = (tab_id IS NOT NULL) OR status = 'ended'),
  attempts        integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  -- `pending`: until when the code can be accepted. `paired`: until when the pairing lasts.
  expires_at      timestamptz NOT NULL,
  paired_at       timestamptz,
  ended_at        timestamptz
);
-- One live pairing per principal: accepting a new one ends the old in the same transaction.
CREATE UNIQUE INDEX ai_browser_pairings_one_per_principal
  ON ai_browser_pairings (principal_kind, principal_id) WHERE status = 'paired';
CREATE INDEX ai_browser_pairings_pending ON ai_browser_pairings (expires_at) WHERE status = 'pending';
CREATE INDEX ai_browser_pairings_tab ON ai_browser_pairings (tab_id) WHERE status = 'paired';
