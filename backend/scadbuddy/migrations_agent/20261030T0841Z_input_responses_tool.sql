-- #5003: an AgentSession turn's attention requests (the gate's `wait_for_user` entries) count
-- against #815 §5's rate limits, as a classic turn's `ai_questions` rows do
-- (questions/service.ts `attentionLimit`). An entry's row in `ai_pending_input` is deleted
-- when it resolves, so its response keeps what the count reads: the tool, and when it was
-- asked (gate/store.ts and its sweep write both). Null on rows written before this, and on
-- agent-durable's (its store.py writes neither, so it never depends on this migration having run).
ALTER TABLE ai_input_responses
  ADD COLUMN tool text,
  ADD COLUMN requested_at timestamptz;
CREATE INDEX ai_input_responses_attention ON ai_input_responses (requested_at) WHERE tool = 'wait_for_user';
