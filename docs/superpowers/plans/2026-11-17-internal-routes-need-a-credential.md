# Internal Routes Need a Service Credential (#5297, part 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In every `SCADBUDDY_AUTH_MODE`, the API refuses each `/api/v1/internal/*` route that is not the slicer's transfer route unless the request carries a live service credential that has the route's class. Before that ships, prod is measured in `observe`.

**Architecture:** The CRUD routes already work this way: `crud/guard.py` `require_crud` refuses in every mode. The three older internal modules (`internal.outputs`, `internal.library`, `internal.workspace`) get the same kind of dependency, `require_service_class`. Today their callers already send credentials with those classes: the print worker uses `ServiceAuth` on `RemoteOutputs` and `RemoteLibrary`, and the agent uses the credential its `main.ts` adds to `fetch`. So the guard changes nothing for a real caller and closes the routes to everyone else. Prod runs `off`, so a first clusters change sets `observe` on the API, and the would-refuse counter proves that no caller lacks a credential before the guard merges.

**Tech Stack:** FastAPI dependencies, pytest, Prometheus counter `scadbuddy_auth_would_refuse_total{class, reason}`, eh-homelab/clusters manifests.

**Spec:** `docs/superpowers/specs/2026-11-05-service-credentials-without-db-design.md` §6.4 (a service credential passes a route only when the route's class is in its scopes) and §11 D3. The rollout is README "The rollout" (steps 3-5, auth spec 2026-11-01 §9.1). Issue #5297.

## Global Constraints

- The classes stay as they are: `INTERNAL_MODULES` in `backend/scadbuddy/auth/routes.py` (`scadbuddy.api.internal` → `internal.outputs`, `scadbuddy.api.internal_library` → `internal.library`, `scadbuddy.api.internal_workspace` → `internal.workspace`, `scadbuddy.api.internal_blobs` → `transfer`).
- `transfer` (`api/internal_blobs.py`) is NOT guarded by a service credential: the slicer's transfer token is that route's credential (C6).
- Refusals match `require_crud`'s in shape. No live credential, or a person, a personal token or anonymous: 401, type `…/auth/signed-out`, `WWW-Authenticate: Bearer realm="scadbuddy"`, reason `no_credential`. A live service credential without the class: 403, type `…/auth/forbidden`, reason `out_of_scope`. Never 401 for that case, or the client would exchange again and loop (#5514).
- Scopes stay as they are (`auth/service_credentials.py` `SCOPES`): print `{internal.outputs, internal.library, crud.print}`, agent `{api, internal.workspace, crud.agent}`.
- No change under `agent/`: #5299's live session owns it (PRs #6497, #6510).
- `SCADBUDDY_AUTH_MODE=enforce` is OUT of scope. Prod has no login configured: no `SCADBUDDY_AUTH_OIDC_*` env and no stored auth settings (checked 2026-11-17). README rollout step 1 is the owner's (pick the issuer, register the redirect URI, save the client secret). The agent's 8081 guard and approvals half waits for #5299. Both come in part 2.

## Review Focus

1. A person signed in through the browser calls an internal route (for example by pasting the URL): refused 401, even in `off`. Task 2's test covers it with a `Person` caller.
2. The agent's credential on `internal.outputs`, or the print credential on `internal.workspace`: 403 `out_of_scope`, not 401. Task 2's parametrized test covers it.
3. A personal access token with `write` scope on an internal route: 401 `no_credential` (it is not a service credential). Task 2 covers it.
4. A new internal module added later without the guard. Task 2's route test fails for any `INTERNAL_MODULES` route other than `transfer` that lacks the dependency.
5. The print worker fetching an output after the guard ships: it must still work. Task 2 updates the internal-output and library tests to send a print credential, and `test_print_worker_nodb.py` keeps passing.

---

### Task 1: Measure prod in `observe` (eh-homelab/clusters)

**Files:**
- Modify (eh-homelab/clusters): `applications/scadbuddy/scadbuddy.yaml`, the `scadbuddy` container's env (not the `agent` container).

**Interfaces:**
- Produces: a prod measurement recorded on #5297. Task 2's PR may merge only after it shows a zero `internal.*` count.

- [ ] **Step 1:** Add `SCADBUDDY_AUTH_MODE` = `observe` to the API container, with a comment citing #5297 and README rollout step 3. Nothing is refused in `observe` (`auth/check.py`), and the start check only runs in `enforce`.
- [ ] **Step 2:** Build with kustomize v5.6.0 (`kustomize build applications/scadbuddy`), open the PR, and let it merge through the gate.
- [ ] **Step 3:** After Argo has applied it (`.status.operationState.syncResult.revision` at or after the merge, `deploy/CLAUDE.md` "Prod checks"), check that every API pod logs no start error and that `/healthz` answers.
- [ ] **Step 4:** After at least 24 h that include a print sent from ScadBuddy and an agent chat that writes a workspace file, read the counter on every API pod:
  `GOGC=off kubectl --context primary -n bambuddy exec <pod> -c scadbuddy -- python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8080/metrics').read().decode())" | grep scadbuddy_auth_would_refuse_total`
  Expected: no series with `class` `internal.outputs`, `internal.library` or `internal.workspace`. Counts for `api` and `person` (people with no login yet) are expected; ignore them.
- [ ] **Step 5:** Post the reading on #5297, with pods, time window and series. If an `internal.*` series is non-zero, stop and find the caller before Task 2 merges.

### Task 2: Guard the internal routes in every mode (backend)

**Files:**
- Create: `backend/scadbuddy/auth/internal_guard.py`
- Modify: `backend/scadbuddy/api/internal.py:26`, `backend/scadbuddy/api/internal_library.py:30`, `backend/scadbuddy/api/internal_workspace.py:45` (each `APIRouter(...)` gets `dependencies=[require_service_class("<class>")]`)
- Test: `backend/tests/auth/test_internal_guard.py` (new), `backend/tests/auth/test_routes.py`, `backend/tests/api/test_internal_outputs.py` and every other test that calls these routes without a credential (grep `internal/outputs`, `internal/library`, `internal/workspace-blobs`, `INTERNAL_OUTPUTS`, `INTERNAL_LIBRARY`, `INTERNAL_WORKSPACE` under `backend/tests`)
- Docs: README "The rollout" step 4 (an `internal.*` count is now also a refusal in every mode), `docs/security/trust-boundaries.md` Known gap 5 and the order-of-work step for #5297, `backend/CLAUDE.md` if it lists the guards.

**Interfaces:**
- Produces: `require_service_class(route_class: RouteClass) -> Any` in `auth/internal_guard.py`. It returns a `Depends(check)` whose `check.__name__` is `f"require_service_{route_class.replace('.', '_')}"`, so a route test can find it as `_depends_on_guard` finds `require_crud_<caller>`.

- [ ] **Step 1: Write the failing tests** in `backend/tests/auth/test_internal_guard.py`. Build the app the way `tests/crud/test_guard.py` `_client(mode)` does, with `AuthGate`, the app-level `auth_check`, and the same fake `Credentials` tokens. Mount a router `APIRouter(prefix="/internal/outputs", dependencies=[require_service_class("internal.outputs")])` with a `GET /ping`.
  - `test_the_internal_guard_refuses_in_every_mode`, parametrized `mode` in `off`, `observe`, `enforce`:
    - no header → 401, type ends `/auth/signed-out`, `WWW-Authenticate == 'Bearer realm="scadbuddy"'`, `reason == "no_credential"`;
    - a dead `sbsvc_…` token → 401;
    - the agent's token (scopes without `internal.outputs`) → 403, type ends `/auth/forbidden`, `reason == "out_of_scope"`;
    - the print token → 200 `{"ok": true}`.
  - `test_a_person_or_a_personal_token_is_not_a_service`: a `Person` session caller and a `PersonalToken` with `scope="write"` → 401 `no_credential` in `off`. Add the fakes the gate needs, as `tests/auth/test_gate.py` does.
- [ ] **Step 2: Add the route test** in `tests/auth/test_routes.py`: `test_every_internal_module_route_has_its_guard`. For each mounted route whose `route_class` is in `{"internal.outputs", "internal.library", "internal.workspace"}`, assert that a dependency named `require_service_<class with . as _>` is present. Assert that the set of classes checked equals those three, and that no `transfer` route has such a dependency.
- [ ] **Step 3: Run them and confirm they fail.**
  Run: `cd backend && uvx uv@0.13.6 run pytest tests/auth/test_internal_guard.py tests/auth/test_routes.py -q`
  Expected: an ImportError for `scadbuddy.auth.internal_guard`, then the route test fails.
- [ ] **Step 4: Implement `require_service_class`** in `auth/internal_guard.py`. It is `require_crud`'s check without the `wrong_caller` branch: no `Service` → 401 as above; class not in `principal.scopes` → 403 `out_of_scope`. Reuse `SIGNED_OUT` and `FORBIDDEN` from `auth/check.py`, and `ApiError`. Add the dependency to the three routers.
- [ ] **Step 5: Make the existing internal-route tests send a credential.** Every test that reaches these routes through the app sends the matching caller's bearer: print for outputs and library, agent for workspace. Mint it the way `tests/api/test_service_credentials.py` mints a live credential. Do not weaken the guard for tests.
- [ ] **Step 6: Run the focused suites.**
  Run: `cd backend && uvx uv@0.13.6 run pytest tests/auth tests/crud/test_guard.py tests/api/test_internal_outputs.py tests/test_print_worker_nodb.py tests/print_library -q` (each run with its own Postgres, per `backend/CLAUDE.md`)
  Expected: all pass. `ruff check`, `ruff format --check` and `mypy` are clean as `backend/CLAUDE.md` runs them.
- [ ] **Step 7: Docs.** README rollout step 4, trust page gap 5 ("`/api/v1/internal/*` refuses a request without a service credential in every mode since #5297 part 1; the agent's 8081 routes do not yet"), and its order-of-work line for #5297. Cite this PR.
- [ ] **Step 8: Commit, open the PR** with `Part of #5297` (not `Fixes`: part 2 remains), and auto-merge. Only after Task 1 Step 5 shows zero `internal.*`.

### Task 3: Prod check after the guard deploys

- [ ] **Step 1:** After the deploy PR for Task 2's image has synced, check that the print worker still sends: its log has no `401` or `403` from `/api/v1/internal/` (`kubectl logs deploy/scadbuddy-print --since=1h | grep -E "internal/(outputs|library)" | grep -E " 40[13]"`). Expected: nothing.
- [ ] **Step 2:** Check that anonymous access is refused, from inside the cluster: `kubectl exec deploy/scadbuddy-print -- python -c "import urllib.request as u; u.urlopen('http://scadbuddy.bambuddy.svc.cluster.local:8080/api/v1/internal/library')"` raises `HTTP Error 401`.
- [ ] **Step 3:** Post both on #5297. Leave #5297 open for part 2 (`enforce`, the agent's 8081 half).
