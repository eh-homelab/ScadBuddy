-- #5330: when a report last tried a session's orders still to report
-- (src/sessions/orderCosts.ts). The sweep takes the sessions it tried longest ago first,
-- the untried ones before all, so sessions it cannot report never take every place in it.
ALTER TABLE ai_session_order_costs ADD COLUMN tried_at timestamptz;
