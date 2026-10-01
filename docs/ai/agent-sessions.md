# Agent sessions over MCP (agent-to-agent control)

Issue [#300](https://github.com/eh-homelab/ScadBuddy/issues/300); spec §6
("Agent-to-agent"), §7 and §8.2. Another agent can start, watch, steer, fork,
interrupt and hand off ScadBuddy's agent sessions over `/mcp`, and decide other agents'
approvals when its token holds the grant. The tools are
[`agent/src/tools/sessions.ts`](../../agent/src/tools/sessions.ts), on top of the
session manager ([`agent/src/sessions/manager.ts`](../../agent/src/sessions/manager.ts),
#377). They are registry tools, so they are on both projections: `/mcp`, and in-process
for a session's own model (spec §5.1).

## 1. The tools

The spec writes `sessions.list`, `sessions.start` and so on. The registry's names are
also the harness's, and the Messages API allows only `^[a-zA-Z0-9_-]{1,64}$` in a tool
name ([tool use](https://docs.claude.com/en/docs/agents-and-tools/tool-use/implement-tool-use)),
so the dot is an underscore.

| Tool | Tier | What it does |
|---|---|---|
| `sessions_list` | `read` | Sessions the caller may see, newest first, those offered to it flagged `offered_to_you`; filter by `status`, `origin` |
| `sessions_start` | `write` | A new session owned by the caller, with an optional first `prompt`, `title`, `tags` and `scope` (`model`, `output`, `job`) |
| `sessions_send` | `write` | A user turn in a session the caller owns; refused while a turn runs |
| `sessions_get` | `read` | Status, owner, pending approvals, and the transcript after `after_seq` (streamed text joined per message), paged by `next_seq` |
| `sessions_resources` | `read` | What the session's tool calls touched, oldest first (§4.1); `GET /api/v1/ai/sessions/:id/resources` answers the same to the UI |
| `sessions_attach` | `read` | Waits up to `wait_seconds` (≤ 300) for events after `after_seq` and returns them once they pause; a progress notification per event |
| `sessions_fork` | `write` | A copy of a session the caller may see, owned by the caller, with the conversation so far; counts against the new-session limit, as a start does |
| `sessions_interrupt` | `write` | Stops the running turn on whichever replica runs it |
| `sessions_handoff` | `write` | Gives a session the caller owns to `"browser"` (the user in the UI) at once, or **offers** it to `"token:<id>"` or `"oidc:<issuer>#<sub>"` (§2.1); with `to` the caller's own id, accepts an offer made to it |
| `sessions_accept_handoff` | `write` | Accepts a session offered to the caller: it becomes the owner |
| `sessions_cancel_handoff` | `write` | Ends a pending offer: the owner withdraws it, the target declines it |
| `sessions_list_approvals` | `read` | Pending approvals of one session, or every pending one for a grant holder, with each `input_hash` |
| `sessions_approve`, `sessions_deny` | `outward` | Decide another agent's approval; needs the grant (§3) |

`sessions_start` and `sessions_send` take `wait_seconds` (≤ 600, default 0): with 0 they
answer once the turn has started; otherwise they wait for it, reporting progress every
5 s, and say how it ended (`turn.finished`, `turn.result`). Either way they return
`after_seq`, where the turn's events begin, for `sessions_get` or `sessions_attach`.

Tier choices (spec §8.1): starting, sending, forking, interrupting and handing off
(offering, accepting, withdrawing or declining) change only ScadBuddy's own session state, so they are `write`. Deciding an approval is
what lets an outward action run, so it is `outward`; like `confirm_action`, these two
are the approval path and are not gated again.

## 2. Who sees and controls what

Every call acts as its caller (`ownerOf()` in
[`agent/src/approvals/mcp.ts`](../../agent/src/approvals/mcp.ts), re-read on every
request), and the manager's rules apply unchanged (spec §6):

- A caller sees the sessions it owns or started, and those offered to it (§2.1). The
  browser user sees all of them. Any other session answers "no session", so its
  existence is not revealed.
- Only the owner sends. Two sends cannot both claim a turn: a send while one runs gets
  `a turn is already running` (the claim in `send()`).
- Anyone who may see a session may interrupt it (spec §8.6).
- Only the owner hands off, and to another MCP principal only as an offer that
  principal accepts (§2.1); the browser user may also take any session over (the panel
  and `POST /api/v1/ai/sessions/:id/handoff`). A change of owner cancels pending
  approvals. An offer is made whether or not `to` names a live token, so the answer
  never tells a caller which token ids are live; one nobody can accept lapses at its
  `until` (an hour), and the owner can withdraw it with `sessions_cancel_handoff`
  (PR #715 review).
- A turn a token sends runs its in-process tools with **that token's tiers**
  (`SendOptions.tiers` in `manager.ts`, `turnPrincipal()` in
  [`agent/src/tools/harness.ts`](../../agent/src/tools/harness.ts)). Without them a
  non-browser owner's session would be `read` only (`harnessPrincipal()`). Outward calls
  still park for a human approval in the UI. The approval records the turn's tiers
  (`ai_approvals.requested_tiers`), so if it is approved after a restart the turn that
  resumes it (`resumeApproved()` in `manager.ts`) is offered the approved tool again:
  only while the requester still owns the session, and cut down to what a bearer token
  holds then (`liveTokenTiers()` in `auth/tokens.ts`; a revoked token leaves `read`).
- **Inside a session** the same tools run as the session's owner, but a model may not
  decide approvals, or hand a session off, accept or decline one (`notInHarness()` in
  `sessions.ts`): those are the owner's decisions, and a model running as the browser
  user would otherwise approve its own outward calls.
- **Ids.** A caller is shown only its own principal id (§2.2).

### 2.1 Handoff to another agent is an offer

Before the PR #715 review, `sessions_handoff` wrote whatever principal `to` named as the
owner, unchecked, and the only rule was that the caller owned the session. A creator
keeps seeing a session it handed on, and saw the new owner's id in it, so agent A could
delegate one session to agent B, read B's id, and then make B the owner, and the sole
sender, of any other session A chose, prompt injection included, without B asking
([review](https://github.com/eh-homelab/ScadBuddy/pull/715#issuecomment-5896053771);
spec §6 "Handoff": ownership moves "explicitly"). Now (`handoff()` in `manager.ts`):

- **To `"browser"`** it moves at once. The human may take any session over anyway, and
  sees every one.
- **To an MCP principal** it is an **offer**: `pending_owner_*` on `ai_sessions`
  (`agent/src/db/migrations/20260929T1825Z_session_handoff_offers.sql`). The owner keeps
  the session. Only the named principal can accept it, with `sessions_accept_handoff` or
  a `sessions_handoff` to itself, the way `confirm_action` completes only for the
  principal that prepared the call (`claim()` in
  [`agent/src/approvals/mcp.ts`](../../agent/src/approvals/mcp.ts)). The accept is one
  `UPDATE` conditional on the owner and the offer both being unchanged.
- **The target sees it** in `sessions_list` (`offered_to_you: true`, `offer.until`) and
  may read it with `sessions_get` before deciding. Reading is all an offer lets anyone
  push at another agent, and the transcript arrives in the untrusted-data envelope like
  any other.
- **It ends** when the owner withdraws it or the target declines it
  (`sessions_cancel_handoff`), after `HANDOFF_OFFER_TTL_MS` (one hour; an expired offer
  reads as none), or when the owner changes by any route, the browser's take-over
  included. One offer at a time: a new one replaces it.
- **Announced** as `session.owner` on the bus (§4) when it is made, withdrawn or
  declined, as when it is accepted. An offer is state on the session row, not a
  transcript event (the panel's protocol has none), so it is announced without an
  append (`EventLog.announce()`).

### 2.2 Principal ids

A principal id is what a handoff addresses, so an MCP caller is shown only its own.
Every other principal in what the tools return (a session's `owner`, an offer's `to`, a
transcript event's `owner`, `author` or `by`, an approval's `requested_by` and
`decided_by`) is shown by `kind` and a label that does not name it: `another MCP
token`, `another MCP OIDC principal` or `an anonymous MCP client`, since the stored
labels carry the id (`MCP token:<id>`) or the OIDC subject (`ownerSeenBy()` and
`publicLabel()` in [`agent/src/sessions/protocol.ts`](../../agent/src/sessions/protocol.ts)).
Inside a session's own turn no id is shown, the caller's own included (`viewerOf()` in
`sessions.ts`): what the model reads there is kept in that session's transcript, which
whoever the session is later offered or handed to reads as it was written (PR #715
review).
The same applies to the resources, which are these tools. The browser user sees every id:
the panel's badges and Take over read them over the chat socket, and
`/api/v1/ai/sessions` is the browser user's. Free text another principal may read (a
cancelled approval's reason, "interrupted by …") uses the same labels.

## 3. Approvals by another agent: the per-token grant

Spec §6: "Approvals of outward actions by another agent are off by default and need a
per-token grant". §8.2: "Only the browser user decides, or another principal with a
per-token grant (§6), and never for its own calls or sessions."

- **The grant** is `approval_grant` on `ai_mcp_tokens`
  (`agent/src/db/migrations/20260929T0249Z_mcp_token_approval_grant.sql`), off for
  every token. Mint a token with it through `POST /api/v1/ai/mcp-tokens`
  (`{"name":…, "tier":"outward", "approval_grant":true}`,
  [operating.md §4.1](operating.md#41-mcp-access-tokens)). Only an `outward` token can
  hold it: the route answers 400 otherwise, and the table's `CHECK` refuses it. It
  cannot be changed on an existing token; mint another.
- **The check** is `approvalGrantCheck()` in
  [`agent/src/auth/tokens.ts`](../../agent/src/auth/tokens.ts), read on every decision,
  so revoking or expiring the token withdraws the grant at once. OIDC subjects and
  `anonymous` callers never hold one.
- **The rules** are `authorize()` in
  [`agent/src/approvals/service.ts`](../../agent/src/approvals/service.ts): without the
  grant a decision is refused; with it, never for an approval the caller requested, or
  in a session it owns or started. A grant holder sees every pending approval
  (`sessions_list_approvals` with no session). Passing the `input_hash` it was shown
  makes the decision fail if the approval is for a different input.

## 4. The session resources and `session.*` events

`scadbuddy://sessions` (`sessions_list`) and `scadbuddy://sessions/{session_id}`
(`sessions_get`) are MCP resources ([mcp-resources.md](mcp-resources.md)). Subscribing
to one session reads it first, so a session the caller may not see answers `-32002`.

Every batch a session appends to its event log is published on `scadbuddy_events` as
one ids-only event
([`agent/src/sessions/busEvents.ts`](../../agent/src/sessions/busEvents.ts)):

| Kind | When |
|---|---|
| `session.started` | Created (start, fork) |
| `session.owner` | Handed off, or a handoff offer made, withdrawn or declined (§2.1) |
| `session.waiting` | Waiting for a human (`waiting_approval`, `waiting_input`) |
| `session.done` | A turn or the session ended (`idle`, `done`, `failed`) |
| `session.message` | Anything else (streamed text, tool calls), at most one per 100 ms per session |

The payload is `{ id, at, kind, session_id, seq, status?, replica }`. The agent and the
backend each LISTEN on the channel on their own (spec §7):

- **Agent replicas** wake their event-log followers (`followSessionEvents()` →
  `EventLog.wake()`), so a watcher on replica B sees a turn on replica A at once rather
  than at the next poll (`agent/test/sessionTools.pg.test.ts` sets the poll to 60 s and
  still sees it). A replica skips its own events, which it woke locally.
- **MCP subscribers** get `notifications/resources/updated` for the session, and for
  `scadbuddy://sessions` on the lifecycle kinds.
- **The backend** decodes them (`SessionBusEvent` in
  `backend/scadbuddy/core/events.py`) and sends them to no WebSocket topic: the UI
  follows sessions over the agent's own chat socket, which knows who may see which.

They are not written to the backend's `events` log, so a reconnecting listener cannot
replay them. It reports the reconnect instead (`onReconnect` in
[`agent/src/events/bus.ts`](../../agent/src/events/bus.ts)), and followers and session
subscriptions re-read. The event log's one-second poll remains the fallback.

### 4.1 What a session touched (#931)

Every ScadBuddy tool call a session makes, once it
succeeds, is mapped to the resources it created, changed or deleted, one
`ai_session_resources` row each (`agent/src/sessions/touched.ts`). A per-tool
extractor reads the call's parsed input and its result: models, revisions (with the
parent and new commit), presets, assets, render jobs, outputs, print runs and prints (a `print` is always a Bambuddy queue item id, whichever tool queued it). A `write` or
`outward` tool with no extractor yet is listed as `unclassified` with its tool, so the
gap stays visible; a `read` tool records nothing. It is recorded in `runToolWithOutcome`
(`agent/src/tools/registry.ts`) whenever the call carries a session, not in a
projection, so a durable session's tool activities (the durable-sessions spec, §5.3)
record the same way. Rows go with their session. Calls
over `/mcp` outside a session record nothing. Revision commits already name their
session in a git trailer (#252, `agent/src/tools/authorship.ts`).

## 5. Not built yet

- **Skills on start.** The issue's `sessions.start` takes an optional skill
  (`/scadbuddy:…`). Session queries load ScadBuddy's plugin and have the Skill tool
  (#896), so the model can use a skill, but `sessions.start` does not take one yet.
- **Settings UI** for the grant: the route takes `approval_grant`, but Settings → "MCP
  access tokens" has no checkbox for it yet.
- **A2A** is deferred (spec §6).
- **The rest of #931**: extractors for the remaining tools (libraries, fonts, Bambuddy
  projects, settings and remembered choices, `browser_*` param changes), a backfill
  from `ai_audit`, the session view's "Touched" panel and resource-to-session links,
  filtering sessions by resource, and "restore to before this session".
