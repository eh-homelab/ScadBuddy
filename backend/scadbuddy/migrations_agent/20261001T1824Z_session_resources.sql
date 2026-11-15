-- #931 (agent sessions: record and show what a session touched): one row per
-- resource a session's tool call created, changed or deleted. Written inline
-- by src/tools/registry.ts runToolWithOutcome from each call's input and
-- result (src/sessions/touched.ts: successful calls, and the failed calls of
-- the few tools whose error still names what they made); read by
-- GET /api/v1/ai/sessions/:id/resources.
--
-- `resource_type` 'unclassified' is a write with no extractor: `resource_id`
-- is NULL and `tool` names the call, so the gap stays visible. `model_slug`
-- is the model the resource belongs to (a model's own slug for a model);
-- `before_id`/`after_id` are what it was and became where that has an id (a
-- revision's new commit, and its parent only where the call named one). A
-- `print` is always a Bambuddy queue item id, whichever tool queued it; a
-- `print_run` is ScadBuddy's own run id. Prints, print runs and deleted
-- outputs carry no `model_slug` (the call does not name the model), so a
-- lookup by model misses them.
CREATE TABLE ai_session_resources (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id     uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  at             timestamptz NOT NULL DEFAULT now(),
  tool           text NOT NULL,
  resource_type  text NOT NULL CHECK (resource_type IN
                   ('model', 'revision', 'preset', 'asset', 'render_job', 'output', 'print_run', 'print',
                    'unclassified')),
  resource_id    text,
  action         text NOT NULL CHECK (action IN ('created', 'modified', 'deleted')),
  model_slug     text,
  before_id      text,
  after_id       text,
  CHECK ((resource_type = 'unclassified') = (resource_id IS NULL))
);

CREATE INDEX ai_session_resources_session ON ai_session_resources (session_id, id);
-- "Which sessions touched this?" (#931's reverse link).
CREATE INDEX ai_session_resources_resource ON ai_session_resources (resource_type, resource_id);
CREATE INDEX ai_session_resources_model ON ai_session_resources (model_slug) WHERE model_slug IS NOT NULL;
