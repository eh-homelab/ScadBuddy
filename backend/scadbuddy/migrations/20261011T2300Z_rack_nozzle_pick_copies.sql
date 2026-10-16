-- #2134: a pick's queue item can print several copies (Bambuddy's ``quantity``), and
-- each copy settles its own print row. Least used counts a pick as pending for each
-- copy not yet settled, not once until the first copy settles. Picks recorded before
-- this are taken as one copy.
ALTER TABLE rack_nozzle_picks ADD COLUMN copies integer NOT NULL DEFAULT 1;
