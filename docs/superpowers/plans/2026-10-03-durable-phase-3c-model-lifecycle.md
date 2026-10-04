# Phase 3c: a model's lifecycle as commands on `library` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **As built (PR #1126, review fixes).** Where this plan and the code differ, the code
> holds:
> - `ClaimStore` keeps flat files named by the sha256 of their bytes,
>   `cache/claims/<digest>`, not `cache/claims/<operation key>/<part>`. Its API is
>   `put(data) -> name`, `get(name)`, `drop(name)` and `sweep(max_age)`. A put always
>   writes a fresh file, which renews it; the sweep moves a claim aside before removing
>   it and puts it back if a put renewed it meanwhile.
> - The run does not remove its claims. The route drops a request's claims once its
>   answer is final (a refusal before any operation, the 413 cap, a recorded failure
>   or a result), unless an operation still `running` names the same digest
>   (`OperationStore.named_by_running`). A 202 leaves them to the day-long sweep.
> - Claimed parts: a create's source, thumbnail and README; a patch's `presets`; an
>   import's URL. The import's request holds the URL without its query.
> - The subject is the slug, except for an import, whose subject is the URL's host.
> - A keyed create first looks up its recorded operation (`recorded`); if there is
>   none, a taken slug is refused before anything is claimed.

**Goal:** Five model writes become `Operation` commands (§4.2) on the `library` queue:
- create (`POST /models`, all three bodies)
- import (`POST /models/import`)
- patch (`PATCH /models/{slug}`)
- duplicate (`POST /models/{slug}/duplicate`)
- delete (`DELETE /models/{slug}`)

Request bytes too large for Temporal's history travel by **claim check**.

**Architecture:**
- **The claim-check store.** `operations/claims.py` `ClaimStore`:
  - The route writes a request's large parts (a source of up to 1M characters, a thumbnail of up to 10 MB, a README) to `cache/claims/<operation key>/<part>` on the data volume, and passes only the names.
  - The `library` run reads them, and removes them when it ends.
  - A new housekeeping sweep, `housekeeping_sweep_claims`, removes claims older than `CLAIM_MAX_AGE`, which is 1 day. That covers a request whose start never reached Temporal.
  - Temporal documents this as the pattern for payloads past its limits: 2 MB per payload by default, and a warning from 512 KB.
- **Routes keep their body parsing.** It is the part that reads only the request: content type, slug, decode, caps, PNG check, `model.json` part, tags. Each route then calls `run_operation`.
- **Kinds.** `library/model_operations.py` `model_kinds(state)`:
  - Each check makes the refusals that need the volume (`_require_new`, `require_model_exists`, duplicates, an unfinished render, valid presets).
  - Each run is the route's former body, unchanged: `_create`, `fetch_model` then `_create`, `catalogue.update`, `catalogue.duplicate`, `_delete_model` plus cleanup.
  - All are `done` and `queue="library"`. The broken-`model.json` mapping from #1119 (`_answered`) is shared.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §4.1–§4.3 (the
`library` row: "model create/import/patch/duplicate/delete"). Stacked on #1119.

## Rulings

1. **Claim check on the volume, not the blob store.**
   - The `library` worker runs in the API process and shares its volume.
   - The blob store's `StoreBundle` (#672) is not wired yet.
   - Cost if wrong: when `library` moves to its own container, claims must move to a store both can reach. That move is one class.
2. **A run re-makes its check's refusals.** An example is `_delete_model`, which checks again for duplicates and a running render. The check answers them without a record, and the run's copy is the race guard. A refusal that only the run catches is recorded as a failed operation. Cost: one record per lost race.
3. **Import's fetch is the run's.** It is the outward effect. The capacity refusal (`imports.full()`, 503) is made in both places, as above.
4. **`require_mine` stays in the routes**, as in 3b. It refuses on the path alone.

## Global Constraints

- Kind names: `model_create`, `model_import`, `model_patch`, `model_duplicate`, `model_delete`.
- The subject is the slug: for create and import, the slug derived from the request.
- Answers and status codes are unchanged: 201 for create, import and duplicate; 200 for patch; 204 for delete. Past the deadline the answer is 202 with the operation.
- Never commit `backend/openapi.json` or a `schema.d.ts`. Commit with `git add <paths>`.

