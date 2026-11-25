# The Python backend moves to Go, Python stays as activities

Status: draft for the owner's approval (epic #7667). Nothing is built.

Sources: counts and file references are from `main` at `1f9bf6962` (2026-11-25), read by
two surveys of the tree. "Inferred" marks what was not read or run. Prod figures name
where they were measured.

## 1. Intent

The owner's decisions of 2026-11-25:

- Move everything that holds SQL to Go or Rust, and everything else that does not need
  Python.
- Anything that needs Python becomes a Python Temporal activity.
- A second SQL holder is accepted while the move lasts. It is an exception to the rule
  of #5295 for this one service. It is not one for a worker or for untrusted code.
- The reason: less Python should speed CI and runtime.

### 1.1 What to expect from it

| Goal | Measured | So |
|---|---|---|
| CI | Of the backend pytest time in `backend/.test_durations`, the agent CRUD tests are 2.2% and the other CRUD tests 2.2%. `tests/api` otherwise is 71.5% | The CI gain comes late, when the public routes' tests leave Python |
| Runtime | The agent's polling was 12.8 of the API's 14.8 requests a second; the API used 0.82 cores on average (prod Prometheus, read 2026-11-24) | The polled agent routes are the first target |
| Not helped | A Postgres commit on prod takes 1 to 2 s at times (a known issue, designed around) | Go does not make it shorter |

### 1.2 Goals

- One Go service, `scadbuddy-core`, that holds the SQL at the end. Python holds none.
- An area's Python route is deleted as soon as Go serves it on prod. The rollback is then
  the previous image. There is no soak period (the owner, 2026-11-25).
- A Python SQL module outlives its area only while Python code of a later phase still calls
  it (§5.1).

### 1.3 Non-goals

- The agent (TypeScript), the frontend, the Rust slicer and the API's contract do not change.
- No new routes and no new behaviour. A bug found in Python is ported as it is, with an
  issue, unless keeping it is unsafe.
- Numpy and trimesh code is not rewritten here. #3824 moves some of it to Rust.

### 1.4 Anti-goals

