-- #1437: a `retired` row is a Work folder ScadBuddy made and may still delete from,
-- which no find uploads into any more. `_claim_legacy` writes one when a pre-#683 Work
-- folder is verified as ScadBuddy's on this instance but its slot already records
-- another Work folder (a find that made a new one first): dropping the row instead
-- would leave the files already in that folder in no recorded Work folder, and every
-- delete of them refused for good.
ALTER TABLE store_folders DROP CONSTRAINT store_folders_role_check;
ALTER TABLE store_folders
    ADD CONSTRAINT store_folders_role_check CHECK (role IN ('template', 'work', 'retired'));
