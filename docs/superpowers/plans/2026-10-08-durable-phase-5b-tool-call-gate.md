# Durable phase 5b: the tool-call gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build every piece of spec §6.6's tool-call gate that does not need the `DurableSession` workflow itself: the durable projection and response tables, one validator shared by the classic route and the durable Update (proved by shared vectors), the durable `open_input` / `resolve_input` activities, the classic question expiry, the `input.*` events in both modes, the two reads and the `respond` route over both modes, the answer tools and the activity's answer read, and the orphan sweep. Phase 5c then writes `DurableSession` on top of these.

**Architecture:** The gate's rules live twice, once per language, and a JSON file of vectors pins them together, as the credential vectors do (5a). TypeScript: `agent/src/gate/` (request ids, the pure validator, the route's role, the durable client over Temporal, the projection read, the orphan sweep, the answer read). Python: `agent-durable/src/scadbuddy_durable/gate/` (ids, the same validator, the entry builder, and the two activities that are the only writers of `ai_pending_input` / `ai_input_responses`). The classic stores (`ai_approvals`, `ai_questions`) stay; the classic `respond` route now runs the shared validator before it writes.

**Tech Stack:** TypeScript (Hono, postgres.js, zod, vitest, `@temporalio/client` / `@temporalio/testing` pinned as they are), Python 3.12 (psycopg 3, `temporalio` 1.33–1.34, pytest).

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §6.4, §6.5 step 3, §6.6 (whole), §8 ("Durable session"), §10 phase 5. Sub-plan table: `docs/superpowers/plans/2026-10-08-durable-phase-5a-agent-durable-foundation.md`. Issue #1056, epic #1058.

## What 5c consumes from 5b

5c (`DurableSession`) is written against these, and changes none of them:

| 5b produces | 5c uses it for |
|---|---|
| `ai_pending_input`, `ai_input_responses`, `ai_audit.request_id` (migration `20261009T0052Z_pending_input.sql`) | nothing directly: only through the activities below |
| `scadbuddy_durable.gate.ids.durable_request_id(session_id, run_id, tool_use_id)` | the entry's `id` in workflow state |
| `scadbuddy_durable.gate.entry.build_entry(...) -> GateEntry` (refuses a prompt over 16 KiB with `PromptTooLargeError`) | the state it keeps when the plugin's `pending_approvals()` gains a call |
| `scadbuddy_durable.gate.validate.validate_respond(entry, request)` (raises `RespondRefused(code, message)`) | the `respond` Update's validator (plus the plugin's `validate_decision`) |
| activities `open_input` and `resolve_input` (`scadbuddy_durable.gate.activities`, `GateActivities(pool)`), with `RESOLVE_ON_RESPOND` (schedule-to-close 30 s) and `RESOLVE_ON_TIMER` (no deadline, backoff capped at 300 s) retry settings | every write about an entry; registered on its `agent` queue worker |
| names `PENDING_INPUT_QUERY = "pending_input"`, `RESPOND_UPDATE = "respond"`, `CANCEL_INPUT_UPDATE = "cancel_input"`, `INTERRUPT_SIGNAL = "interrupt"` (both languages, pinned equal by the vectors file) | its handler names; the TypeScript client already sends them |
| `RespondRefused` codes → `ApplicationFailure` type `GateRefused:<code>` | how a validator rejection reaches the route, which maps it to HTTP |
| the manifest's `hitl` field (`dist/tools.json`) and the durable-only tools `ask_user` / `wait_for_user` | `activity_as_tool(..., needs_approval=hitl is not None)` |
| the `agent-tools` activity reading the answer from `ai_input_responses` by `request_id`, and the `tool_call` audit row naming its approver through `request_id` | the answer kinds' results, and the approved call's audit |
| `DurableGate.cancelInput` / `interrupt` (TypeScript client) | 5c wires them into the manager's interrupt, handoff and send paths, beside the workflow's handlers |
| the activity `gate.describe_call` on `agent-tools` (`{tool, input}` → `{summary, input_hash}`, Ruling 14) | `OpenInput.summary` and `input_hash` for an approval entry |
| the orphan sweep (`PendingInputSweep`) | nothing: it already runs in the agent service |

## Rulings (decisions where §6.6 is ambiguous; the spec governs everything else)

