-- A plugin package's admin may allow what the vetting refuses (command hooks,
-- hooks modules, stdio MCP servers, ...; src/plugins/packages/vet.ts
-- `refused`), for the one pin they approved: set with approved_at, and
-- reset by the approval of a re-pin. Such a package loads as it is, and its
-- code runs with the Claude credential in its environment.
ALTER TABLE ai_plugin_packages
  ADD COLUMN allow_refused boolean NOT NULL DEFAULT false,
  ADD CONSTRAINT ai_plugin_packages_allow_refused_approved CHECK (NOT allow_refused OR approved_at IS NOT NULL);
