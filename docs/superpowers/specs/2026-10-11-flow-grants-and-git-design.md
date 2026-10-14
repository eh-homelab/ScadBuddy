# Flow run grants, delegated approvals, and flow scripts in git

Amends `2026-10-01-durable-printing-agents-flows-design.md` (the "10-01 spec") §6.6 and
§7.2–7.4. Issue #2480 (epic #2486). Implemented by #2481 (grants), #2482 (escalation)
and #2503 (git); the plan is `docs/superpowers/plans/2026-10-11-flow-grants-and-git.md`.

Where this document and the 10-01 spec disagree, this one wins for flows and for the
durable sessions a flow drives. Everything else in the 10-01 spec stands: the gate's
entry shape, `respond`, the validators, `resolve_input`, the timers that never approve,
the input-hash binding (#2318), and the rule that no principal decides its own call.

## 1. Why

Today (origin/main, 2026-10-11):

- A flow's own outward calls (`queue_print`, `arrange`, an outward `tool(...)`) park on
  the run, and only the browser user decides them on the Workflows page
  (`backend/scadbuddy/api/flows.py` `_browser_only`, `decide_flow_run`).
- An `agent(...)` or `ask_session(...)` session parks its outward calls in its own chat
  (`agent-durable/.../session/workflow.py` `_park`), answered through
  `POST /api/v1/ai/pending-input/{id}`.
- So a twelve-plate run asks for twelve clicks spread over a run page and several chats,
  and the session that started the run has no part in any of it.
- A run acts as its starter (`agent/src/temporal/flowRuns.ts` `startedBy`, plan 6
  Ruling 6) with the starter's current tiers (`toolActivities.ts` `flowCaller`,
  `currentTiers`, #2320). Letting that session approve its own run's calls would be
  self-approval: it acts as the same principal with the same tiers.
- A flow's script lives in `workflow_definitions.script`, one immutable row per version,
  with no history, diff or restore, while every template is versioned in git
  (`backend/scadbuddy/library/history.py`).

This amendment adds three things:

1. **Run grants.** A person approves a bounded grant once, at run start. Calls inside it
   run without asking, and each records the grant and the call hash it matched.
2. **Delegated approvals.** A call outside the grant goes up a chain: child session,
   then the flow, then the session that started the run, then the person. A parent may
   deny, narrow or pass up. It never approves, and it never widens the grant. What
   reaches the person lands in one queue per run.
3. **Flow scripts in git**, in their own repository `data/flows`, on a git core shared
   with `ModelHistory`. `workflow_definitions` becomes an index, and each run records
   the commit it ran.

## 2. Terms

- **Run grant** (`flow_grants`): what a person approved for one run. It is not the
  per-token *approval grant* (`ai_mcp_tokens.approval_grant`, `approvals/service.ts`
  `grants(principal)`), which lets a token decide *other* agents' approvals. This
  document never says "grant" alone for the token flag.
- **Flow-own call**: a gated host call of `ProjectWorkflow`: `queue_print`, `arrange`,
  or `tool(name, args)` whose tool is outward (`flow_tools.py` `_gated`). Its request id
  is `flow:<run id>:<workflow run id>:<call id>`.
- **Child session**: a durable session the run drives. `agent(...)` starts one;
  `ask_session(...)` sends a turn to one the starter owns (`flowSessions.ts`). A
  **flow turn** is a turn the run sent; a child's calls are covered by the run only
  during a flow turn. Its request id is `durable:<session>:<workflow run id>:<tool_use_id>`.
- **Starter**: `workflow_runs.started_by`. **Starter session**: `started_by.session`,
  when the run was started from a session.
- **Hop**: where an undecided call currently waits: `parent` (the starter session) or
  `person`. The flow itself is a hop with no wait (§4.3).

## 3. Run grants

### 3.1 Shape

A grant is a JSON document, validated by one Pydantic model (`flows/grants.py`
`GrantSpec`) and never interpreted anywhere else:

```json
{
  "expires_at": "2026-10-12T18:00:00Z",
  "sessions": true,
  "ask_starter_session": true,
  "rules": [
    {"fn": "queue_print",
     "printers": [3, 4], "models": ["coaster"], "library_files": [],
     "max_prints": 12, "max_filament_g": 800, "max_calls": 12},
    {"fn": "arrange", "models": ["coaster"], "max_calls": 4},
    {"fn": "tool", "tool": "send_to_bambuddy",
     "args": [{"path": "/printer_id", "in": [3, 4]}], "max_calls": 2}
  ]
}
```

- `rules` lists what may run without asking. A call matches when one rule's `fn` (and,
  for `tool`, `tool`) equals the call's and every bound the rule sets admits the call.
  A bound the rule omits is not checked. A rule with no bounds is allowed, and the start
  dialog says so in red ("any printer, any number of prints").
- **The bound vocabulary is closed.** No regular expressions, no expressions, no script
  predicates:
  - `printers`: printer ids. `queue_print`'s `request.printer_id`; an omitted one is the
    configured printer, resolved when the call is checked.
  - `models`: template slugs. For `queue_print` with `{"output_id"}`, the output's slug;
    for `arrange`, every object's source slug.
  - `library_files`: Bambuddy library file ids, for `queue_print` with `{"file_id"}`.
    A file source matches a rule only through this list, never through `models`.
  - `max_prints`: the rule's total of print items: `copies` (the remembered quantity
    when omitted, as the route resolves it) times plates (1, or the output's plate count
    with `all_plates`).
  - `max_filament_g`: the rule's total of filament grams. A print whose grams are not
    known when it is checked (`used_grams` unknown, `bambuddy/filaments.py`: ScadBuddy
    uploads unsliced plates) does not match a rule that sets this bound.
  - `max_calls`: the rule's number of matched calls.
  - `args` (for `tool` only): `{path, in}` pairs, an RFC 6901 pointer into the call's
    arguments and the values it may hold. A missing path does not match.
- `expires_at`: required. At most 7 days after approval; the dialog's default is 24 h.
  After it, no call matches.
- `sessions`: whether the grant extends to child sessions' flow turns (§3.4). Off unless
  set; the start dialog shows it unchecked.