- **Ruling 1, the response cap.** §6.6 says 16 KiB. The classic route already caps a response at `RESPONSE_MAX` (`routes/pendingInput.ts`), derived from the largest answer the panel can send, because 16 KiB is smaller than one valid answer. The durable validator uses the same `RESPONSE_MAX`, written into the vectors file and asserted equal in both suites, so the two modes accept the same answers. The *prompt* cap stays the spec's 16 KiB.
- **Ruling 2, `flow:` ids.** Flows are phase 6. Until `ProjectWorkflow` exists, the route refuses a `flow:` id as stale (404), as it refuses any unknown prefix. Flow entries are answered on the Workflows page only (§6.6), so nothing lists one here.
- **Ruling 3, `cancel_input`.** Its handler and its three callers (interrupt, handoff, superseding send) need the workflow, so they are 5c's. 5b ships the client calls (`DurableGate.cancelInput` with its 10 s bound and `DurableGate.interrupt`), tested against a stand-in workflow.
- **Ruling 4, the answer tools.** `ask_user` and `wait_for_user` are durable-only tools: activities on `agent-tools` and entries in the manifest, never in `/mcp`'s list or a classic turn. A classic session keeps `AskUserQuestion` and `request_user_attention` (its question server), and an MCP client must not get a tool only the browser user can answer. `wait_for_user` offers only `on_timeout: proceed` (§6.6 Timeouts) until 5c has verified cancelling a turn.
- **Ruling 5, `input.*` on the chat socket.** `input.requested` / `input.resolved` are written to the session's log and announced on the bus in both modes, but the chat socket does not forward them: the panel's cards keep their per-kind events (§6.6 Notifications), and a panel bundle that predates the types would report each as a protocol error.
- **Ruling 6, an answer after a question's timer.** A classic question past its `expires_at` refuses an answer (409, "nobody answered in time") even before the sweep has resolved it, so "answered after the timer" is refused in both modes (§8). Attention requests are unchanged: their parked waiter is their timer (#815).
- **Ruling 7, the approval routes.** `POST /api/v1/ai/approvals/{id}/approve|deny` keep taking `ai_approvals` ids only. A durable approval is decided through `respond` (the browser) or `sessions_approve` / `sessions_deny`, which now also take a `durable:` id (a grant holder), with the role the route computes.
- **Ruling 8, an orphan's outcome.** The sweep resolves an orphan as `cancelled` with reason `the session's workflow run has ended`, writing the outcome, `input.resolved` and (for an approval) the audit row, like any other resolution.
- **Ruling 9, `requested_by`.** Stored as `jsonb` (`{kind, id, label}`), the entry shape's own field; `responders` as `text[]`; the attention details as `attention jsonb`, which the aggregate read needs for the badge's counts.
- **Ruling 10, the per-session read.** `GET /api/v1/ai/sessions/{id}/pending-input` is a UI read behind `uiReadProblem`, like the aggregate. Other principals read through the two read-tier tools, which apply the approvals' visibility rules (grant holders see every approval; others their own sessions' entries and their own MCP prepares; answers only for sessions they own).
- **Ruling 11, the durable read while the worker is down.** The per-session read sends the Query and answers 503 ("the session's worker is not answering") when it does not answer within 5 s. It never falls back to the projection: the spec makes the Query the source of truth.
- **Ruling 12, `wait_for_user` has no `done` reason.** A `done` summary waits for nothing and is posted, not parked, so it is not a gated call. The durable tool takes `tab_disconnected`, `question` and `blocked` only; a durable `done` summary is 5c's to design if it is wanted.
- **Ruling 13, the session's status after an orphan.** The sweep cancels a closed run's rows and moves a session still `waiting_*` with nothing else parked to `idle`, not `running` as `resolve_input` does: no run holds the entry any more. If a newer run exists, DurableSession sets its own status at its next transition.
- **Ruling 14, an approval's summary and hash come from the agent.** Python cannot reproduce the agent's scrubbing or the approvals' HMAC key, so 5b adds the `gate.describe_call` activity on `agent-tools` (`{tool, input}` → `{summary, input_hash}`). `OpenInput` takes both as given.

## Global Constraints

- Agent schema changes are new files in `agent/src/db/migrations/`, `<yyyymmdd>T<hhmm>Z_<slug>.sql`; never edit a merged one.
- `question_expiry_seconds`: default 3600, bounded 10–86 400 like `approval_expiry_seconds`, clamped as `ApprovalService.expirySeconds` clamps.
- A timer never approves and never answers. Approval timer → `expired`; question timer → `cancelled`; attention timer → `timed_out`.
- An `approval` entry never carries the call's raw input; an `answer` entry's `prompt` is scrubbed and at most 16 KiB.
- Request ids: `approval:<uuid>`, `question:<uuid>`, `durable:<session id>:<workflow run id>:<tool_use_id>`, `flow:<run id>:<workflow run id>:<call id>`. Only the route's prefix dispatch and the sweep parse them.
- Roles: `browser`, `grant`, `owner`. Ownership wins: a non-browser owner with the grant is `owner`. `approval` accepts `browser` and `grant` (never self); `answer` accepts `browser` only.
- Both writers of `ai_pending_input` remove a row through one guarded `DELETE … WHERE request_id = $1 RETURNING`; only the winner writes the outcome, the event and the audit.
- Stay out of `agent/src/harness/fallback*` and the budget code (another session's work).
- `@temporalio/*` stay pinned exactly as they are.

## Review Focus

1. **Two resolutions racing** (a respond and the sweep, or a respond and the classic expiry): exactly one `input.resolved` and one outcome row. Pinned in Task 4 (Python, two concurrent `resolve_input`) and Task 9 (sweep vs. resolve).
2. **A grant holder that owns the session** answers `durable:` approvals through `sessions_approve`: role `owner`, refused. Pinned in Task 2 (`roleOf`) and Task 7 (the tool).
3. **A durable entry whose worker has been down past `expires_at`**: listed with `expiring: true` and not counted by the badge. Pinned in Task 6 (read) and the frontend count test.
4. **A question answered after its expiry but before the sweep ran**: refused, the row then resolves `cancelled` exactly once. Pinned in Task 5.
5. **A malformed `durable:` id** (missing parts, a non-UUID session, extra colons inside `tool_use_id`): stale, never a Temporal call with a garbage workflow id. Pinned in Task 2 (ids) and Task 7 (route).

---

## File structure

TypeScript (`agent/`):
- Create `src/db/migrations/20261009T0052Z_pending_input.sql`: the two tables and `ai_audit.request_id`.
- Create `src/gate/ids.ts`: format and parse request ids.
- Create `src/gate/validate.ts`: `GateEntry`, `RespondRequest`, `validateRespond`, `RespondRefusal`, `REFUSAL_STATUS`.
- Create `src/gate/role.ts`: `roleOf`.
- Create `src/gate/names.ts`: the Query/Update/Signal names and `GATE_REFUSED` failure type prefix.
- Create `src/gate/durable.ts`: `DurableGate` (Query, Update, cancel, Signal over a Temporal `Client`).
- Create `src/gate/projection.ts`: `durableEntries(sql, filter)` over `ai_pending_input`.
- Create `src/gate/sweep.ts`: `PendingInputSweep`.
- Create `src/gate/answers.ts`: `answerResult(sql, requestId, kind)` for the answer tools' activity.
- Create `src/tools/answerTools.ts`: `ask_user`, `wait_for_user` (durable-only) and the read tools `pending_input_list`, `sessions_pending_input`.
- Modify `src/routes/pendingInput.ts`: union with the projection, `expiring`, the per-session route, `durable:` dispatch, the shared validator.
- Modify `src/questions/service.ts`: question expiry (deadline, `expireDue`, refused late answer), `input.*` events.
- Modify `src/approvals/service.ts`: `input.*` events on create and settle.
- Modify `src/sessions/protocol.ts`: `input.requested` / `input.resolved`.
- Modify `src/routes/chat.ts`: drop `input.*` on the socket.
- Modify `src/audit/log.ts`: `requestId`, approver from `ai_input_responses`.
- Modify `src/temporal/toolActivities.ts`: request id, answer tools, audit `requestId`.
- Modify `src/tools/manifest.ts`: `hitl`, the durable-only tools.
- Modify `src/tools/sessions.ts`: `sessions_approve` / `sessions_deny` take a `durable:` id.
- Modify `src/tools/coverage.ts`: `AGENT_ROUTES` for the three routes.
- Modify `src/main.ts`: question sweep, `DurableGate`, `PendingInputSweep`.
- Create `test/fixtures/pending-input-vectors.json`; tests listed per task.

Python (`agent-durable/`):
- Create `src/scadbuddy_durable/gate/__init__.py`, `ids.py`, `names.py`, `validate.py`, `entry.py`, `store.py`, `activities.py`.
- Create `tests/test_gate_vectors.py`, `tests/test_gate_entry.py`, `tests/test_gate_store.py`.

Frontend:
- Modify `frontend/src/agent/chat/protocol.ts` and `frontend/src/agent/attention.ts`: an `expiring` entry is not counted.

Docs: `CLAUDE.md` (layout lines for `agent/src/gate/` and `scadbuddy_durable/gate/`).

---

### Task 1: The tables, and `input.*` in the protocol

**Files:**
- Create: `agent/src/db/migrations/20261009T0052Z_pending_input.sql`
- Modify: `agent/src/sessions/protocol.ts`, `agent/src/routes/chat.ts`
- Test: `agent/test/pendingInput.migration.pg.test.ts`, `agent/test/chat.test.ts` (existing file, one case)

**Interfaces:**
- Produces: tables below; `ServerEvent` variants `input.requested` `{sessionId, entry: InputEntry}` and `input.resolved` `{sessionId, id, kind, outcome, reason?}`; `InputEntry` type (the §6.6 entry shape).

- [ ] **Step 1: Write the failing test** — a pg test that, after `migrate`, inserts an `ai_pending_input` row for a session, an `ai_input_responses` row, an `ai_audit` row with `request_id`, and checks the outcome and kind CHECKs refuse `outcome = 'maybe'` and `kind = 'other'`, and that deleting the session deletes both rows (FK cascade, so `forgetSubject`'s row deletion in 5c is one statement). A chat test that a logged `input.requested` is not sent on the socket while the `approval.required` beside it is.
- [ ] **Step 2: Run, expect FAIL** (`relation "ai_pending_input" does not exist`).
- [ ] **Step 3: Write the migration**

```sql
-- The tool-call gate's durable projection and outcomes (spec 2026-10-01 §6.6).
-- Written only by the agent-durable worker's open_input / resolve_input activities
-- and removed by the agent service's orphan sweep, each through one guarded
-- DELETE … RETURNING. Classic entries stay in ai_approvals / ai_questions.
CREATE TABLE ai_pending_input (
  request_id      text PRIMARY KEY,
  session_id      uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  workflow_id     text NOT NULL,
  workflow_run_id text NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('approval', 'answer')),
  tool            text NOT NULL,
  summary         text NOT NULL DEFAULT '',
  input_hash      text,
  prompt          text NOT NULL DEFAULT '',
  requested_by    jsonb,
  responders      text[] NOT NULL,
  attention       jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL,
  last_checked_at timestamptz,
  CHECK (expires_at <= created_at + interval '86400 seconds')
);
CREATE INDEX ai_pending_input_session ON ai_pending_input (session_id);
CREATE INDEX ai_pending_input_run ON ai_pending_input (workflow_id, workflow_run_id);

CREATE TABLE ai_input_responses (
  request_id  text PRIMARY KEY,
  session_id  uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (kind IN ('approval', 'answer')),
  outcome     text NOT NULL CHECK (outcome IN ('approved', 'denied', 'expired', 'answered', 'cancelled', 'timed_out')),
  response    jsonb,
  responder   jsonb NOT NULL,
  reason      text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_input_responses_session ON ai_input_responses (session_id);

-- Joins a durable approval's decision row to the tool_call row of the call it let run.
ALTER TABLE ai_audit ADD COLUMN request_id text;
CREATE INDEX ai_audit_request ON ai_audit (request_id) WHERE request_id IS NOT NULL;
```

- [ ] **Step 4: Add the protocol variants** (`protocol.ts`), and in `ChatConnection.follow` skip `e.type.startsWith('input.')` (Ruling 5).
- [ ] **Step 5: Run, expect PASS; commit** `feat(agent): ai_pending_input, ai_input_responses and the input.* events (#1056)`.

### Task 2: Request ids, the shared validator, the route's role, and the vectors (TypeScript)

**Files:**
- Create: `agent/src/gate/ids.ts`, `agent/src/gate/validate.ts`, `agent/src/gate/role.ts`, `agent/src/gate/names.ts`, `agent/test/fixtures/pending-input-vectors.json`
- Test: `agent/test/gate.vectors.test.ts`, `agent/test/gate.ids.test.ts`, `agent/test/gate.role.test.ts`

**Interfaces:**
- Produces:

```ts
// ids.ts
export type RequestId =
  | { store: 'approval' | 'question'; rowId: string }
  | { store: 'durable'; sessionId: string; runId: string; toolUseId: string }
  | { store: 'flow'; runId: string; workflowRunId: string; callId: string }
export function parseRequestId(id: string): RequestId | undefined
export function durableRequestId(sessionId: string, runId: string, toolUseId: string): string
// validate.ts
export type Role = 'browser' | 'grant' | 'owner'
export type GateEntry = {
  id: string
  kind: 'approval' | 'answer'
  state: 'pending' | 'resolving' | 'resolved'
  inputHash: string | null
  requestedBy: Owner | null
  sessionOwner: Pick<Owner, 'kind' | 'id'> | null   // at open_input
  sessionCreator: Pick<Owner, 'kind' | 'id'> | null
  questions?: { question: string; multiSelect: boolean }[]   // a question
  options?: string[]                                         // an attention request
}
export type RespondRequest = { requestId: string; response: unknown; responder: Owner; role: Role }
export const REFUSALS = ['stale', 'resolved', 'resolving', 'forbidden', 'self', 'malformed', 'too_large', 'input_mismatch'] as const
export type RefusalCode = (typeof REFUSALS)[number]
export const REFUSAL_STATUS: Record<RefusalCode, 400 | 403 | 404 | 409 | 413>
export class RespondRefusal extends Error { code: RefusalCode }
/** Throws RespondRefusal; returns the normalised answer (one string per question) for an answer. */
export function validateRespond(entry: GateEntry | undefined, request: RespondRequest): { decision?: 'approve' | 'deny'; answers?: string[] }
// role.ts
export function roleOf(principal: Owner, session: { owner: Owner; creator: Pick<Owner,'kind'|'id'> } | null, hasGrant: boolean): Role | undefined
// names.ts
export const PENDING_INPUT_QUERY = 'pending_input', RESPOND_UPDATE = 'respond', CANCEL_INPUT_UPDATE = 'cancel_input', INTERRUPT_SIGNAL = 'interrupt', GATE_REFUSED = 'GateRefused'
```

- Order of checks in `validateRespond` (both languages): stale (no entry, or id differs) → resolved → resolving → too_large (`JSON.stringify(response)` UTF-8 bytes > `RESPONSE_MAX`) → role allowed for the kind (`forbidden`) → self (approval only: responder equals `requestedBy`, `sessionOwner` or `sessionCreator`, unless role `browser`) → shape (`malformed`): approval `{kind:'approval', decision, input_hash?}`, question `{kind:'answer', answers}` keyed exactly by the questions, a list only for a multi-select with no `, ` inside a pick, each answer 1..ANSWER_MAX after joining; attention exactly one of `choice` (in `options`) or `text` → `input_mismatch` when `input_hash` is given and differs.
- `RESPONSE_MAX` and `ANSWER_MAX` move from `routes/pendingInput.ts` / `harness/questions.ts` imports into `validate.ts` re-exports, so the route keeps importing them.

- [ ] **Step 1: Write the vectors file.** Top level: `{"response_max": <RESPONSE_MAX>, "answer_max": 20000, "names": {...the four names, "refused_type": "GateRefused"}, "vectors": [...]}`. Each vector: `{"name", "entry": <GateEntry in snake_case or null>, "request": {"request_id", "response", "responder", "role"}, "expect": {"ok": true, "answers"?: [...] } | {"refused": "<code>"}, "durable_only"?: true}`. Vectors, at least: stale (no entry); stale (other id); resolved approval; resolved answer; resolving (`durable_only`); approval approved by browser in another principal's session (ok); approval by grant (ok); approval by owner role (forbidden); self, `kind: approval`, grant holder that requested the call (self); owner (browser) answers their own session's question (ok); answer by grant (forbidden); approval with an answer body (malformed); question missing a key (malformed); multi-select list (ok, joined with `, `); single-select given a list (malformed); attention choice outside options (malformed); attention both choice and text (malformed); over `response_max` (too_large); input_hash mismatch (input_mismatch); input_hash match (ok).
- [ ] **Step 2: Write the failing tests.** `gate.vectors.test.ts` loads the file, asserts `response_max === RESPONSE_MAX`, `answer_max === ANSWER_MAX` and the names, and runs every vector through `validateRespond`; a `durable_only` vector is skipped *by name with `it.skip`* so it shows as skipped, never silently passes (§8). `gate.ids.test.ts`: round trip, a `tool_use_id` containing `:` kept whole, a non-UUID session refused, unknown prefix refused, empty parts refused. `gate.role.test.ts`: browser → `browser` whoever owns; non-browser owner with grant → `owner`; creator with grant → `owner`; other with grant → `grant`; other without grant → undefined.
- [ ] **Step 3: Run, expect FAIL** (modules missing).
- [ ] **Step 4: Implement** `ids.ts`, `validate.ts`, `role.ts`, `names.ts`.
- [ ] **Step 5: Run, expect PASS; commit** `feat(agent): the gate's request ids, shared validator and role, with vectors (#1056)`.

