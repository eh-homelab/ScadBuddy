-- #2318: the input hash an approval was given for: the agent's inputHash (HMAC under a
-- key derived from the KEK) of the parked tool call's name and arguments, so the agent
-- runs a flow's tool call only when it is the call a person approved, not only one with
-- the approved call id. NULL for answers, denials, a ScadBuddy host function, and rows
-- from before it: the agent approves no tool call on a NULL hash.
ALTER TABLE workflow_run_decisions ADD COLUMN input_hash text;
