# Async command acceptance: answer 202 and follow the Update (#2340)

Status: design. Implementation in three stacked steps: render submit (§6.1), print
runs (§6.2), Operation routes (§6.3).
Related: spec 2026-10-01 §4.2 (the command shape), #1053/#1066 (render as a command),
#2341 (worker pickup delay, closed), #2462 (preview-slice answered once recorded).

## 1. Problem

Every command route starts its workflow with update-with-start and waits for the
workflow's `accepted` Update (`workflows/commands.py` `start_command`). The start is
instant. The Update is answered only after a worker runs the workflow's first workflow
task. If that pickup is slow (worker-version routing, a busy worker, a rolling deploy),
the route hits `COMMAND_ANSWER_DEADLINE` (10 s, plus a 2 s connect margin) and answers
**503 `command-still-accepting`**. The client sends the same request again with the same
`Idempotency-Key`, about every 12 s, for up to `printRunPoll.acceptingMs` (240 s).

Measured on 2026-10-11 (issue comments carry the traces):

- `POST /models/{slug}/render`: 79 × 503 in 24 h, 58 of them in one hour. The render
  went on to succeed every time. Trace `92ec3011…` shows a 503 at 12.0 s, the worker
  pickup at +15.6 s, and the render done at +37.8 s.
- `POST /print/library/{id}/run`: 22 × 503, and once about 20 re-POSTs over 244 s for
  one print. When the retries run out, the dialog says "it may print twice".