### Task 3: The same rules in Python

**Files:**
- Create: `agent-durable/src/scadbuddy_durable/gate/__init__.py`, `ids.py`, `names.py`, `validate.py`, `entry.py`
- Test: `agent-durable/tests/test_gate_vectors.py`, `agent-durable/tests/test_gate_entry.py`

**Interfaces:**
- Produces: `durable_request_id(session_id: str, run_id: str, tool_use_id: str) -> str`; `parse_durable_request_id(s) -> tuple[str, str, str] | None`; `GateEntry` (frozen dataclass, the TypeScript fields in snake_case, `state: Literal["pending","resolving","resolved"]`); `RespondRequest`; `RespondRefused(Exception)` with `.code`; `validate_respond(entry: GateEntry | None, request: RespondRequest) -> ValidAnswer` (deterministic: no I/O, no clock); `RESPONSE_MAX`, `ANSWER_MAX`, `PROMPT_MAX = 16 * 1024`; `build_entry(*, request_id, kind, tool, tool_input, summary, input_hash, requested_by, session_owner, session_creator, secrets) -> GateEntry` and `entry_prompt(kind, tool_input, secrets) -> str` raising `PromptTooLargeError`; `refused_failure_type(code) -> str` = `"GateRefused:" + code`.
- `entry_prompt` for `ask_user`: the questions joined by newlines (as the classic read does), each string with the secrets replaced by `[redacted]` (`agent/src/secrets.ts` `redact`'s marker); for `wait_for_user`: the message. Over 16 KiB UTF-8 → `PromptTooLargeError`.

- [ ] **Step 1: Write the failing tests** — run every vector (none skipped: the durable side runs `durable_only` too), assert the file's `response_max`, `answer_max` and names equal the module's; entry tests for the redaction, the 16 KiB refusal, and that an approval entry's prompt is empty and carries no input.
- [ ] **Step 2: Run** `cd agent-durable && uv run --frozen pytest tests/test_gate_vectors.py tests/test_gate_entry.py` — expect FAIL.
- [ ] **Step 3: Implement**, mirroring Task 2's check order exactly.
- [ ] **Step 4: Run, expect PASS** (plus `ruff check`, `ruff format --check`, `mypy`); commit `feat(agent-durable): the gate's validator and entries, on the shared vectors (#1056)`.

### Task 4: `open_input` and `resolve_input`

**Files:**
- Create: `agent-durable/src/scadbuddy_durable/gate/store.py`, `gate/activities.py`
- Test: `agent-durable/tests/test_gate_store.py` (`requires_postgres`, the `agent_db` fixture)

**Interfaces:**
- Consumes: Task 1's tables; Task 3's `GateEntry`.
- Produces:

```python
@dataclass(frozen=True)
class OpenInput:
    entry: GateEntry; session_id: str; workflow_id: str; workflow_run_id: str
    tool: str; tool_use_id: str; summary: str; prompt: str; expires_at: datetime
    attention: dict[str, Any] | None; questions: list[dict[str, Any]] | None  # the card's questions
@dataclass(frozen=True)
class ResolveInput:
    request_id: str; outcome: Literal["approved","denied","expired","answered","cancelled","timed_out"]
    response: Any | None; responder: dict[str, str]; reason: str | None
async def open_input(conn, args: OpenInput) -> None
async def resolve_input(conn, args: ResolveInput) -> bool   # False: someone else resolved it
class GateActivities:  # @activity.defn(name="open_input"), (name="resolve_input")
RESOLVE_ON_RESPOND: dict  # schedule_to_close_timeout=30 s
RESOLVE_ON_TIMER: RetryPolicy  # maximum_interval=300 s, no maximum_attempts
```

- `open_input`, one transaction per step: (a) delete this session's rows whose `workflow_run_id` differs (a Reset), each with an `ai_input_responses` row `cancelled`, reason `reset`, and its `input.resolved`; (b) `INSERT … ON CONFLICT (request_id) DO NOTHING` the row; when it inserted, append to `ai_session_events` (bumping `ai_sessions.event_seq` exactly as `eventLog.ts` `append` does): `approval.required` (approval) or `question.asked` (answer, with the card's questions and the attention view), then `input.requested` with the entry, then `session.status` `waiting_approval` / `waiting_input` when the status changed; and `pg_notify('scadbuddy_events', …)` with the `session.waiting` payload `busEvents.ts` sends (replica `agent-durable`).
- `resolve_input`, one transaction: `DELETE FROM ai_pending_input WHERE request_id = %s RETURNING *`; none → return False and write nothing; else insert `ai_input_responses`, append `approval.resolved` (`decision` = outcome, `by` for approve/deny, `reason` for expired/cancelled) or `question.resolved`, then `input.resolved`, then `session.status` back to `running` when no other entry of the session is parked; for an approval, insert the `ai_audit` row `kind='approval'`, `action` = outcome, `request_id`, `approval_id` NULL, actor the responder (or the system actor `{system, scadbuddy, ScadBuddy}` for `system:*` responders), outcome `ok`/`denied`/`refused` as `auditOutcome` maps them, `surface` `http` for a person and `system` otherwise; `pg_notify` after.

