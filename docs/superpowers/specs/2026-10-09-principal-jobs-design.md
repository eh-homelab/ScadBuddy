# Jobs: background work by principal

Date: 2026-10-09. Depends on #1953 (internal tool sets listed and permissioned like any
plugin), which lands first.

## 1. Goal

An activity log of long-running work, in the manner of Azure's Activity log and
Notifications: every render, operation, print run and agent command, who started it,
where it is, and what it made. One record serves three readers:

- the browser user, who sees every principal's jobs in the UI;
- auditing non-browser principals (an MCP token, an OIDC client, a flow);
- each principal over the API and `/mcp`, listing and polling its own jobs.

Filters are required. Nothing is hard-coded: the kinds, facets, principals and tools in
the filter bar come from registrations and data. The UI follows the app's
Simple/Advanced convention (`LibraryPage`, `AdvancedSwitch`).

Out of scope: cancel and retry (a later spec may add them per kind).

## 2. Facts this rests on

- Background work lives in four records with no shared shape and no principal column:
  `render_jobs`, `operations`, `print_runs` (backend) and `ai_operations` (agent).
- A principal exists only in the agent (`agent/src/auth/principal.ts`): `token:<id>`,
  `oidc:<issuer>#<sub>`, `anonymous:<mcp session>`, `flow`, and the browser user. The
  backend learns it only from `X-ScadBuddy-Agent-Author` (`core/authorship.py`).
- The backend has no user authentication: a request without the header is the browser
  user, or anyone who can reach the UI.
- `Operation` and `PrintRun` already upsert `ScadbuddyKind`, `ScadbuddySubject` and
  `ScadbuddyStatus` search attributes.

## 3. Record

A backend table `jobs` (new migration):