- Operation routes (`preview-slice`, `library/delete`, `presets`, `outputs`): 29
  Update timeouts. (#2462 since answers `preview-slice` once its record is written,
  which removes that route's effect time but not the pickup wait.)

What this costs is not the work, which always finishes. It is a red 503 in the
browser and in Tempo for every slow pickup, re-POSTs of the whole request body
(re-pinning a snapshot, re-validating parameters) every 12 s, and a hard 240 s limit
after which the client tells the user something failed that did not.

Not a goal: making pickup faster. The worker still has to run the first task. That
was #2341 (closed).

## 2. What must not change

These were hardened over many review rounds (#1066), and the design keeps all of them:

1. **A row exists only once its execution does.** The workflow's first activity writes
   it (`render_accept`, the print run's and the Operation's record activities). No API
   writes a pending row that a reconciler then has to reap.
2. **A re-sent request is the same claim.** The request's key is the `accepted`
   Update's id, so Temporal answers a second Update with the same id on the same
   execution with the first one's outcome.
3. **A closing run rejects rather than answers** (the `CLOSING` validator), so the same
   key, sent again after the close, starts the next run.
4. **Refusals keep their answers:** a full queue (503 + `Retry-After`), an unstartable
   render (500), 413, 422.

## 3. Options

**A. The API writes the pending row** (P5's option 1). The route inserts the
`render_jobs` row and starts `render-<key>` with that job id. A request that finds an
open row joins it, sending its claim as a signal instead of an Update. Supersede moves
into the workflow, and settle reaps rows whose run never started.
This gives up §2.1, and with it the reconciliation-free design #1053 was built to get.
It would also need doing again for print-run records and Operation records, each of
which has its own first activity.

**B. Return the key, and the client follows a new read** (P5's option 2). The route
returns `render_key`/workflow id, and the client polls `GET /renders/{key}` until a job
row exists. But whether this request's claim landed, and what its answer was (joined,
queue full, unstartable), is the Update's outcome, not the row. A row read cannot say
"queue full" or "this request's claim". So the read would need its own copy of the
claim logic. It also changes `RenderAccepted`, `useRenderJob` and the agent's render
tools.

**C. Follow the command's own Update by id (chosen).** Wait for the Update as today,
but only briefly. Past that, answer **202 Accepted** with the workflow id and Update id.
The client then follows `POST /commands/{workflow_id}/updates/{update_id}`, which
long-polls that same Update's outcome (`WorkflowHandle.get_update_handle(id).result()`)
and answers it exactly as the POST would have.

## 4. Why C

- **It keeps all of §2, because it changes nothing in any workflow.** The follow reads
  the outcome of the Update the POST sent: the same claim, the same refusals, the same
  closing rejection. No row is written early, and no claim logic is copied.
- **One mechanism for all three kinds.** Render, print run and Operation all go through
  `start_command`, and their Updates all have ids. The 202 and the follow route are
  written once, with a small per-kind adapter that turns the Update's answer into that
  route's response (§5.3).
- **The healthy path does not change.** On a healthy worker the Update answers in
  ~100–300 ms, inside the short wait, so the POST answers 200 as it does now, with no
  extra round trip.
- **It is backed by measurement, not docs alone.** Measured on 2026-10-13 against the
  Temporal dev server (temporalio 1.34, a task queue with no worker):
  - Update-with-start ends in `WorkflowUpdateRPCTimeoutOrCancelledError`, and the
    execution exists.
  - Polling that Update id waits for it (it times out at the poll's own deadline). It
    is not unknown.
  - Polling an id that was never sent answers `NOT_FOUND "update … not found"`.
  - Once a worker starts, the same poll returns the Update's answer.

  `WorkflowUpdateStage.ADMITTED`, which would let the POST return as soon as the server
  has the Update, is refused by this SDK (`ValueError("ADMITTED wait stage not
  supported")`). That is why the POST still waits briefly, and why §5.2 handles a lost
  Update.

## 5. Design

### 5.1 The POST

- `start_command` takes `accept_within` (default `COMMAND_ANSWER_DEADLINE`, so every
  caller that does not opt in is unchanged). A route that opts in passes
  `FAST_ACCEPT = 3 s`.
- Past `accept_within`, if the execution exists (`late_answer`'s describe), it raises
  `CommandAcceptingError(workflow_id, update_id)`.
- If Temporal does not answer the describe, the route answers 503
  `temporal-unavailable` as today.
- The Update always has an id. When the request carries no `Idempotency-Key`, the route
  makes one, so there is always something to follow.
- **Opt-in by the client:** only a request with `Prefer: respond-async` (RFC 7240) gets
  the 202. Every other request keeps today's 503 `command-still-accepting`, so a cached
  SPA bundle during a rollout, the agent, and any script keep working unchanged. The
  202 echoes `Preference-Applied: respond-async`.
- The 202 body is `CommandAccepting`:
  - `status: "accepting"`
  - `workflow_id`, `update_id`
  - `follow_url`, also sent as `Location`
  - `retry_after` (seconds), also sent as `Retry-After`

### 5.2 The follow: `POST /api/v1/commands/{workflow_id}/updates/{update_id}`

- **A POST, not a GET.** A render's follow makes its supersede (§5.3): it releases, and
  may cancel, the job the request replaces. `api/cross_site.py` (#962) checks only unsafe
  methods, on the basis that a GET changes nothing. As a GET, a page on another origin
  could make a LAN user's browser fire the follow with an `<img src>`. As a POST it goes
  through `CrossSiteGate` like the command it follows. Following again is still safe:
  the Update's outcome is fixed, and the release is idempotent (§5.3).

- **Long-poll.** It waits up to `FOLLOW_WAIT = 10 s` (below Envoy's 15 s route timeout,
  like every other bound here) on the Update handle's `result()`.
- **Answered:** the kind's adapter (§5.3) turns the answer into the original route's
  own response: 200 with its body, or its refusal problem (429/503 queue full, 500
  unstartable, and so on). The client cannot tell it from a POST that answered at once.
- **Still waiting:** 202 with the same `CommandAccepting` body.
- **Pacing.** The follow already waits up to `FOLLOW_WAIT` on the server, so a client
  follows again at once after a "still waiting", with only its own small polling floor.
  It does not also wait `Retry-After`, which would wait twice per round. `Retry-After`
  is sent for HTTP's sake, and for a client that does not long-poll.
- **`NOT_FOUND` (the Update or the execution):** 409, type
  `https://scadbuddy.dev/problems/command-resend`. This happens when:
  - the Update was never durably admitted (an admitted Update lives in the server's
    registry, not in history, until the worker accepts it);
  - the run closed and rejected it (§2.3). The POST's own `_accepted` loop waits out a
    closing run and starts the next one before it answers. A followed request cannot
    do that, so it takes one extra round trip, the re-send, that a fast POST does not;
  - the execution is gone.

  The client then sends the **original POST again with the same key**. That is exactly
  today's re-send, and safe for the same reasons (§2.2–2.3). It is the only re-send left,
  and it is rare.
- **The poll's own RPC fails:** `temporal_failure` maps it to the same 503s as the POST
  routes.
- **The kind comes from the workflow id's prefix** (`render-`, `print-`, `op-`), each
  registered with its adapter. An unknown prefix is 404, so the route cannot be used to
  read arbitrary executions. Ids are not secrets, but this route reads no more than the
  POST already answered.

### 5.3 Per-kind adapters

| kind | Update answer | response | after the answer |
|---|---|---|---|
| render (`render-…`) | `RenderAnswer` | `RenderAccepted` (job id, status url, inputs) | the supersede (`_supersede` of the job the request named), then the job |
| print run (`print-…`) | the run record | `PrintRun` (what `followPrintRun` already polls) | none |
| Operation (`op-…`) | `Operation` | the route's body, or `OperationAccepted` 202 | none |

A render's supersede ran after acceptance inside the POST. It now runs wherever the
answer arrives: the POST when it was fast, the follow otherwise. The follow URL carries
the superseded job id as `?supersedes=<job id>`. The release is already idempotent, its
Update id being `{request_id}:release:{job id}`.

The `supersedes` in the follow URL is whatever the client sends, so the follow checks it
exactly as the POST does (`RenderService._superseded`). It releases only a job of the
answered job's template, still `pending` or `running`, and not the answered job or its
run. Anything else is left alone, as the POST leaves it.

A followed render's `inputs` are the
job's, which differ from the request's only when it joined another request's job. The
web client reads only `job_id`.

### 5.4 Clients

- **Web.** `reattach` sends `Prefer: respond-async` on command POSTs. On a 202
  `accepting`, it follows `follow_url`, paced by `Retry-After`, until an answer. On 409
  `command-resend` it re-sends the original request with the same key.
  - The overall budget stays `acceptingMs` (240 s), now spent following instead of
    re-POSTing. The `useRenderJob` and print-dialog states are unchanged: they see the
    same answer, later.
  - **No 503 reaches the console** for a slow pickup.
- **Agent** (`agent/src/tools/command.ts`): the same, in its own follow loop, bounded
  by `COMMAND_FOLLOW_MS` as today. Until it opts in, it keeps the 503 path.

### 5.5 Observability

- A 202 `accepting` is not an error: no error span and no 5xx in the access log. It
  still counts as `render_accept_pending` (and the per-kind equivalents), once per
  request, not per follow. That metric's help text (`core/metrics.py`), which says
  "answered 503 command-still-accepting", changes with it to cover the 202.
- The follow route gets its own server span, linked to the POST's trace through the
  render's `traceparent`.

## 6. Rollout

Each step is its own PR, stacked on this one.

1. **Render submit** (`POST /models/{slug}/render`). Arrange shares
   `RenderService._answer` but is not opted in at this step: its route keeps the 503
   re-send, so §5.3 needs no Arrange row yet. Opting it in later means adding a row
   whose response is Arrange's own `RenderAccepted`-shaped job answer.
   - `start_command`'s `accept_within` and `CommandAcceptingError`
   - `CommandAccepting` and the follow route, with the render adapter
   - the supersede in the follow
   - the web client's `render` call opting in

   Tests:
   - a backend test with a worker that is not yet running: the POST answers 202, the
     follow answers 202 and then 200 with the job once the worker runs;
   - without `Prefer`, the same request still answers 503;
   - a lost Update is a 409 resend;
   - vitest for the client's follow and resend.
2. **Print runs:** the print-run adapter, `followPrintRun` opting in.
3. **Operation routes:** the Operation adapter, `command()` opting in.
4. **Agent:** `tools/command.ts` opting in for all three.

## 7. Out of scope

- How fast the worker picks up (#2341).
- Replacing Updates with signals.
- Any change to what a workflow does once it has run its first task.
