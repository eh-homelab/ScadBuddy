# Durable phase 5c: `DurableSession` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run an assistant session as a Temporal workflow, `DurableSession` (`session-<ai_sessions.id>`, queue `agent`), in the `agent-durable` sidecar. A durable turn is driven from the agent service's existing session paths, its events reach `ai_session_events` in the panel's vocabulary, its payloads are encrypted per session, and `forgetSubject` crypto-shreds it.

**Architecture:** Three PRs, each merged before the next builds on it:

1. **Codec, forget, security fixes.** `ai_payload_keys`, the subject payload codec in Python and TypeScript (one vectors file pins them together), `forgetSubject` in the agent service, the key re-wrap on rotation, and the two 5b security findings.
2. **The workflow.** `DurableSession` on the plugin's `DurableClaudeAgent`, with the 5b gate wired into it. The segment runner opens the credential and spends from the session's budget per segment. The `follow_session` activity is the event subscriber, and `finish_turn` closes each turn. Image inputs travel by claim check. The worker starts in `python -m scadbuddy_durable`.
3. **The agent service's dispatch.** The `session_mode` setting picks a new session's mode at insert. `SessionManager` sends a durable session's turn by update-with-start. Interrupt and handoff go through `cancel_input` and `interrupt`. The turn's `done` follows the event log. Fork is refused.

**Tech Stack:** Python 3.12 (`temporalio` 1.34, `temporalio-claude-agent-sdk` at `b1cf3848…`, psycopg 3, cryptography), TypeScript (`@temporalio/*` 1.24.0, postgres.js, vitest).

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §6 (all of it), §8 ("Durable session"), §10 phase 5. Sub-plan table: `2026-10-08-durable-phase-5a-agent-durable-foundation.md` (row 5c). Consumes 5b (`2026-10-08-durable-phase-5b-tool-call-gate.md`, "What 5c consumes from 5b") unchanged. Issue #1056, epic #1058.

## What the pinned plugin is (re-read 2026-10-09, `b1cf3848`)

- `DurableClaudeAgent(tools=, builtin_tools=, tool_activities=, max_turns=, max_segments=, auto_continue_as_new=, continue_as_new_args=, live_output=)`. `run(prompt: str)` runs one task, and must be called from the workflow's run method when `auto_continue_as_new` is on. `decide`, `validate_decision`, `pending_approvals()`, `continue_as_new()` and `should_continue_as_new()` are the public API.
- Several durable calls in one message run at once (`asyncio.gather`), so a durable session can hold more than one parked entry. The 5b gate is per `tool_use_id`, so nothing changes.
- A durable tool's result reaches Claude as `ToolOutcome(content=result)`, rendered `json.dumps` unless it is a string (`_runner.py` `_result_content`).
- `SegmentInput.prompt` is `str`. There is no public way to put image blocks in a task's prompt.
- By default the conversation lives in the workflow (no session store), and any worker continues it.
- `ClaudeAgentSdkRunner(env=, max_budget_usd=, cwd=, extra_options=)` fixes its credential and budget at construction. `extra_options["mcp_servers"]` and `allowed_tools` are merged with the plugin's own.
- Live output: `follow_agent(client, workflow_id, from_offset=)` yields `prompt`, `text`, `tool_call`, `approval_needed`, `tool_result`, `retry`, `continued_as_new`, `done`, `error`, `cancelled`, each with its stream `offset`, and follows continue-as-new.

## Rulings (where the spec is ambiguous or the plugin differs; the spec governs the rest)

