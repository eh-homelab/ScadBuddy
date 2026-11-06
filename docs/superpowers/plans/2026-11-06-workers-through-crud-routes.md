# Workers reach the database only through CRUD routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The render, print and projects workers make no SQL call. Each one reads and
writes through CRUD routes of the API (`/api/v1/internal/crud/<caller>/…`) with a
short-lived service credential. Then each worker loses its database secret, its Postgres
role and its grant list (#5298, step 1 of the epic #5295).

**Architecture:** For each store a worker uses, the API gets a thin route module that
calls the same store class it runs today, in the same transaction, with the same locks.
The worker gets a `Remote<Store>` with the same method names and the same sync or async
shape, so the callers do not change. The in-process workers of the API
(`SCADBUDDY_TEMPORAL_WORKER_INPROCESS`) keep the local stores. Logic that holds a lock or a
transaction across a call moves into the API. It is never split across HTTP.

**Tech stack:** Python 3.12 backend (FastAPI, psycopg 3, httpx, temporalio, pytest);
TypeScript agent only for `agent/src/db/grants.ts`; Kubernetes manifests in
eh-homelab/clusters.

**Rule:** root `CLAUDE.md` "Database access (#5295)"; `docs/security/trust-boundaries.md`.

**Spec:** `docs/superpowers/specs/2026-11-05-service-credentials-without-db-design.md`
(branch `docs/5325-service-credentials-design`, PR #5332), §6.4 (scopes `crud.render`,
`crud.print`, `crud.projects`, `flow.host`), §8 (migration) and §9 (the KEK).

**Base:** every `file:line` is `origin/main` at `edcb22be8` (2026-11-06) unless it names
`clusters`, which is eh-homelab/clusters `origin/main` at `92588fc9`. Paths without a prefix
are under `backend/scadbuddy/`.

**Prerequisite (assumed done):** #5494, the token exchange (`POST /api/v1/auth/service-token`,
the `render` caller, the exchanging `TokenReader`, the in-process provider, and spec §8
step 4, which stops the database read of a credential). This plan never reads a
credential from the database.

## Global constraints

- Backend check: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest -n auto`.
- Postgres tests need `SCADBUDDY_TEST_DATABASE_URL` (backend/CLAUDE.md "Commands"). Use a
  database of your own when another run is in progress (NOTIFY cross-talk).
- Migrations: a NEW file `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` in `backend/scadbuddy/migrations/`
  that sorts after `20261106T0933Z_library_copy_tries.sql` (check `ls | tail -1` first).
  Never edit a merged file. Every schema change in this plan is additive until the
  revoke tasks, so an old worker on SQL keeps working beside a new API.
- No workflow code changes. Only activity implementations change, with the same activity
  names, inputs and outputs. So no `workflow.patched` and no new replay history.
- Every new route requires a service credential of its class **in every auth mode**,
  from its first PR (root CLAUDE.md "Database access"). `off` and `observe` do not relax it.
- Never add a grant. A task removes grant entries only when no worker SQL needs them.
  `grant_role` never revokes (`render/worker_role.py:91-92`), so a removed entry stays
  granted until the revoke task. That is what keeps old pods working during a roll.
- uv: the image's (`uvx uv@0.13.6`); never `uv lock` with a local uv.
- Kill processes only by pid or process group; never `pkill -f` or `killall`.
- PR titles are conventional commits; each body says `Part of #5298` (the last one per
  worker that ends it: `Fixes #5298` only on the last print PR).

## Decisions

Each decision takes the more secure option. The reason follows it.

- **D1. Route paths and classes.** Every route is under `/api/v1/internal/crud/<caller>/<area>/…`.
  Its class is `crud.<caller>`, decided by the path prefix in `auth/routes.py`
  `route_class` (not by module, so one route factory can serve two callers, as the
  operations routes do). Reason: classes stay disjoint (spec §6.4), and the completeness
  test (`auth/routes.py:1-7`) covers every new route.
- **D2. Every mode refuses.** A new dependency `require_crud(caller)` refuses a request
  unless the caller is a `Service` whose `caller` is `<caller>` and whose scopes hold
  `crud.<caller>`: 401 with no credential, 403 otherwise. It ignores
  `SCADBUDDY_AUTH_MODE`. The gate resolves a `sbsvc_` bearer in every mode
  (`auth/gate.py:88-99`), so this works in `off`. Reason: production runs `off`
  (trust page, "Zones"); a route that serves anyone in `off` would be a new open write path.
- **D3. A CRUD listener on port 8082.** The API serves a second listener (env
  `SCADBUDDY_CRUD_PORT`, default 8082) that answers only `/api/v1/internal/crud/*`,
  `/api/v1/auth/service-token` and `/healthz`; every other path is 404 there. The render
  worker reaches only 8082 (clusters NetworkPolicy). Reason: template Python runs in the
  render pod until #5296, and the API runs `off`, where 8080 makes any caller the browser
  principal. The render pod is deliberately not admitted to 8080 today
  (clusters `networkpolicy-agent.yaml:58`). The print and projects workers keep 8080
  (they need `internal.*` and `flow.host`) and also move to 8082 for their CRUD calls.
- **D4. Route-missing marker.** Every CRUD response carries `X-ScadBuddy-Crud: 1`. The
  client treats an answer without it (an old API pod's plain 404, `api/static.py:78-98`)
  as "route missing": retryable. A 404 with the header is "resource missing". Reason: the
  rolling deploy (§ "Rolling deploy").
- **D5. Idempotent writes.** A write is *natural* when a retry after a lost answer leaves
  the same state and gives the same answer (an `ON CONFLICT` on a key the caller chose, a
  state guard that answers the same). Prefer natural writes: the client chooses ids and
  tokens (lease tokens, install-slot tokens, event ids). Every other write takes an
  `Idempotency-Key` header, and the API keeps `(caller, key) → status, body` in a new table
  `crud_requests` written **in the same transaction** as the write, kept 24 h. The key is
  `sha256(workflow_id, activity_id, call ordinal)` inside an activity and a UUID per
  logical write elsewhere; the client reuses it on every retry. Reason: a blob-index `put`
  answers the object it replaced (`store/index.py:151-188`); a retry that answered the new
  object would make the caller delete what it just stored.
- **D6. Logic that spans a lock moves into the API.** No route holds a lock across two
  requests, except the copy lease (D9), which is a row with a TTL, not a lock.
- **D7. Events.** A write that publishes in its transaction (`publish_in`,
  `core/pg_events.py:458`) moves into the API with its event, so the event still commits
  with the change. Events a worker emits outside a transaction go to
  `POST …/events`, with a per-caller allowlist of kinds, written with
  `ON CONFLICT (event_id) DO NOTHING` (the column is UNIQUE,
  `migrations/20260928T0630Z_events.sql:5`) and NOTIFYed only when inserted. No worker
  LISTENs. Reason: LISTEN needs a database session; the one listener that matters
  (`worker.py:1189`, `RecentRuns.heard`) only drops a cache, and the worker then reads the
  latest run through a route on each use.
- **D8. Secrets in answers.** A route answers a secret in plaintext only to the caller that
  uses it: the render settings (render key, S3 keys) to `crud.render`; the Bambuddy key and
  the spool providers' keys in the print settings projection, and a printer's access code,
  to `crud.print`. Every such answer has `Cache-Control: no-store` and is logged with what
  was released and never the value. Reason: today the same pods hold the KEK and a database
  password, which open every secret (spec §10); after this, the render and print pods
  hold no KEK (spec §9).
- **D9. The copy lock becomes a lease.** `BambuddyUploadStore.copy_lock`
  (`bambuddy/uploads.py:143-164`) is a session advisory lock held across a Bambuddy upload.
  It becomes a row in a new table `copy_leases (key PRIMARY KEY, token uuid, holder,
  expires_at)`: acquire with a client token (long poll ≤ 25 s), renew every 30 s for a
  120 s TTL, release by token; the record step that ends the section names the token and is
  refused when the lease is not that token's (fencing). The API's own holders
  (`print_library/copies.py:163`, `print_library/sweep.py:280`, `bambuddy/send.py:913`,
  `:1132`) use the same lease. Reason: a session lock cannot outlive one HTTP request on one
  of three API replicas.
- **D10. Payload keys stay sealed in this plan.** The projects route answers the sealed
  data key and its `kek_id`, never the open key; the worker opens it with the KEK it still
  holds for the approval hash key (`flows/keys.py:194-202`). Creation, under the subject's
  advisory lock (`workflows/payload_codec.py:186-206`), moves to the API. A subject must be
  `flow-<uuid>` (read or create) or `session-<uuid>` (read only). The key-release route
  that removes the KEK from projects is #5342 (spec §11 D6). Reason: the in-cluster HTTP is
  plain text; a sealed key is useless without the KEK.
- **D11. A standalone render worker needs a remote store.** After the render series, a
  render worker with no database refuses to start on `store_backend = local`; the local
  backend runs only in-process. Reason: on `local` the worker shares the API's volume and
  git (`worker.py:277`), the phase-1 layout (README "Render worker"), which nothing deploys.
- **D12. Order: projects, then render, then print.** Projects is smallest (19 relations, one
  store class) and already calls the API, so it proves the pattern: the guard, the client,
  the marker, the rolling deploy. Render is next because it is the only pod with untrusted
  code beside a database password and no egress policy (trust page, "What each pod holds").
  Print is last: it has 34 tables but no untrusted code and an egress allowlist.
- **D13. Remove a role in four steps, never one.** (1) app: the worker needs no database;
  (2) clusters: remove the secret and the env from the Deployment; (3) app: the API revokes
  everything from the role and drops its row policies at start; (4) clusters: CNPG
  `ensure: absent`. Reason: a revoke in the same release as (1) breaks the old pods still
  rolling; a role with privileges cannot be dropped. Rollback past (1) needs (2) reverted
  first; the old API re-grants at its start, so (3) is safe to roll back while the role exists.
- **D14. Temporal and Postgres.** Temporal keeps its own database (`scadbuddy-temporal-db`,
  clusters `temporal-db.yaml`). No worker connects to it, and no Temporal SDK path opens the
  app database. Nothing to move.

## Inventory

How it was read: each worker's object graph from `worker.py` (`build_worker_deps`
`:200-289`, `build_print_deps` `:1137-1313`, `run_projects_worker` `:1385-1482`), every
activity registered on its queues, and every SQL statement in the store classes they
build, found with an AST scan of string constants for `FROM|INTO|UPDATE|JOIN` and lock
keywords, then checked by hand. One row is one store method (or one tight group) the
worker's graph calls. R = read, W = write.