- `ask_starter_session`: whether calls outside the grant go to the starter session
  before the person (§4.4). Ignored when there is no starter session.
- The grant never names a read or write tool: those run at the starter's tiers without
  asking, as today. A rule for a tool that is not outward is refused when the grant is
  created.

### 3.2 Who creates and approves a grant

**A person approves every grant, in the browser.** No agent creates, approves or
widens one.

- **Started from the Workflows page** (browser, no `X-ScadBuddy-Agent-Author`): the start
  dialog asks for the grant. `POST /workflows/{id}/runs` takes `grant` and inserts the
  row `approved` (approved by `browser`) before the workflow starts. A run started with
  no grant has none: every outward call goes to the person, as today.
- **Started by an agent** (any request with the author header, from a session or a
  token): the body may carry `grant`, which is inserted as `requested`. It authorises
  nothing. It is an entry in the run's queue (§4.5) that the person approves as is,
  approves edited (the person may change any bound: the request was only a
  proposal), or declines. Until then, every
  outward call takes the escalation chain. The run is not held: reads and writes go on.
- `POST /workflow-runs/{id}/grant/approve` and `.../decline` are browser-only, through
  the same `_browser_only()` guard as `decide` (403 `flow-entry-browser-only`). They get
  no agent tool, as `answer` and `decide` have none (plan 6 Ruling 6). An agent in the
  browser user's own panel session sends the author header too, so it is refused like
  any other agent.
- **One grant per run, immutable once approved, and revocation is final.** Nothing edits
  an approved grant in place, so a grant's matches always describe one approved
  document. After a revoke (or a decline) the run has no grant for the rest of its life:
  its outward calls take the escalation chain. A person who wants different bounds
  starts a new run.

### 3.3 Matching and recording

There is **one evaluator**, `backend/scadbuddy/flows/grants.py` `match_grant`, in
Python. It is served as the activity `flow_grant_check` on the `projects` queue (the
projects worker already holds the database URL, the flows payload codec and the KEK for
`approval_hash`), and called in-process by the backend's routes. Nothing else matches
grants: the TypeScript agent only reads whether a recorded match is still in force
(§3.5).

`flow_grant_check(run_id, request_id, fn, tool?, args, session_id?, flow_call_id?)`:

1. Reads the run's grant. No grant, a grant not `approved`, revoked or past
   `expires_at`: **outside**.
2. A child call: outside unless `sessions` is true and the run's step `flow_call_id`
   is an `agent` or `ask_session` step that recorded `session_id` (`note_session`):
   the call is in a flow turn of that run (§3.4).
3. Resolves what the bounds need (the configured printer, the output's slug and plate
   count, known grams) through the backend API at `api_internal_url`, as the host
   functions do: the `projects` worker mounts no data volume. A lookup that fails is
   **outside**: fail toward asking.
4. Finds the first rule that admits the call, intersected with the script's narrowing
   for that child (§4.3).
5. **Consumes** the rule's counters and records the match in one transaction:
   `INSERT INTO flow_grant_uses (request_id, …) ON CONFLICT (request_id) DO NOTHING`,
   then `UPDATE flow_grants SET used = …` guarded by `used + n <= max`. A request id
   already recorded returns its first answer, so a retried activity consumes once.
   Counters are never given back: a call that later fails still spent its share.
