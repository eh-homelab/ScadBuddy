-- #683: a Bambuddy folder id means something only on the Bambuddy that issued it.
-- Keyed by the inbox alone, a `bambuddy_url` repointed at another instance with the
-- same inbox id would reuse the first instance's recorded ids: uploading into, and
-- deleting from, whatever unrelated folder has that id there.
--
-- `instance` is the Bambuddy base URL normalised by `instance_key` (#1431): scheme
-- and host lowercased, a default port dropped, trailing slashes stripped, so a
-- respelling of one URL is one instance. Rows from before this
-- carry '' (the URL is often an environment seed, which SQL cannot read). Each
-- process settles them before its instance's first find or delete
-- (`BambuddyContentBackend._claim_legacy`, which writes that same normalised key;
-- nothing here backfills, as SQL cannot read an environment-seeded URL): a row is claimed only when its folder
-- sits where ScadBuddy put it on that instance, and dropped otherwise, so ids from an
-- instance the URL was already repointed away from are never trusted.
--
-- The DEFAULT stays for the rollout: a render worker of the previous release, still
-- draining, inserts without `instance`, and its row lands as '' for the same check
-- rather than failing the render. Drop it in a later migration.
ALTER TABLE store_folders ADD COLUMN instance text NOT NULL DEFAULT '';
ALTER TABLE store_folders DROP CONSTRAINT store_folders_pkey;
ALTER TABLE store_folders DROP CONSTRAINT store_folders_folder_id_key;
ALTER TABLE store_folders ADD PRIMARY KEY (instance, inbox_id, slug, role);
ALTER TABLE store_folders ADD UNIQUE (instance, folder_id);