### Projects worker (`--queue projects`): 27 accesses

| # | file:line | Table / view | R/W | What for | Semantics that matter |
|---|---|---|---|---|---|
| J1 | `auth/caller_grants.py:439-454`, `render/worker_role.py:214-242` | `scadbuddy_migrations`, catalog | R | wait for the API's migrations and grants | polls every 5 s |
| J2 | `worker.py:1422-1424` | `service_credentials_projects` view | R | its credential | replaced by #5494 |
| J3 | `core/pg_events.py:368-412` | LISTEN `scadbuddy_events`; `events` | R | bus start | holds a session; no listener uses it |
| J4 | `core/pg_events.py:549-570` | `events` | W (DELETE) | prune on a timer | not granted: fails and logs |
| J5 | `library/settings_store.py:841-913` | `settings`, `model_print_choices`, `printer_bed_types`, `printers` | R | `operation_retention_seconds` (`workflows/operation_activities.py:60`) | REPEATABLE READ snapshot; opens sealed rows with the KEK |
| J6 | `flows/store.py:901-932` | `workflow_runs`, `workflow_definitions` | R/W | `flow_record` (`workflows/flow_activities.py:22`) | ON CONFLICT; `publish_in` |
| J7 | `flows/store.py:934-996` | `workflow_runs` | R/W | `flow_project` local activity | FOR UPDATE; `publish_in` |
| J8 | `flows/store.py:876-881` | `workflow_runs` | R | `get_run` (grants, kinds) | |
| J9 | `flows/store.py:644-651` | `workflow_runs` | R | `run_talking_to` (`workflows/flow_grants.py:201`) | |
| J10 | `flows/store.py:883-888` | `workflow_runs` | R | `reset_floor` (`flows/operations.py:744`) | |
| J11 | `flows/store.py:998-1081` | `workflow_runs` | R/W | `reset_run` (`flows/operations.py:808`) | **FOR UPDATE held across the Temporal Reset RPC** (`:262-294`) |
| J12 | `flows/store.py:749-806` | `workflow_runs`, `workflow_run_proposals` | R/W | `move_hop` | FOR UPDATE; ON CONFLICT; `publish_in` |
| J13 | `flows/store.py:700-706` | `workflow_run_decisions` | R | `get_decision` | |
| J14 | `flows/store.py:657-698` | `workflow_runs`, `workflow_run_decisions` | R/W | `record_decision` | FOR UPDATE on the run; ON CONFLICT |
| J15 | `flows/store.py:857-859` | `workflow_run_decisions` | W | `delete_decision` | |
| J16 | `flows/store.py:708-747` | `flow_grants`, `flow_grant_uses`, `workflow_run_decisions` | R/W | `record_grant_decision` | FOR SHARE OF the grant |
| J17 | `flows/store.py:861-867` | `workflow_run_decisions` | W | `withdraw_grant_decision` | |
| J18 | `flows/store.py:522-527` | `flow_grant_uses` | R | `grant_use` | |
| J19 | `flows/store.py:459-464` | `flow_grants` | R | `get_grant` (`flows/grants.py:450`) | |
| J20 | `flows/store.py:538-631` | `flow_grants`, `flow_trigger_grants`, `flow_grant_uses` | R/W | `consume_grant` (`flows/grants.py:513`) | FOR UPDATE; **runs a Python callback `pick` inside the transaction** |
| J21 | `flows/store.py:808-815` | `workflow_run_proposals` | R | `get_proposal` | |
| J22 | `flows/store.py:817-842` | `ai_sessions`, `ai_mcp_tokens` | R | `parent_tiers` | agent tables |
| J23 | `flows/store.py:844-855` | `ai_settings` | R | `approval_expiry_s` | agent table |
| J24 | `operations/store.py:206-258` | `operations` | R/W | `op_insert` (`operation_activities.py:56`) | DELETE past retention; ON CONFLICT (workflow_id, workflow_run_id); `publish_in` |
| J25 | `operations/store.py:260-292` | `operations` | R/W | `op_finish` | state guard; `publish_in` |
| J26 | `workflows/payload_codec.py:178-224` | `ai_payload_keys`, `ai_forgotten_subjects` | R/W | a run's payload data key | `pg_advisory_xact_lock(hashtext(subject))` shared with the agent's forgetSubject |
| J27 | `core/pg_events.py:292-299` (through J6, J7, J11, J12, J24, J25) | `events`, `pg_notify` | W | the run's and the operation's events | `EVENT_LOG_LOCK` held to commit |

### Render worker (default queue): 24 accesses

| # | file:line | Table / view | R/W | What for | Semantics that matter |
|---|---|---|---|---|---|
| R1 | `worker.py:246`, `render/worker_role.py:249-286` | `scadbuddy_migrations`, catalog | R | wait for migrations and grants | |
| R2 | `library/settings_store.py:1367-1394`, `store/bambuddy.py:134-157`, `worker.py:182-190` | `render_settings` view | R | store backend, Bambuddy URL, render key, S3, solid concurrency; re-read every 30 s | opened with the KEK |
| R3 | `render/projection.py:218-280` | `render_jobs`, `blob_refs` | R/W | `render_accept` (`workflows/activities.py:865-897`) | QueueFull check and insert in one txn; ON CONFLICT on workflow ids; `publish_in` |
| R4 | `render/projection.py:282-291` | `render_jobs` | W | `render_claims` (`activities.py:899-901`) | 5 s pool wait in a 10 s attempt |
| R5 | `render/projection.py:375-385` | `render_jobs` | W | `project` running | state guard; `publish_in` |
| R6 | `render/projection.py:387-393` | `render_jobs` | W | `project` steps | |
| R7 | `render/projection.py:395-442` | `render_jobs` | W | `project` settle | state guard; returns whether it settled |
| R8 | `render/projection.py:444-451` | `render_jobs` | R | `project` read | |
| R9 | `activity/index.py:205-307` | `activity` | R/W | the job's activity row in R3, R5, R7 | row policy `id LIKE 'render:%'` (`render/worker_role.py:71-74`); takes the event-log lock first |
| R10 | `core/pg_events.py:458-475` | `events`, `pg_notify` | W | job.* events | `EVENT_LOG_LOCK` to commit |
| R11 | `store/refs.py:24-30` | `blob_refs` | W | hold a finished job's blobs (`activities.py:964`, `pipeline_activities.py:330`) | ON CONFLICT DO NOTHING |
| R12 | `store/geometry.py:33-62` | `piece_geometry` | R/W | reuse, record, forget a geometry (`activities.py:593`, `:604`, `:633`, `:811`) | |
| R13 | `store/index.py:63-77`, `:225-231` | `store_blobs` | R | `get`, `by_sha`, `now` | |
| R14 | `store/index.py:151-223`, `:80-149` | `store_blobs` | R/W | `put`, `swap` | row locks in key order, FOR UPDATE/FOR SHARE, deadlock retry; **answer is the replaced object** |
| R15 | `store/index.py:233-277` | `store_blobs` | R/W | `mark`, `touch`, `delete`, `delete_if_stale` | FOR UPDATE in `mark` |
| R16 | `store/index.py:279-289`, `store/bambuddy.py:325-330` | `store_blobs` | R | `shares_backend_id`, `_recorded_id` | |
| R17 | `store/bambuddy.py:397-437` | `store_folders` | R/W | find or create a Bambuddy folder | **`pg_advisory_xact_lock` with a 30 s lock_timeout held across Bambuddy calls** |
| R18 | `store/bambuddy.py:439-458` | `store_folders` | R/W | forget a model's folders; list Work folders | |
| R19 | `library/assets.py:556-586` | `assets` | R/W | `get`, `use` (marks used) | waits on the sweep's row lock |
| R20 | `library/assets.py:668-688` | `assets` | W | `adopt` a fetched upload | `pg_advisory_xact_lock(asset)`; ON CONFLICT |
| R21 | `library/assets.py:810-845`, `worker.py:998` | `assets` | R/W | `prune_local` its copies | **FOR UPDATE held while it unlinks a local file** |
| R22 | `library/libraries.py:475-500` | `library_leases` | R/W | checkout leases (`activities.py:556`, `:768`) | `pg_advisory_xact_lock_shared(REMOVAL_LOCK)` against the API's removal |
| R23 | `library/libraries.py:502-525`, `:683` | `library_pin_holds` | R/W | pin holds while fetching | same shared lock |
| R24 | `library/libraries.py:737-776`, `:1373` | `library_install_slots` | R/W | install permits | slot claim with a token and a TTL |

The render worker builds a `PgNotifyEventBus` but never starts its listener
(`worker.py:219-223`), so it makes no LISTEN and no outbox write.

### Print worker (`--queue bambuddy`): 31 accesses