| column | meaning |
|---|---|
| `id` | `<source>:<source_id>`, stable |
| `kind` | a registered key, e.g. `render`, `operation.model_import`, `print_run`, `agent.operation.<kind>` |
| `principal` | from `X-ScadBuddy-Agent-Author`; for a headless-browser request (`X-ScadBuddy-Agent-Session`, `api/agent_actor.py`), the owner of that session (`ai_sessions`); `browser` when neither is present; `system` for schedules |
| `session` | the agent session, when there is one |
| `via_tool` | the tool that started it, when an agent did (a #1953 tool) |
| `subject` | what it acts on: a model slug, an output id, a printer |
| `status` | `queued`, `running`, `waiting`, `succeeded`, `failed`, `cancelled` |
| `title` | from the kind's title template |
| `parent_id` | a pipeline over its pieces, an arrange over its renders |
| `facets` | `jsonb`, the kind's declared filterable fields |
| `source`, `source_id` | the row it mirrors |
| `workflow_id`, `run_id`, `traceparent` | Advanced only |
| `created_at`, `updated_at`, `finished_at` | |

Indexes: `(created_at DESC, id)`, `(principal, created_at DESC)`, `(kind, created_at
DESC)`, `(status) WHERE finished_at IS NULL`, `(parent_id)`, GIN on `facets`.

The six statuses are the one fixed vocabulary; each source maps into it in its kind
(render `superseded` → `cancelled`; a print on the printer → `running` with a facet).

## 4. Registry

Each feature exports `JOB_KINDS` from `scadbuddy/<feature>/jobs.py`, found like
`OPERATION_KINDS`, never by editing a list. A `JobKind` declares:

- `key` and `label`;
- `title(row)`;
- its facets: name, type (`keyword`, `number`, `bool`, `time`), label, filterable;
- `detail(row)`: stages with timings, request, result, error, read from its source;
- `links(row)`: what the job made (an output, a version, a print).

Every `OperationKind` yields a job kind without its own `jobs.py`. The agent's kinds are
data, not code: at start the agent upserts one row per agent operation kind from its tool
manifest (`dist/tools.json`) into its own table `ai_job_kinds` (key, label, title template,
facets, `retired_at`), and sets `retired_at` on rows it no longer declares. The backend
reads that table, as it already reads `ai_*` tables for the headless-grant gate
(`api/agent_actor.py` `GRANT_SQL`), and never writes it. A retired kind still labels and
filters its old jobs; it is left out of `/jobs/facets` once none of its jobs remain. Agent
keys are `agent.`-prefixed, and a code-registered key may not start with `agent.`
(checked at discovery), so the two never collide. A new tool or agent operation needs no
backend edit.

## 5. Writers

Each source writes its job at the one place it already writes its own record:

| source | writer |
|---|---|
| renders | the `project` activity (`render/projection.py`) |
| operations | the insert and finish activities (`workflows/operation_activities.py`) |
| print runs | the status upsert (`workflows/printing.py`) |
| agent operations | read from `ai_operations` by the backend: `AgentOperation`'s record activities send `NOTIFY ai_operations` with the row id, and the backend's listener upserts the job |

The agent writes no backend table and the backend gains no write route: `/api/v1/internal/*`
has no authentication (README "Deploying"), so a write route there would let anyone who
reaches the UI forge jobs. `ai_operations` gains `principal`, `session` and `via_tool`
columns (an agent migration), filled from the tool call's principal.

Each write is an upsert keyed by `id` and sends `NOTIFY jobs`. A failed job write never
fails the job: it is logged and counted (a `core/metrics.py` counter), and the repair pass
(§8) re-derives it.

## 6. API

Under `/api/v1`, feature `scadbuddy/jobs/`, routes `api/jobs.py`:

- `GET /jobs`: newest first, keyset paging. Filters: `kind`, `status`, `principal`,
  `session`, `via_tool`, `subject`, `since`, `until`, `parent`, `q` (title), and
  `facet.<name>=<value>` for any filterable facet of the selected kinds. A facet no
  selected kind declares answers 400, checked against the registry.
- `GET /jobs/facets?since=…`: the kinds with labels and facets, the statuses, and the
  distinct principals, tools and subjects present in the window, with counts. The filter
  bar is built from this alone.
- `GET /jobs/{id}`: the row, its children, `links`, and the kind's `detail`. Advanced
  fields only with `?advanced=1`.
- `GET /jobs/stream`: SSE of upserts that match the same filter parameters, fed by
  `NOTIFY jobs`, resumable from `Last-Event-ID` (`updated_at`, `id`).
- `run_operation`'s 202 adds `Job-Location: /api/v1/jobs/<id>`.

## 7. Visibility

- The backend routes and the realtime `activity` topic read everything, as the browser
  does. The backend has no user authentication, so filtering there would not protect
  anything (since #2568 no route pins a principal).
- The agent's `activity_list`, `activity_get` and `activity_watch` tools are the boundary
  (in `ALL_TOOLS`, so in-process and on `/mcp`, `read` tier). A caller without `outward`
  sees only its own: they pin `principal=<caller>`, and a row of another principal answers
  as a missing one.
- The `browser_*` tools drive the user's own tab and see what the user sees, as on every
  other page. A caller that can navigate that tab to the Activity page can read it; the
  tier of `browser_navigate` is what gates that, not a rule about this page.
- The backend does not check tiers. `X-ScadBuddy-Agent-Author` labels a request; it does
  not authenticate it (`core/authorship.py`: "Neither header is authentication"). The
  backend adds no tier header and trusts none.

## 8. Retention and repair

One activity in `housekeeping.py`'s `SWEEPS`:

- deletes finished jobs older than `jobs_ttl` (a stored setting);
- re-derives jobs from the sources for the last `jobs_repair_window` (a stored setting),
  upserting any the writers missed.

## 9. UI

A top-level **Jobs** page and a bell.

- **Filter bar**, built from `/jobs/facets`. Chips for kind, status, principal (shown as
  "You", the token's name, the OIDC client, "System"), tool and subject, plus a time range
  and a search box. A kind's own facets appear once that kind is selected. Filters live in
  the URL.
- **Table**: status, title, started by, started, duration. Children nest under their
  parent. Rows update from the stream.
- **Detail drawer**.
  - Simple: status, a stage timeline, links to what the job made, the error in plain
    words.
  - Advanced: adds the workflow and run ids with a Temporal UI link, the trace id, the raw
    request, result and error, the facets, and `via_tool` linking to its #1953 tool row.
    The Advanced switch is remembered as the Library page's is.
- **Bell**: a count of recently finished jobs from followed principals, with a toast on
  finish. Which principals are followed is a stored setting, defaulting to the browser
  user and its sessions.
- msw mocks under `src/mocks/features/jobs/`.

## 10. Errors

- A job whose source row is gone shows `source gone` in its detail; it is not an error.
- A job write failure: see §5.
- Every error is an RFC 9457 problem (`core/problems.py`). An unknown query parameter, or
  a facet no selected kind declares, answers 400 with a problem naming it, not FastAPI's
  default 422.

## 11. Testing

- Backend:
  - each writer upserts its job;
  - the status mapping per source;
  - an unknown facet answers 400;
  - the `ai_operations` listener upserts an agent job, and the repair pass restores one it
    missed;
  - a code-registered kind key starting with `agent.` fails discovery;
  - SSE resumes from `Last-Event-ID`;
  - the sweep deletes, and the repair pass restores a missed row.
- Agent:
  - the `activity_*` tools pin the caller;
  - `outward` sees every principal;
  - `test/coverage.test.ts` covers the new operations;
  - `AgentOperation` records `principal`, `session` and `via_tool` and sends its NOTIFY;
  - `ai_job_kinds` is upserted from the manifest at start, and an undeclared kind is
    retired.
- Frontend:
  - the filter bar renders from facets, including a kind the client has never seen;
  - live updates;
  - Simple and Advanced;
  - an e2e run of the page.
