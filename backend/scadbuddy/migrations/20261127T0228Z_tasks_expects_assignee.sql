-- Task screens (#8062): what a task's scan fields expect, who it is for, and which places
-- are stations. `expects` holds `kind:<kind>` and `code:<CODE>` entries, so a scan finds
-- the open tasks that wait for it. `assignee` is a principal string; nothing refuses
-- another person's answer.
ALTER TABLE tasks ADD COLUMN expects text[] NOT NULL DEFAULT '{}';
ALTER TABLE tasks ADD COLUMN assignee text CONSTRAINT tasks_assignee CHECK
  (length(assignee) BETWEEN 1 AND 200 AND assignee !~ '[[:cntrl:]]');
CREATE INDEX tasks_open_expects ON tasks USING gin (expects) WHERE state = 'open';

ALTER TABLE filament_locations ADD COLUMN station boolean NOT NULL DEFAULT false;
COMMENT ON COLUMN filament_locations.station IS 'A station for flow tasks (#8062, spec D3)';