- [ ] **Step 1: Write the failing tests**: open inserts once (retried open adds no row and no second event); open after a Reset cancels the old run's row with `reason = 'reset'`, leaving exactly one row; resolve writes the outcome, the events and (approval) one audit row with `request_id`; resolve twice concurrently (`asyncio.gather` on two connections) → exactly one True, one `input.resolved`, one outcome; resolve of an answer stores the response; status returns to `running`; a 17 KiB prompt never reaches `open_input` (Task 3 refuses it).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** with `psycopg` (`AsyncConnectionPool` is not a dependency; `GateActivities` takes a `Callable[[], AsyncContextManager[AsyncConnection]]`, and 5c gives it its pool).
- [ ] **Step 4: Run, expect PASS; commit** `feat(agent-durable): open_input and resolve_input, the gate's only writers (#1056)`.

### Task 5: Classic question expiry

**Files:**
- Modify: `agent/src/questions/service.ts`, `agent/src/main.ts`
- Test: `agent/test/questions.pg.test.ts` (new cases)

**Interfaces:**
- Produces: `SETTING_QUESTION_EXPIRY_SECONDS = 'question_expiry_seconds'`, `DEFAULT_QUESTION_EXPIRY_SECONDS = 3600`, `MIN_… = 10`, `MAX_… = 86_400`; `QuestionService.expirySeconds()`; `QuestionService.expireDue(): Promise<number>`; `QuestionServiceDeps.settings?: SettingsReader`; `QuestionService.startSweeper(intervalMs, options)`.

