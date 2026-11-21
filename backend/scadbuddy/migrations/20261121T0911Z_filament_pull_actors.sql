-- Who started a Bambuddy pull a person asked for (#6772, plan Decision 181): the API writes
-- the row before it starts `FilamentBbPull` and deletes it once the run ends. The CRUD
-- pull routes read the actor here by the run's `pull_id`, never from the print worker's
-- body; a run with no row (the Schedule's) books as `bambuddy`. A row an API that died
-- mid-run left is deleted by a later write, an hour on.
CREATE TABLE filament_pull_actors (
    pull_id text PRIMARY KEY,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
