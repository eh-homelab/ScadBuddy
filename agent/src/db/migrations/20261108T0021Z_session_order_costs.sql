-- #5330 (job cost spec 2026-11-05 §4.4): the order items a session gave a design revision
-- (order_item_revision_add, _after_acceptance), so the session's spend is reported to the
-- backend as one `assistant` cost entry per order (src/sessions/orderCosts.ts). Written by
-- the tool call that made the revision; `reported_at` is set once the backend recorded the
-- entry (or refused it for good), and `reported_usd` is the part of the session's
-- `cost_usd` that report carried, so the next report carries only the spend since.
-- `attempted_usd` is the share a report sent and has no answer for yet: the next report
-- sends that same share again, so an answer lost after the backend wrote it loses no spend.
CREATE TABLE ai_session_order_costs (
  session_id     uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  order_id       text NOT NULL,
  order_item_id  text NOT NULL,
  at             timestamptz NOT NULL DEFAULT now(),
  reported_at    timestamptz,
  reported_usd   double precision NOT NULL DEFAULT 0 CHECK (reported_usd >= 0),
  attempted_usd  double precision CHECK (attempted_usd >= 0),
  PRIMARY KEY (session_id, order_id, order_item_id)
);
