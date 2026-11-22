# Hosted plugins and marketplace feeds (epic #6931)

Spec: `docs/superpowers/specs/2026-11-21-hosted-plugin-feeds-design.md` (merged in #6928).
The spec is the authority. Each task is one sub-issue and one PR.

## Global constraints

- Read the repo's `CLAUDE.md` and the area `CLAUDE.md` files for each tree you touch
  (`backend/CLAUDE.md`, `agent/CLAUDE.md`, `frontend/CLAUDE.md`).
- Only the CRUD service (the API) gets SQL access. The agent reaches every new table over
  CRUD routes under `/api/v1/internal/crud/agent/` with its service credential
  (`crud_agent_packages.py` is the area to extend). Never a new database client.
- Bytes go to the ContentStore as blobs, never `bytea`, never base64 in JSON (#2479).
- Migrations: a NEW file `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` in
  `backend/scadbuddy/migrations_agent/` plus its entry in
  `agent/src/db/expected-migrations.json`
  (`UPDATE_EXPECTED_MIGRATIONS=1 pnpm exec vitest run test/schema.test.ts`). Never edit a
  merged migration.
- New tables take the `ai_` prefix.
- Fit the existing package model (`agent/src/plugins/packages/`, `store.ts` lifecycle):
  install unapproved, approve the exact pin, enable, re-pin into `pending_*` with a diff.
  Never auto-approve, never auto-update.
- Installing and adding to a feed are outward settings writes. The MCP tool that publishes
  returns a pending action (`confirm_action`).
- Every tool works in classic and durable sessions.
- A UI change loads the `frontend-design` skill.
- Tests: a parallel backend or agent test run uses its own database (NOTIFY cross-talk).
  Use the uv version the Dockerfile pins (`uvx uv@<version>`), not the local uv.
- Never `pkill -f`, `killall` or any machine-wide kill. Kill only by pid or process group.
- PRs: conventional-commit title, body `Fixes #N`, cite sources for external facts, open
  ready (not draft), no auto-merge (the controller merges). Commit trailer:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. PR body ends with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Never commit `backend/openapi.json` or either `schema.d.ts`.

## Rulings

- R1: Task 4 (#6935) owns the one `ai_plugin_packages` pin migration of spec §6.5:
  `pin_kind`, `version`, `bytes_sha256`, their `pending_*` forms, `source_kind`
  `hosted` and `marketplace-url`/`archive`/`npm`, and the replacement CHECKs. Task 3
  builds on it. So Task 3 needs Tasks 1 and 4.
- R2: Tasks 1 and 4 run in parallel in separate worktrees. Whichever merges second
  rebases and regenerates `expected-migrations.json`.
- R3: The hosted plugin's package name must not clash with an installed or built-in
  package: `POST /api/v1/hosted-plugins` answers 409 when an `ai_plugin_packages` row or
  a built-in has that name.

## Task 1: hosted plugin drafts, files and immutable versions (#6932)

Spec §6.1 (the `ai_plugin_feeds` with its `default` row, `ai_hosted_plugins`,
`ai_hosted_plugin_files` and `ai_hosted_plugin_versions` tables; leave
`ai_plugin_feed_packages` to Task 5), §6.2 (the `/api/v1/hosted-plugins` routes, path
rules, caps, executables only under `skills/`; not the MCP tools or the UI), §6.3 steps
1–4 (validate, generate `plugin.json`, deterministic zip, one transaction). Also the CRUD
read route the agent will use for a version's zip
(`GET /api/v1/internal/crud/agent/hosted-plugins/{name}/versions/{N}.zip`) and a CRUD
read of a plugin's `latest_version`. The `content_hash` must equal what
`agent/src/plugins/packages/hash.ts` computes for the unpacked tree: port its algorithm
and add a test with a fixture whose hash is computed by the TypeScript code. Backend
pytest for every route and rule, including the same-draft-same-bytes zip test.

## Task 2: the feed, served by the agent on the /mcp auth (#6933)

Spec §6.4. Needs Task 1 (and lists only what Task 5's `ai_plugin_feed_packages` holds; if
Task 5 has not landed, this task adds that table and its CRUD read, and Task 5 adds the
write routes). Agent routes under `/api/v1/ai/plugin-feeds/{feed}/`, authenticated as
`/mcp` is (`agent/src/auth/authenticate.ts`). The Settings → MCP access feed section (load
`frontend-design`). Check §9's front-door reachability and record the evidence on the
issue.

## Task 3: the assistant loads hosted plugins (#6934)

Spec §6.3 step 5 and §6.5. Needs Tasks 1 and 4 (R1). `PackageSource` kind `hosted`;
`PackageInstaller` install and re-pin for it (decided by whether the package row exists);
materialise from the CRUD zip route, hash check, `vetPackage`; the UI-guarded routes
accept the new source; the "version N not offered" state on the package view.

## Task 4: install from any URL Claude Code takes, pinned to bytes (#6935)

Spec §6.6 and R1. `marketplace-url`, `archive` and `npm` sources; `marketplaceTarget()`
accepts `archive` and `npm` entries and still refuses `command`; bytes pins with the
mirror CRUD routes (`package-mirrors`); safe unpacking; the pin migration of R1. Agent
vitest and backend pytest for the CRUD routes.

## Task 5: outside packages in the feed (#6936)

Spec §6.7. Needs Tasks 2 and 4.

## Task 6: the hosted plugin editor in Settings (#6937)

Spec §6.2 (UI) and the Offer button of §6.3 step 5. Needs Tasks 1 and 3. Load
`frontend-design`. Playwright e2e for create, edit, publish and offer.

## Task 7: MCP tools to author and publish hosted plugins (#6938)

Spec §6.2 (tools). Needs Tasks 1 and 3.
