-- Durable sessions (#1056, final fix wave round 2): the take that marked a message `run`.
-- agent-durable inputs.py `start` passes a token per take (workflow.uuid4): a retry of
-- that take finds its own token and answers taken again, and any other take of a message
-- already `run` is refused, so no reordering of a run's loads and takes runs a message
-- twice. `release` (a Stop cut the take short) abandons only a message still pending or
-- taken with its token. NULL for rows taken before this column existed.
ALTER TABLE ai_durable_inputs ADD COLUMN taken_by text;
