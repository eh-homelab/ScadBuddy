-- #5276: when each recorded project was last made or found in Bambuddy, so an ensure
-- within `EXISTS_TTL_SECONDS` of it (`orders/projects.py`) asks Bambuddy nothing. In the
-- shop's record rather than each replica's memory (#5273 kept it there): every replica
-- reads the same, and a failed print of the order's clears it for all of them, so its
-- retry asks Bambuddy again rather than filing under a project deleted there. Null is
-- "ask": every row from before this file is asked once.
ALTER TABLE order_projects ADD COLUMN seen_at timestamptz;