- A process that holds a database secret and runs untrusted code (#5295, #5296).
- Two writers with different invariants on one table.
- A rewrite that lands all at once.

## 2. Language

Go for the service that holds SQL and, later, its Temporal workflows. Rust stays for heavy
compute (`workers/rust`). This is my proposal; the owner said either language.

- `pgx` with `sqlc` keeps the hand-written SQL and checks it against the migrations. Whether
  sqlc reads this repo's 405 migration files (functions and triggers among them) is
  **unproven**. Phase 0 tests it. If it does not, the SQL stays in Go strings, checked by
  tests against Postgres.
- The Go toolchain, the `go` CI job and the vendoring exist (`tools/go/CLAUDE.md`).

## 3. Approaches

| | What | Trade-off |
|---|---|---|
| A. Area by area, path-routed (the owner's pick) | A Go service takes over areas | As first described it needed the callers or an ingress to route by path. The survey shows that does not work: the agent has one base URL for every client (`agent/src/config.ts`), and the routes under `/sessions/{id}/` mix several areas, so a path prefix cannot split them |
| **A with a front (this design)** | Go is the front process in the API pod. It serves the areas it has, and forwards the rest to Python | One more local hop for what is not ported. No caller changes |
| B. Gateway before any port | The same front, ported to no area at first | Phase 0 is this, and it is only a step |
| C. Rewrite the whole API | | Rejected: 394,845 lines of Python tests describe its behaviour |

## 4. Shape

```
 API pod
 ┌──────────────────────────────────────────────────────────────┐
 │  scadbuddy-core (Go)  :8080 and :8082                         │
 │    ported areas ──────────────► Postgres                      │
 │    the rest ─► unix socket ─► uvicorn (Python) ─► Postgres    │
 │  agent sidecar ─► 127.0.0.1:8080                              │
 └──────────────────────────────────────────────────────────────┘
```

- **Ports.** Go listens on 8080 and, for CRUD callers, on 8082, with 8082's path filter
  (`crud/listener.py`). Python listens on two Unix sockets in a shared `emptyDir`
  mounted in these two containers only.
- **Why a socket.** Python believes `X-Forwarded-For` only from trusted peers
  (`core/proxies.py`) and refuses uvicorn's own proxy headers, so that a caller in the pod
  cannot name a client. A loopback port would make every caller, the agent sidecar
  included, look like the front. Over the socket the front sends the real peer in a
  header it overwrites, and Python reads it only on that socket.
- **The socket checks its peer.** `openscad-lsp` runs in the Python container as the API's uid,
  and its seccomp filter allows `AF_UNIX` (`editor/nonet.py`). So mounting the socket in
  two containers does not keep it out. The Go container runs under its own uid, and
  Python closes a connection whose `SO_PEERCRED` uid is another. Phase 0 tests that a
  process with the API's uid is refused. Without that test, phase 0 does not ship.
- **Forwarding** passes WebSockets (`/ws`, the LSP routes), SSE (`flush` at once), large
  and streamed bodies, and headers unchanged. It buffers nothing.
- **Switch.** `SCADBUDDY_CORE_AREAS` lists what Go answers: `<area>` for all its routes, or
  `<area>:read` for its GETs. The names are those of `api/crud_areas.py`. A route not
  listed is forwarded. Python keeps answering `/areas` with every name.
- **Front rollback.** Python listens either on the sockets or on 8080 and 8082, by one
  setting, never both. Going back is a Deployment change that drops the Go container.
- **Packaging.** `tools/go/scadbuddy-core`, its own image through `build-go-image.yml`, so the
  Go CI needs no new wiring. The pod's two images have different commits; `/healthz` names
  both. The pod manifest is a change in eh-homelab/clusters.
- **Database.** Go uses the API's database role. A role per process is not a boundary
  (#5295). Its pool is small (proposed 4) and it joins the connection budget
  (`backend/tests/test_pg_pools.py`).
- **Migrations, role revocation, KEK re-wrap and the housekeeping schedules stay in Python**
  until phase 7. Go waits until the migration ledgers hold the files it was built against.

## 5. What every ported route must keep

| Behaviour | Where Python does it | Note |
|---|---|---|
| Service credential: SHA-256 of the token against `service_credentials`, not suspended, `last_used_at` at most once a minute | `auth/service_credentials.py` | No cache today, and Go adds none |
| A database error while resolving a credential is 401, not 503 | `auth/gate.py` | Kept as it is; the agent drops its token on it |
| The refusal texts differ by `SCADBUDDY_AUTH_MODE` | `auth/check.py`, `crud/guard.py` | Prod never runs `enforce` |
| `X-ScadBuddy-Crud: 1` and `Cache-Control: no-store` on the answers of matched CRUD routes only | `crud/guard.py` | The agent requires the first; its absence on an unmatched path is how a client sees an old pod |
| Before the route: compression, the cross-site gate, the auth gate, the body-size gate (413, no CRUD headers), the event cursor, agent authorship | `main.py` middlewares, in that order | |
| In the route: malformed JSON is 422 before auth; then auth; then length (411); then path, query and body | FastAPI `routing.py` | |
| Problem JSON; `title` is `Error` for some statuses; a 422's `errors[].loc` | `core/problems.py` | The agent reads `loc`, not the text |
| `event_seq` is bumped in the statement that inserts the events, the row locked to the commit | `agent_data/events.py` | Other Python writers share it: `gate.py`, `turns.py`, `workspace.py` |
| Advisory lock keys and their order | Appendix A, a start the first plan completes | `hashtext` and `hashtextextended` run in Postgres, so the SQL carries over |
| Metric `scadbuddy_http_requests_total` labels and the `scadbuddy-api` trace name | `core/metrics.py`, `core/tracing.py` | The dashboard lint reads `core/metrics.py` |
| JSON text parity | pydantic | Known traps: NaN and lone surrogates accepted by Python's `json`, `\u0000` in an event's JSON, lax integers, a bad UUID that must reach Postgres to be 422 |

### 5.1 SQL that two phases share

Python code of a later phase still calls some of an area's SQL: `gate.py` and `turns.py` call
`events.append_in`; `flows/forget.py`, `flows/images.py` and `hosted_plugins/store.py` use
`agent_data`. Such a statement exists in both languages until its last Python caller is
ported. One file per statement is the source, and a test in each language compares its
text with it. The route handler and the tests that only cover it are deleted on schedule.

### 5.2 How we check

1. **Contract suite.** The area's black-box tests (`backend/tests/crud/test_crud_agent_*.py`)
   get a base URL and a seeded credential, and run against Python and against the Go
   binary on the same throwaway database. Today their fixture builds a partial app and makes
   a database per test; the Go binary starts per test with that database's DSN.
2. **Difference test for reads.** The same request goes to both, and status, headers and
   JSON must match. Intended differences are listed in the test.
3. Go unit tests for what the suite cannot reach.
4. When the area's Python is deleted, its Python tests go too, with the suite's cases
   rewritten in Go in that same PR, so no case is lost.

## 6. Order of work

Each phase gets its own plan, and phases 3 to 7 each get a short design of their own before
code. Stop and report at the end of each phase.

| Phase | What | Done when |
|---|---|---|
| 0 | The front with no ported area; the sqlc test; the contract suite's base URL | Prod runs through the front. The hop's cost is measured: p50 and p99, and the API pod's CPU |
| 1 | Area `session-events` (`:read` first, then all). Area `session-turns` `:read` for `GET …/turn`. The held call's one-second re-read, whose route the plan takes from traffic. No new held or long-poll read | Their Python handlers are deleted once Go serves them on prod |
| 2a | The crypto of the sealed columns, pinned by the existing vectors, and the KEK mounted in the Go container | The vectors pass in Go. Python keeps its KEK |
| 2 | The rest of `/internal/crud/agent/*` (60 files, 20,251 lines today), except routes whose handler uses code of a later phase: `orders`, `activity`, `hosted_plugins`, `store.content`, `library.url_import`, `flow_triggers` | Only those routes are still forwarded. Each moves with the package it uses |
| 3 | The other internal CRUD: render, print, projects, storefront, camera, intake, profile-fetch | |
| 4 | The domain packages and their public routes: payments, inventory, entities, filament, printers, store, print_library, storefront, rack, tasks, hosted_plugins, activity, operations, camera. Their few Python-bound files (`filament/label_pdf.py` reportlab, `store/verify_bambuddy.py` PIL, and `print_activities.py` and `native.py`, which import `render/bambu3mf.py`) become activities, or get a Go port of `bambu3mf`'s metadata reads | One package a PR series. This is where CI gains |
| 5 | The packages that mix both: api, orders, bambuddy, library, flows, analyzers. Their Python-bound files become activities first, the calls a person waits on among them (§7) | |
| 6 | Auth: people, tokens, service-credential minting | |
| 7 | Migrations, start-up, schedules, and the SQL-holding workflows | No Python process holds the database secret. The projects worker keeps its KEK until #5342, as `ProjectWorkflow` stays in Python (§7) |

No gate between phases (the owner, 2026-11-25): areas are ported in parallel, and the goal is Python's routes gone as fast as possible. Latency is not a reason to wait.

## 7. Python as activities

The owner asked for Python as activities. That fits durable work: geometry and mesh
operations, 3MF builds, analyzers, PIL and lxml work, the customer-file check. The Python
activity worker holds no database secret and no KEK: its inputs and outputs are blob
references and JSON, as the render worker's are now.

A call a person waits on (the editor's geometry, `render_view`, the flow type check) follows
the same rule (the owner's decision, 2026-11-25): no separate local Python process. It stays
where it is, in the Python API behind the front, until its package is ported. It then
becomes a Python activity. Temporal's scheduling delay on it is accepted until the migration is
complete.

Two workflows are not activities, and I propose they stay Python workflows:

| Case | Why |
|---|---|
| `TemplatePipeline` | The template's `pipeline.py` is compiled and run **inside** the workflow (`workflows/sandbox.py`) |
| `ProjectWorkflow` (flows) | The harness's Monty code runs inside the workflow |

Python therefore remains in: the render worker and template pipelines, the projects
worker, `meshmodel` and the numpy part of `render`, the analyzers' geometry, the flow
type check, and the profile-fetch and intake code until someone ports it.

## 8. Temporal

- Go workflows never replay a Python history. A moved workflow gets a new type name, or
  the same name on a new queue, and the old build drains.
- The perpetual ones (`PrinterWatch`, `FilamentBbSync`) hand over when they continue as new.
- `PrintRun`, `FollowPrint` and `StagedFile` drain before the type moves.
- Payloads are plain JSON through the pydantic converter on every queue except flows'
  (inferred: a Go worker reads them; the Rust slicer already does). `timedelta` fields are
  ISO-8601 (inferred). Vector files pin each model, as `workers/rust/vectors` does.
- Flows' payloads are encrypted per subject. The Go side does not read them until phase 6.

## 9. Risks

| Risk | Answer |
|---|---|
| The front breaks streaming, WebSockets or client addresses for every route, at once | Phase 0 tests each. Going back is one Deployment change (§4) |
| The two languages disagree on a rare input | The contract suite, the difference test, and the known traps above as cases |
| A mixed deploy runs an area on both languages | Shared invariants are in SQL. A rollout switches the setting only after every pod has the Go binary |
| The migration stops half-way | Each phase leaves a working system, and Python stays the migrator until phase 7 |
| Two SQL holders for a long time | Accepted by the owner. The Go process runs no untrusted code, and Python's own untrusted code is #5296 |
| The Go container's KEK (from phase 2a) | It is the CRUD service #5342 names. No other new holder |

## 10. Left to the builder

The Go HTTP router, package layout under `tools/go/scadbuddy-core`, the error type, the pool's exact
size, and how the area switch is parsed.

## Appendix A. Locks and channels

From the survey of `1f9bf6962`; the plan of each area checks them again.

| Kind | Keys |
|---|---|
| 64-bit constants | migrations `0x5343414442444459` (`render/pg_store.py`), agent migrations `0x5343414441474E54` (`render/agent_migrations.py`), event log `0x5343414445564E54` (`core/pg_events.py`), role revocation `0x53434144524E4452` (`auth/revoke.py`), library removal `0x534341444C454153` |
| `hashtext` | `scadbuddy:ai_payload_keys:<subject>` (exclusive to create a key or forget; shared for a fork's log, a transcript append, a session fork), `bgtask:<session>`, `scadbuddy:agent_audit:…` |
| `hashtextextended(…, 0)` | `scadbuddy:service_credentials:<caller>`, `ai_sessions.start:<kind>:<id>`, `scadbuddy:ai_session_files:<workspace>`, `scadbuddy:ai_approvals:mcp_prepare`, `scadbuddy:ai_attachments:<kind>:<id>`, and more |
| Two integers | idempotency `0x53435251` with `hashtext(caller, 0x1f, key)`, decisions `0x53424144`, reports `0x53425052`, and more |
| Order | event log before rows; the session row before its files |
| NOTIFY | `scadbuddy_events`, `_input`, `_pending`, `_mcp`, `_warm_evict`, `_settings`, `scadbuddy_models_head`; the `session-events` routes send none, the agent publishes |