## Review Focus

1. A retried create with the same `Idempotency-Key` makes one model and one commit.
2. A create whose source exceeds 512 KB still works: it goes by claim, never in history.
3. Delete with `?force=false` and a duplicate keeps its 409 with `duplicates` and `slugs`.
4. Claims are gone after the run, whatever its outcome. An orphan is swept.
5. The frontend's and agent's calls to these five routes follow a 202.

---

### Task 1: `ClaimStore` and its sweep

**Files:**
- Create: `backend/scadbuddy/operations/claims.py`
  - `ClaimStore(root: Path)`
  - `put(key, part, data: bytes) -> str`: writes atomically and returns the claim name.
  - `get(key, part) -> bytes`
  - `drop(key)`
  - `sweep(max_age) -> int`
- Modify: `backend/scadbuddy/core/paths.py` (`claims` → `cache/claims`)
- Modify: `backend/scadbuddy/main.py`: a fifth sweep, `housekeeping_sweep_claims`, in `SWEEPS`, after the staging sweep.
- Modify: `backend/scadbuddy/workflows/housekeeping.py` (`SWEEPS`)
- Test: `backend/tests/test_claims.py`. Update the `SWEEPS` count in `tests/test_housekeeping.py`, where it is read.

- [ ] Step 1: Failing tests:
  - put then get round-trips;
  - put twice with the same key and part is idempotent;
  - drop removes the key;
  - sweep removes only keys older than max age;
  - a part name with a path separator is refused (`ValueError`).
- [ ] Step 2: Run `uv run --frozen pytest tests/test_claims.py -q`. Expected: FAIL (no module).
- [ ] Step 3: Implement. Write to a temp file in the key's directory, then `os.replace`.
- [ ] Step 4: Run it, plus `tests/test_housekeeping.py`. Expected: PASS.
- [ ] Step 5: Commit `feat(operations): a claim check for request bytes too large for history (#1054)`.

### Task 2: The five model writes as `library` kinds

**Files:**
- Create: `backend/scadbuddy/library/model_operations.py` (`model_kinds(state)`)
- Modify: `backend/scadbuddy/api/models.py`: the five routes call `run_operation` and keep their body parsing. Add `responses=OPERATION_RESPONSES` and `idempotency_key: IdempotencyKey`.
- Modify: `backend/scadbuddy/library/operations.py`: export `_answered` as `answered_as_routes`.
- Modify: `backend/scadbuddy/main.py` (register `model_kinds`)
- Test: the existing `tests/api/test_models*.py`, `test_import*.py`, `test_duplicate*.py` and `test_delete*.py` keep their assertions. New tests:
  - `test_a_repeated_create_makes_one_model`
  - `test_a_create_past_512kb_goes_by_claim_and_leaves_none` (no history payload holds the source; the claims directory is empty after)
  - `test_a_slow_create_answers_202_and_is_followed`

- [ ] Step 1: Failing tests.
- [ ] Step 2: Run them. Expected: FAIL.
- [ ] Step 3: Implement. Run bodies return `ModelRecord.model_dump(mode="json")`, or `{}` for delete.
- [ ] Step 4: Run the model API tests. Expected: PASS.
- [ ] Step 5: Commit `feat(models): create, import, patch, duplicate and delete are commands (#1054)`.

### Task 3: Clients follow the commands

**Files:**
- Modify: `frontend/src/api/client.ts`: the create, import, update, duplicate and delete calls go through `command()`.
- Modify: `agent/src/tools/`: `create_model`, `import_model`, `update_model_details`, `duplicate_model` and `delete_model` go through `command()`.
- Test: `frontend/src/api/client.test.ts` and `agent/test/tools.test.ts`: each sends a key and follows a 202.

- [ ] Steps: failing tests, implement, then `pnpm lint && pnpm typecheck && pnpm test` in both packages. Commit `feat(models): the UI and the agent follow model commands (#1054)`.

### Task 4: Docs

- Spec §10 "As built so far (3c)".
- `CLAUDE.md`: claims, and `library/model_operations.py`.
- Commit `docs: model lifecycle as commands (#1054)`.
