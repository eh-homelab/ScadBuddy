-- #3724: a hash of the raw response a person sent (canonical JSON, sha256 hex), written
-- by resolve_input in both stores. A retry after a continue-as-new, which Temporal's
-- per-run Update ids cannot join, is the same answer when its hash and responder match.
-- Null for a timer, a cancel, a grant, a flow's refusal and rows written before this.
ALTER TABLE ai_input_responses ADD COLUMN response_hash text;