6. Returns `{verdict: "inside", grant_id, rule, input_hash, args}` or `{verdict:
   "outside", reason}`. **`args` are the call's arguments with every default the bounds
   resolved pinned in**: for `queue_print`, `request.printer_id` and `request.copies` as
   checked. The call runs with these `args`, and `input_hash` covers them, so a change
   of the configured printer or the remembered quantity between the check and the send
   cannot change what runs. Agent tools' `args` bounds read only what the call sent
   (a missing path does not match), so nothing is resolved for them. `input_hash` is `approval_hash.input_hash` of the call as the agent
   will run it (`{"tool", "input"}` for a `tool(...)` or child call; for `queue_print`
   and `arrange`, the host function's name and its arguments).

An inside verdict is recorded where a person's approval of the same call is recorded
today, so every existing check of "approved with this hash" applies unchanged:

- **Flow-own call**: a `workflow_run_decisions` row, `outcome = 'approved'`,
  `responder = 'grant:<grant id>'`, `input_hash` as above. The workflow then approves its
  own parked call with the harness's public `approve_tool` Update from an activity
  (`flow_grant_approve`, update id `grant-<call id>`), as `flow_entry_timeout` denies one
  today (`flow_entries.py`). No person's decision is faked: the responder says it was the
  grant.
- **Child call**: `resolve_input` with outcome `approved` and responder
  `{kind: "run_grant", id: <grant id>}`, written with its `ai_audit` approval row
  (`input_hash` from `describe_call`, as today), before `agent.decide(…, True, …)`.
  No entry is opened, so nothing is listed and nobody is notified.
- `flow_grant_uses(request_id primary key, grant_id, run_id, fn, tool, input_hash,
  rule_index, prints, filament_g, session_id, created_at)` is the grant's record of what
  it let through, shown on the run page.

### 3.4 Child sessions and flow turns

- A child's call is covered only during a **flow turn**: a turn the run itself sent with
  `sessions_start` or `sessions_send`. A later turn the person sends in the same
  session, or a turn of a session the flow merely asked once, is not covered.
- The agent service marks a flow turn from the activity's own context, never from the
  script's arguments: when `sessions_start` or `sessions_send` runs as an activity of a
  workflow `flow-<run id>` (`flowOf`, `flowRuns.ts`), with activity id `tool-<call id>`,
  `DurableTurns.send` puts `flow: {run_id, call_id}` on the `send_message` Message.
  `DurableSession` keeps it for that turn only and passes it with `session_id` to
  `flow_grant_check`. The backend checks it against the run's own `steps`, so a Message
  naming another run, another step or another session matches nothing.
- The script's narrowing and `on_outside` for that child (§4.3) are read from the
  run's record of the step (`steps[].policy`), never from the Message.
- The activity crosses queues: `DurableSession` runs `flow_grant_check` on `projects`.
  Its arguments are encoded under the session's payload key, which the projects worker
  can open: it runs the subject codec already (10-01 §6.5, "Who runs it").

### 3.5 Revocation, expiry and the check at run time

- `POST /workflow-runs/{id}/grant/revoke` sets `revoked_at` and `revoked_by`. It is open
  to the browser user and to the starter (by author header): revoking only removes
  authority, so an agent may stop its own run's grant. It gets a `write`-tier tool,
  `revoke_flow_grant`. The run is not stopped.
- **Revoking pauses the run at its next outward call**: from then on nothing matches,
  so the next outward call takes the escalation chain and waits.
- **The check at run time.** A call approved by a grant runs only if the grant is still
  in force when its activity runs:
  - `agent/src/temporal/flowRuns.ts` `approved()` and `PgApprovalRecords.approved()`
    already require the approved row with the call's hash. When its responder is a run
    grant, they also require `flow_grants.revoked_at IS NULL AND now() < expires_at`.
  - A call caught between its match and its run (milliseconds: the same workflow task
    sequence) is refused `GrantRevoked`, which the script or the child model reads as
    the tool's failure. It is not re-parked: the Update that let it through has already
    been sent.
  - `queue_print` and `arrange` run in the workflow, not on `agent-tools`. Their match
    is checked in the same workflow task sequence that sends them, so a revocation
    after the match is too late for that one call, as a person's approval followed by a
    change of mind is today. The next call is outside.
- Expiry behaves as revocation from `expires_at` on.

## 4. Delegated approvals: the escalation chain

### 4.1 The chain

A call outside the grant does not go straight to the person:

| Call | Hop 1 | Hop 2 | Hop 3 |
|---|---|---|---|
| Child session's call (flow turn) | the flow (§4.3), no wait | the starter session (§4.4), if any and `ask_starter_session` | the person (§4.5) |
| Flow-own call | the starter session, if any and `ask_starter_session` | the person | |
| Child session's call outside a flow turn | the person, in that session's chat, as today | | |

- **Parents never approve.** At every hop but the person's, the only actions are:
  - **deny**: the call does not run; the reason reaches the caller where the plugin
    allows (§4.2);
  - **narrow**: replace the call's input with a *smaller* input for the same call
    (`flows/narrowing.py`, a closed vocabulary like the grant's bounds: a print may drop
    copies it named or all plates, an arrange objects or copies of one; every other
    field stays as it was, so a different printer or source is a substitution and is
    refused; an agent tool's call is never narrowed). Only a parent whose owner holds
    `outward` now may narrow. The narrowed call is matched by `flow_grant_check` like any
    call: inside, it runs as approved by the grant (responder `run_grant`, with
    `narrowed_by` recorded); outside, it goes up with the narrowed input as the
    proposal, written once per call with its input hash. The person sees it beside the
    call (`GET /workflow-runs/{id}/proposals/{call}`, browser only) and approves it by
    that hash, so a proposal replaced after it was shown is refused (409);
  - **pass up**: the call moves to the next hop unchanged.
- **Never wider.** Every way a call runs without the person is a grant match, and the
  grant is the person's. A parent can make a call smaller or stop it; it can never make
  the grant admit anything it did not admit.
- **No self-approval.** The starter session, its child sessions and the run all act as
  the starter. None of them holds role `browser`, and the `owner`/self rules of 10-01
  §6.6 still refuse any approval by the starter's principal. The new roles below can
  deny and narrow-into-the-grant only.
- **The person may decide at any hop.** An entry is in the run's queue from the moment it
  is parked (marked "with the starting session" while it waits at hop `parent`), so the
  person can approve or deny it before the parent acts.

### 4.2 Roles and the validator

10-01 §6.6 splits authorization: the route decides who the caller is (`role`), the
Update's validator checks only what the workflow holds. This keeps that split and adds
two roles (`agent/src/gate/role.ts`, `gate/validate.ts`, and their Python port
`agent-durable/.../gate/validate.py`, with the shared vectors):

| Role | Who | `approval` entries may | `answer` entries may |
|---|---|---|---|
| `browser` | the browser user | approve, deny, narrow | answer |
| `grant` | a token with the approval grant, not owner or creator | approve, deny, **only outside a run's queue** | nothing |
| `owner` | the session's own non-browser principal | nothing | nothing |
| `parent` (new) | the starter session, at hop `parent` | deny, narrow | nothing |
| `run_grant` (new) | `flow_grant_check`'s inside verdict | approve | nothing |

- `parent` is decided by the route from the escalation's record (§4.6): the caller's
  `ctx.session` is the run's starter session and the entry is at hop `parent`. The
  same principal in any other session is `owner`, as before.
- **The author header is not authentication.** On the backend, the starter session and
  the starter (for `parent` actions on a flow-own entry, and for revoke) are read from
  `X-ScadBuddy-Agent-Author` / `-Session`, which `core/authorship.py` documents as a
  label, not a credential. Only the browser and the agent sidecar reach the backend,
  and the browser is already the person. So a forged header can at most deny a call,
  narrow it into the grant, pass it up, or revoke the grant: each only removes
  authority. No header ever approves, creates or widens anything; those need the absence
  of the header (`_browser_only`). `core/authorship.py`'s docstring gains this use and
  this bound (plan B3).
- `run_grant` is never a route's caller. It is the responder `DurableSession` passes
  to `resolve_input` itself when `flow_grant_check` answered inside; no Update carries
  it, and `respond`'s validator refuses it.
- **Entries in a run's queue are the person's.** A token holding the approval grant
  decides other agents' calls today. In a run's queue it does not: the epic's rule is
  that delegation comes from a run grant a person approved, and a token's flag is not
  one. The route refuses role `grant` for an entry whose session is a child in a flow
  turn, and for every `flow:` entry (already browser-only).
- **Who matches a narrowed input.** A `browser` narrow is the person approving the
  narrowed input; nothing is matched. A `parent` narrow is matched by
  `flow_grant_check` where the entry lives: for a child, inside `DurableSession`'s
  `respond` handler (an activity, before `resolve_input`); for a flow-own call, by the
  backend route in-process. Inside: resolved `approved`, responder `run_grant`, with
  `narrowed_by` the parent session. Outside: the entry stays parked, moves to hop
  `person`, and carries the narrowed input as its `proposal`.
- **Narrowing a durable child's call.** The plugin's `decide` carries only yes or no, and
  runs the input it parked. So a narrowed approval is carried the way an `answer` is
  (10-01 §6.6, "How each kind resolves"): `resolve_input` writes the narrowed input to
  `ai_input_responses.response`, and the `agent-tools` activity, finding a narrowed
  approval for its request id, runs that input instead of the plugin's, after checking
  that its hash is the approval's. The tool's result begins with one line saying the
  call ran narrowed and by whom. A narrowed flow-own `tool(...)` works the same way
  through `workflow_run_decisions.response`; a narrowed `queue_print` or `arrange` is
  read by the approved body through a `flow_decision` activity, behind
  `workflow.patched("flow-narrowed-decision")` (`ProjectWorkflow` is unversioned, plan 6
  Ruling 13).
- **A parent's deny reason.** A flow-own call's caller is the script, which reads the
  reason (`ToolApprovalDenied: <reason>`, the harness's `approve_tool(reason=…)`). A
  durable child's model reads the plugin's fixed rejection ("A human reviewer rejected
  this action. Do not retry it.", 10-01 §9's known difference); the reason is recorded
  in `ai_input_responses` and shown in the child's chat, not sent to its model.

### 4.3 The flow as a parent

A flow is a script with no model, so it decides nothing at run time. Its part is
declared in the script, per child:

```python
agent("Slice and check plate 3", skills=["print"], result_schema=None,
      grant={"rules": [{"fn": "tool", "tool": "send_to_bambuddy", "max_calls": 1}]},
      on_outside="escalate")
```

- `grant` **narrows** the run grant for that child: a child's call must be admitted by
  the run grant *and* by this narrowing (an intersection, so no subset check is needed
  and a script cannot widen anything). It uses the same shape without `expires_at`,
  `sessions` and `ask_starter_session`. Passing `None` means no narrowing: the run
  grant as it is.
- `on_outside` is `"escalate"` (pass up) or `"deny"` (the call is refused at once, never
  parked, and recorded as denied by the flow).
- `ask_session(session_id, message, images, grant, on_outside)` takes the same two.
- Both are **optional** (§8, Ruling 9, as amended by #2636): `grant` defaults to `None`
  (no narrowing) and `on_outside` to `"escalate"`. The harness's Code Mode stubs keep a
  parameter's default since temporal-agent-harness c0f148f9 (#171), so a script may
  leave them out. Every script registered before them, which calls `agent` or
  `ask_session` with the old arity, keeps type-checking and runs as if it had passed
  the defaults: no script breaks.
- The step records the narrowing (`steps[].policy`), which `flow_grant_check` reads.

### 4.4 The starter session as a parent

When the run was started from a session (`started_by.session`) and the grant's
`ask_starter_session` is true (the default for an agent-started run; the person can turn
it off when approving the grant), an outside call goes there first.

- **Delivery.** The agent's `EscalationService` (`agent/src/flows/escalations.ts`)
  sends the starter session a turn, authored `system:escalation`, listing every entry of
  that session's runs at hop `parent` that was not yet delivered: run, step, tool, the
  scrubbed summary, why it is outside the grant, and for a child's call the child's
  reasoning (§4.5). There is no proposal to send: at hop `parent` nothing has narrowed
  the call yet, since the starter session is the only parent that narrows (§4.1). A run's
  own call has no scrubbed summary in its projection; its step and function stand in.
  It sends only to an idle session. A session in a turn gets the batch on a later
  tick of the agent's 30 s sweep, once the turn has ended. It goes through
  `SessionManager.send` (classic or durable), as the session's owner: the turn is the
  owner's, and its text opens with ScadBuddy's own instruction, the calls' details
  after it in the untrusted-data envelope (#2482; no separate `system:escalation`
  author). The turn runs with the tiers the owner holds now: the browser user's all of
  them, a token's its live tier (as the backend's `parent_tiers` reads it), anyone else
  `read`, which cannot call `escalation_respond` (`write`), so such a session is
  skipped. A child's entry records its delivery in `ai_pending_input.delivered_at`, a
  run's own call in `ai_escalation_deliveries`, each claimed before the turn is sent and
  released if it did not start.
- **The answer** is one tool, `escalation_respond(request_id, action, input?, reason?)`,
  `write` tier, with `action` one of `deny`, `narrow`, `pass_up`. It dispatches on the
  id's prefix as the pending-input route does: `durable:` to the agent's
  `flows/escalations.ts` (`pass_up` moves the row in Postgres; `deny` is the child's
  `respond` Update with role `parent`; `narrow` is refused, a tool call is never
  narrowed); `flow:` to the backend's `POST /workflow-runs/{id}/escalations/{call_id}`,
  sent with the session's author header, which the backend authorises against
  `started_by.session` and the entry's hop. **There is no HTTP route of its own**
  (#2482): the caller is the session its turn runs in (the tool context's session, set
  by the session manager or the session's workflow), which no HTTP request can show,
  and on `/mcp` the tool refuses for want of one. A child's entry records its parent
  (`ai_pending_input.parent_session`, `parent_principal`) from `flow_grant_check`'s
  verdict, which reads the run's `started_by`; the tool requires the caller's session to
  be it, still owned (not archived, not handed off) by that principal, and the caller to
  be its owner.
- **Cost.** Each delivery is a turn of the starter session, paid from its budget
  (`budget_root_id`, #823). A session out of budget, archived, deleted, or whose owner
  changed by handoff is skipped: a child's entries go to the person at once; a run's own
  call waits out its hop (its run's timer is the only writer of `waiting_on`).
- **A child that is the starter session.** A script may `ask_session` the very session
  that started the run. Its outside calls would escalate to itself in the middle of its
  own turn, and it receives escalations only when idle, so they would wait out the whole
  parent hop. So the parent hop is skipped when the child is the starter session: its
  outside calls go to the person on the delivery's next tick, and it is never sent them
  (#2482).
- **The escalation turn's text is untrusted.** The summary, the proposal, the outside
  reason and above all the child's reasoning are model or tool output. The turn wraps
  them in the untrusted-data envelope every tool result uses
  (`agent/src/safety/untrusted.ts`), with the instruction that they are information
  about a call to judge, never instructions, as §4.5 shows them to the person. The
  starter session's own tiers still bound whatever it does next.
- **One hop only.** The chain does not climb past the starter session to whatever
  started *it*. A run started by a child session of another run has that child as its
  starter session, and the other run is not consulted.

### 4.5 The person: one queue per run

Whatever reaches the person waits in **one queue per run**: the run's own parked calls,
its child sessions' parked calls from flow turns, and a requested grant.

- **Where it shows.** On the run's page (the Workflows page, and the Flows tab of #2350),
  and in the starter's assistant panel: the panel of the starter session, or for a
  browser-started run, the badge. The badge counts every run-queue entry at hop
  `person` (10-01 §6.6's "Flow entries stay out of the assistant's reads" is replaced:
  they are now in the aggregate read, grouped by run).
  - As built (#2482): the panel's `SessionFlowRuns` reads each open run's queue and lists
    its entries under the run, each with where it waits and why, linking to the run's
    page, where they are decided. Each aggregate-read entry of a run carries
    `run: {id, hop, reason}`, and the badge skips one at hop `parent`. The run's own
    calls are added to the aggregate read only by its HTTP route, the browser user's:
    the `pending_input_list` tool and every other principal's read never list them.
- **One read.** `GET /api/v1/ai/flow-runs/{run_id}/queue`, in the agent service, is the
  union of:
  - the run's own entries, from `workflow_runs.waiting_on` (which `ProjectWorkflow`
    projects, and the agent already reads `workflow_runs`);
  - the child entries, from `ai_pending_input` where `flow_run_id` is the run;
  - the requested grant, from `flow_grants`.

  `GET /api/v1/ai/pending-input` includes the same entries for the badge. Each entry
  keeps its own id and is answered where it is answered today: a `flow:` entry by
  `POST /workflow-runs/{id}/decide` (now with an optional narrowed `args`), a `durable:`
  entry by `POST /api/v1/ai/pending-input/{id}`, a grant request by
  `POST /workflow-runs/{id}/grant/approve`.
- **Context beside each entry**, all of it scrubbed as entry summaries are, and none of
  it the call's raw input (10-01 §6.6):
  - the run, its flow's name and the commit it runs (§6), with a link to the script at
    that commit;
  - the step (`steps[].seq`, `fn`), and for a child the step that drives it (the run's
    latest running step whose `session_id` is the child's);
  - for a child's call, **the child's reasoning**: the text the child's model wrote in
    that turn before the call (`assistant.text.delta` events since the turn's start),
    the last 2048 characters (`REASONING_MAX`), shown as untrusted text, as every tool
    result is. It is read from
    `ai_session_events` when the queue is read (`flows/queue.ts` `childReasoning`): the
    deltas after the turn's `user.turn` and before the call's `tool.call`, so nothing
    in a workflow records it;
  - the hop it came through, and the parent's action if it narrowed it ("narrowed by the
    starting session from 6 copies to 2");
  - why it is outside the grant (`flow_grant_check`'s `reason`: "printer 5 is not in the
    grant", "max_prints reached: 12 of 12").

### 4.6 Hops, timeouts and records

- **Hops are Postgres, not workflow state.** A workflow parks a call and keeps its timer,
  as in 10-01 §6.6. It does not know hops. The hop of each entry is recorded beside its
  projection, by the service that owns it:
  - a flow-own entry: `workflow_runs.waiting_on[]` gains `hop`, `hop_until`,
    `proposal` (narrowed args) and `reason`, written by the backend;
  - a child entry: `ai_pending_input` gains `flow_run_id`, `hop`, `hop_until`,
    `parent_session`, `parent_principal`, `outside_reason`, `hops` and `delivered_at`,
    written by `open_input` and the agent's `flows/escalations.ts` (no `proposal`: a
    child's call is never narrowed).
  So routes authorise on it (§4.2), and nothing in a workflow changes when an entry moves.
- **Timeouts follow the run's approval timeout at each hop** (`approval_timeout_s`, the
  run's resolved value; 10-01 §7.3 and `workflow_runs.approval_timeout_s`):
  - At hop `parent`, when `hop_until` passes, the entry **passes up** to the person. A
    parent that does not answer never denies anything: fail toward asking.
  - At hop `person`, the existing timer **denies** (`flow_entry_timeout` for a flow-own
    call; the durable gate's timer for a child, 10-01 §6.6 "Timeouts").
  - The workflow's timer therefore covers both hops: a call parked with a parent hop
    gets a timer of the parent hop's length plus the run's timeout, so the person's
    deadline is `hop_until` plus the run's timeout (later, if the parent passes up
    early). The sweep may show the move up to one tick (30 s) late, but the person can
    decide at any hop (§4.1), so no decision time is lost. For `ProjectWorkflow` this
    changes `run_gated`'s timeout, behind `workflow.patched("flow-escalation-timer")`.
  - A child's entries in a flow turn use the run's approval timeout, not the session's
    `approval_expiry_seconds`. `flow_grant_check`'s outside verdict carries it.
  - **A run timeout of 0 ("never")** still bounds the parent hop: it then lasts
    `approval_expiry_seconds` (default 600 s), so a starter session that never answers
    cannot hold the person's queue back. With a run timeout of 0 **no timer ever
    denies the call**, at either hop, as today.
- **Each owner moves its own entries.** The agent's existing 30 s sweep
  (`APPROVAL_SWEEP_MS`, `gate/sweep.ts`) moves due child entries to `person`. A
  flow-own entry is moved by its own run: `ProjectWorkflow` waits out the parent hop
  with a timer and then runs `flow_hop_due`, which moves the entry in Postgres
  (`FlowStore.move_hop`, once, unless the parent passed it up first) and denies
  nothing; the person's timer, when the run has one, starts from there (#2482). The
  backend's flow sweep runs with the daily housekeeping sweeps, too seldom for a hop of
  minutes. Both write the move as the `system` actor.
- **Every hop action is recorded on the entry**: a `hops` array of `{hop, action, actor,
  at, reason?}` in the entry's projection (`waiting_on[].hops`,
  `ai_pending_input.hops`). A flow-own narrowing's input is kept beside it, in
  `workflow_run_proposals`, so no read of the run returns it raw; the entry says only
  that it is `proposed`. When the entry resolves, its `hops` are copied into the
  resolution record (`workflow_run_decisions.hops`, `ai_input_responses.hops`), beside
  the decision recorded where it is today (with `ai_audit` for a child). So a flow-own
  entry's and a child's history read alike on the run page, and no service writes the
  other's tables.

## 5. Identity and tiers

- **Whose tiers.** Every call of the run, its children's included, runs with the
  starter's tiers *as they are when the call runs*, as today (`flowCaller`,
  `turnTiers`, #2320). A grant removes the approval step; it never adds a tier. A grant
  rule for a tool the starter cannot call at `outward` is refused when the grant is
  created, and the run-time check refuses it anyway.
- **A tier lost mid-run.** When a token-started run's token is lowered or revoked, its
  next call that needs the lost tier is refused at run time (`liveTokenTiers`), inside
  the grant or not. It is **refused, not parked**: a person's approval cannot give a
  token a tier it does not hold. The script (or child model) reads the failure, and the
  run page shows "the starter's token no longer holds `outward`". The grant is not
  revoked; restoring the tier lets the next call match again.
- **A token-started run asks a person** through its queue: its requested grant and every
  outside call are entries the person sees on the run's page and in the badge
  (§4.5), with the existing notifications (10-01 §6.6, "Notifications") triggered when an
  entry reaches hop `person`. A token-started run with no starter session has no
  parent hop.
- **A browser-started run** with a grant behaves the same, without a parent hop. Its
  starter is the browser user, so its tiers are all tiers, and the grant is all that
  stands between the script and an unattended outward call. That is why every rule's
  bounds are shown in the dialog and an unbounded rule is marked.
- **Handoffs.** A child session handed off to another principal leaves the run: its
  later turns are not flow turns, and its parked entries are cancelled as 10-01 §6.6
  says for a handoff. A starter session handed off stops being a parent (§4.4).

## 6. Flow scripts in git (amends 10-01 §7.3 and §7.4)

### 6.1 The repository

- **`data/flows` is a git repository of its own.** Never inside `data/models`, never a
  `ModelHistory`: flows are not models. Layout: `<name>/flow.py`, one directory per flow
  name. No metadata file: the approval timeout is state, kept in Postgres (§6.2).
  A flow's inputs schema is source, `<name>/inputs.json` in the same commit (§10).
- **A shared git core.** `backend/scadbuddy/core/gitrepo.py` `GitRepo` holds the
  generic plumbing now in `library/history.py`: the hermetic `git_env()`
  (`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` at `/dev/null`), identity from the
  environment, `safe.directory` as command-line config, the thread-and-`flock` write
  lock with its deadline, one commit per action with the author trailers from
  `core/authorship.py` (`current_author()`: person, agent principal, session), `log`,
  `show`, diff between two commits, restore as a new commit, `resolve`, `head`,
  `ensure_repo`, `maintain`. `ModelHistory` keeps its model-specific behaviour
  (`.gitignore` body, built-in seeding, symlink-safe tree reads, export, merge) and
  calls the core. Its tests stay green unchanged.
- **`FlowHistory`** (`backend/scadbuddy/flows/repository.py`; `flows/history.py` is
  already the Reset preview) is a `GitRepo` over `data/flows`, with: `write(name,
  script) -> commit`, `script_at(name, commit)`, `log(name)`, `diff(name, base, head)`,
  `restore(name, commit) -> commit`. Its lock file is in `data/flows`, separate from the
  models repository's, so a flow commit never waits on a template commit.
- **Deployment.** `data/flows` is on the data volume beside `data/models`. The
  `projects` worker still receives the script as the run's input (`RunFlow.script`), so
  no worker mounts the volume.

### 6.2 Postgres keeps state, not source

- `workflow_definitions` becomes an index. It keeps **one row per version**, so every
  existing run's `definition_id` and every version id an agent holds keep working:
  `(id, name, version, commit, approval_timeout_s, created_by, created_at)`. `commit`
  is new and required for new rows; `script` becomes nullable and is no longer written
  or read once the backfill (§6.4) has run, and a later migration drops it.
- **Every version is its own commit.** Each registration, restore and backfilled row
  makes exactly one commit, with `--allow-empty` when the script is unchanged (a
  re-registration only to change `approval_timeout_s` is a version today,
  `flows/store.py` `COALESCE(MAX(version), 0) + 1`, and stays one). The commit carries
  two trailers, `ScadBuddy-Flow: <name>` and `ScadBuddy-Flow-Version: <definition id>`;
  the definition id is chosen before the commit.
- The **version number** shown to people is the commit's position among the commits
  with that name's `ScadBuddy-Flow` trailer, oldest first. **The row's version is always
  that position, never `MAX(version) + 1`**: `FlowHistory` computes it, and nothing else
  numbers a version. So the history routes, the index and the reconciler can never give
  one commit two numbers. For migrated rows it equals the existing numbering, identical
  consecutive scripts included (§6.4).
- `workflow_runs` gains `commit`, the commit its script was read from, set from
  `FlowStart.commit` by the run's first activity. Reset replays the run's own history,
  which carries the script itself, so a Reset always reruns that commit's script; the
  column is what the run page links to and what the queue shows (§4.5).
- **Registering** (`register_flow` or the editor's Save, #2487) type-checks, then, **under
  the flows repository's write lock held across both steps**, commits `<name>/flow.py`
  (subject `Register <name> v<N>`; its author recorded as `ModelHistory` records one) and
  inserts the index row with that commit and its position as the version. The lock is
  the thread lock plus `flock` (`GitRepo.write_lock`), so two registrations of one name,
  in one process or two, cannot interleave their commits and inserts. Restore and the
  reconciler take the same lock.
- **A failed insert.** If the insert fails after the commit, the commit stays (git is the
  source) and the request fails. The commit still holds its position: the next
  registration commits at the position after it and inserts that number, so no later
  row takes the orphan's number. The reconciler (at start, and before each registration
  under the same lock) indexes any commit whose `ScadBuddy-Flow-Version` trailer no row
  names, with that id and its own position, so it never collides on
  `UNIQUE (name, version)`. It matches on the trailer, never on the script's content.
- **Starting a run** reads the script from git at the version's commit, not from the row.
  A missing commit (a damaged volume) is 409 "this version's script is missing".

### 6.3 Routes

For a flow name (names match `^[a-z0-9][a-z0-9-]{0,62}$`, and a definition id is a
UUID, which that pattern also matches, so name routes take their own prefix):

- `GET /api/v1/workflows/names/{name}/versions`: the history, newest first, with author
  and date, from git.
- `GET /api/v1/workflows/names/{name}/versions/{commit}/source`: the script at a commit.
- `GET /api/v1/workflows/names/{name}/versions/{commit}/diff?base=`: a unified diff.
- `POST /api/v1/workflows/names/{name}/versions/{commit}/restore`: a new commit with
  that script, and a new version row. Type-checked first, as a registration is.

They mirror the model routes (`api/versions.py`) but are their own. Each gets an agent
tool per the coverage rule (`list_flow_versions`, `get_flow_version_source`,
`diff_flow_version`: `read`; `restore_flow_version`: `write`).

### 6.4 Migration

- A one-time backfill at backend start (`flows/backfill.py`), under the flows
  repository's lock: every `workflow_definitions` row with no `commit`, in
  `(name, version)` order (not `created_at`: it is the transaction's start, taken before
  the advisory lock in `flows/store.py`, so two concurrent registrations can have it in
  the opposite order to their versions), is written to `<name>/flow.py` and committed with
  `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` set to `created_at` and trailers from
  `created_by`, one commit per row (`--allow-empty` for a script equal to the previous
  version's), carrying the row's id in `ScadBuddy-Flow-Version`; then the row's `commit`
  is set. It is idempotent: a row with a commit is skipped, and a crash between commit
  and update is repaired by finding the commit whose trailer names the row.
- Each existing run maps to its version row's commit:
  `UPDATE workflow_runs r SET commit = d.commit FROM workflow_definitions d WHERE
  r.definition_id = d.id AND r.commit IS NULL`, in the same backfill.
- The SQL side is new migration files only: add `workflow_definitions.commit`, make
  `script` nullable, add `workflow_runs.commit`. Dropping `script` is a later file once a
  release has run the backfill.

## 7. What changes in the 10-01 spec, section by section

- **§6.6, table row "Outward tool call"**: in a flow turn, also role `run_grant`
  (automatic, by the run's grant) and `parent` (deny or narrow at hop `parent`); a token
  with the approval grant may not decide a run-queue entry. Timer: the run's approval
  timeout per hop (§4.6).
- **§6.6, "Who answers a flow's entries"**: unchanged for the person (browser only).
  Added: the starter session may deny, narrow or pass up at hop `parent`; a run grant
  approves inside its bounds.
- **§6.6, "Flow entries stay out of the assistant's reads"**: replaced by §4.5. Entries at
  hop `person` are in `GET /api/v1/ai/pending-input` and the badge, grouped by run.
- **§6.6, `ai_pending_input`**: gains `flow_run_id`, `hop`, `hop_until`, `proposal`,
  `outside_reason` and `hops`; `ai_input_responses` gains `hops`.
- **§6.6, "Responses"**: roles `parent` and `run_grant` (§4.2); a narrowed approval
  carries its input in `ai_input_responses.response`.
- **§7.1**: `agent(...)` and `ask_session(...)` take `grant` and `on_outside` (§4.3).
- **§7.2**: an outward host call first runs `flow_grant_check`; inside, it is approved by
  the grant (§3.3); outside, it parks and escalates (§4).
- **§7.3**: tables as in §6.2, plus `flow_grants`, `flow_grant_uses`, and `hops` on
  `workflow_run_decisions`; the start route takes `grant`; new grant, escalation and version
  routes; the Workflows page shows the grant, its uses and the run's one queue.
- **§7.4**: a Reset replays the run's recorded commit. Grant uses are not undone by a
  Reset (as no side effect is): a replayed call past the reset point is a new request id
  (its workflow run id changed) and is matched, and consumes, again. The reset preview
  says so for each outward call it lists.

## 8. Rulings

Decided here where #2480 left it open. Those marked **(user)** change what a person
sees or what is safe, and are listed in the PR for the user.

1. **One evaluator, in Python, on the `projects` queue** (§3.3). Durable child sessions
   call it across queues; TypeScript only reads whether a match is in force. No second
   matcher and no shared match vectors.
2. **Grants are backend tables** (`flow_grants`, `flow_grant_uses`), next to
   `workflow_runs`. The agent reads them, as it already reads `workflow_runs` and
   `workflow_run_decisions`, and writes none of them. Hop state and hop history live on
   each entry's own projection, written by the service that owns the entry (§4.6).
3. **The bound vocabulary is closed** (§3.1): printers, models, library files, prints,
   filament grams, calls, and pointer-`in` constraints for agent tools.
4. **(user) Unknown grams do not match a filament bound** (§3.1). Until prints are
   sliced before they are queued, a grant with `max_filament_g` will send most prints to
   the person.
5. **(user) An agent-started run runs before its grant is approved** (§3.2): outward calls
   wait in the queue meanwhile. The alternative, holding the whole run, was rejected
   because reads and renders would wait for nothing.
6. **(user) One grant per run, immutable once approved, revocation final; revoke is open
   to the starter** (§3.2, §3.5).
7. **(user) Grant expiry at most 7 days, default 24 h** (§3.1).
8. **(user) A token with the approval grant may not decide run-queue entries** (§4.2).
9. **`agent`/`ask_session` gain optional `grant` (default `None`, no narrowing) and
   `on_outside` (default `"escalate"`) parameters, and no script breaks** (§4.3).
   Amended by #2636: this ruling first made both required, because the harness rendered
   no defaults; the harness pin past c0f148f9 keeps them.
10. **"Narrow" means "replace with a smaller input of the same call that the grant
    admits"** (§4.1; amended by #2482's security review, which found that "any input the
    grant admits" let a parent substitute a call and have a person approve the
    substitute as a narrowing). Smaller is checked against the parked call in a closed
    vocabulary, and a tool call is never narrowed.
11. **(user) A parent hop that times out passes up; with a run timeout of 0 the parent hop
    still ends after `approval_expiry_seconds`** (§4.6).
12. **Hops are Postgres state, outside the workflows** (§4.6), so no workflow changes
    when an entry moves. What does change `ProjectWorkflow` and `DurableSession` (the
    grant check, narrowing and the escalation timer) goes behind `workflow.patched`.
13. **(user) Delivering an escalation costs the starter session a turn** (§4.4); the person
    can turn the parent hop off when approving the grant.
14. **Counters are never given back** (§3.3), and a Reset's replayed calls consume again
    (§7).
15. **Every version is its own commit, its number is its git position (never
    `MAX + 1`), the commit and the index insert share one lock, and one row per version
    stays** in `workflow_definitions` (§6.2), rather than one row per name, so existing
    ids and runs keep working.
16. **No metadata file in `data/flows`** (§6.1).
17. **`FlowHistory` lives in `flows/repository.py`**, since `flows/history.py` is the Reset
    preview.

## 9. Testing

The plan's tasks carry the tests; these are what the spec requires to be shown:

- A call inside the grant runs with no entry, and its `workflow_run_decisions` or
  `ai_input_responses` row names `grant:<id>` with the call's hash. The same call with
  one argument changed after matching is refused at run time (#2318's test, extended).
- `max_prints`, `max_calls` and `max_filament_g` stop matching at their limit under
  concurrent calls (two parallel `queue_print`s for the last print: one matches).
- Revoking the grant: the next outward call parks; a call approved before the revoke but
  run after it is refused `GrantRevoked`.
- A script's `grant=` cannot widen: a child call admitted by the narrowing but not by
  the run grant is outside.
- A child's call in a turn the person sent is outside, though the session is the run's.
- The starter session can deny, narrow and pass up, and cannot approve: an
  `escalation_respond` with any other action, or one from another session of the same
  principal, is refused; `respond` with role `run_grant` from a route is refused.
- A narrowed child call runs with the narrowed input and its result says so; a narrowed
  input outside the grant reaches the person as a proposal.
- Hop timeouts: a parent that never answers passes the entry up after the run's timeout;
  the person's timer denies.
- A token-started run with a requested grant: nothing matches until the person approves;
  the approve route refuses any request with the author header.
- A token lowered mid-run: the next outward call inside the grant is refused, not
  parked.
- Git: registering commits once with the author trailers; history, diff, source and
  restore; re-registering an unchanged script is a new commit (`--allow-empty`) and a
  new version; the backfill reproduces every existing version, consecutive identical ones
  included, in order with its dates and
  maps every run; `ModelHistory`'s test suite passes unchanged on the shared core.

## 10. Amendment: typed inputs (#2483)

Amends §6.1 and Ruling 16, and 10-01 §7.3–7.4 ("the script is the run's input").

- **Declared in git.** A flow may declare typed inputs as a JSON Schema,
  `<name>/inputs.json` beside `<name>/flow.py`, written in the version's one commit
  (`FlowHistory.register(..., inputs=)`). A version without one has no file; a restore
  brings back the file as it was at that commit. Ruling 16 still holds for state: the
  schema is source, versioned with the script, while the approval timeout stays in
  Postgres.
- **A closed subset** (`flows/inputs.py`): the top level is `type: object` with
  `properties`, `required` and at most `additionalProperties: false` (extra inputs are
  always refused); each input is a `string`, `integer`, `number`, `boolean` or an
  `array` of one of those, with `title`, `description`, `default` and its type's own
  constraints, and a `string` or `integer` may be an `enum`. So each input has one
  Python type and one form control. Names are `^[a-z][a-z0-9_]{0,63}$`, never a Python
  keyword or a pydantic model attribute.
- **Checked at registration** (and restore, and before each run): the schema against
  the subset, each default against its input, then the script's Code Mode type check
  with `inputs()` added to the host functions. `inputs()` returns `FlowInputs`, a
  TypedDict rendered from the schema: an input that is required or has a default is
  always present (a boolean without a default is false, as an unticked box sends it);
  any other is `T | None`. A pattern is at most 256 characters, since every start runs
  it. A flow without a schema has no `inputs()`.
  `ScriptProblem.file` says whether a problem is in `flow.py` or `inputs.json`.
- **Checked when a run starts**: `POST /workflows/{id}/runs` takes `inputs`, fills in
  the defaults, validates them (422 `flow-inputs` with `problems`) and coerces them to
  the script's types. A flow without a schema refuses any input.
- **Recorded.** The values and the schema travel in the run's `execute` message
  (`RunFlow.inputs`, `RunFlow.inputs_schema`), so they are sealed by the flow payload
  codec with the rest of the run's history, and a Reset replays the run with its
  original inputs and its own commit's types. The run's first activity copies the
  values to `workflow_runs.inputs`, which the run page shows; nothing reads that copy
  back into a run.
- **Grants do not reference inputs.** §3.1's bound vocabulary is closed (no
  expressions); a bound that named an input would be one. A grant's bounds stay
  literal values a person approves.

## 11. Out of scope

- Grants for scheduled or event-started runs: #2484 builds on these grants.
- Editing an approved grant in place, or widening one without a new approval.
- A parent chain longer than one session (§4.4).
- The flow editor (#2487) and the Flows tab (#2350) beyond showing the grant, its uses,
  the queue and the history this document defines.
- Pushing `data/flows` off the box (the same seam as `ModelHistory`'s, not built).
