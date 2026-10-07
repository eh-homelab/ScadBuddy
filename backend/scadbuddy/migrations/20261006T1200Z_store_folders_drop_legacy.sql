-- #1429: retire the pre-#683 `store_folders` rows instead of settling them.
--
-- 20261005T2000Z kept `instance DEFAULT ''` so rows recorded before folders were per
-- instance, and inserts from a still-draining older worker, landed as '' and were
-- claimed or dropped at runtime by verifying the folder tree. That path is gone: every
-- running release records its instance, and production had no `store_folders` rows at
-- all when this was written (checked 2026-10-06), so there is nothing to claim.
--
-- A dropped row only forgets a folder id. The next find for its slot adopts the
-- template folder and its `Work` folder again by name under the inbox (or makes them),
-- and records them under this instance; deletes in that `Work` folder then go through.
--
-- Without the DEFAULT, an insert that omits `instance` fails rather than recording a
-- folder no instance owns.
DELETE FROM store_folders WHERE instance = '';
ALTER TABLE store_folders ALTER COLUMN instance DROP DEFAULT;
