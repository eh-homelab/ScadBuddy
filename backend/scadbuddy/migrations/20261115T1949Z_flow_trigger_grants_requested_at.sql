-- #6399: when a standing grant last started to wait for a person. An edit of its trigger
-- returns an approved grant to `requested` in place (ruling 20), keeping its id and
-- created_at, so the request's own time is this column: the bell's "asked … ago", the
-- order of "Needs you", Web Push's 24 h window and its notice id read it.
ALTER TABLE flow_trigger_grants ADD COLUMN requested_at timestamptz;
UPDATE flow_trigger_grants SET requested_at = created_at WHERE requested_at IS NULL;
ALTER TABLE flow_trigger_grants ALTER COLUMN requested_at SET DEFAULT now();
ALTER TABLE flow_trigger_grants ALTER COLUMN requested_at SET NOT NULL;
