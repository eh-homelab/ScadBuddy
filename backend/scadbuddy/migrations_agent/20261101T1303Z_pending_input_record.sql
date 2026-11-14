-- ai_pending_input is the record of an open request (unified runtime spec 2026-10-13 §14.7
-- R5, plan Step 9.7, decision Q7). `gate` is what the gate's validator checks a response
-- against (gate/validate.ts GateEntry, without its state), written by open_input from the
-- AgentSession workflow's own entry. With it the respond route decides the request in its
-- own transaction, under the row's lock (gate/store.ts decideInput), and then wakes the
-- workflow, which reads the response row. NULL: the row's workflow holds the entry (a run
-- that opened it before `patched('gate-record-r5')`), and a response goes to it as the
-- `respond` Update, as before.
ALTER TABLE ai_pending_input ADD COLUMN gate jsonb;
