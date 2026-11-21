-- Who started a Bambuddy pull a person asked for (#6772, plan Decision 181): the API writes
-- the row before it starts `FilamentBbPull` and deletes it once the read answered (a
-- report or a problem); a row left behind, because the read did not answer in time or the
-- API died mid-run, is deleted by a later `remember_actor` once it is an hour old. The CRUD
-- pull routes read the actor here by the run's `pull_id`, never from the print worker's
-- body; a run with no row (the Schedule's) books as `bambuddy`.
CREATE TABLE filament_pull_actors (
    pull_id text PRIMARY KEY,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
