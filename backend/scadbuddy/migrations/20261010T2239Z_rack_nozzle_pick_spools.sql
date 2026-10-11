-- #2170: which spools ran through each rack hotend. One row per filament slot of a
-- picked group, written with the pick (`rack/usage.py` `record_picks`), as the spool was
-- then: a spool deleted from Bambuddy later still reads by its label. A print's history
-- is its `rack_nozzle_prints` row joined on (queue_item_id, group_id), so a hotend's
-- spools follow its serial, never its position.
CREATE TABLE rack_nozzle_pick_spools (
    queue_item_id integer NOT NULL,
    group_id      integer NOT NULL,
    slot_id       integer NOT NULL,
    -- Bambuddy's inventory spool; NULL when the slot printed from no inventory spool.
    spool_id      integer,
    label         text,
    material      text,
    -- `#RRGGBB`.
    colour        text,
    -- The slicer's grams for one plate, NULL when it did not say.
    grams         numeric,
    PRIMARY KEY (queue_item_id, group_id, slot_id)
);
