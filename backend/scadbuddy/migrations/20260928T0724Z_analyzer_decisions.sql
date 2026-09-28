-- Print-analyzer decisions (#284; `scadbuddy.analyzers.decisions`). One row per
-- rule, instance ('' for every instance) and scope; `body` is the whole decision.
CREATE TABLE IF NOT EXISTS analyzer_decisions (
    id            text PRIMARY KEY,
    diagnostic_id text NOT NULL,
    instance      text NOT NULL DEFAULT '',
    scope_kind    text NOT NULL,
    scope_key     text NOT NULL DEFAULT '',
    kind          text NOT NULL CHECK (kind IN ('accept', 'ignore', 'suppress')),
    body          jsonb NOT NULL,
    created_at    timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS analyzer_decisions_target
    ON analyzer_decisions (scope_kind, scope_key, diagnostic_id, instance);
