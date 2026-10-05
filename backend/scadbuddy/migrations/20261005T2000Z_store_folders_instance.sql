-- #683: a Bambuddy folder id means something only on the Bambuddy that issued it.
-- Keyed by the inbox alone, a `bambuddy_url` repointed at another instance with the
-- same inbox id would reuse the first instance's recorded ids: uploading into, and
-- deleting from, whatever unrelated folder has that id there.
--
-- `instance` is the Bambuddy base URL with no trailing slash. Rows from before this
-- carry '' and are claimed by the first process that uses the store afterwards
-- (`BambuddyContentBackend._claim_legacy`): the URL is often an environment seed,
-- which SQL cannot read, and that process runs against the instance they were made on.
ALTER TABLE store_folders ADD COLUMN instance text NOT NULL DEFAULT '';
ALTER TABLE store_folders ALTER COLUMN instance DROP DEFAULT;
ALTER TABLE store_folders DROP CONSTRAINT store_folders_pkey;
ALTER TABLE store_folders DROP CONSTRAINT store_folders_folder_id_key;
ALTER TABLE store_folders ADD PRIMARY KEY (instance, inbox_id, slug, role);
ALTER TABLE store_folders ADD UNIQUE (instance, folder_id);
