-- A plugin package's name may now be anything an admin approves (a reserved
-- or non-kebab name is an allowable refusal, src/plugins/packages/vet.ts),
-- but it is still a directory of the package cache and the row's key, so it
-- stays a safe path segment (vet.ts SAFE_NAME_RE).
ALTER TABLE ai_plugin_packages DROP CONSTRAINT ai_plugin_packages_name_check;
ALTER TABLE ai_plugin_packages
  ADD CONSTRAINT ai_plugin_packages_name_check CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$');
