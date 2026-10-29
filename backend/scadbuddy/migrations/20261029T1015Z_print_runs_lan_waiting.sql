-- #4942: the native runs with prints still to start, neither all started nor stopped,
-- by printer (`PrintRunStore.lan_waiting`, read by every Prints page poll). Few rows
-- ever match, so the index stays small however many runs are kept.
CREATE INDEX IF NOT EXISTS print_runs_lan_waiting ON print_runs (printer, created_at DESC)
    WHERE status = 'succeeded'
        AND result->'lan_waiting' <> '[]'::jsonb
        AND result->>'lan_stopped' IS NULL;
