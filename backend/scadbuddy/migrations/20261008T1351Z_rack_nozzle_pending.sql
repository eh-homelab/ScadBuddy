-- #1079: Least used counts open picks, the picks whose print has not settled yet
-- (spec docs/superpowers/specs/2026-10-01-rack-nozzle-selection-design.md §4). A print
-- row now names the queue item it settled, so an open pick is one with no print row for
-- its item and group.
ALTER TABLE rack_nozzle_prints ADD COLUMN queue_item_id integer;

-- The rows settled before this: their item is the one the archive's link names (the
-- settle wrote them from that link), with a pick for the same group.
UPDATE rack_nozzle_prints AS p
SET queue_item_id = k.queue_item_id
FROM print_links AS l
JOIN rack_nozzle_picks AS k ON k.queue_item_id = l.queue_item_id
WHERE l.archive_id = p.archive_id AND k.group_id = p.group_id AND p.queue_item_id IS NULL;

CREATE INDEX rack_nozzle_prints_queue_item ON rack_nozzle_prints (queue_item_id, group_id);
CREATE INDEX rack_nozzle_picks_serial ON rack_nozzle_picks (serial, picked_at);
