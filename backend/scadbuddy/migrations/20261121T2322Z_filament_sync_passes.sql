-- #6905: one row per ScadBuddy-to-Bambuddy sync pass, so Settings → Filament and
-- `GET /filament/sync/passes` show the passes before the last one, the failed ones too.
-- `filament_bb_sync_state` still holds the last pass; `bb_sync_results.record_pass` writes
-- both in one transaction. `id` is the pass's own (the worker makes it when the pass
-- starts), so a CRUD client's retry of one record adds no second row. `started_at` is by
-- the worker's clock, `ended_at` by the database's. `trigger` is what started the pass
-- (`bb_sync_models.PassTrigger`), null from a worker that does not say. `error` is a fixed
-- label, never a message. The housekeeping's activity sweep deletes rows older than the
-- setting `filament_sync_pass_ttl_days` (30 days by default).
CREATE TABLE filament_bb_sync_passes (
    id uuid PRIMARY KEY,
    instance text,
    trigger text,
    started_at timestamptz NOT NULL,
    ended_at timestamptz NOT NULL DEFAULT now(),
    written integer NOT NULL DEFAULT 0,
    failed integer NOT NULL DEFAULT 0,
    deferred integer NOT NULL DEFAULT 0,
    conflicts integer NOT NULL DEFAULT 0,
    unmapped_k integer NOT NULL DEFAULT 0,
    skipped text,
    instance_changed boolean NOT NULL DEFAULT false,
    error text
);
-- Newest first, for the page and its cursor; the prune reads it from the old end.
CREATE INDEX filament_bb_sync_passes_newest ON filament_bb_sync_passes (ended_at DESC, id DESC);