- **Ruling 1, no session store.** The conversation stays in the workflow, the plugin's default since 995bac6. It is encrypted by the codec like every other payload, and `forgetSubject` has nothing more to delete. `ai_session_entries` holds nothing for a durable session. A Postgres `SessionStore` is a follow-up, needed only when a conversation outgrows a payload without External Storage. The plugin then fails the task with a message that says so.
- **Ruling 2, credential and budget per segment.** `ScadBuddyRunner` is the `SegmentRunner` the plugin gets. For each segment it reads the session row (from the activity's workflow ID) and opens the first usable credential. It builds a `ClaudeAgentSdkRunner` with that credential's env and `max_budget_usd` = the lineage's remaining budget, runs the segment, then adds the segment's `cost_usd` to `ai_sessions.cost_usd` and 1 to `turns`, in the same activity. Each attempt is counted, because each attempt was really spent. Credential fallback stays the classic harness's (`harness/fallback.ts`, another session's work): a durable segment uses the first usable credential.
- **Ruling 3, limits.** `max_turns` is the plugin's per-segment engine cap, set from the row. The plugin's `max_segments` (50) bounds a task. A segment started with no budget left ends the task as `budget_exhausted`, like a classic turn's `error_max_budget_usd`.
- **Ruling 4, tools.** The manifest (`dist/tools.json`, path in `SCADBUDDY_DURABLE_TOOLS_JSON`) is loaded once at worker start. Each entry is an `activity_as_tool` on `agent-tools` with `needs_approval = hitl is not None`, its own `activity.defn(name=…)` stub that never runs. `builtin_tools=["Skill"]`, `tool_activities=()`. No subagents, no `ai_plugins`, no plugin packages (§6.3b). Durable tool results reach the model as text. The `agent-tools` activity joins a result's text blocks into one string, and an image block becomes a note that durable sessions do not yet receive images from tools. Otherwise the plugin would JSON-encode base64 into the model's context.
- **Ruling 5, images in the user's message (provisional; the coordinator is putting it to the user).** The plugin's prompt is text only, and overriding its private `_user_message` would break silently on a pin bump. So the agent service stores the images in `ai_session_blobs` (the claim check, by sha256 name), and `send_message` carries only `{name, mediaType}`. Each segment then gets a read-only in-segment MCP server through the runner's documented `extra_options["mcp_servers"]`. Its one tool, `view_user_images`, returns the session's named blobs as image blocks, and the task's prompt asks the model to call it. Remaining risk: the model may not call the tool.
- **Ruling 6, events.** The subscriber is an activity, `follow_session`, which `DurableSession` starts with each turn and waits for after it. It runs `follow_agent` from the offset stored in `ai_sessions.durable_offset` (new column). It appends each translated event and moves the offset in one transaction, so a retried or doubled subscriber (a continue-as-new mid-task) writes each event once. The translation:
  - `text` becomes `assistant.text.delta` + `assistant.text.done`;
  - `tool_call` becomes `tool.call` (risk from the manifest tier);
  - `tool_result` becomes `tool.result` (`ok` = status `done`, summary = the status, since the plugin's event carries no result);
  - `done`, `error` and `cancelled` end the subscriber and write nothing: `finish_turn` writes the turn's error (`turn_failed`, `interrupted`, `error_max_budget_usd`), since only the workflow knows which it was;
  - `approval_needed`, `prompt`, `retry` and `continued_as_new` are dropped (`approval_needed` per §6.6).

  A `text` superseded by a `retry` cannot be withdrawn and stays.
- **Ruling 7, a turn's bookkeeping.** The agent service writes `user.turn` and `session.status running` when it accepts the send, as a classic turn does. `finish_turn` writes `session.result` (the row's spend and the lineage budget), any error, and the final `session.status` (`idle`, or `failed`), after the subscriber has finished. Durable turns take no `turn_id`/lease claim: the workflow is the turn's owner, and `send_message`'s validator refuses a second turn (`busy`). The agent service's claim is a guarded `UPDATE … SET status = 'running' WHERE status NOT IN ('running','waiting_approval','waiting_input','done')`, given back if the Update is refused or unanswered.
- **Ruling 8, the superseding send.** An entry exists only while its turn runs, and a send is refused `busy` while a turn runs. So a durable send never supersedes an entry, and `cancel_input` is not sent on the send path, where it would cancel the parked call of a running turn before the busy refusal. Interrupt and handoff send it, as the spec says.
- **Ruling 9, an answer tool's input is untrusted text.** `build_entry` runs where no secret is held: the workflow must never hold the credential. So the redaction secrets are the turn's tool-call secrets the workflow has (none, today). `reason` must be one of the durable tool's reasons, and `on_timeout` must be `proceed` (Ruling 4 of 5b). Every string is capped, and any other value is refused to the model without parking. The prompt is never logged or traced. (Security finding 2.)
- **Ruling 10, an outward call's activity needs its approval row.** `gate: 'workflow'` trusted any workflow named `session-<durable id>`. Now the `agent-tools` activity runs a gated tool only when `ai_input_responses` holds `outcome = 'approved'` for its own request id (session, run, tool_use_id). `gate.describe_call` runs only for a durable session's workflow. (Security finding 1.)
- **Ruling 11, the codec's format.** A payload of `session-<uuid>` or `flow-<uuid>` becomes metadata `encoding: binary/scadbuddy-subject`, `scadbuddy-subject: <subject>`, and data = the sealed (v2, `secrets.ts` format) serialized `Payload`. It is sealed under the subject's data key, context `payload:<subject>`. The data key is sealed under the KEK, context `dek:ai_payload_keys:<subject>`. The subject is taken from the payload's own metadata on decode, so a payload decodes with or without serialization context. A forgotten subject fails to decode (`SubjectForgotten`). Keys are created by whichever side first encodes (`INSERT … ON CONFLICT DO NOTHING`, then read), never for a subject in `ai_forgotten_subjects`, and cached for 30 s at most. Both sides encode failure messages into payloads (Python `DefaultFailureConverterWithEncodedAttributes`; the agent-tools worker and its workflow bundle `src/temporal/failureConverter.ts`), so the codec seals them too.
- **Ruling 12, `forgetSubject`.** `agent/src/sessions/forget.ts` deletes the `ai_payload_keys` row and records a tombstone in `ai_forgotten_subjects` in one transaction, then terminates and deletes the workflow (each "not found" is fine), then the rows: `ai_sessions` (which cascades to events, pending input, responses, blobs and resources) and `ai_session_entries`. Operators run it as `node dist/forget-subject.js session-<uuid>`. `SessionStore.delete` never runs for a durable session, which has no entries, so it does not call it. No delete route is added (§6.5).
- **Ruling 13, the mode switch.** `mode` is set at insert from the `ai_settings` key `session_mode` (default `classic`), and nothing else sets it in 5c. Its routes and UI are 5d. A durable start with no Temporal client or no KEK is refused `unavailable`, never run as classic. Fork of a durable session is refused (`invalid`): its conversation is in the workflow.
- **Ruling 14, `wait_for_user` stays `proceed`-only.** Cancelling a turn (`interrupt`) is tested here. Offering `stop`/`wait` is 5d's call.
- **Ruling 15, what PR 2 settled while building.** These are the shapes PR 3 sends and relies on:
  - `SessionStart` carries `system_append`, what a classic turn appends to Claude Code's preset prompt (`manager.ts` `systemPromptAppend`). The runner gives the engine that preset with the append, so both modes prompt alike.
  - An outward tool's activity runs at most once (`maximum_attempts=1`). Other tools retry a lost worker 3 times, never a `ToolError`.
  - A malformed answer call is refused to the model through the plugin's `decide(False)`, the only refusal it offers, and never parks.
  - The segment runner reads the turn's images and the append from the workflow's `segment_context` Query, not from the prompt.
  - The engine inherits the worker's environment, so the runner empties the worker's own `SCADBUDDY_*`, `OTEL_*` and `TEMPORAL_*` variables for it (the database URL carries a password).
  - Continue-as-new between turns is the plugin's `should_continue_as_new()`. It is not forced in tests: the server's suggestion cannot be.