- [ ] **Step 1: Write the failing tests**: a question row gets `expires_at = now() + question_expiry_seconds`; a parked `AskUserQuestion` whose deadline passes returns `{answered: false, message: 'The user did not answer: nobody answered in time…'}` and its row is `cancelled`, never answered; `expireDue()` cancels an expired orphan row exactly once (two concurrent calls → one `question.resolved`); an answer to an expired-but-pending question is refused (`conflict`, reason "nobody answered in time", Ruling 6); `expirySeconds` clamps 5 → 10 and 100 000 → 86 400; a `done` summary and attention requests are untouched by `expireDue`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**: the gate's question insert sets `expires_at`, the wait gets that deadline, and on `'due'` the row is cancelled through the same guarded UPDATE as `cancelPending` (`questionId`), reason `nobody answered in time`; `expireDue` selects `kind = 'question' AND outcome IS NULL AND expires_at <= now()` and cancels each; `answer()`'s UPDATE adds `AND (kind <> 'question' OR expires_at IS NULL OR expires_at > now())`, and a lost update whose row is a due question reports that reason. `main.ts` runs `expireDue` beside the approval sweep, every 30 s.
- [ ] **Step 4: Run, expect PASS; commit** `feat(agent): questions expire at question_expiry_seconds, cancelled never answered (#1056)`.

