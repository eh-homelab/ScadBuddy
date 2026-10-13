# Flow grants, delegated approvals and flow scripts in git: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person approves one bounded grant per flow run; calls inside it run without a
click; calls outside it climb child session → flow → starter session → person, where a
parent may deny, narrow or pass up but never approve; the person sees one queue per run.
Flow scripts are versioned in their own git repository, `data/flows`.

**Spec:** `docs/superpowers/specs/2026-10-11-flow-grants-and-git-design.md` (the
"amendment"; section numbers below are its own unless they say 10-01). It amends
`2026-10-01-durable-printing-agents-flows-design.md` §6.6 and §7.2–7.4.

**Issues:** #2481 (grants, Part A), #2482 (escalation, Part B), #2503 (git, Part C).
Epic #2486. One PR per issue. Part C is independent and may land first. Part B needs
Part A.

**Tech stack:** Python 3.12 (backend, `projects` worker, agent-durable; temporalio,
temporal-agent-harness), TypeScript (agent, Temporal TS SDK, Hono), React 19, Postgres.

## Global constraints

- **Workflows are replayed.** `ProjectWorkflow` is unversioned (plan 6 Ruling 13): every
  change to its code path goes behind `workflow.patched("<id>")`, and
  `tests/flows/test_project_replay.py` replays `tests/fixtures/project_workflow_histories/`
  unchanged. `DurableSession` changes likewise go behind `workflow.patched` and replay
  agent-durable's recorded histories. Patch ids used here: `flow-grant-check`,
  `flow-escalation-timer`, `flow-narrowed-decision`, `session-flow-grant`.
- **Migrations** are new files, `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`. A backend migration
  the agent reads (`flow_grants`, `flow_grant_uses`, new `workflow_runs`/
  `workflow_run_decisions` columns) is also copied into
  `agent/test/fixtures/backend-flow-migrations/` (`flowRuns.pg.test.ts` applies them).
- **Ownership.** The backend writes `workflow_*` and `flow_*`; the agent and agent-durable
  write `ai_*`. Neither writes the other's tables (amendment Ruling 2). The agent reads
  `workflow_runs`, `workflow_run_decisions`, `flow_grants`.
- **No self-approval.** No new path lets an agent approve. Every approval without a person
  is a grant match recorded as `grant:<id>` / role `run_grant`. Every browser-only route
  uses `_browser_only()` (`api/flows.py`) or `roleOf` (`agent/src/gate/role.ts`).
