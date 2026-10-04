# Phase 3b: library pins as commands on `library` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The four library-pin writes become `Operation` commands (§4.2) on the `library`
queue: pin (`PUT /models/{slug}/libraries/{name}`), re-pin (`PATCH …`), unpin (`DELETE …`)
and remove checkouts (`DELETE /libraries/{name}`). Phase 2a's generic `OperationWorkflow`
and `operations` table serve them, and the API process's `library` worker (phase 3a) runs
them.

**Architecture:**
- `OperationKind` gains `queue` (`"bambuddy"` or `"library"`, default `"bambuddy"`).
  `OperationCommands` maps each to its configured task queue, and `run_operation`
  starts the workflow on the kind's queue.
- The `bambuddy` worker registers the operation activities for the `bambuddy` kinds. The
  `library` worker registers `OperationWorkflow` and the activities for the `library`
  kinds. Each worker serves only the kinds whose effect it holds (§4.3).
- `library/operations.py` `library_kinds(state)` moves each route's body into a kind:
  the check makes the refusals that need no lock, and the run does the effect, unchanged.
  It includes the refusals that must hold the checkout gate with the effect (a removal's
  users and leases, a re-pin's changed entry).
- Routes call `run_operation` and answer `operation_answer`. A run's `ApiError` is
  recorded as the failed operation, so the route answers the same status and problem as
  today.
- Clients: the frontend's `pinModelLibrary`, `repinModelLibrary` and `unpinModelLibrary`,
  and the agent's `pin_library`, `pin_library_from_url`, `repin_library`,
  `repin_library_from_pinned_url`, `unpin_library` and `remove_library_checkout`, go
  through `command()`.

**Tech Stack:** Python 3.12, temporalio 1.33, FastAPI, Postgres 17; React 19; TypeScript
agent.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §4.1–§4.3
(the `library` row: "library pin/repin/unpin/remove"), §10 phase 3. Stacked on #1095.

## Rulings

1. **Pin and re-pin are `done`, not `accepted`.**
   - §4.2 lists "a pin's clone" among the `accepted` kinds. But a `done` kind answers
     today's body when the effect ends within `command_answer_deadline`, and 202 with
     the operation past it.
   - So a slow clone still never holds a request past the deadline, which is the point
     of `accepted`.
   - A fast one keeps today's answer (the `ModelRecord`), so the UI's pin flow is
     unchanged.
   - `OperationWorkflow` has no `accepted` mode yet. Adding one only for this would
     change the API for no gain.
   - Cost if wrong: a later switch to `accepted` changes these routes' answer to an
     always-202.
2. **The library check (`POST …/check`) stays a request.** It records nothing. Its
   clone is a cache fill that the boot sweep removes, and the person waits for the
   verdict (§4.1, "a read … the result belongs to the caller who waits for it").
3. **The checkout gate and the install semaphore stay in-process.** The `library`
   worker runs in the API process (3a), so the kinds close over `state.checkouts` and
   `state.installs` exactly as the routes did. Moving the worker to its own container
   (`scadbuddy-library`) will need them shared; that is not this plan.
4. **`require_mine` stays in the route.** It is a refusal on the path alone. It needs
   no Temporal round-trip and was answered before any work.

## Global Constraints

- Kind names: `library_pin`, `library_repin`, `library_unpin`, `library_remove`.
- Subject: the model slug for the first three; `library:<name>` for the removal.
- Every kind keeps `run_attempts=1`. A git commit is not deduped, and §4.2 says a repeat
  never repeats the effect.
- Never commit `backend/openapi.json` or a `schema.d.ts`. Commit with `git add <paths>`.

## Review Focus

1. A retry with the same `Idempotency-Key` after a pin succeeded must answer the recorded
   model, not clone and commit again.
2. A removal refused because a model pins the library must answer 409 with `models`, as
   today. The extensions survive the record.
3. A pin whose clone outlives the deadline answers 202. The client follows it to the
   `ModelRecord`.
4. A `bambuddy` kind must never start on `library`, and the reverse.
5. A test's app runs both workers on queues of its own (`{queue}-library`).

---

### Task 1: Operations start on their kind's queue; the `library` worker serves them

**Files:**
- Modify: `backend/scadbuddy/operations/kinds.py` (`queue` field)
- Modify: `backend/scadbuddy/api/deps.py` (`OperationCommands.queues: dict[str, str]`, which
  replaces `task_queue`)
- Modify: `backend/scadbuddy/api/operations.py` (`task_queue=ops.queues[kind.queue]`)
- Modify: `backend/scadbuddy/main.py` (the `bambuddy` worker takes the `bambuddy` kinds; the
  `library` worker registers `OperationWorkflow` plus `operation_activities` over the
  `library` kinds)
- Test: `backend/tests/api/test_operations.py` (or the module that covers `run_operation`)

- [ ] **Step 1: Failing test.** A fake `library` kind registered on the app runs on the
  library worker. `test_a_library_kind_runs_on_the_library_queue`: the route answers its
  result. A describe of `op-<kind>-<key>` shows task queue `{queue}-library`.
- [ ] **Step 2: Run it.** Expected: FAIL. The workflow starts on the `bambuddy` queue, and
  nothing serves the kind's activities there.
- [ ] **Step 3: Implement** as in Files.
- [ ] **Step 4: Run it, plus the existing operations tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(operations): a kind runs on its own queue; library serves its kinds (#1054)`.

### Task 2: The pin writes as `library` kinds

**Files:**
- Create: `backend/scadbuddy/library/operations.py` (`library_kinds(state)`)
- Modify: `backend/scadbuddy/api/libraries.py` (the four routes call `run_operation`, with
  `responses=OPERATION_RESPONSES`, an `IdempotencyKey`, and `OperationsDep`)
- Modify: `backend/scadbuddy/main.py` (register `library_kinds`)
- Test: `backend/tests/api/test_libraries.py` (existing tests unchanged in what they
  assert); new tests:
  - `test_a_repeated_pin_answers_the_recorded_model_without_a_second_commit` (same
    `Idempotency-Key`, the history has one commit)
  - `test_a_refused_removal_keeps_its_models_extension`
  - `test_a_slow_pin_answers_202_and_its_operation_ends_with_the_model` (a clone held by
    an event past a lowered `command_answer_deadline`)

- [ ] **Step 1: Failing tests** (above).
- [ ] **Step 2: Run them.** Expected: FAIL. No `Idempotency-Key` dedupe, and no 202.
- [ ] **Step 3: Implement.** The run bodies are the routes' bodies, moved. Results are
  JSON: `ModelRecord.model_dump(mode="json")`, and `{}` for the removal, whose route
  answers 204.
- [ ] **Step 4: Run** `tests/api/test_libraries.py tests/api/test_create_libraries.py
  tests/api/test_library_copies.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(library): pins, re-pins, unpins and removals are commands (#1054)`.

### Task 3: Clients follow the commands

**Files:**
- Modify: `frontend/src/api/client.ts` (`pinModelLibrary`, `repinModelLibrary`,
  `unpinModelLibrary` through `command()`)
- Modify: `agent/src/tools/libraries.ts` (the six tools through `command()`)
- Test: `frontend/src/api/client.test.ts` (a 202 for a pin is followed to the model);
  `agent/test/` libraries tool test (a 202 is followed)

- [ ] **Step 1: Failing tests.**
- [ ] **Step 2: Run.** Expected: FAIL. The 202's body is returned as the model.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm lint && pnpm typecheck && pnpm test` in both packages. Expected:
  PASS.
- [ ] **Step 5: Commit** `feat(library): the UI and the agent follow pin commands (#1054)`.

### Task 4: Docs

- Spec §10 "As built so far (3b)".
- `CLAUDE.md`: `library/operations.py` in the layout.
- Commit `docs: library pins as commands (#1054)`.

## As built (corrections, review #1119)

- Task 1: `OperationCommands.queues` is `dict[Queue, str]` (`operations/kinds.py`
  `Queue`), not `dict[str, str]`.
- Task 2: the check also refuses an unknown model for the unpin (`model_check`), where
  the old route checked inside the same synchronous call as the unpin; the unpin's own
  `ModelNotFoundError` still answers a model deleted in between.
- A cancelled run (past its `run_timeout`) is cancelled before its next step. A clone or
  commit already in its thread (`to_thread_to_end`) finishes, and keeps the checkout gate
  and install slot until it has: a commit in flight can still land after the operation is
  recorded failed.