### Task 6: `input.*` in classic sessions

**Files:**
- Modify: `agent/src/approvals/service.ts` (`create`, `settle`), `agent/src/questions/service.ts` (ask insert, `answer`, `cancelPending`, `reconnected`, `timeOut`, the supersede, Task 5's expiry)
- Test: `agent/test/approvals.pg.test.ts`, `agent/test/questions.pg.test.ts` (new cases)

- [ ] **Step 1: Write the failing tests**: one parked classic approval logs exactly one `input.requested` (entry id `approval:<id>`, no input) after its `approval.required`, and its decision one `input.resolved` (outcome `approved`); a question likewise (`question:<id>`, outcome `answered` / `cancelled` / `timed_out`; `reconnected` maps to outcome `cancelled` with the reconnect reason, since `ai_input_responses`' vocabulary has no `reconnected`). Session-less MCP approvals log nothing (no session log), as today.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** with one helper per store that builds the entry from the row (`classicApprovalEntry`, `classicQuestionEntry`, shared with Task 7's read) and pushes the event into the same transaction's `tail`.
- [ ] **Step 4: Run, expect PASS; commit** `feat(agent): classic gates log input.requested and input.resolved (#1056)`.

### Task 7: The reads and `respond` over both modes

**Files:**
- Create: `agent/src/gate/projection.ts`, `agent/src/gate/durable.ts`
- Modify: `agent/src/routes/pendingInput.ts`, `agent/src/tools/sessions.ts`, `agent/src/app.ts` deps (`gate?: DurableGate`), `agent/src/main.ts`, `frontend/src/agent/chat/protocol.ts`, `frontend/src/agent/attention.ts`
- Test: `agent/test/respond.pg.test.ts` (new cases), `agent/test/gate.durable.temporal.test.ts`, `agent/test/support/gateWorkflows.ts` (a stand-in session workflow), `frontend/src/agent/attention.test.ts`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces:

```ts
// projection.ts
export function durableEntries(sql: Sql, filter: { sessionId?: string; ownerOf?: Owner }): Promise<PendingInputEntry[]>  // expiring set past expires_at
// durable.ts
export class DurableUnavailable extends Error {}
export class DurableGate {
  constructor(client: Client, options?: { timeoutMs?: number })   // default 5 s; cancel 10 s
  pendingInput(sessionId: string): Promise<PendingInputEntry[]>
  respond(sessionId: string, args: { request_id: string; response: unknown; responder: Owner; role: Role }): Promise<{ outcome: string }>
  cancelInput(sessionId: string, reason: string): Promise<'cancelled' | 'none'>
  interrupt(sessionId: string, reason: string): Promise<void>   // a Signal: recorded with no worker
}
// pendingInput.ts
export type PendingInputEntry = { …existing…; expiring?: true }
export async function pendingInput(sessions, gate, principal?, filter?): Promise<PendingInputPage>
```

- `respond` dispatch: `approval:` / `question:` as now, but the entry is first read into a `GateEntry` and checked with `validateRespond` (role `browser` for the route), whose refusals map through `REFUSAL_STATUS`; then the existing store write (`approvals.decide`, `questions.answer`). `durable:` → session must exist and be `mode = 'durable'`, else stale; role from `roleOf` (route: always `browser`); `gate.respond`; a `WorkflowUpdateFailedError` whose cause is an `ApplicationFailure` of type `GateRefused:<code>` maps to its status; a not-found workflow → stale; no answer within the timeout → 503 `{retry: true}`. `flow:` → stale (Ruling 2).
- `GET /api/v1/ai/sessions/:id/pending-input`: classic → the union filtered to that session; durable → `gate.pendingInput` (503 when unavailable, Ruling 11); unknown session → 404.
- `sessions_approve` / `sessions_deny` with a `durable:` id: `roleOf` from `ai_sessions` and `grants`, then `gate.respond` with `{kind:'approval', decision, input_hash?}`.
- Frontend: `PendingInputSchema` entries gain `expiring: z.boolean().optional()`; `read()` skips an `expiring` entry.

- [ ] **Step 1: Write the failing tests**: the aggregate lists a durable row beside a classic approval, question and a session-less MCP approval, oldest first, and a row past `expires_at` with `expiring: true`; the frontend counts skip it; a durable entry carries no input (`summary` and `input_hash` only); the per-session route for a classic session; `respond` with `flow:x`, `durable:not-a-uuid:…`, an unknown session, or a classic session's id → 404 stale with no Temporal call; the validator now refuses a classic over-cap `input_hash` mismatch etc. with the same statuses as before (existing tests stay green). Temporal (`describe.skipIf(!TEMPORAL_CLI)`): against the stand-in workflow (`pending_input` Query, `respond` Update with a validator that throws `ApplicationFailure.nonRetryable(msg, 'GateRefused:resolved')`, `cancel_input` Update, `interrupt` Signal): `pendingInput` returns its entries; `respond` returns the outcome; a refusal maps to 409; a workflow with no worker → `DurableUnavailable` within the timeout, and `interrupt` still lands in history (`handle.fetchHistory()` has the Signal); `sessions_approve` by a grant holder that owns the session → refused `owner`, no Update sent.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run agent and frontend checks, expect PASS; commit** `feat(agent): pending-input reads and respond over classic and durable sessions (#1056)`.

### Task 8: The answer tools, `hitl` in the manifest, and the approved call's audit

**Files:**
- Create: `agent/src/tools/answerTools.ts`, `agent/src/gate/answers.ts`
- Modify: `agent/src/tools/manifest.ts`, `agent/src/temporal/toolActivities.ts`, `agent/src/audit/log.ts`, `agent/src/main.ts`
- Test: `agent/test/toolManifest.test.ts` (existing or new), `agent/test/gate.answers.pg.test.ts`, `agent/test/temporal.worker.test.ts` (new case), `agent/test/audit.pg.test.ts` or the existing audit test file (new case)

**Interfaces:**
- Produces: `DURABLE_ONLY_TOOLS: Tool[]` (`ask_user`: the `AskUserQuestion` input, `QuestionSchema` list 1..4; `wait_for_user`: `AttentionInputSchema` with `on_timeout` narrowed to `proceed`); `ToolManifestEntry.hitl?: 'approval' | 'answer'`; `answerResult(sql, requestId): Promise<CallToolResult>` — `answered` → the answers as `answersText` / `answeredText`; `timed_out` → `timedOutText`; `cancelled` or no row → an error result ("nobody answered: <reason>"), never an answer; `AuditEntry.requestId?: string`.
- The answer tools' handlers outside an activity refuse ("only a durable session's workflow asks through this tool"). In `runAsActivity`, a tool in `DURABLE_ONLY_TOOLS` returns `answerResult(sql, durableRequestId(session, runId, toolUseId))` instead of running a handler; every call's audit row gets `requestId`, and `AuditLog.record` copies `approved_by_*` from `ai_input_responses` (`outcome = 'approved'`, `responder`) when `requestId` is a `durable:` id.

- [ ] **Step 1: Write the failing tests**: the manifest marks every outward tool `hitl: 'approval'`, `ask_user` / `wait_for_user` `hitl: 'answer'`, others none; `wait_for_user`'s schema enum is `["proceed"]`; `/mcp`'s list does not contain either answer tool; `answerResult` for each outcome and for a missing row; the activity (Temporal) for `ask_user` returns the stored answers; a `tool_call` audit row with a `durable:` request id whose response row is approved names the approver, and one whose row is denied names nobody.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS; commit** `feat(agent): durable answer tools, hitl in the manifest, approver by request_id (#1056)`.

### Task 9: The orphan sweep

**Files:**
- Create: `agent/src/gate/sweep.ts`
- Modify: `agent/src/main.ts`
- Test: `agent/test/gate.sweep.pg.test.ts` (Postgres, a fake describer), `agent/test/gate.durable.temporal.test.ts` (one case against a terminated stand-in)

**Interfaces:**
- Produces:

```ts
export type RunState = 'open' | 'closed'   // not found counts as closed
export type DescribeRun = (workflowId: string, runId: string) => Promise<RunState>
export class PendingInputSweep {
  constructor(deps: { sql: Sql; describe: DescribeRun; events: EventLog; audit?: AuditSink; minAgeS?: number /*600*/; recheckS?: number /*600*/ })
  sweep(): Promise<number>          // rows removed
  start(intervalMs: number, options): () => void
}
export function temporalDescriber(client: Client): DescribeRun
```

- One tick: select distinct `(workflow_id, workflow_run_id)` of rows `created_at < now() - minAge` and (`last_checked_at IS NULL` or `< now() - recheck`); describe each once (an error is skipped, not treated as closed); open → `UPDATE … SET last_checked_at = now()`; closed → per row the guarded `DELETE … RETURNING`, then the outcome `cancelled` (Ruling 8), `approval.resolved` / `question.resolved` and `input.resolved`, the status refresh, and for an approval the audit row, in one transaction.

- [ ] **Step 1: Write the failing tests**: a row whose run is closed is removed with exactly one `input.resolved` and an outcome row; a row whose run is open is kept and stamped, and not described again within `recheckS` (describer call count); a young row is not described; a describer that throws leaves the row; a row a concurrent `resolve` deleted first is not written twice (the guarded delete). Temporal: a terminated stand-in's row leaves within one sweep.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement; wire into `main.ts`** beside the approval sweep (same 30 s), with the lazy client `main.ts` already builds.
- [ ] **Step 4: Run, expect PASS; commit** `feat(agent): sweep ai_pending_input rows whose workflow run has ended (#1056)`.

### Task 10: The read tools and the agent-route coverage check

**Files:**
- Modify: `agent/src/tools/answerTools.ts` (the two read tools), `agent/src/tools/index.ts`, `agent/src/tools/coverage.ts`
- Test: `agent/test/coverage.test.ts` (new describe), `agent/test/respond.pg.test.ts` (tool cases)

**Interfaces:**
- Produces: tools `pending_input_list` (risk `read`) and `sessions_pending_input` (`{session_id}`, risk `read`), both with `routes: []`; `AGENT_ROUTES: readonly { route: string; tool?: string; reason?: string }[]` naming `GET /api/v1/ai/pending-input` → `pending_input_list`, `GET /api/v1/ai/sessions/{id}/pending-input` → `sessions_pending_input`, and `POST /api/v1/ai/pending-input/{request_id}` with the reason "browser-only for `answer` kinds; `approval` through `sessions_approve` / `sessions_deny`".

- [ ] **Step 1: Write the failing tests**: every `AGENT_ROUTES` entry names a tool in `ALL_TOOLS` or has a reason over 20 characters; the three routes are registered on the app (a request to each is not 404 from Hono's not-found); the tools return the principal's visible entries (Ruling 10): a grant holder sees every approval, a non-grant MCP principal only its own prepares and its sessions', answers only for sessions it owns.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS; commit** `feat(agent): read tools for pending input, and the agent-route coverage check (#1056)`.

### Task 11: Docs, full checks, PR

- [ ] Add `agent/src/gate/` and `scadbuddy_durable/gate/` to `CLAUDE.md`'s layout.
- [ ] Run `cd agent && pnpm lint && pnpm typecheck && pnpm test && pnpm build` with `SCADBUDDY_TEST_DATABASE_URL` and `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER`; `cd agent-durable && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest`; frontend `pnpm lint && pnpm typecheck && pnpm test && pnpm build`.
- [ ] Commit `docs: the gate's layout (#1056)`; open the PR (`feat(agent): …`, `Part of #1056`, the rulings).