| # | file:line | Table / view | R/W | What for | Semantics that matter |
|---|---|---|---|---|---|
| Q1 | `auth/caller_grants.py:439-454` | `scadbuddy_migrations` | R | wait | |
| Q2 | `worker.py:1158-1160` | `service_credentials_print` view | R | its credential | replaced by #5494 |
| Q3 | `library/settings_store.py:841-913` (`worker.py:1196`, `:1203`, `:1221`, `:1229`, `:1214`, `:1334`) | `settings`, `model_print_choices`, `printer_bed_types`, `printers` | R | the Bambuddy key and URL, print options, choices, plates, retention, spool providers | REPEATABLE READ; KEK opens sealed rows |
| Q4 | `core/pg_events.py:368-412`, `worker.py:1189` | LISTEN `scadbuddy_events` | R | `RecentRuns.heard` drops its cache on `print.run` | session-long LISTEN |
| Q5 | `core/pg_events.py:434-456`, `:572-594` | `events`, `pg_notify` | W | out-of-transaction emits: `bambuddy/follow.py:212`, `bambuddy/progress.py:611`, `:639`, `printers/watch.py:189`, `printers/store.py:425` | outbox, autocommit |
| Q6 | `core/pg_events.py:549-570` | `events` | W (DELETE) | prune | not granted: fails and logs |
| Q7 | `core/pg_events.py:458-475` (through `bambuddy/runs.py:357`, `bambuddy/outcomes.py:113`, `operations/store.py:167`) | `events` | W | in-transaction events | `EVENT_LOG_LOCK` |
| Q8 | `activity/index.py:205-307` | `activity` | R/W | run and operation activity rows | event-log lock first |
| Q9 | `operations/store.py:206-292` | `operations` | R/W | `op_insert`, `op_finish` for the Bambuddy kinds (`bambuddy/operations.py:419-435`) | as J24, J25 |
| Q10 | `bambuddy/runs.py:496-538` | `print_runs`, `printers` | R/W | `print_insert` (`workflows/print_activities.py:411`) | DELETE past retention; ON CONFLICT |
| Q11 | `bambuddy/runs.py:540-610` | `print_runs` | R/W | `start_enqueue`, `update_result`, `succeed`, `fail`, `fail_lost` | state guards; `publish_in` |
| Q12 | `bambuddy/runs.py:367-494` | `print_runs` | R | `find`, `get`, `get_many`, `latest_for_output`, `lan_waiting`, `running_for` | |
| Q13 | `bambuddy/runs.py:410-435` | `print_runs` | R/W | `record_lan_stopped` | FOR UPDATE |
| Q14 | `bambuddy/outcomes.py:77-114` | `print_outcomes`, `print_runs` | R/W | the follow's outcome (`worker.py:1231`) | ON CONFLICT; `publish_in` |
| Q15 | `bambuddy/uploads.py:643-918` | `output_bambuddy_uploads`, `output_bambuddy_slices` | R/W | copies and slices: `for_output(s)`, `record`, `same_content`, `shared`, `recorded`, `touch`, `forget`, `record_sliced`, `slices_of`, `forget_gone_slice`, `record_slice_hash`, `sent_between` | ON CONFLICT |
| Q16 | `bambuddy/uploads.py:339-414` | `print_preview_slices` | R/W | preview slices: record, read, sliced, retire, `claim_preview` | `claim_preview` is a claim (bool) |
| Q17 | `bambuddy/uploads.py:443-489` | `print_slice_starts`, `print_preview_slices` | R/W | `record_slice_start` and the slice-job reads | bigserial |
| Q18 | `bambuddy/uploads.py:556-640` | `project_print_targets`, `folderless_projects`, `destination_touches` | R/W | Bambuddy project targets | ON CONFLICT |
| Q19 | `bambuddy/uploads.py:143-164` | none (advisory lock) | lock | `copy_lock` | **session advisory lock on its own connection, held across a Bambuddy upload; the API holds the same keys** |
| Q20 | `bambuddy/print_links.py:223-364` | `print_links`, `print_sends` | R/W | links, sends, `pending_library` (claims rows), `link_library`, `library_gone` | ON CONFLICT |
| Q21 | `library/output_prints.py:72-155` | `output_last_prints` | R/W | an output's last print (`worker.py:1224`, `:1257`) | ON CONFLICT |
| Q22 | `printers/store.py:311-343` | `printers` | R | `get`, `by_serial`, `by_bambuddy_id`, `list` | |
| Q23 | `printers/store.py:571-594`, `worker.py:1182-1183` | `printers` | R | `open_access_code` with the KEK | a secret |
| Q24 | `printers/store.py:611-743`, `:401-469`, `:162-220` | `printers`, `settings`, `activity`, `printer_bed_types`, `tray_spool_links` | R/W | `ensure_bambuddy(_written)` and its merge | `pg_advisory_xact_lock('scadbuddy:printers')` (`:68`); rewrites every `PRINTER_REFERENCES` table |
| Q25 | `printers/store.py:471-500`, `:765-830` | `printers` | R/W | `mark_bambuddy_active`, `mark_virtual_listed`, `record_bambuddy_view`, `set_lan_sync_error`, `upsert_virtual` | FOR UPDATE in `record_bambuddy_view` |
| Q26 | `printers/reports.py:73-124` | `printer_reports`, `printers` | R/W | the watch (`printers/watch.py:173-201`): `put`, `touch`, `get`, `all` | FOR UPDATE; **`put` answers the print that ended on this write only** |
| Q27 | `rack/usage.py:190-628` (`worker.py:1175`) | `rack_nozzle_seen`, `rack_nozzle_picks`, `rack_nozzle_pick_spools`, `rack_nozzle_prints` | R/W | rack usage; its own pool | `record_lan_picks` deletes and inserts |
| Q28 | `spools/store.py:84-160`, `:163-400` | `spool_provider_records`, `spool_provider_refreshes`, `spool_record_links`, `physical_spools`, `tray_spool_links` | R/W | provider recache and tray links | `pg_advisory_xact_lock('scadbuddy:spools')` (`:65`) |
| Q29 | `spools/native.py:184-300` | `native_spools`, `native_spool_uses`, `print_runs` | R/W | a native print's spool uses | FOR UPDATE in `settle_uses` |
| Q30 | `slicing/store.py:72-310`, `slicing/held_presets.py:124-160` | `native_slices`, `held_local_presets` | R/W | native slice start and reads; held presets | `pg_advisory_xact_lock(workflow_id)` in `start` |
| Q31 | `worker.py:1270-1272` | none (KEK) | sign | transfer tokens for a LAN print's blob reads | KEK in the pod |

### Cross-check against the grant lists

- `RENDER_GRANTS` (`render/worker_role.py:34-66`, 13 relations) matches R1 to R24. Nothing
  missing.
- `PROJECTS_GRANTS` (`auth/caller_grants.py:100-121`, 14) plus `PROJECTS_AI_GRANTS`
  (`agent/src/db/grants.ts:15-21`, 5) match J1 to J27, except J4: `events` has no DELETE, so
  the bus's prune fails at every start and every `PRUNE_INTERVAL`. Moving the bus out fixes it.
- `PRINT_GRANTS` (`auth/caller_grants.py:43-91`, 34, and 3 sequences) match Q1 to Q31, except
  Q6 (the same prune failure). `take_preview`, `forget_preview` (DELETE on
  `print_preview_slices`) and `NativeSliceStore.finish` (`blob_refs`) are not granted; the
  API's sweeps call them, not the worker, so they are not in the inventory.
- `PRINT_GRANTS` comment says the merge rewrites `settings` and `activity`; Q24 confirms it.

## Things that are not a simple route

| Item | Where | Design |
|---|---|---|
| Reset under a row lock across a Temporal RPC | J11 | `POST …/runs/{id}/reset`. The API runs `FlowStore.reset_run` with its own `flows.client` (`flows/component.py:120-125`), so the lock and the RPC stay in one process. Idempotent by `request_id` (`flows/store.py:262-284`; Temporal dedupes on it). |
| A Python callback inside a locked transaction | J20 | `POST …/grants/{run_id}/consume` carries what `pick` closes over (`flows/grants.py:493-512`): `CallFacts`, `narrowing`, `flow_call_id`, `request_id`, `fn`, `tool`, `session_id`, `input_hash`. The API rebuilds `pick` and runs `choose` under the locks. Natural: a known `request_id` answers its first use (`flows/store.py:573`). |
| Session advisory lock across an upload | Q19 | D9's lease. |
| Advisory lock across Bambuddy calls | R17 | `POST …/store-folders/find`: the API finds or creates the folder itself, with the render target it reads from Settings. The worker sends `slug` and `role`. |
| Row lock across a local unlink | R21 | The lock only orders `prune_local` against `use` of the same local copy, and both run in the same worker. A per-asset `threading.Lock` in the worker replaces it; the route `POST …/assets/last-used` answers `last_used_at` for a batch of ids. |
| LISTEN | J3, Q4 | Removed (D7). |
| Events outside a transaction | Q5 | `POST …/events` (D7). |
| A key created under the agent's advisory lock | J26 | Creation moves into the API's route, under the same lock (D10). |
| Answers that are not idempotent | R14, R15, Q16, Q20, Q26, Q27, Q28 | `crud_requests` (D5), unless the task makes them natural with a client token. |
| A rack store with its own pool | Q27 | `RemoteRackUsage` replaces it; no pool in the worker. |

## Rolling deploy

All Deployments run the same image and roll together with `maxSurge: 1`,
`maxUnavailable: 0` (spec §2.4). So for minutes an old and a new pod of each run side by side.