- **Ruling 16, what PR 3 settled while building.**
  - **The turn's blobs.** `DurableTurns.send` puts the turn's images in `ai_session_blobs` itself (`SessionBlobs.put`, under the `<sha256>.<ext>` names `AttachmentStore.claim` gives), before the Update. The segment runner may read them before the chat route's `claim` runs, and an inline image (`sessions_send` over `/mcp`) has no attachment at all. The route's `claim` after the send then only deletes the staging rows.
  - **The claim.** Besides Ruling 7's status guard, a durable claim checks the owner and the lineage's budget, as a classic claim does, so a refusal reads the same (`whyNotClaimed`). Only a definite refusal gives the claim back, restoring the status the row had before it (so a refused send keeps `failed`) and writing that `session.status` (the `user.turn` already written stays; the refusal is what the caller hears even if the give-back fails): the validator's `busy`, any other failed Update (`unavailable`), an ended workflow (`closed`), or images that could not be stored (`unavailable`).
  - **An unanswered Update keeps the claim (PR #1988 review).** The Update's id is the turn id. One not accepted within 10 s, or a call that failed in transit, has an unknown outcome: with update-with-start Temporal has already started or kept `session-<id>` and admitted the message, which a worker delivers when it polls. So the row stays `running` and `send` returns the turn, whose `done` follows the log to `finish_turn`'s status; a send meanwhile is refused `busy`, and an interrupt reaches the workflow. A kept claim whose workflow never runs the turn (it ends first, or was never started) is left to the sweep of #2001, which resets a `running` durable row whose workflow is closed or not found.
  - **A closed workflow.** The update-with-start uses `REJECT_DUPLICATE`: a session whose workflow ended (failed, terminated) has lost its conversation, so it is refused `closed` rather than restarted empty under the same session.
  - **`unavailable`.** A new `SessionError` code (HTTP 503) for a durable session this service cannot reach: no Temporal or no KEK at start or send, a failed Update, a turn's images not stored.
  - **Interrupt.** `cancel_input` going unanswered does not stop the interrupt: the Signal is recorded without a worker and ends the parked calls itself. False when the row shows no running turn, or the session has no workflow yet.
  - **Handoff (PR #1988 review).** The conditional owner UPDATE runs alone and commits first; only when it changed the row is `cancel_input` sent, with no lock or transaction held. It is best-effort: a cancel that goes unanswered neither fails nor undoes the handoff, and the `interrupt` Signal (recorded even with no worker, a no-op outside a turn) then ends the parked calls and the turn. If even the Signal is not sent, a payload-free warning is logged. A handoff that lost the owner race sends nothing. Between the commit and the cancel the old owner cannot answer as `owner`: the respond route reads the current owner (`gate/role.ts`). A service without Temporal refuses a durable handoff (`unavailable`) before anything changes. A durable session that never ran a turn has no workflow: its handoff passes (nothing is parked).
  - **Fork.** A fork's child is always `classic` (it copies the SDK transcript, which only a classic session has), whatever `session_mode` says. A durable parent is refused before the transcript is read.
  - **`done`.** Follows the event log only when read, from the turn's own `session.status running` to the first settled status: `interrupted` and `turn_failed` errors map to their outcomes, `error_max_budget_usd` to that result subtype.
  - **The scripted runner.** `SCADBUDDY_DURABLE_SCRIPTED=1` (exactly `1`) makes the worker's segment runner the plugin's `ScriptedClaude` with an echo policy (`session/scripted.py`), for `agent/test/durable.e2e.test.ts` only. It calls no tool.
- **Ruling 17, the sweep of a durable `running` row (#2001).** `DurableRunningSweep` (`sessions/durableSweep.ts`, every 30 s from main.ts beside the lease reaper) describes `session-<id>` for a durable row `running` with no write for 120 s (the send timeout is 10 s, so a start still in flight is never mistaken for one that never arrived). Closed or not found: a `turn_failed` error and `session.status idle` (not `failed`: the next send restarts a never-started workflow, or is refused `closed`), guarded on `running` and the same `event_seq`, so a turn that claimed the row meanwhile is left alone. A pass reads 10 rows after an in-memory id cursor (no column; a restart starts over), so rows it cannot resolve never starve the rest, and gives each describe a deadline of its own. Out of scope: a kept claim whose Update never reached a workflow that is still open (between turns) is not reset, since an open workflow is never second-guessed; and `not_found` cannot tell "never started" from "closed, then removed by namespace retention", where the next send starts an empty workflow under the old session. (Both settled by Ruling 18.)
- **Ruling 18, the two cases Ruling 17 left (#2078).**
  - **A kept claim on an open workflow.** DurableSession has a `turn` Query (`TURN_QUERY`, session/models.py): the turn it holds (running, handed over by continue-as-new, or finishing until `finish_turn` returns), else a message taken but not yet started, else null. No new event or history: a Query handler is replay-safe. The sweep's describer asks it when the workflow runs: a turn held is `open`, left to its own `finish_turn`; null is `between_turns`, so the kept claim's Update never reached the workflow, and the row is reset as for a closed one ("the message never reached the session's workflow; send it again"), under the same guard. The update handle (`updateId` = turn id) is not polled: an Update's outcome is lost at continue-as-new, so "not found" there would read a running turn as never taken. A workflow that cannot answer the Query (no worker, or a build without it) fails the describe, and the row waits for a later pass. An Update admitted but not yet delivered is not seen by the Query; with a worker polling it is delivered long within the 120 s grace.
  - **"Not found" after a turn.** `ai_sessions.durable_offset` (moved by `follow_session` for every event of a turn's output, the `prompt` first) tells them apart without a migration: 0 is never started, more is a workflow that ran a turn and was later removed by namespace retention. A send to a row past 0 never starts a workflow: it sends a plain `send_message` Update, and a workflow not found is refused `closed` ("session …'s workflow is gone (it ended and was removed); continue in a new chat"), the claim given back as for any refusal. The sweep resets such a kept claim to `idle` as for the other cases (not `failed`: the send's own refusal is what keeps it closed, as for a closed workflow), with `turn_failed` saying the workflow is gone and to continue in a new chat. The panel shows both messages as they are. A workflow that ended before its first turn wrote any output still reads as never started and is started afresh, which loses nothing.

## Global Constraints

- Agent schema changes are new files in `agent/src/db/migrations/`. Never edit a merged one.
- Never log, return in an error, write to disk or `repr` a secret, data key or KEK. Never record parameter values or log lines in traces.
- The workflow holds no credential, and nothing secret enters history.
- `sdkEvents.ts` is untouched. `@temporalio/*` pins are unchanged.
- Every grep or rg names a path.

---

## PR 1: codec, forget, security fixes

### Task 1.1: Security finding 1 (toolActivities)
- Test (`agent/test/temporal.worker.test.ts`, new cases): a gated tool's activity with no `approved` row fails `NotApproved` and its handler never runs; with the row it runs; a denied row refuses. `gate.describe_call` from a non-session workflow fails `UnknownSession`.
- Implement: `ToolActivityDeps.approvals: ApprovalRecords` (`approved(requestId): Promise<boolean>`, Pg implementation over `ai_input_responses`), checked when `tool.gated`; `gateActivities` takes `sessions` and checks the workflow.

### Task 1.2: Security finding 2 (entry.py)
- Test (`agent-durable/tests/test_gate_entry.py`): `reason` outside the tool's set, `on_timeout` other than `proceed`, a non-list `questions`, an over-cap header or option: each raises `MalformedAnswerInput` (a `ValueError`), so nothing parks; an answer's card strings are capped; `reason` never reaches the card unvalidated.
- Implement in `build_entry`.

### Task 1.3: `ai_payload_keys` and byte sealing
- Migration `<ts>_payload_keys.sql`: `ai_payload_keys(subject text primary key check (subject ~ '^(session|flow)-[0-9a-f-]{36}$'), dek_sealed bytea not null, kek_id text not null, created_at timestamptz not null default now())`.
- `agent/src/secrets.ts`: export `sealBytes(key, bytes, context, random?)` / `openBytes(key, sealed, context)`. Python `secrets.py`: `seal_bytes(key, data, context, iv=None)`, `open_bytes`.

### Task 1.4: The codec, both languages, on one vectors file
- `agent/src/temporal/codec.ts` `SubjectPayloadCodec` (`encode/decode(payloads, context)`), `PgPayloadKeys`. `agent-durable/src/scadbuddy_durable/codec.py` `SubjectPayloadCodec(PayloadCodec, WithSerializationContext)`, `PayloadKeys`.
- `agent/test/fixtures/payload-vectors.json`, written by `agent/test/payloadVectors.test.ts` under fixed bytes and compared; Python `tests/test_codec.py` decodes every vector and re-encodes under the same IV byte for byte.
- Tests: subject passthrough for other workflow ids; a forgotten subject fails to decode; a key created by one side opens on the other (pg); round trip through a real Temporal (`requires_temporal`): a `session-<uuid>` workflow's history holds no plaintext.
- Rotation: the agent re-wraps `ai_payload_keys` at start beside credentials; Python opens with the previous key too.

### Task 1.5: `forgetSubject`
- `agent/src/sessions/forget.ts`, `agent/src/forget-subject.ts` (CLI, `dist/forget-subject.js`).
- Test (pg + Temporal): after it, the key row, the workflow, the session rows, `ai_pending_input` and `ai_input_responses` are gone; history payloads of a terminated run cannot be decoded.

### Task 1.6: Main wiring
- `main.ts`: the Temporal client and the `agent-tools` worker get the codec when the KEK and database are there.

## PR 2: the workflow

### Task 2.1: Manifest and tool stubs (`session/tools.py`)
### Task 2.2: `ScadBuddyRunner` (`session/runner.py`): credential, budget, cost, images MCP server; fake-Anthropic e2e (`tests/fake_anthropic.py`) including the image round trip.
### Task 2.3: Event translation and `follow_session` (`session/events.py`), migration `<ts>_session_durable_offset.sql`.
### Task 2.4: `DurableSession` (`session/workflow.py`): `send_message`, the gate (`open_input` on a new pending approval, timers, `respond`, `cancel_input`, `interrupt`, `pending_input`), `finish_turn`, continue-as-new between messages. Tests with `ScriptedClaude` and stand-in `agent-tools` activities on a dev server: approve, deny, expiry, question answered and timed out, cancel_input, interrupt with the worker down, the handler set, one `approval.required` per approval.
### Task 2.5: Worker (`worker.py`, `__main__.py`): `agent` queue, codec, health status.

## PR 3: the agent service's dispatch

### Task 3.1: `mode` on `SessionRecord`, set from `session_mode` at insert; durable start refused without Temporal or KEK.
### Task 3.2: `DurableTurns` (`sessions/durable.ts`): send (claim, blobs, `user.turn`, update-with-start `send_message`), `done` from the log, interrupt (`cancel_input` then `interrupt` Signal), handoff (`cancel_input`, refused retryable when unanswered); `SessionManager` dispatches on `mode`; fork refused.
### Task 3.3: End to end: chat socket → durable turn, against a Python worker with a scripted runner (`SCADBUDDY_DURABLE_SCRIPTED`, tests only), skipped without uv/Temporal/Postgres.

## What 5d and 5e need from 5c

- 5d: `session_mode` is read at start (`SessionManager.start`, `modeSetting`), and `StartOptions.mode` exists but is not exposed. `SessionRecord.mode` is read but no route view carries it yet. 5d adds the routes, the picker, the badge, and `mode` on the three create paths. Open: nothing reaps a durable session whose row says `running` after its workflow ended without `finish_turn` (the classic reaper reads the lease, which durable turns do not take).
- 5e: the image needs `SCADBUDDY_DURABLE_TOOLS_JSON` (copy `agent/dist/tools.json`), `SCADBUDDY_DURABLE_CWD=/srv/agent`, `SCADBUDDY_TEMPORAL_ADDRESS`/`_NAMESPACE`, the KEK files, the database URL and `SCADBUDDY_DURABLE_SKILLS_DIR` (the plugin's skills). The agent container now also needs the KEK for the codec, which it already mounts.