- **Binding (#2318).** Every approval, grant-made or narrowed, carries the input hash of
  the call that will run; the run-time checks in `flowRuns.ts` and
  `PgApprovalRecords` stay the gate.
- **Coverage.** Every new `/api/v1` operation gets a tool or a `NOT_A_TOOL` entry with its
  reason (`agent/src/tools/coverage.ts`, `agent/test/coverage.test.ts`).
- **Images**: none of this carries images. No base64 anywhere.
- **TDD**: each step's test is written and seen failing first.

## Part A: run grants (#2481)

### Task A1: Grant tables and the evaluator

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_flow_grants.sql`
- Create: `backend/scadbuddy/flows/grants.py`
- Modify: `backend/scadbuddy/flows/store.py`, `backend/scadbuddy/flows/models.py`
- Copy: the migration into `agent/test/fixtures/backend-flow-migrations/`
- Test: `backend/tests/flows/test_grants.py` (pure), `backend/tests/flows/test_grant_store.py` (`requires_postgres`)

**Schema:**

```sql
CREATE TABLE flow_grants (
    id          text PRIMARY KEY,
    run_id      text NOT NULL UNIQUE,          -- one grant per run (Ruling 6)
    status      text NOT NULL CHECK (status IN ('requested', 'approved', 'declined')),
    spec        jsonb NOT NULL,                -- GrantSpec, as approved (immutable once approved)
    used        jsonb NOT NULL DEFAULT '{}',   -- per rule index: {prints, filament_g, calls}
    requested_by jsonb NOT NULL,
    approved_by text, approved_at timestamptz,
    revoked_by  text, revoked_at timestamptz,
    expires_at  timestamptz,                   -- from spec, set at approval
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE flow_grant_uses (
    request_id  text PRIMARY KEY,
    grant_id    text NOT NULL REFERENCES flow_grants (id) ON DELETE CASCADE,
    run_id      text NOT NULL,
    fn          text NOT NULL, tool text,
    input_hash  text NOT NULL,
    rule_index  integer NOT NULL,
    prints integer NOT NULL DEFAULT 0, filament_g double precision NOT NULL DEFAULT 0,
    session_id  text, narrowed_by text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
```

`run_id` is not a foreign key: a grant is inserted before the run's row exists (the first
activity inserts `workflow_runs`). `forget_run` (`flows/forget.py`) deletes both. A start
that fails after the insert leaves a grant with no run: the flow orphan sweep
(`flows/sweep.py`) deletes grants older than one hour whose `run_id` has no
`workflow_runs` row and no open `flow-<run id>` execution.

**Interfaces (`flows/grants.py`):**

```python
class GrantRule(BaseModel): fn: Literal["queue_print", "arrange", "tool"]; tool: str | None
    printers: list[int] | None; models: list[str] | None; library_files: list[int] | None
    max_prints: int | None; max_filament_g: float | None; max_calls: int | None
    args: list[ArgBound] | None  # ArgBound: {path: str (RFC 6901), in: list[JsonScalar]}
class GrantSpec(BaseModel): expires_at: datetime; sessions: bool = False
    ask_starter_session: bool = True; rules: list[GrantRule] = Field(max_length=32)
class Narrowing(BaseModel): rules: list[GrantRule]  # a script's agent(..., grant=)
class CallFacts(BaseModel): fn; tool; args; printer_id: int | None; models: list[str]
    library_file: int | None; prints: int; filament_g: float | None
Verdict = Inside(grant_id, rule_index, input_hash) | Outside(reason: str, timeout_s: int, parent_hop: bool)

def validate_spec(spec: GrantSpec, *, tool_tiers: Mapping[str, str], now: datetime) -> list[str]
def admits(rule: GrantRule, facts: CallFacts, used: RuleUse) -> str | None   # None = admits; else reason
async def match_grant(store, facts_of, *, run_id, request_id, fn, tool, args,
                      session_id=None, flow_call_id=None, now) -> Verdict
```

- `validate_spec`: `expires_at` within 7 days of `now`; `fn == "tool"` needs `tool` and
  only a tool whose tier is `outward` (from `tool_tiers`, `raise_tiers(tiers(), overrides)`
  as `start_flow_run` computes it); `args` pointers parse; non-negative maxima.
- `admits`: every set bound (amendment §3.1); `printers` uses `facts.printer_id`
  resolved; `max_filament_g` with `facts.filament_g is None` → reason "filament unknown".
- `match_grant`: amendment §3.3 steps 1–6. Consumption is one transaction in the store:
  `INSERT … flow_grant_uses … ON CONFLICT (request_id) DO NOTHING RETURNING`; on conflict
  return the recorded verdict; else `UPDATE flow_grants SET used = jsonb_set(…) WHERE id = $1
  AND revoked_at IS NULL AND now() < expires_at AND (<limits hold>) RETURNING` and roll
  back the insert when no row returns (try the next admitting rule).

**Steps:**
- [ ] Write `test_grants.py`: each bound admits/refuses; unknown grams with a filament
  bound refuse; a missing pointer refuses; `validate_spec` refuses a read-tier tool, an
  expiry past 7 days, and accepts an unbounded rule. Run, see them fail.
- [ ] Implement `grants.py` models, `validate_spec`, `admits`.
- [ ] Write `test_grant_store.py`: two concurrent `match_grant` for the last of
  `max_prints` → exactly one `Inside`; a repeated request id returns the first verdict
  and consumes once; revoked or expired → `Outside`. See them fail.
- [ ] Migration, store methods (`insert_grant`, `get_grant(run_id)`, `consume`,
  `approve_grant`, `decline_grant`, `revoke_grant`, `grant_uses(run_id)`), `match_grant`.
- [ ] `forget_run` deletes the run's grant; extend `tests/flows/test_flow_routes.py`'s
  delete test.
- [ ] Copy the migration into the agent fixture directory.
- [ ] `uv run --frozen ruff check . && ruff format --check . && mypy && pytest tests/flows`.

### Task A2: Start with a grant; approve, decline, revoke

**Files:**
- Modify: `backend/scadbuddy/api/flows.py` (`RunCreate.grant`, new routes), `flows/models.py` (`Grant`, `GrantUse`, `RunView.grant`)
- Modify: `agent/src/tools/flows.ts` (`start_flow_run` takes `grant`; new `revoke_flow_grant`), `agent/src/tools/coverage.ts`
- Test: `backend/tests/api/test_flows.py`, `agent/test/tools.flows.test.ts` (or the existing flows tools test)

**Routes:**
- `POST /workflows/{id}/runs` body `RunCreate {approval_timeout?, grant?: GrantSpec}`:
  `validate_spec` (422 `flow-grant-invalid` with the reasons); insert the grant before
  `start_and_submit_message`, `approved` when `current_author() is None`, else
  `requested`. A repeat (same run id) does not insert again.
- `POST /workflow-runs/{id}/grant/approve` body `{spec?: GrantSpec, ask_starter_session?}`:
  browser-only. `spec`, when sent, replaces the requested one (validated by
  `validate_spec`): the person is the authority and may edit it either way; the agent's
  request was only a proposal. Sets `approved`, `approved_by = 'browser'`, `expires_at`.
- `POST /workflow-runs/{id}/grant/decline`: browser-only.
- `POST /workflow-runs/{id}/grant/revoke`: browser, or an author whose principal is the
  starter's. 409 when already revoked.
- `GET /workflow-runs/{id}` gains `grant` (spec, status, used, uses) on `RunView`.

**Steps:**
- [ ] Tests: browser start with grant → `approved`; agent start → `requested`; approve
  and decline refuse the author header (403 `flow-entry-browser-only`); approve with an
  edited spec stores the edit; revoke by another principal → 403; revoke by the starter → 200;
  `RunView.grant` shows uses. See them fail.
- [ ] Implement routes and models.
- [ ] Agent: `start_flow_run` input gains optional `grant`; `revoke_flow_grant` tool
  (`write`); `NOT_A_TOOL` entries for approve and decline with the reason "A run grant
  is approved by the browser user only (amendment §3.2)". `pnpm gen:api` regenerates the
  schema (never committed).
- [ ] Backend and agent lint, typecheck, tests.

### Task A3: The flow-own gate checks the grant

**Files:**
- Create: `backend/scadbuddy/workflows/flow_grants.py` (activities `flow_grant_check`, `flow_grant_approve`)
- Modify: `backend/scadbuddy/workflows/flow_tools.py` (`_gated`), `workflows/projects_worker.py` (register), `workflows/flow_steps.py` (`step(..., grant=…)` records the verdict on the step)
- Modify: `backend/scadbuddy/flows/operations.py` (`flow_decide` refuses a call already approved by a grant: stale)
- Test: `backend/tests/flows/test_grant_gate.py`, `tests/flows/test_project_replay.py` (must stay green)

**Interfaces:**

```python
@activity.defn(name="flow_grant_check")
async def flow_grant_check(req: GrantCheck) -> GrantVerdict
# GrantCheck: run_id, request_id, fn, tool | None, args, session_id | None, flow_call_id | None
# GrantVerdict: verdict "inside"|"outside", grant_id?, input_hash?, reason?, timeout_s, parent_hop

@activity.defn(name="flow_grant_approve")
async def flow_grant_approve(req: GrantApprove) -> bool
# records workflow_run_decisions(outcome approved, responder 'grant:<id>', input_hash),
# then AgentClient.approve_tool(call_id, approved=True, update_id=f"grant-{call_id}")
```

`facts_of` (in `flow_grants.py`) resolves `CallFacts` through the backend API, never the
data volume: the `projects` worker has none (`worker.py` `run_projects_worker`: Postgres,
the KEK and `SCADBUDDY_API_INTERNAL_URL` only). It reads with the same internal httpx
client and `api_internal_url` the host functions use (`flow_routes.py`): the configured
printer and the remembered quantity from the print options read, the output's slug and
plate count from `GET /api/v1/outputs/{id}`, grams from the output's print filament
options when known, and `copies` defaulted as `print_run.py` resolves it. A failing
lookup returns facts that admit nothing. A test runs it against a fake API with no
volume mounted, as a split deployment runs it.

**Pinned args:** an inside verdict's `args` replace the call's (amendment §3.3 step 6):
`queue_print` runs `approved_print` with `request.printer_id` and `request.copies` set to
what was checked; the decision's `input_hash` is over those args.

**`_gated` change**, behind `workflow.patched("flow-grant-check")`: before
`run_gated`, run `flow_grant_check` (start-to-close 10 s, retries 5). Inside: the step
is opened with no `waiting` (so no `waiting_on` entry is projected and nobody is
notified), the gated call is started, and `flow_grant_approve` runs at once; the harness
holds it as a pending approval only until that Update lands, then runs the body.
Outside: unchanged (parks for the person, with its `waiting_on` entry; Part B adds
hops).

**Steps:**
- [ ] Test (time-skipping env, fake API as `test_outward_host_functions.py` does): a
  `queue_print` inside the grant runs with no `waiting_on` entry and its decision row has
  `responder = grant:<id>` and the hash; outside, it parks as before; `max_calls: 1` →
  the second call parks; a `queue_print` with no `printer_id` matched against the
  configured printer is sent with that printer pinned even after the setting changes.
  See them fail.
- [ ] Test: an outward `tool(...)` inside the grant reaches the agent activity with an
  approved decision whose hash matches (fake agent-tools worker, as
  `test_tool_host_function.py`).
- [ ] Implement activities, `facts_of`, `_gated` behind the patch; register on the
  `projects` worker.
- [ ] `test_project_replay.py` green on the recorded histories.
- [ ] Backend lint, mypy, `pytest tests/flows`.

### Task A4: The run-time check in the agent

**Files:**
- Modify: `agent/src/temporal/flowRuns.ts` (`approved` joins `flow_grants`), `agent/src/temporal/toolActivities.ts` (`PgApprovalRecords.approved`, the `GrantRevoked` refusal), `backend/scadbuddy/workflows/flow_tools.py` (`_TOOL_REFUSALS` gains `GrantRevoked`)
- Test: `agent/test/flowRuns.pg.test.ts`, `agent/test/toolActivities.test.ts`

**Rule:** a decision whose responder is `grant:<id>` (flow) or an `ai_input_responses`
row whose responder kind is `run_grant` (child) approves only while
`flow_grants.revoked_at IS NULL AND now() < expires_at`. Otherwise the activity refuses
with type `GrantRevoked` ("the run's grant was revoked or expired before this call ran"),
audited `refused` like `NotApproved`.

**Steps:**
- [ ] Tests: grant-approved call runs; same call after revoke → `GrantRevoked`; after
  expiry → `GrantRevoked`; changed input → `NotApproved` (#2318 unchanged). See them fail.
- [ ] Implement; `pnpm lint && pnpm typecheck && pnpm test` (with
  `SCADBUDDY_TEST_DATABASE_URL`).

### Task A5: Grants reach child sessions' flow turns

**Files:**
- Modify: `agent/src/sessions/durable.ts` (`DurableTurns.send` sets `flow` from the activity context), `agent/src/temporal/toolActivities.ts` (pass the flow run and call id to `sessions_start`/`sessions_send` runs)
- Modify: `agent-durable/src/scadbuddy_durable/session/models.py` (`Message.flow: FlowTurn | None`), `session/workflow.py` (`_park`: grant check first, behind `workflow.patched("session-flow-grant")`)
- Modify: `backend/scadbuddy/workflows/flow_tools.py` (`agent`, `ask_session` take `grant: dict | None = None` and `on_outside: str = "escalate"`, both optional (amendment Ruling 9 as amended by #2636); validated with `Narrowing`; recorded as `steps[].policy`), `workflows/flow_steps.py`, `flows/typecheck.py` if stubs need it
- Test: `agent-durable/tests/test_session_workflow.py`, `agent/test/durable.test.ts`, `backend/tests/flows/test_agent_host_functions.py`

**Interfaces:**

```python
@dataclass
class FlowTurn:  # on Message, set only by the agent service
    run_id: str
    call_id: str
```

`DurableSession._park`, for an `approval` call while the turn's `Message.flow` is set:
run `flow_grant_check` on task queue `projects` with `session_id`, `flow_call_id`,
`tool`, `args = call["input"]`. Inside: `_resolve`-equivalent without opening an entry:
run `resolve_input` with outcome `approved`, responder `{kind: "run_grant", id}`,
and the `describe_call` hash for the audit row, then `agent.decide(…, True, …)`.
Outside: park as today (Part B adds hops); the entry's timer is the verdict's
`timeout_s` instead of `approval_expiry_s`.

The agent sets `Message.flow` only when the `sessions_start`/`sessions_send` activity
runs under a `flow-<run id>` workflow; the tools' input schemas gain nothing, so a
script cannot name a run.

**Steps:**
- [ ] Tests: a child call in a flow turn inside the grant runs with no entry and its
  `ai_input_responses` responder is `run_grant`; a call in a later browser turn of the
  same session parks; a script `grant=` narrower than the run grant refuses what it
  excludes; a script `grant=` wider admits nothing beyond the run grant; `sessions:
  false` → parks; a Message's `flow` naming another run's step → parks. See them fail.
- [ ] Test (agent): `sessions_send` from a non-flow context never sets `flow`; from a
  flow activity it sets the run and call id.
- [ ] Implement. Old-arity scripts keep working (amendment Ruling 9 as amended by
  #2636): keep a test that one still type-checks and runs with the defaults.
- [ ] agent-durable `uv run --frozen ruff check . && mypy && pytest`; agent and backend
  suites.

### Task A6: The start dialog and the run's grant panel

**Files:**
- Create: `frontend/src/components/flows/GrantEditor.tsx` (+ test), `frontend/src/components/flows/GrantPanel.tsx` (+ test)
- Modify: `frontend/src/pages/WorkflowsPage.tsx` (start dialog), `frontend/src/pages/WorkflowRunPage.tsx` (grant, uses, approve a requested grant, revoke), `frontend/src/mocks/features/flows.ts`
- Test: vitest beside each; `frontend/e2e/flows.spec.ts` (mocked)

**Behaviour:** the editor lists one rule per host function and outward tool the run may
use, with printers and models as pickers fed by the existing print-targets and models
reads, numeric maxima, an expiry (default 24 h, max 7 days), "extends to the sessions it
starts", and "ask the starting session first". An unbounded rule shows a red warning.
The panel shows status, each rule's used/max, the uses list (time, fn, tool, session),
Revoke, and for a `requested` grant Approve (with the editor pre-filled)
and Decline.

**Steps:**
- [ ] Vitest: editor output validates; unbounded warning; approve sends the narrowed
  spec; revoke confirms. See them fail. Implement. `pnpm lint && pnpm typecheck && pnpm
  test && pnpm build && pnpm exec playwright test e2e/flows.spec.ts`.

## Part B: escalation (#2482)

### Task B1: Hop columns and records

**Files:**
- Create: `agent/src/db/migrations/<ts>_pending_input_hops.sql`
- Create: `backend/scadbuddy/migrations/<ts>_workflow_run_decision_hops.sql` (+ agent fixture copy)
- Modify: `backend/scadbuddy/workflows/flow_models.py` (`FlowWaiting` gains `hop`, `hop_until`, `proposal`, `reason`, `hops`), `agent-durable/.../gate/store.py` (`open_input` writes the new columns), `agent/src/gate/projection.ts`
- Test: `agent-durable/tests/test_gate_store.py`, `agent/test/gate.projection.test.ts`

```sql
ALTER TABLE ai_pending_input
  ADD COLUMN flow_run_id text, ADD COLUMN hop text CHECK (hop IN ('parent', 'person')),
  ADD COLUMN hop_until timestamptz, ADD COLUMN proposal jsonb,
  ADD COLUMN outside_reason text, ADD COLUMN hops jsonb NOT NULL DEFAULT '[]';
CREATE INDEX ai_pending_input_flow_run ON ai_pending_input (flow_run_id) WHERE flow_run_id IS NOT NULL;
ALTER TABLE ai_input_responses ADD COLUMN hops jsonb NOT NULL DEFAULT '[]';
-- backend:
ALTER TABLE workflow_run_decisions ADD COLUMN hops jsonb NOT NULL DEFAULT '[]';
```

`proposal` holds narrowed input; it is scrubbed for display like `summary` and never
returned raw by a read (amendment §4.5).

- [ ] Tests for the writes, see them fail, implement, run both suites.

### Task B2: Roles `parent` and `run_grant`

**Files:**
- Modify: `agent/src/gate/validate.ts`, `agent/src/gate/role.ts`, `agent-durable/src/scadbuddy_durable/gate/validate.py`, `agent/test/fixtures/pending-input-vectors.json`
- Test: `agent/test/gate.vectors.test.ts`, `agent-durable/tests/test_gate_vectors.py`

**Rules (amendment §4.2):** `approval` accepts `browser` (approve, deny, narrow), `grant`
(approve, deny, refused when the entry has `flow_run_id` or is `flow:`), `parent`
(deny, narrow; never approve); `answer` accepts `browser` only; `run_grant` is refused
from any `respond`. A narrow response is `{"kind": "approval", "decision": "narrow",
"input": {…}}`, at most 16 KiB like any response. `roleOf` gains the escalation's
record: `parent` when the principal is the session's owner AND `ctx.session` is the
run's starter session AND the entry's hop is `parent`.

- [ ] Add vectors first (each role × kind × action, narrow shape, a `grant` token on a
  run-queue entry, the starter's principal from a non-starter session). Both suites
  fail. Implement both validators and `roleOf`. Both suites pass.

### Task B3: `escalation_respond` and the backend's parent route

**Files:**
- Create: `agent/src/routes/escalations.ts` (`POST /api/v1/ai/escalations/{request_id}`), `agent/src/flows/escalations.ts` (`EscalationService`), `agent/src/tools/escalations.ts` (`escalation_respond`, `write`)
- Modify: `backend/scadbuddy/api/flows.py` (`POST /workflow-runs/{id}/escalations/{call_id}`), `backend/scadbuddy/flows/operations.py` (a `flow_escalate` kind beside `flow_decide`)
- Modify: `agent-durable/.../session/workflow.py` (`respond` accepts `parent` deny/narrow; narrow runs `flow_grant_check` before `resolve_input`, behind `session-flow-grant`)
- Test: `agent/test/escalations.test.ts`, `backend/tests/api/test_flow_escalations.py`, `agent-durable/tests/test_session_workflow.py`

**Dispatch:** `durable:` → the child's `respond` Update with role from `roleOf`;
`flow:` → backend route through `api/backend.ts` with the session's author header. The
backend authorises: author's session equals `started_by.session`, entry at hop
`parent`. Actions:
- `deny` → `flow_decide` with `approved=false`, responder `session:<id>`, reason.
- `narrow` → `match_grant` on the narrowed args (in-process); inside → `flow_decide`
  approve with responder `grant:<id>`, `narrowed_by`, and `response.args`; outside →
  `waiting_on` entry to hop `person` with `proposal`.
- `pass_up` → hop `person`.
Each appends to the entry's `hops`. The flow's own hop is applied at park time: a
verdict for a child whose step's policy is `on_outside: "deny"` is refused without
parking (`agent.decide(…, False, "flow")`, recorded `denied` by `flow`); a flow-own call
has no flow hop.

- [ ] Add to `core/authorship.py`'s docstring: the header now also names the starter
  session for `parent` actions and the starter for revoke, and a forged one can only
  deny, narrow into the grant, pass up or revoke (amendment §4.2).
- [ ] Tests: each action at hop `parent`; refused at hop `person` (409 "with the person
  now"); refused from another session of the same owner (403); `approve` is not an
  action (422); a narrow inside the grant runs, outside reaches the person with the
  proposal. See them fail. Implement. All three suites.

### Task B4: Narrowed approvals run their narrowed input

**Files:**
- Modify: `agent/src/temporal/toolActivities.ts` (`runAsActivity`: a narrowed approval substitutes `response.input` / `response.args` after checking its hash; the result starts "Ran narrowed by …"), `agent/src/temporal/flowRuns.ts` (`approved` returns the narrowed args)
- Modify: `backend/scadbuddy/workflows/flow_tools.py` (`approved_print`, `approved_arrange` read a narrowed request through activity `flow_decision`, behind `workflow.patched("flow-narrowed-decision")`)
- Test: `agent/test/toolActivities.test.ts`, `backend/tests/flows/test_outward_host_functions.py`, replay test

- [ ] Tests: narrowed child call runs with the narrowed input; a narrowed hash that does
  not match the recorded one → `NotApproved`; a narrowed `queue_print` sends the
  narrowed copies and printer; replay green. See them fail. Implement.

### Task B5: Delivery to the starter session

**Files:**
- Modify: `agent/src/flows/escalations.ts` (`deliver()`), `agent/src/main.ts` (run it on the approval sweep's tick and on `session.result` for a starter session)
- Modify: `agent-durable/.../gate/store.py` (`open_input`: hop `parent` when the verdict says `parent_hop`, else `person`; `hop_until`), `backend/scadbuddy/workflows/flow_steps.py` (same for flow-own entries)
- Test: `agent/test/escalations.delivery.test.ts`

**Behaviour (amendment §4.4):** for each starter session with undelivered `parent`
entries: skip (move to `person`) when archived, deleted, handed off since the run
started, or out of budget; wait when in a turn; else send one turn authored
`{kind: "system", id: "escalation"}` listing the entries (run, step, tool, summary,
proposal, outside reason, the child's reasoning) and how to answer with
`escalation_respond`; mark delivered (`hops` gains `{action: "delivered"}`).

- [ ] Tests: the turn wraps summary, proposal, reason and reasoning in the untrusted-data
  envelope (`safety/untrusted.ts`); a run that `ask_session`s its own starter session
  opens that child's outside calls at hop `person`, never `parent`; idle classic and
  durable sessions get one batched turn; a busy session gets
  it after its turn ends; an archived one sends the entries to the person; a run started
  by a token with no session never delivers. See them fail. Implement.

### Task B6: Hop timeouts

**Files:**
- Modify: `agent/src/gate/sweep.ts` (due `parent` child entries → `person`), `backend/scadbuddy/flows/sweep.py` (due `parent` flow-own entries → `person`), `backend/scadbuddy/workflows/flow_entries.py` / `flow_tools.py` (`run_gated` timer = parent hop + run timeout, behind `workflow.patched("flow-escalation-timer")`), `agent-durable/.../session/workflow.py` (same for a child entry)
- Test: `agent/test/gate.sweep.test.ts`, `backend/tests/flows/test_flow_sweep.py`, `backend/tests/flows/test_project_workflow.py`

**Rule:** parent hop length = the run's `approval_timeout_s`, or
`approval_expiry_seconds` when that is 0. Passing up never denies. The person's timer
denies as today.

- [ ] Tests (time-skipping): a parent that never answers → entry at `person` after one
  timeout, denied after the second; with a run timeout of 0, the parent hop ends after
  `approval_expiry_seconds` and the person's hop never times out. See them fail.
  Implement. Replay green.

### Task B7: One queue per run, and the badge

**Files:**
- Create: `agent/src/routes/flowQueue.ts` (`GET /api/v1/ai/flow-runs/{run_id}/queue`)
- Modify: `agent/src/routes/pendingInput.ts` (aggregate includes run entries at hop `person`, grouped by run), `agent/src/gate/projection.ts`, `agent/src/tools/coverage.ts` or a `read` tool `get_flow_run_queue`
- Modify: `frontend/src/agent/attention.ts`, `frontend/src/pages/WorkflowRunPage.tsx`, assistant panel pending list, `frontend/src/mocks/features/flows.ts`
- Test: `agent/test/flowQueue.test.ts`, frontend vitest, `frontend/e2e/flows.spec.ts`

**Entry shape:** 10-01 §6.6's entry plus `{run_id, source: "flow"|"session"|"grant",
hop, step: {seq, fn}, flow: {name, commit}, reasoning?, proposal_summary?,
outside_reason, hops}`. `reasoning` is the child turn's assistant text before the call,
last 2 KiB, wrapped as untrusted data. The read is visible to the browser user and the
starter.

- [ ] Tests: the union of the three sources; a child entry outside a flow turn is not in
  it; the badge counts hop `person` only; the page answers each kind through its own
  route. See them fail. Implement.

## Part C: flow scripts in git (#2503)

### Task C1: Extract the git core

**Files:**
- Create: `backend/scadbuddy/core/gitrepo.py` (`GitRepo`, `git_env`, `GitError`, `GitUnavailableError`, `GitTimeoutError`, `RevisionNotFoundError`, `Revision`, `RevisionRange`, `FileChange`, `subject_line`, log parsing, the lock)
- Modify: `backend/scadbuddy/library/history.py` (`ModelHistory(GitRepo)` keeps `.gitignore`, seeding, symlink-safe reads, export, merge; re-exports the moved names so imports keep working)
- Test: `backend/tests/test_history.py` **unchanged**; new `backend/tests/test_gitrepo.py`

**`GitRepo` interface:** `__init__(root, *, timeout)`, `available`, `ensure_repo()`,
`write_lock()`, `commit(message, *paths, prepare=None, trailers=(), allow_empty=False,
date=None) -> str | None` (author trailers from `current_author()`, then `trailers`;
`None` only when nothing changed and `allow_empty` is false, as `ModelHistory` behaves
today; `date` sets `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` for the backfill), `head()`, `resolve(commit)`, `log(path=None, *, limit)`,
`show(commit, path) -> bytes`, `revision_range(base, head)`, `diff(range, path=None)`,
`restore(path, commit, message, *, trailers=(), allow_empty=False) -> str` (always one
commit when `allow_empty`, even restoring the head's own content, so
`FlowHistory.restore` carries its version trailer), `maintain()`, and a hook
`_gitignore_body() -> str | None` for subclasses.

- [ ] Write `test_gitrepo.py` against a bare `GitRepo` (commit with trailers, log, show,
  diff, restore as a new commit, lock timeout). See it fail. Move the code. Run
  `test_history.py` untouched, and the whole backend suite (`requires_git`).

### Task C2: `FlowHistory`

**Files:**
- Create: `backend/scadbuddy/flows/repository.py`
- Modify: `backend/scadbuddy/core/paths.py` (`flows` → `root / "flows"`), `backend/scadbuddy/flows/component.py` (builds it)
- Test: `backend/tests/flows/test_flow_repository.py`

```python
class FlowHistory(GitRepo):
    def write(self, name: str, script: str, version_id: str) -> str
        # always one commit ("Register <name> v<N>", --allow-empty when unchanged),
        # trailers ScadBuddy-Flow: <name>, ScadBuddy-Flow-Version: <version_id>
    def register(self, name: str, script: str, version_id: str,
                 index: Callable[[str, int], None]) -> tuple[str, int]
        # under write_lock(): reconcile pending orphans, write(), then
        # index(commit, position); the lock is held across all three
    def script_at(self, name: str, commit: str) -> str
    def versions(self, name: str) -> list[Revision]          # commits with the name's trailer, newest first
    def version_of(self, name: str, commit: str) -> int      # 1-based position among them, oldest first
    def commit_for(self, version_id: str) -> str | None      # by ScadBuddy-Flow-Version trailer
    def diff(self, name: str, base: str | None, head: str) -> str
    def restore(self, name: str, commit: str, version_id: str) -> str  # "Restore <name> to <short>", one commit
```

- [ ] Tests: a write is one commit with the author and flow trailers; an identical
  script is still a new commit (`--allow-empty`) with the next version number; restore
  to the head's own content is a new commit; `versions`, `version_of`, `commit_for`;
  diff; the lock file is in `data/flows`. See them fail. Implement.

### Task C3: Index rows and runs record the commit

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_workflow_definition_commits.sql` (+ agent fixture copy)
- Modify: `backend/scadbuddy/flows/store.py`, `flows/models.py` (`Definition.commit`; `script` filled from git), `api/flows.py` (`register_flow` commits then indexes; `start_flow_run` reads the script at the commit), `workflows/project.py` (`FlowStart.commit: str = ""`, recorded by `FLOW_RECORD`), `workflows/flow_models.py` (`FlowRecord.commit`)
- Test: `backend/tests/api/test_flows.py`, `backend/tests/flows/test_project_replay.py`

```sql
ALTER TABLE workflow_definitions ADD COLUMN commit text;
ALTER TABLE workflow_definitions ALTER COLUMN script DROP NOT NULL;
ALTER TABLE workflow_runs ADD COLUMN commit text;
```

`FlowStart.commit` defaults to `""` so recorded histories decode and replay unchanged.

**Registration under one lock:** `register_flow` and `restore` run
`FlowHistory.register(name, script, version_id, index)` where `index(commit, version)`
inserts the row; `register` holds `write_lock()` across the reconcile-pending check, the
commit and `index`, and computes `version` as the commit's position. `store.py`'s
`MAX(version) + 1` is no longer used for flows.

- [ ] Tests: register writes a commit and a row with it; re-registering an unchanged
  script (only `approval_timeout` changed) is a new commit and version, never a
  `UNIQUE (name, version)` failure; version equals the commit's position; an insert
  that fails after its commit, then another registration, then the reconciler: the
  orphan is indexed at its own position, the later row keeps its own, no collision;
  two registrations of one name from two threads (and two processes, `flock`) each get
  the version of their commit's position; start reads git (a script edited in the row but not in git is ignored); a
  missing commit → 409; the run row's `commit` is set. Replay green. See them fail.
  Implement.

### Task C4: History routes and tools

**Files:**
- Modify: `backend/scadbuddy/api/flows.py` (four routes under `/workflows/names/{name}/versions`)
- Modify: `agent/src/tools/flows.ts` (`list_flow_versions`, `get_flow_version_source`, `diff_flow_version`: `read`; `restore_flow_version`: `write`)
- Test: `backend/tests/api/test_flows.py`, agent tools test, `agent/test/coverage.test.ts`

- [ ] Tests: history newest first with author; source at a commit; diff; restore type-
  checks and makes a new version; unknown name 404; a commit not touching the name 404.
  See them fail. Implement.

### Task C5: Backfill and reconciler

**Files:**
- Create: `backend/scadbuddy/flows/backfill.py`
- Modify: `backend/scadbuddy/flows/component.py` (run at start, after migrations, under the repository's lock)
- Test: `backend/tests/flows/test_flow_backfill.py` (`requires_postgres`, `requires_git`)

**Behaviour (amendment §6.4):** rows without `commit`, ordered `(name, version)` (never
`created_at`, which concurrent registrations can have out of version order):
write, commit with `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` = `created_at` and trailers from
`created_by`, set `commit`; then map runs. Reconciler: a commit whose `ScadBuddy-Flow-Version`
trailer no row names gets a row with that id (`created_by` from its trailers); a row
with no `commit` whose id a commit's trailer names is joined to it. It never matches on
content. Idempotent.

- [ ] Tests: three versions of one name (two of them identical and consecutive) and one
  of another backfill in order with their dates and authors and keep their version
  numbers; the reconciler indexes an orphan commit at its position after later rows
  exist, without a `UNIQUE (name, version)` failure; a second run changes nothing; a crash after commit before update
  is repaired; runs map to their version's commit. See them fail. Implement.

### Task C6: Deployment and docs

**Files:**
- Modify: `README.md` ("Deploying": `data/flows` on the data volume), `docs/ai/operating.md` if it lists the data layout, `CLAUDE.md` (Layout: `flows/repository.py`, `core/gitrepo.py`)

- [ ] Docs only; no test.

## Spec coverage

| Amendment | Tasks |
|---|---|
| §3.1 shape, §3.2 who approves | A1, A2, A6 |
| §3.3 one evaluator, recording | A1, A3, A5 |
| §3.4 flow turns | A5 |
| §3.5 revocation, run-time check | A2, A4 |
| §4.1–4.2 chain, roles, narrowing | B2, B3, B4 |
| §4.3 the flow as parent | A5 (params, narrowing), B3 (`on_outside`) |
| §4.4 starter session | B5 |
| §4.5 one queue | B7 |
| §4.6 hops, timeouts | B1, B6 |
| §5 identity and tiers | A4 (tier lost: existing `liveTokenTiers` refusal, asserted), B5 |
| §6 git | C1–C6 |
| §9 testing | each task's tests |

## Open questions for the user

The rulings marked **(user)** in amendment §8 (4, 5, 6, 7, 8, 11, 13). Part A and Part C
can start on the rulings as written; Part B's B5 and B6 depend on 11 and 13.