**New worker, old API.** A new route may land on an old API pod. It answers 404 without
`X-ScadBuddy-Crud` (D4). The client retries a route-missing answer on a new connection
for up to 30 s, then raises `CrudUnavailableError`, which is retryable, so Temporal retries
the activity. Before it polls Temporal, a new worker waits until `GET …/<caller>/areas` lists
every area it needs (once the first new API pod is ready; `crud/client.py` `wait_for_areas`).
A credential minted by an old API pod lacks a new class: a 403 `out_of_scope` on a CRUD
route makes the client drop its token and retry inside the same 30 s. An exchanging caller
exchanges once more. A caller that still reads its view (no exchange deployed) reads it
again at most every 5 s (`TokenReader.invalidate`, `REREAD_MIN_SECONDS`), and finds a new
one: a new API pod's start mints one when the newest live credential's scopes differ from
`SCOPES` (`rotate_due`), and keeps the old one live until it expires, for the old pods.

**Old worker, new API.** The old worker still runs SQL. Its grants stay, because `grant_role`
never revokes and only D13 step 3 revokes. Schema changes are additive. The route runs the
same store method, so old SQL writers and new HTTP writers take the same row and advisory
locks, and concurrency is as today. The one new primitive, the copy lease, is in Task 27.

**Rollback** of an area PR: the old build uses SQL with grants that are still there. Rollback
past a "no database" PR: revert its clusters secret PR first (D13).

Per-step specifics are in each task's **Rolling deploy** line.

## Order and PR map

| PR | Tasks | Repo | Grants it removes |
|---|---|---|---|
| A foundation | 1-3 | app | none |
| P1 flows | 4-5 | app + agent | `workflow_runs`, `workflow_definitions`, `workflow_run_decisions`, `workflow_run_proposals`, `flow_grants`, `flow_grant_uses`, `flow_trigger_grants`; agent: `ai_sessions`, `ai_mcp_tokens`, `ai_settings` |
| P2 operations | 6 | app | `operations`, `events`, `settings`, `model_print_choices`, `printer_bed_types`, `printers` |
| P3 payload keys | 7 | app + agent | agent: `ai_payload_keys`, `ai_forgotten_subjects` |
| P4 flow.host | 8 | app | (scope `api` off `projects`) |
| P5 no database | 9 | app | `scadbuddy_migrations`: `PROJECTS_GRANTS` empty |
| P6, P7, P8 | 10-11 | clusters, app, clusters | role `scadbuddy_projects` |
| R0 network | 12 | clusters | none |
| R1-R7 | 13-19 | app | per task |
| R8 no database | 20 | app | `RENDER_GRANTS` empty |
| R9-R11 | 21-22 | clusters, app, clusters | role `scadbuddy_render`, KEK in render |
| Q1-Q10 | 23-32 | app | per task |
| Q11 no database | 33 | app | `PRINT_GRANTS` empty |
| Q12-Q14 | 34-35 | clusters, app, clusters | role `scadbuddy_print`, KEK in print |

## Review focus

1. A route that serves without a credential in `off` (D2). Task 1's test calls every
   `crud.*` route in `off` with no bearer and with each other caller's credential.
2. A retry after a lost answer that changes the answer (D5). Each write route's test calls it
   twice with the same key or token and asserts the same answer.
3. A lock or transaction split across two requests (D6). The reset, consume, folder find and
   copy lease tests run two callers at once.
4. A worker that still opens a database connection. Tasks 9, 20 and 33 build the worker with
   `psycopg.connect` and `ConnectionPool` patched to raise.
5. A secret in a log or a cached answer (D8).

---

### Task 1: CRUD route classes and the every-mode guard

**Files:**
- Modify: `auth/routes.py` (`RouteClass`, `route_class`, `would_refuse`)
- Modify: `auth/service_credentials.py` (`SCOPES`: add `crud.projects`, `crud.render`, `crud.print`; `flow.host` comes in Task 8)
- Create: `backend/scadbuddy/crud/__init__.py`, `crud/guard.py`
- Test: `tests/auth/test_routes.py`, `tests/crud/test_guard.py`

**Interfaces:**
- Produces: `RouteClass` gains `crud.projects`, `crud.render`, `crud.print`, `flow.host`;
  `route_class` answers `crud.<caller>` for a path under `/internal/crud/<caller>/`;
  `require_crud(caller: ServiceCaller) -> Depends` raising 401 `…/auth/signed-out` (no or
  dead credential) or 403 `…/auth/forbidden` (another caller, or no class), in every mode;
  `crud_router(caller) -> APIRouter` with prefix `/internal/crud/<caller>`,
  `include_in_schema=False`, the guard on every route, and a response hook that sets
  `X-ScadBuddy-Crud: 1` and `Cache-Control: no-store`.

- [ ] **Step 1:** Write `tests/crud/test_guard.py`: a test app with one `crud_router("projects")` route; in `off`, `observe` and `enforce` it answers 401 with no bearer, 403 for a `print` and an `agent` credential, 200 for a `projects` credential with `crud.projects`, 403 for a `projects` credential without it; every answer, refusals included, carries `X-ScadBuddy-Crud: 1`.
- [ ] **Step 2:** Run `uv run --frozen pytest tests/crud/test_guard.py -v`; expect failures (module missing).
- [ ] **Step 3:** Implement `crud/guard.py` and the `routes.py` changes. `would_refuse` treats `crud.*` and `flow.host` like `internal.*` (class must be in the scopes).
- [ ] **Step 4:** Extend `tests/auth/test_routes.py`: every mounted path under `/api/v1/internal/crud/<caller>/` has class `crud.<caller>` and depends on `require_crud(<caller>)` (walk `app.routes`, check `route.dependant`).
- [ ] **Step 5:** Run the backend check; commit `feat(auth): crud route classes, refused without a credential in every mode (#5298)`.

### Task 2: The CRUD client

**Files:**
- Create: `crud/client.py`
- Modify: `core/metrics.py` (`scadbuddy_crud_client_seconds{caller,area,outcome}` histogram)
- Test: `tests/crud/test_client.py`

**Interfaces:**
- Consumes: `ServiceAuth` (`auth/service_credentials.py`, exchanging after #5494).
- Produces: `CrudClient(base_url, caller, auth, *, timeout=10.0)` with `request(method, path, *, json=None, idempotency_key=None) -> httpx.Response` (async) and `CrudSyncClient` (same, sync, for stores called through `asyncio.to_thread`); exceptions `CrudUnavailableError` (route missing, 5xx, transport; retryable), `CrudRefusedError` (4xx problem with the header; carries the problem). `activity_key(ordinal: int) -> str` builds D5's key from `activity.info()`.

- [ ] **Step 1:** Tests against an `httpx.MockTransport`: a 404 without the header is retried on a new connection then raises `CrudUnavailableError` after the budget (inject a 0.2 s budget); a 404 with the header raises `CrudRefusedError`; a 403 `out_of_scope` calls `auth.refresh()` once and retries; a 5xx and a connect error retry with the same `Idempotency-Key`; the histogram counts each outcome.
- [ ] **Step 2:** Run; expect failures. Implement. Run; pass.
- [ ] **Step 3:** Backend check; commit `feat(crud): the workers' CRUD client (#5298)`.

### Task 3: The CRUD listener, `areas`, and the idempotency ledger

**Files:**
- Create: `migrations/<stamp>_crud_requests.sql` (`crud_requests(caller text, key text, route text, body_sha256 text, status int, body jsonb, created_at timestamptz default now(), PRIMARY KEY (caller, key))`, index on `created_at`)
- Create: `crud/idempotency.py`, `api/crud_areas.py`
- Modify: `main.py` (serve `SCADBUDDY_CRUD_PORT` with a path filter; prune `crud_requests` older than 24 h in the auth sweep, `main.py:1092-1106`), `core/settings.py` (`crud_port: int = 8082`)
- Test: `tests/crud/test_idempotency.py`, `tests/crud/test_crud_port.py`

**Interfaces:**
- Produces: `idempotent(conn, caller, key, route, body) -> Replay | None` (inside the caller's transaction: answers the stored reply, refuses the same key with another body as 422, or records after the write); `GET /api/v1/internal/crud/{caller}/areas -> {"areas": [str]}` (one route per caller, each under its own prefix), fed by a registry `CRUD_AREAS[caller]` that each later task appends to; the CRUD listener answers only `/api/v1/internal/crud/*`, `/api/v1/auth/service-token`, `/healthz`.

- [ ] **Step 1:** Tests: two calls with one key give one row and one write; the second answers the first's body; another body with the same key is 422; a rollback of the write leaves no ledger row; the CRUD port answers 404 (plain) for `/api/v1/models` and 401 for a crud route without a bearer.
- [ ] **Step 2:** Implement. The listener is a second `uvicorn.Server` on the same app with an ASGI path filter in front, started and stopped in the lifespan.
- [ ] **Step 3:** Backend check; commit `feat(crud): crud listener, areas and the idempotency ledger (#5298)`. Open PR A.

**Rolling deploy:** additive table and a new port; nothing calls them yet.

### Task 4: Projects: flow runs, hops, decisions and proposals routes

**Files:**
- Create: `api/crud_projects.py`
- Create: `crud/projects.py` (`RemoteFlowStore`, part 1)
- Test: `tests/crud/test_crud_projects_runs.py`

**Interfaces (prefix `/api/v1/internal/crud/projects`; bodies are the store's own models):**
- `POST /runs` `FlowRecord` → `Run` (J6, natural)
- `POST /runs/{run_id}/project` `ProjectionWrite` → `Run | null` (J7, natural: the same write applied twice leaves the row; the test proves it)
- `GET /runs/{run_id}` → `Run` or 404 problem (J8)
- `GET /runs/{run_id}/reset-floor` → `{"floor": int | null}` (J10)
- `GET /runs/talking-to?session_id=&call_id=` → `Run | null` (J9)
- `POST /runs/{run_id}/hops` `{call_id, record: HopRecord, proposal?}` → `Run | null` (J12, natural on the proposal's ON CONFLICT and the hop's state)
- `GET /decisions/{request_id}`, `POST /decisions` `{decision, at_hop?}` → `{"recorded": bool}`, `DELETE /decisions/{request_id}` (J13-J15; `recorded` answers true for a row that holds this very decision, so a retry answers the same)
- `GET /proposals/{request_id}` (J21); `GET /sessions/{session}/tiers?principal=` → `{"tiers": [str] | null}` (J22); `GET /approval-expiry` → `{"seconds": int}` (J23)
- `RemoteFlowStore` implements these methods with `CrudClient`, same names and signatures as `FlowStore`.

- [ ] **Step 1:** Tests per route against the app with a seeded run (reuse `tests/test_flow_store.py` seeds): the route's answer equals `FlowStore`'s for the same input; each write called twice answers the same; `move_hop` and `project` publish one event each (read `events`).
- [ ] **Step 2:** Implement routes as thin calls to `get_state(request).flows.store`. Register area `flows` in `CRUD_AREAS["projects"]` only in Task 5 (the area is complete then).
- [ ] **Step 3:** Backend check; commit `feat(crud): flow run routes for the projects worker (#5298)`.

**Rolling deploy:** routes only; no caller yet.

### Task 5: Projects: grants, the server-side consume and reset; switch the FlowStore

**Files:**
- Modify: `api/crud_projects.py`, `crud/projects.py`
- Modify: `flows/store.py` (a `consume_with(run_id, ConsumeRequest)` that builds `pick` from data; `reset_run` unchanged)
- Modify: `flows/grants.py` (`match_grant` calls `store.consume(run_id, ConsumeRequest)` when the store is remote: add `consume` to `GrantStore`, `flows/grants.py:400-412`, and implement it on both stores)
- Modify: `flows/operations.py:731-810` (the reset kind calls `store.reset_run(...)`; on `RemoteFlowStore` that is the route and the API does the RPC)
- Modify: `worker.py:1428` (`FlowStore` → `RemoteFlowStore`)
- Modify: `auth/caller_grants.py` (`PROJECTS_GRANTS` drop the seven flow relations), `agent/src/db/grants.ts` (`PROJECTS_AI_GRANTS` drop `ai_sessions`, `ai_mcp_tokens`, `ai_settings`; `API_AI_GRANTS` keeps them by spelling them out)
- Test: `tests/crud/test_crud_projects_grants.py`, `tests/auth/test_caller_grants.py`, `agent/test/grants.test.ts`

**Interfaces:**
- `GET /grants/{run_id}` → `Grant | null` (J19); `GET /grant-uses/{request_id}` → `GrantUse | null` (J18)
- `POST /decisions/grant` `Decision` → `{"recorded": bool}` (J16); `DELETE /decisions/{request_id}/grant` (J17)
- `POST /grants/{run_id}/consume` `ConsumeRequest{request_id, fn, tool, session_id, flow_call_id, narrowing, facts: CallFacts, input_hash}` → `{"use": GrantUse} | {"outside": str}` (J20, natural by `request_id`)
- `POST /runs/{run_id}/reset` `{replaced, point, request_id}` → `Run | null`, 409 problem `reset-superseded` (J11)
- Area `flows` registered.

- [ ] **Step 1:** Tests: `consume` twice with one `request_id` gives one `flow_grant_uses` row and one spend; two concurrent consumes on a trigger-seeded grant at its limit admit exactly one (`asyncio.gather`); `reset` with a fake `flows.client` whose reset answers a run id moves the row once, a second call with the same `request_id` answers the same row, a call with another `request_id` for the same `replaced` is 409; `test_caller_grants.py` runs the remaining projects stores as a role with the shortened list.
- [ ] **Step 2:** Implement; run the flow tests (`tests/test_flow_*`, `tests/test_project_workflow*`) against a projects worker built with `RemoteFlowStore` over the test app (add a fixture `crud_app` that serves the app on a local port and a `CrudClient` with an in-process credential).
- [ ] **Step 3:** Backend check, agent check (`cd agent && pnpm lint && pnpm typecheck && pnpm test`); commit `feat(projects): flow runs, decisions and grants through crud routes (#5298)`. Open PR P1.

**Rolling deploy:** new worker on an old API pod: route missing, retried (D4). Old worker on SQL: grants stay (never revoked). A reset by an old worker (SQL, lock in the worker) and one by a new worker (route, lock in the API) take the same row lock, so they serialise.

### Task 6: Projects: operations routes; the projects bus ends

**Files:**
- Create: `crud/operations.py` (`operations_router(caller)`, `RemoteOperationStore`)
- Modify: `api/crud_projects.py` (include `operations_router("projects")`)
- Modify: `workflows/operation_activities.py:53-74` (the insert takes retention from the store: `OperationStore.insert` reads `operation_retention_seconds` itself when given `retention=None`… keep the parameter; the route reads it server-side with the API's `SettingsStore`, and `RemoteOperationStore` sends none)
- Modify: `worker.py:1411-1446` (no `PgNotifyEventBus`, no `SettingsStore` load for retention; `operation_activities` gets a settings stub that is not read on the remote path)
- Modify: `auth/caller_grants.py` (drop `operations`, `events`, `settings`, `model_print_choices`, `printer_bed_types`, `printers`)
- Test: `tests/crud/test_crud_operations.py`

**Interfaces:** `POST /operations` `{op_id, kind, subject, operation_key, request, workflow_id, workflow_run_id, idempotency_key, author, person}` → `Operation` (J24, natural on the workflow ids); `POST /operations/{id}/finish` `{result, error}` → `Operation` (J25, natural on the state guard). The API counts the finish metric (`operations/store.py:293-298`) only when its write ended the row, as today. Area `operations`.

- [ ] **Step 1:** Tests: insert twice gives one row and one event; retention prunes finished rows on the API side; finish twice counts once.
- [ ] **Step 2:** Implement; the projects worker starts with no bus (no LISTEN, no prune, J3, J4).
- [ ] **Step 3:** Backend check; commit `feat(projects): operations through crud routes; no event bus in the worker (#5298)`. PR P2.

**Rolling deploy:** as Task 5. An old projects pod still prunes and fails (J4) until it goes.

### Task 7: Projects: payload keys

**Files:**
- Modify: `api/crud_projects.py`, `crud/projects.py` (`RemotePayloadKeys` implements `PayloadKeys`, `workflows/payload_codec.py:143-145`, with the same 30 s cache)
- Modify: `workflows/payload_codec.py` (factor the create into `create_key(conn, subject, kek)` used by `PgPayloadKeys` and the route)
- Modify: `worker.py:1399-1404` (`payload_keys` → a `RemotePayloadKeys` built with the KEK it still holds)
- Modify: `agent/src/db/grants.ts` (`PROJECTS_AI_GRANTS` becomes empty; keep the export for Task 11)
- Test: `tests/crud/test_crud_payload_keys.py`

**Interfaces:** `POST /payload-keys/{subject}` `{"create": bool}` → `{"dek_sealed": base64, "kek_id": str}`; 410 problem `subject-forgotten`; 403 problem for a subject outside D10's patterns or `create` on `session-*`. Area `payload-keys`.

- [ ] **Step 1:** Tests: create twice gives one row (concurrent `gather`); a forgotten subject is 410 and creates nothing; `session-<uuid>` with `create` is 403; the answer never holds the open key (assert it differs from the opened bytes).
- [ ] **Step 2:** Implement; the flow workflow tests pass on the remote keys.
- [ ] **Step 3:** Backend and agent checks; commit `feat(projects): payload keys through a crud route (#5298)`. PR P3.

**Rolling deploy:** the create runs under the same advisory lock in the API as in the old worker, so an old and a new worker never make two keys.

### Task 8: `flow.host`: the projects credential loses `api`

**Files:**
- Modify: `auth/routes.py` (`FLOW_HOST_ENDPOINTS`: the endpoints `workflows/flow_tools.py:358-484`, `workflows/flow_grants.py:109-127` and `flows/operations.py:412`, `:656` call: render, jobs read, outputs create and read, operations read, print runs create and read, arrange, print options read, the printer reads `bind_call` makes)
- Modify: `auth/service_credentials.py` (`SCOPES["projects"] = {"flow.host", "crud.projects"}`)
- Test: `tests/auth/test_routes.py`, `tests/workflows/test_flow_host_paths.py`

- [ ] **Step 1:** Write a test that records every path the flow host functions and grant checks call (run them against an `httpx.MockTransport` that logs paths) and asserts each one matches a `flow.host` route, and that no other route is `flow.host`.
- [ ] **Step 2:** Implement. `would_refuse` already admits a class in the scopes (Task 1).
- [ ] **Step 3:** Backend check; commit `feat(auth): flow.host, the projects worker's only public routes (#5298)`. PR P4.

**Rolling deploy:** a credential an old pod minted still carries `api`; an exchanged one expires within 15 minutes (spec §6.3), and a sealed one read through the view is replaced at the new API's start (`rotate_due` mints on a scope change) but stays live until it expires. In `off` nothing is refused yet; `scadbuddy_auth_would_refuse_total{class="api"}` for `projects` must stay 0 for a day before #5297 moves to `enforce`.

### Task 9: The projects worker needs no database

**Files:**
- Modify: `worker.py:1385-1482` (no `SettingsStore`, no `open_for_caller`; start gate `wait_for_areas(client, "projects", {"flows", "operations", "payload-keys"})`; log a warning and ignore `SCADBUDDY_DATABASE_URL` when set)
- Modify: `auth/caller_grants.py` (`PROJECTS_GRANTS = {}`), `README.md` "Flow runs on the `projects` queue" (what it needs: no database)
- Test: `tests/test_projects_worker_nodb.py`

- [ ] **Step 1:** Test: build and start the projects worker with `SCADBUDDY_DATABASE_URL` unset and `psycopg.connect`, `psycopg.AsyncConnection.connect` and `ConnectionPool.__init__` patched to raise; run one flow through record, a decision, a grant consume and close against the `crud_app` fixture.
- [ ] **Step 2:** Implement `crud/start.py` `wait_for_areas` (polls every 2 s, logs what is missing, honours `stopping`).
- [ ] **Step 3:** Backend check; commit `feat(projects): the projects worker needs no database (#5298)`. PR P5.

**Rolling deploy:** every area exists on the API since P1 to P3. The Deployment still sets the secret; the worker ignores it. Rollback to P4's build works (secret still there).

### Task 10: Clusters: remove the projects database secret

**Files (eh-homelab/clusters):**
- Modify: `applications/scadbuddy/scadbuddy-projects.yaml:131-137` (drop `SCADBUDDY_DATABASE_URL`, `PGPASSWORD`); add `SCADBUDDY_API_INTERNAL_URL` port 8082 for crud if it differs
- Modify: `applications/scadbuddy/networkpolicy-projects.yaml` (drop the Postgres egress rule)
- Modify: `applications/scadbuddy/scadbuddy.yaml` (Service port 8082) and `networkpolicy-agent.yaml` (admit print and projects to 8082). This task owns the Service port; Task 12 adds only render's admission and egress.

- [ ] **Step 1:** Before merge: `kubectl --context primary -n bambuddy exec scadbuddy-db-1 -- psql -c "select count(*) from pg_stat_activity where usename='scadbuddy_projects'"` is 0 for 24 h after P5 deployed. Record the output in the PR.
- [ ] **Step 2:** Edit, `kustomize build` the app, open the PR (`fix(scadbuddy): projects worker without a database secret (ScadBuddy#5298)`).

**Rolling deploy:** the new pods start without the secret on P5's image. Rollback of the app past P5 requires reverting this PR first.

### Task 11: Revoke and forget `scadbuddy_projects`

**Files:**
- Create: `auth/revoke.py` (`revoke_role(conn, role)`: under `GRANT_LOCK`, if the role exists, `REVOKE ALL ON ALL TABLES/SEQUENCES IN SCHEMA` and `ON SCHEMA`, drop its `only_<role>_*` policies; idempotent)
- Modify: `main.py` start (`revoke_role(conn, "scadbuddy_projects")` after the grants), `auth/caller_grants.py` (drop `PROJECTS_GRANTS`, `projects` from `GRANTS`), `auth/service_credentials.py` (`ROLES`, `VIEWS` lose `projects` if #5494 left them), `agent/src/db/grants.ts` (remove `PROJECTS_AI_GRANTS` and the projects branch of `grantBackendRoles`; the agent revokes its `ai_*` grants from the role when it exists)
- Modify: `docs/security/trust-boundaries.md` (tables), root `CLAUDE.md` and `backend/CLAUDE.md` "Database access" (six processes hold a secret), `README.md` "Each caller's own database role"
- Then (eh-homelab/clusters): `applications/scadbuddy/cnpg-cluster.yaml:114-119` `ensure: absent`; delete `clusters/prod/scadbuddy/onepassworditem-projects-db.yaml` and its kustomization line
- Test: `tests/auth/test_revoke.py`, `agent/test/grants.test.ts`

- [ ] **Step 1:** Test: a role with grants and a row policy has none after `revoke_role`; a second call is a no-op; a missing role is a no-op; `DROP ROLE` then succeeds in the test database.
- [ ] **Step 2:** Implement; backend and agent checks; commit `feat(auth): revoke the projects worker's database role (#5298)`. PR P7.
- [ ] **Step 3:** After P7 deploys, open the clusters PR P8. The owner archives the 1Password item `scadbuddy-projects-db` after it merges.

**Rolling deploy:** P7's API revokes at start; no pod uses the role since Task 10. Rollback of P7: the old API re-grants at start; harmless.

### Task 12: Clusters: the render worker reaches the CRUD port

**Files (eh-homelab/clusters):** `applications/scadbuddy/scadbuddy-render.yaml` (`SCADBUDDY_API_INTERNAL_URL=http://scadbuddy:8082`, the projected token of #5494 if not there), `networkpolicy-agent.yaml` (render to 8082 only; replace the comment at `:58`; the Service port 8082 is Task 10's), and a new egress policy for render: DNS, the API on 8082, Temporal, Postgres (until Task 21), the store's endpoints (Bambuddy, S3), OTLP.

- [ ] **Step 1:** Edit; `kubectl --context primary -n bambuddy exec deploy/scadbuddy-render -- python -c "import httpx;print(httpx.get('http://scadbuddy:8082/api/v1/models').status_code)"` answers 404 and `…:8080/…` times out after merge. Record it.

**Rolling deploy:** no code uses the URL yet. The egress policy must not cut Bambuddy or S3: check the render health `store` block after the roll.

### Task 13: Render: the render settings route

**Files:** Create `api/crud_render.py`, `crud/render.py` (`RemoteRenderSettingsSource`, same API as `store/bambuddy.py:113-178`); modify `worker.py:246-249`, `render/worker_role.py` (drop `render_settings`); test `tests/crud/test_crud_render_settings.py`.

**Interfaces:** `GET /api/v1/internal/crud/render/settings` → `RenderStoreSettings` with the opened render key (or the fallback full key, `library/settings_store.py:1383`) and S3 keys (D8). Area `settings`.

- [ ] **Step 1:** Tests: answer equals `load_render_store_settings`; `no-store` header; the log line names the fields released, not values; a `print` credential is 403.
- [ ] **Step 2:** Implement; the worker reads settings through the route; the 30 s TTL and "keep the last good" behaviour stay (`store/bambuddy.py:134-152`).
- [ ] **Step 3:** Backend check; commit `feat(render): store settings through a crud route (#5298)`. PR R1.

**Rolling deploy:** as Task 5. During the roll the worker's first read may be route-missing: the start gate waits for area `settings`.

### Task 14: Render: the job projection

**Files:** `api/crud_render.py`, `crud/render.py` (`RemoteJobProjection`); modify `workflows/activities.py` only where it passes `pool_timeout` (the remote store maps it to a 5 s request timeout, inside the 10 s attempt, `render/projection.py:50`); `worker.py:224-232`; `render/worker_role.py` (drop `render_jobs`, `events`, `activity`; `RENDER_ROWS` stays until Task 22); test `tests/crud/test_crud_render_jobs.py`.

**Interfaces:** `POST /jobs/accept` `{job, key, workflow_id, run_id, max_pending, supersedes}` → `Job`, 409 problem `queue-full` mapped back to `QueueFullError` (R3, natural); `PUT /jobs/{id}/claims` (R4); `POST /jobs/{id}/started` → `Job | null` (R5); `PUT /jobs/{id}/steps` (R6); `POST /jobs/{id}/finish` `Job` → `{"settled": bool}` (R7: answers true when the row already holds this terminal state and `finished_at`, so a retry answers the same); `GET /jobs/{id}` (R8). Activity and events stay in the API's transaction (R9, R10). Area `jobs`.

- [ ] **Step 1:** Tests: accept twice, one row, one event, one activity row; `max_pending` full is 409 and writes nothing; finish twice answers true twice and announces once; a 6 s slow route makes the client raise inside the attempt.
- [ ] **Step 2:** Implement; run `tests/test_render_workflow*.py` with the worker on the remote projection.
- [ ] **Step 3:** Backend check; commit `feat(render): job projection through crud routes (#5298)`. PR R2.

**Rolling deploy:** both paths take the event-log lock then the row (`core/pg_events.py:20-28`), so an old SQL writer and the API never deadlock in a new order.

### Task 15: Render: blob refs and piece geometry

**Files:** `api/crud_render.py`, `crud/render.py` (`RemoteBlobRefs.add`, `RemoteGeometryIndex`); `worker.py:274`, `:276`; `render/worker_role.py` (drop `blob_refs`, `piece_geometry`); test `tests/crud/test_crud_render_refs.py`.

**Interfaces:** `POST /blob-refs` `{key, holder_kind, holder_id}` (R11, natural; holder kind must be `job`); `GET|PUT|DELETE /geometry/{geometry_key}` (`DELETE` takes `piece_key`) (R12, natural). Area `refs`.

- [ ] Steps: tests (twice-called writes, `holder_kind` other than `job` is 403), implement, check, commit `feat(render): blob refs and geometry through crud routes (#5298)`. PR R3.

**Rolling deploy:** as Task 5.

### Task 16: Render: the blob index

**Files:** `api/crud_render.py`, `crud/render.py` (`RemoteBlobIndex` with the methods `ContentStore` calls on the worker: `get`, `by_sha`, `now`, `put`, `swap`, `mark`, `touch`, `delete`, `delete_if_stale`, `shares_backend_id`); `store/bambuddy.py:325-330` (`_recorded_id` through the index); `store/factory.py:84-92` (take an index); `render/worker_role.py` (drop `store_blobs`); test `tests/crud/test_crud_blob_index.py`.

**Interfaces:** `GET /blob-index/{key}`; `GET /blob-index?sha256=&kind=&backend=`; `GET /blob-index/now`; `PUT /blob-index/{key}` `{ref, slug, meta, reuse}` → `{"previous": BlobRef | null}`; `POST /blob-index/{key}/swap` `{ref, expected, slug, meta, reuse}` → `{"swapped": bool}`; `POST /blob-index/mark`; `POST /blob-index/{key}/touch`; `DELETE /blob-index/{key}?backend=` → `BlobStat | null`; `DELETE /blob-index/{key}/stale?before=` → `bool`; `GET /blob-index/backends/{backend}/{backend_id}` → `{"shared": bool}`. `put`, `swap`, both deletes and `mark` require `Idempotency-Key` (D5). `ReuseLostError` maps to a 409 problem and back. Area `blob-index`.

- [ ] **Step 1:** Tests: a `put` retried with its key answers the first `previous`, not the new ref; a `put` without a key is 428; two concurrent `put`s swapping objects between two keys both finish (the deadlock retry stays in the API, `store/index.py:134-149`); `ReuseLostError` round-trips.
- [ ] **Step 2:** Implement; run `tests/store/` with the worker's `ContentStore` over `RemoteBlobIndex`.
- [ ] **Step 3:** Backend check; commit `feat(render): blob index through crud routes (#5298)`. PR R4.

**Rolling deploy:** the row-lock order is the API's and the old worker's, the same code.

### Task 17: Render: store folders found on the API

**Files:** `store/bambuddy.py:360-458` (split `find_folder` into the API-side `FolderFinder` that keeps the lock, and the worker-side call); `api/crud_render.py`; `crud/render.py`; `render/worker_role.py` (drop `store_folders`); test `tests/crud/test_crud_store_folders.py`.

**Interfaces:** `POST /store-folders/find` `{slug, role}` → `{"folder_id": int}` (the API reads its render target, takes the advisory lock with the 30 s `lock_timeout`, finds or creates, records; natural); `DELETE /store-folders/{slug}`; `GET /store-folders/work` → `[int]`. Area `store-folders`.

- [ ] **Step 1:** Tests with a fake Bambuddy: two concurrent finds create one folder; a find after a recorded folder makes no Bambuddy call.
- [ ] **Step 2:** Implement; commit `feat(render): store folders found by the API (#5298)`. PR R5.

**Rolling deploy:** an old worker and the API take the same advisory key (`folder_lock_key`, `store/bambuddy.py:181`), so they still exclude each other.

### Task 18: Render: assets

**Files:** `library/assets.py` (a `RemoteAssetStore` view: `get`, `use`, `adopt` over HTTP, the files local; `prune_local` with a per-id `threading.Lock` shared with `use` in the process); `api/crud_render.py`; `crud/render.py`; `worker.py:239-244`, `:998`; `render/worker_role.py` (drop `assets`); test `tests/crud/test_crud_assets.py`.

**Interfaces:** `GET /assets/{id}` → `AssetMeta`; `POST /assets/{id}/use` → `AssetMeta` (R19); `POST /assets/{id}/adopt` `AssetMeta` (R20, natural; the API checks the id format and takes the asset's advisory lock); `POST /assets/last-used` `{ids}` → `{id: datetime | null}` (R21). Area `assets`.

- [ ] Steps: tests (a `use` during a local prune keeps the file or re-fetches it; adopt twice one row), implement, check, commit `feat(render): assets through crud routes (#5298)`. PR R6.

**Rolling deploy:** the API's sweep still locks the row before a delete; `use` on the API waits on it as before.

### Task 19: Render: library leases, pin holds, install slots

**Files:** `library/libraries.py:448-776` (stores take a client-chosen token); `api/crud_render.py`; `crud/render.py`; `worker.py:234-238`; `render/worker_role.py` (drop the three `library_*`); test `tests/crud/test_crud_library_leases.py`.

**Interfaces:** `POST /leases` `{token, holder, checkouts: [{library, commit}], ttl_s}`, `PUT /leases/{token}`, `DELETE /leases/{token}`; the same under `/pin-holds`; `POST /install-slots` `{token}` → `{"slot": int | null}` (the same token answers the same slot), `PUT|DELETE /install-slots/{slot}?token=`. All natural. Area `libraries`.

- [ ] Steps: tests (a lease taken while the API's removal holds `REMOVAL_LOCK` waits; a renewed lease survives its first TTL; one token never takes two slots), implement, check, commit `feat(render): library leases through crud routes (#5298)`. PR R7.

**Rolling deploy:** both paths take `pg_advisory_xact_lock_shared(REMOVAL_LOCK)` (`library/libraries.py:480`).

### Task 20: The render worker needs no database and no KEK

**Files:** `worker.py:200-289` (no pool, no `wait_until_ready`, `wait_for_areas("render", {...})`, refuse `local` (D11), no `settings_kek`); `render/worker_role.py` (`RENDER_GRANTS = {}`); `README.md` "Render worker (#424)"; test `tests/test_render_worker_nodb.py`.

- [ ] **Step 1:** Test as Task 9: psycopg patched to raise, `SCADBUDDY_SECRET_KEY_FILE` unset, one render through accept, pieces, finish against `crud_app` with a fake Bambuddy store; `local` refuses with a message naming D11.
- [ ] **Step 2:** Implement; commit `feat(render): the render worker needs no database (#5298)`. PR R8.

**Rolling deploy:** as Task 9.

### Task 21: Clusters: remove the render database secret and KEK

**Files (clusters):** `scadbuddy-render.yaml:161-168` (drop the URL and `PGPASSWORD`), `:223-229`, `:301-307` (drop the KEK env, mount and volume); the render egress policy drops Postgres. Precondition as Task 10 for `scadbuddy_render`.

- [ ] Steps: check `pg_stat_activity` for 24 h; edit; PR `fix(scadbuddy): render worker without a database secret or KEK (ScadBuddy#5298)`.

### Task 22: Revoke and forget `scadbuddy_render`

**Files:** `main.py` (`revoke_role(conn, "scadbuddy_render")`; stop `grant_at_start`, `render/worker_role.py:195-205`); `render/worker_role.py` (delete `RENDER_GRANTS`, `RENDER_ROWS`, `grant_render_role`; keep `grant_role` until Task 35); `tests/test_render_worker_role.py` (replace by a revoke test); docs as Task 11; then clusters `cnpg-cluster.yaml:94-99` `ensure: absent` and delete `onepassworditem-render-db.yaml`.

- [ ] Steps as Task 11. Commit `feat(render): revoke the render worker's database role (#5298)`.

### Task 23: Print: the settings projection

**Files:** `library/settings_store.py` (`print_projection(stored) -> StoredSettings` that blanks every sealed field but the Bambuddy key and each spool provider's key; list them in `PRINT_SECRETS`); `api/crud_print.py`; `crud/print.py` (`RemoteSettingsStore` with sync `load()` and `pool` absent); `worker.py:1150-1153` (callers of `settings_store.load`); `auth/caller_grants.py` (drop `model_print_choices`; keep `settings` and `printer_bed_types`, which the printer merge, `printers/store.py` `_merge`, writes until Task 29); test `tests/crud/test_crud_print_settings.py`.

**Interfaces:** `GET /api/v1/internal/crud/print/settings` → `StoredSettings` (D8). Area `settings`.

- [ ] **Step 1:** Tests: every sealed field outside `PRINT_SECRETS` is null in the answer; the Bambuddy key is opened; `no-store`; a test fails when a new sealed field appears that is in neither list.
- [ ] **Step 2:** Implement; commit `feat(print): settings through a crud route (#5298)`. PR Q1.

**Rolling deploy:** as Task 5. `settings` and `printer_bed_types` stay granted until Task 29 (the merge writes them).

### Task 24: Print: runs, outcomes, operations, events; no LISTEN

**Files:** `api/crud_print.py` (include `operations_router("print")`); `crud/print.py` (`RemotePrintRunStore`, `RemoteOutcomes`, `RemoteEvents` with `publish(event)` posting to `/events`); `core/pg_events.py` (`write_event_once`: `ON CONFLICT (event_id) DO NOTHING`, NOTIFY only when inserted); `bambuddy/runs.py:635-700` (`RecentRuns` without the listener: reads through the store each time); `worker.py:1146-1190`, `:1231`; `auth/caller_grants.py` (drop `operations`, `print_outcomes`, `events`; `print_runs` stays until Task 31, because `spools/native.py:286` (Q29) and the printer merge's `PrinterReference("print_runs", "printer")` (`printers/store.py:226`, Q24) still read it); test `tests/crud/test_crud_print_runs.py`.

**Interfaces:** `/runs` routes for Q10 to Q13 (`POST /runs` `{output_id, request, …}` → `PrintRun`, natural on the run key; `POST /runs/{id}/start-enqueue|succeed|fail|fail-lost|result|lan-stopped`, state-guarded; reads `GET /runs/{id}`, `GET /runs?ids=`, `GET /runs/latest?output_id=`, `GET /runs/find?key=&has_request_id=`, `GET /runs/lan-waiting?printers=`, `GET /runs/running?output_id=`); `POST /outcomes` (Q14, natural); `POST /events` `{event}` with kinds `print.progress`, the kinds `bambuddy/progress.py:639` emits, `printer.report`, `printer.changed` (Q5). Areas `runs`, `operations`, `events`.

- [ ] **Step 1:** Tests: an event posted twice is one row and one NOTIFY; a kind outside the allowlist is 403; a run's `succeed` twice announces once; the worker builds no `PgListener`.
- [ ] **Step 2:** Implement; commit `feat(print): runs, outcomes and events through crud routes (#5298)`. PR Q2.

**Rolling deploy:** `activity` stays granted until Task 29. An old print pod still LISTENs; harmless.

### Task 25: Print: uploads, slices, slice starts

**Files:** `api/crud_print.py`, `crud/print.py` (`RemoteUploadStore` part 1, Q15, Q17); `worker.py:1154`; `auth/caller_grants.py` (drop `output_bambuddy_uploads`, `output_bambuddy_slices`, `print_slice_starts`, its sequence); test `tests/crud/test_crud_uploads.py`.

**Interfaces:** one route per method in Q15 and Q17 under `/uploads`, `/slices`, `/slice-starts`; bodies are the method arguments; answers their returns. All natural (ON CONFLICT or delete-by-key); the test proves each. Area `uploads`.

- [ ] Steps: tests, implement, commit `feat(print): uploads and slices through crud routes (#5298)`. PR Q3.

### Task 26: Print: previews, project targets, folderless projects, destination touches

**Files:** as Task 25 (`RemoteUploadStore` part 2, Q16, Q18); drop `print_preview_slices`, its sequence, `project_print_targets`, `folderless_projects`, `destination_touches`.

**Interfaces:** `/previews…` (`claim_preview` takes `Idempotency-Key`: a lost answer must not turn a won claim into a lost one), `/project-targets…`, `/folderless…`, `/destination-touches…`. Area `previews`.

- [ ] Steps: tests (claim retried with its key answers true once won), implement, commit `feat(print): previews and project targets through crud routes (#5298)`. PR Q4.

### Task 27: Print: the copy lease

**Files:**
- Create: `migrations/<stamp>_copy_leases.sql`
- Create: `bambuddy/copy_leases.py` (`CopyLeases.acquire(key, token, ttl, wait)`, `renew`, `release`, `held_by`)
- Modify: `bambuddy/uploads.py:143-164` (`copy_lock` acquires the lease, renews it in a task every 30 s, releases on exit; on the API side it also holds the old session advisory lock until Task 35), `bambuddy/uploads.py:681` (`record` takes `lease_token` and refuses when not held), `api/crud_print.py`, `crud/print.py`
- Test: `tests/test_copy_leases.py`, `tests/crud/test_crud_copy_leases.py`

**Interfaces:** `POST /copy-leases/{key}` `{token, ttl_s ≤ 300, wait_s ≤ 25}` → `{token, expires_at}` or 409 `busy`; `PUT /copy-leases/{key}` `{token}`; `DELETE /copy-leases/{key}?token=`. The acquire also probes `pg_try_advisory_lock` of the old key and releases it at once; a held old lock is `busy`. Natural (client token). Area `copy-leases`.

- [ ] **Step 1:** Tests: two holders exclude each other; an expired lease is taken over; a record with a stale token is 409; renew keeps a 2-minute upload held; an old-style holder of the advisory lock makes acquire answer `busy`.
- [ ] **Step 2:** Implement; commit `feat(print): copy lease instead of a session lock (#5298)`. PR Q5.

**Rolling deploy:** new API holders take both the lease and the old lock, so they exclude old print pods both ways. A new remote holder holds only the lease; an old print pod that takes the old lock after that acquire is not excluded. That needs both print pods (one replica, `maxSurge: 1`) to copy into one folder in the same seconds of the roll; the worst result is one extra library copy (#317), which a person can delete. Accepted.

### Task 28: Print: links, sends, last prints

**Files:** `api/crud_print.py`, `crud/print.py` (`RemotePrintLinks`, `RemoteOutputPrints`); `worker.py:1155`, `:1224`, `:1257`; drop `print_links`, `print_sends`, `output_last_prints`; test `tests/crud/test_crud_links.py`.

**Interfaces:** one route per method in Q20, Q21. `pending_library` takes `Idempotency-Key` (it claims rows); the rest natural. Area `links`.

- [ ] Steps: tests, implement, commit `feat(print): links and last prints through crud routes (#5298)`. PR Q6.

### Task 29: Print: printer registry, reports and access codes

**Files:** `api/crud_print.py`, `crud/print.py` (`RemotePrinterStore` sync, `RemoteReports` sync); `worker.py:1182-1184`; `printers/store.py:425` (on the API the emit is local); drop `printers`, `printer_reports`, `settings`, `printer_bed_types`, `activity`; test `tests/crud/test_crud_printers.py`.

**Interfaces:** reads (Q22); `POST /printers/ensure-bambuddy` `{bambuddy_printer_id, serial, name, model, listed}` → `{row, written}` (Q24, natural: decided under the lock); the Q25 writes; `GET /printers/{id}/access-code` → `{"code": str}` (Q23, D8); `PUT /reports/{printer_id}` `{report, seen_at}` → `{"ended": PrintEnd | null}` with `Idempotency-Key` (Q26: only the first write answers the end); `POST /reports/{id}/touch`; `GET /reports/{id}`, `GET /reports`. Area `printers`.

- [ ] **Step 1:** Tests: a `put` retried with its key answers the same `PrintEnd`; a merge rewrites `settings` and `activity` on the API; the access code answer is `no-store` and its log line has no code.
- [ ] **Step 2:** Implement; commit `feat(print): printers and reports through crud routes (#5298)`. PR Q7.

**Rolling deploy:** the merge takes `pg_advisory_xact_lock('scadbuddy:printers')` in both paths.

### Task 30: Print: rack usage

**Files:** `rack/usage.py` (`RemoteRackUsage` implements `LanUsage` and the `RackUsageStore` methods Q27 lists); `api/crud_print.py`; `worker.py:1175`; drop the four `rack_nozzle_*`; test `tests/crud/test_crud_rack.py`.

**Interfaces:** `/rack/seen`, `/rack/usage`, `/rack/spool-history`, `/rack/picks`, `/rack/picked`, `/rack/recorded-archives`, `/rack/prints`, `/rack/lan-picks` (`Idempotency-Key`), `/rack/settle-lan`, `/rack/settle-lan-ended`. Area `rack`.

- [ ] Steps: tests, implement, commit `feat(print): rack usage through crud routes (#5298)`. PR Q8.

### Task 31: Print: spools, native spools, held presets, native slices

**Files:** `api/crud_print.py`, `crud/print.py` (`RemoteSpoolStore`, `RemoteNativeSpools`, `RemoteHeldPresets`, `RemoteNativeSlices`); `worker.py:1211-1214`, `:1235-1246`; `spools/spool_providers.py:116`; drop `physical_spools`, `spool_provider_records`, `spool_record_links`, `spool_provider_refreshes`, `tray_spool_links`, `native_spools`, `native_spool_uses`, `held_local_presets`, `native_slices` and its sequence, and `print_runs` (its last readers, Q29 here and the merge of Task 29, are gone); test `tests/crud/test_crud_spools.py`.

**Interfaces:** one route per method in Q28 to Q30. `replace_records` and `link` take `Idempotency-Key`; `NativeSliceStore.start` is natural on its workflow id (`slicing/store.py:125`). Area `spools`.

- [ ] Steps: tests, implement, commit `feat(print): spools and native slices through crud routes (#5298)`. PR Q9.

### Task 32: Print: transfer URLs from the API

**Files:** `bambuddy/output_reader.py` (`RemoteBlobs` asks for a URL instead of signing); `api/crud_print.py`; `worker.py:1270-1272`; test `tests/crud/test_crud_transfer_urls.py`.

**Interfaces:** `POST /transfer-urls` `{key}` → `{"url": str, "expires_at": datetime}`; the API refuses a key that no `native_slices` row names (the LAN print's sliced file) and signs with `slicing/transfer_token.py`. Area `transfer-urls`.

- [ ] Steps: tests (an unknown key is 403; the URL reads the blob through `/internal/blobs`), implement, commit `feat(print): transfer URLs minted by the API (#5298)`. PR Q10.

### Task 33: The print worker needs no database and no KEK

**Files:** `worker.py:1096-1378` (no `SettingsStore`, no pool, no `settings_kek`; `wait_for_areas("print", …)`); `auth/caller_grants.py` (`PRINT_GRANTS = {}`, `PRINT_SEQUENCES = ()`); `README.md` "The print worker (#1060)"; test `tests/test_print_worker_nodb.py`.

- [ ] Steps as Task 20, running a native LAN print and a Bambuddy send against `crud_app` with fakes. Commit `feat(print): the print worker needs no database (#5298)`. PR Q11.

### Task 34: Clusters: remove the print database secret and KEK

**Files (clusters):** `scadbuddy-print.yaml:139-145` (URL, `PGPASSWORD`), `:181-185`, `:237-243` (KEK); `networkpolicy-print.yaml` (drop Postgres). Precondition as Task 10 for `scadbuddy_print`.

- [ ] Steps as Task 21.

### Task 35: Revoke and forget `scadbuddy_print`; finish the docs

**Files:** `main.py` (`revoke_role(conn, "scadbuddy_print")`); `auth/caller_grants.py` (delete; keep `AGENT_GRANTS` for #5299 only if still used, else delete the module); `render/worker_role.py` (delete `grant_role` if unused); `bambuddy/uploads.py` (drop the old advisory lock from `copy_lock`); docs: trust page (no worker holds a secret or a KEK but projects' KEK until #5342), root and backend `CLAUDE.md` "Database access", `README.md`; then clusters `cnpg-cluster.yaml:108-113` `ensure: absent`, delete `onepassworditem-print-db.yaml`.

- [ ] Steps as Task 11. Commit `feat(print): revoke the print worker's database role (#5298)`; the body says `Fixes #5298`.

**Rolling deploy:** dropping the old advisory lock from `copy_lock` is safe only now: no pod of a build before Task 27 is left.

## Not in this plan

- #5494: the exchange route and readers (assumed done).
- #5342: the key-release route that removes the KEK from projects.
- #5299: the agent's data; `AGENT_GRANTS` and `API_AI_GRANTS`.
- #5297: `enforce`. #5296: template Python and openscad out of the render pod.
- eh-homelab/clusters#3182: the Postgres ingress policy.
