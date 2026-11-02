# Backend authentication: implementation plan

Issue #4975. Spec: `docs/superpowers/specs/2026-11-01-backend-auth-design.md` ("the
spec"), whose §13 decisions D1–D11 the owner made on 2026-11-01. This plan splits the
spec into eight PRs. Each phase merges with `SCADBUDDY_AUTH_MODE` at its default, `off`,
in which nothing is refused for lacking a credential, so every phase is safe to merge and
deploy on its own; a later phase never has to land for an earlier one to be correct.

Rules every phase keeps (CLAUDE.md):

- Backend schema changes are new files in `backend/scadbuddy/migrations/`, named
  `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`, expand-only (new tables, views, columns with
  defaults); a merged file is never edited. The agent's are in `agent/src/db/migrations/`.
- A new service is a `Component` in `scadbuddy/auth/component.py`, never an `AppState`
  field; a periodic pass is an activity in `SWEEPS`, never a loop in the API.
- A new metric is declared in `core/metrics.py` before a dashboard reads it.
- Every new `/api/v1` operation needs an agent tool or a `src/tools/coverage.ts` entry.
- Tests: `tests/auth/` for the backend (Postgres-backed ones take `pg_conninfo`), msw
  handlers under `frontend/src/mocks/features/auth.ts`, agent tests in `test/`.

## Phase 1: foundation (mode, sessions, caller, gate as a no-op)

Spec §5.2, §8.1 (the gate's shape only), §9.1 (`off`).

- `core/settings.py`: `auth_mode: Literal["off", "observe", "enforce"] = "off"`
  (`SCADBUDDY_AUTH_MODE`, an environment setting, spec §9.1, never a stored one). Until
  phase 3 implements them, the API refuses to start with `observe` or `enforce`, naming
  the phase that adds them, so no deploy can turn on a gate that does not exist yet.
- `scadbuddy/auth/`:
  - `models.py`: `SessionKind` (`login`, `delegated`, `break_glass`), `AuthSession` (the
    row), and the caller: `Person` (from a session: principal, name, email, kind),
    `Service` (from a service credential, phase 4; declared now so the gate's result
    type does not change), `Anonymous`. `principal_for(issuer, subject)` builds
    `user:<issuer>#<sub>` (spec §5.2).
  - `tokens.py`: a session token is 32 random bytes, base64url; the row's key is its
    SHA-256 (`id_hash`); the token is never stored.
  - `store.py`: `SessionStore` over `auth_sessions`: `create` (returns the token once),
    `resolve` (by hash; `None` when missing, revoked or past either expiry; slides
    `last_seen_at` and the idle deadline at most once a minute), `revoke`,
    `revoke_all(principal)`, `delete_expired(now)` (for phase 2's sweep).
  - `gate.py`: `AuthGate`, a pure ASGI middleware placed inside `CrossSiteGate`. In
    `off` it refuses nothing: it reads the `__Host-scadbuddy_session` cookie if present,
    resolves it (a lookup only when there is a cookie, so a request without one costs
    nothing), and puts the caller on `scope["state"]`. A lookup that fails (the database
    is down) leaves the caller `Anonymous` and logs, never failing the request in `off`.
  - `deps.py`: `CallerDep`, which reads the caller from the scope (`Anonymous` when the
    gate did not run).
  - `component.py`: `AUTH` (`AuthComponent`: the mode, the `SessionStore` over the
    settings pool).
- Migration `<ts>_auth_sessions.sql`: `auth_sessions` (spec §5.2 columns: `id_hash`
  primary key, `principal`, `issuer`, `subject`, `name`, `email`, `kind` with a check,
  `created_at`, `last_seen_at`, `idle_expires_at`, `absolute_expires_at`, `revoked_at`,
  `client_address`, `user_agent`) and an index on `principal`.
- Tests: the setting's default and its refusal of `observe`/`enforce`; the store
  (create, resolve, idle and absolute expiry, the once-a-minute slide, revoke,
  `revoke_all`, `delete_expired`); the gate in `off` (no cookie: no lookup; a live
  cookie: a `Person` on the scope; a dead or unknown one: `Anonymous`; no refusal ever).

Nothing reaches a session yet: no route creates one. Mergeable because the only
behaviour change is a cookie lookup that no browser can trigger.

## Phase 2: login, mode `off`

Spec §5.1, §5.2 (sweep, revocation list), §6.2, §9.2; D1, D3, D5, D7.

- Login settings in the backend's stored settings (D1). As built (#5193): a row of
  their own, `auth_login_config`, behind `GET`/`PUT /auth/config`, not fields of the
  generic `PATCH /settings`, since a save runs discovery first and the secret's AAD binds
  the issuer and client id it was saved for. The configuration:
  issuer (defaulting to the agent's `mcp_oidc.issuer`), client id (moved from
  `ai_settings.mcp_oidc.client_id`, which the MCP section then shows read-only), the
  client secret sealed under the KEK as #602 seals the Bambuddy key, the sign-in rule
  (D3: subjects and/or one group claim and value), and the RP-initiated logout switch
  (D7, off by default). A save with sign-in on runs discovery first and saves nothing
  if it fails (spec §9.2). The homelab's IdP is named in the PR, for phase 7.
  Changed in review (#5208): the issuer and client id are the deployment's,
  `SCADBUDDY_AUTH_OIDC_ISSUER` and `SCADBUDDY_AUTH_OIDC_CLIENT_ID` (bootstrap fields),
  not stored: until the gate refuses anyone (phase 3 classes `PUT /auth/config` as
  `person`, phase 8 enforces it), any caller could have pointed `/auth/login`'s redirect,
  and `POST /auth/config/test`'s fetch, wherever it chose. Settings shows them read-only
  and keeps the secret, the rule and the switches. Every fetch to the issuer (discovery,
  JWKS, token) goes through `library/url_import.py` `PublicOnlyBackend`, public addresses
  only, unless `SCADBUDDY_AUTH_OIDC_PRIVATE_NETWORK=true` for an IdP on the LAN.
- `auth/oidc.py`: discovery and JWKS fetch with the agent's rules (https except
  loopback, no redirects, bounded, cached 10 minutes, unknown `kid` refetch at most every
  30 s), ID-token validation (the `/mcp` alg allowlist, `iss`, `aud`, `azp`, `exp`/`iat`
  with 30 s leeway, `nonce`).
- Routes `api/auth.py`: `GET /auth/login` (the `return_to` rule of spec §5.1, the sealed
  `__Host-scadbuddy_login` cookie), `GET /auth/callback`, `POST /auth/logout`,
  `GET /auth/session`, `GET`/`DELETE /auth/sessions` (the person's own list, revoke,
  "sign out everywhere"). Sessions are 14 days idle, 30 days absolute (D5). Revocation
  publishes `auth.session_revoked`.
- The sweep `housekeeping_sweep_auth_sessions` on the prune Schedule, behind its
  `workflow.patched`.
- Frontend: the sign-in screen, the popup flow from a frame with the `event.origin` and
  `event.source` checks (spec §6.2), the signed-in name in the header, Settings' "Signed-in
  sessions" and the login section. msw answers `/auth/session` with the login off and
  signed out by default, so the header shows nothing and every other suite is unchanged
  (as built; phase 3's sign-in screen revisits it).
- The fake issuer and the backend tests of every callback refusal. As built (#5193), the
  issuer is in-process (`backend/tests/auth/fake_oidc.py`, over `httpx.ASGITransport`);
  phase 7 serves it as the `e2e-stack/fake-oidc/` container.

In `off` a signed-in person is recorded but nothing is refused, so a broken
configuration costs nothing.

## Phase 3: the gate in `observe`, and break-glass

Spec §5.3–§5.5, §8, §9.1 (`observe`), §9.3; D9, D11.

- Route classes (`public`, `api`, `person`, `internal.*`, `transfer`) on every mounted
  route, with a test that fails for an unclassed one; `/metrics` and `/healthz` stay
  `public` (D9).
- `observe` accepted at start: what `enforce` would refuse is served, logged once per
  caller and reason a minute, and counted as `scadbuddy_auth_would_refuse_total{class,
  reason}`; a panel on the API dashboard.
- The CSRF header `X-ScadBuddy-Request: 1` on every cookie request, safe methods included,
  except `SCRIPTLESS_GETS` (D11); added by `frontend/src/api/client.ts` and the
  template-UI host.
- Socket handshakes (realtime, LSP) and the telemetry relay resolve the session and log
  in `observe`; the 4401 close on revocation.
- `python -m scadbuddy.auth.break_glass` (spec §9.3).

As built (#5194): the classes are `auth/routes.py`, by endpoint (the public and person
ones named, the internal modules by module, every other route `api`), and
`auth/check.py` `auth_check` is an app-wide dependency beside `note_preference`, so it
sees the matched route for HTTP and socket alike; `auth/observe.py` counts and logs once
per class and reason a minute (the route template, never the path). The header is one
wrapper round `window.fetch` (`frontend/src/lib/requestHeader.ts`, installed first in
`main.tsx`), so the client, the loaders, template UIs and the relay all carry it, plus the
client's one XHR upload; a socket needs none (its `Origin` is checked). Break-glass codes
are `auth_break_glass_codes` (SHA-256, ten minutes, single use), redeemed at
`GET /auth/break-glass?code=`; the session view's `kind` drives the account menu's
"Break-glass session". The 4401 close waits for `enforce` (phase 8), since `observe`
refuses nothing.

## Phase 4: service credentials

Spec §7.1, §7.2; D4.

- Migration: `service_credentials`, a Postgres role per caller (`scadbuddy_print`,
  `scadbuddy_projects`, `scadbuddy_agent`) with its grants beside `RENDER_GRANTS`, and a
  view `service_credentials_<caller>` per caller. The deployment gives each caller its
  own database URL (a clusters PR, listed in this one).
- Issue and rotation at start and on the prune Schedule (mint at fewer than 7 days left,
  30-day lifetime), Settings' credentials page with revoke and "rotate now" (#891).
- The print worker, projects worker and agent read and present their tokens; the print
  worker now needs the KEK. In `observe` a missed caller shows on the counter.

As built (#5195): `service_credentials` and its three views
(`20261102T0645Z_service_credentials.sql`), `auth/service_credentials.py` (the store,
`TokenReader`, the httpx `ServiceAuth`, `grant_caller_views`) and the gate's bearer lookup
(`Authorization: Bearer sbsvc_…` resolves to a `Service` with its caller's scopes). The
API issues and grants at start (`main._grant_and_rotate_at_start`) and rotates in the
existing `housekeeping_sweep_auth_sessions` activity, so the workflow's history and patches
are unchanged; a newest credential sealed under another KEK is replaced at once. The print
and projects workers present theirs on every internal call (`worker.py`), the agent on
every backend call, the client and the workspace blob routes alike
(`agent/src/api/serviceCredential.ts`, the same AAD). Settings → System
→ Service credentials lists them, with revoke and "Rotate now"; the routes are `person`.
Each role named in `ROLES` (`scadbuddy_agent`, `scadbuddy_print`, `scadbuddy_projects`) is
granted its own view when the deployment made it; the full table grants of those roles,
and the deployment giving each caller its own database URL, are the follow-up this PR
files. Until then a caller on the shared URL can read every view (spec §7.2 says so).

## Phase 5: authorship from the session

Spec §5.6, §7.3, §8.3; D8.

- `core/authorship.py`: author headers believed only from the `agent` and `projects`
  credentials; the projects credential's per-run token. `_browser_only` becomes "a
  person's session". Activity and `started_by` record the session's principal; rows that
  say `browser` stay shared (D8).
- The agent: a backend view of `auth_sessions` granted to its role, the session check in
  `routes/guard.ts` and on its sockets, the browser user's `id` as the principal, the
  author headers set from the turn's owner.

As built (#5196): `core/authorship.py` `AgentAuthorship` asks `auth/authorship_rule.py`
(given by `main.create_app`) what the caller (from `AuthGate`) makes of the headers, and
keeps the requester in a context variable: `current_person()`, `browser_principal()`,
`requester_record()` (a run's `started_by`, a trigger's `created_by`: `{"kind":
"browser", "principal": …}` for a person) and `by_a_person()` (`_browser_only`, the delete
of a run, and whether a grant is approved or only requested: no author, and neither a
service nor a delegated session). Activity rows, an operation's (`OperationInput.person`)
and an order's `created_by` record the person. In `off` every header is still believed and
nothing is looked up; `observe` counts `author_header` and `wrong_author` (and
`out_of_scope`, a credential without `api` on an `api` route) beside phase 3's reasons;
`enforce` would answer 400 or 403. The run's token is `auth/run_tokens.py`, minted by
`flows/launch.py` into `FlowStart.run_token` (""; a run from before names nobody, as
before) and sent by `flow_routes.py` with the starter's headers (`RouteSend.author`). The
in-process projects worker, the flow kinds' and a grant release's reads present the
projects credential too (`Flows.api_auth`). The agent: the view `auth_sessions_agent`
(granted to `scadbuddy_agent` with its credentials' view), `auth/browserSession.ts`
(`personSessions` on `/api/v1/ai/*`, `browserUser(c)` in every route group and the chat
socket, the cache dropped on `auth.session_revoked`), `SCADBUDDY_AUTH_MODE` read
(`enforce` refused), and `sameOwner` taking every browser-kind principal as one (D8), as
the session SQL does. Author headers follow from the turn's owner, whose id is the person.
Approval and grant labels (`approved_by`, `decided_by`, the audit's `UI_ACTOR`) still say
`browser`.

## Phase 6: delegated sessions and personal access tokens

Spec §7.4, §7.5; D6.

- `POST /auth/delegated-sessions` for the `agent` credential (localhost backend URL
  only), refused on `person` routes; the headless browser's Playwright context carries
  it.
- Personal access tokens (Settings "Access tokens"); `bambuddy-fs` takes `--token-file`
  / `$BAMBUDDY_FS_TOKEN_FILE`, the Go client an API-key provider (regenerated by the
  `freshness` job).

As built (#5197): the agent's built-in headless browser (#349) was removed before this
phase (agent migration `20261012T0454Z_drop_headless_browser.sql`; a Playwright MCP
plugin took its place), so there is no Playwright context of the agent's own to set a
cookie in. `POST /auth/delegated-sessions` (the `agent` credential only, in every mode)
answers a one-time sign-in link on the public origin (`auth_delegation_codes`, SHA-256, a
minute, once; `auth/delegation.py`), and `GET /auth/delegated?code=` (public) sets a
`delegated` session of at most an hour as the named principal. The agent's tool
`sign_in_link` (`write`) mints it for the call's own principal, for the model to
open in the plugin's browser. The plugin loads the public URL over https, so the
localhost-only rule of spec §7.4 has nothing to guard and is not built. A delegated
session counts `not_a_person` on `person` routes and fails `by_a_person()`. Personal
access tokens: `personal_access_tokens` (`auth/personal_tokens.py`, `sbpat_<id>.<secret>`,
SHA-256 only), resolved by `AuthGate` to a `PersonalToken` caller acting as its person on
`api` routes (`read`: safe methods only, else `out_of_scope`; never `person`), managed at
`/auth/access-tokens` (`person`) and in Settings → System → Access tokens. The sessions
sweep also forgets spent codes and tokens dead for 30 days. `bambuddy-fs` takes
`--token-file` / `$BAMBUDDY_FS_TOKEN_FILE` (`api.WithToken`: Kiota's API-key provider as
a bearer for the API's host, the raw downloads and uploads too, https only). The generated
client is unchanged; the `freshness` job regenerates it with the new routes.

## Phase 7: real-stack e2e

Spec §11. `e2e-stack/run.sh --auth` and `e2e/real-auth.spec.ts` in Chromium, Firefox and
WebKit, including the replica of Bambuddy's sandboxed frame on same-site and cross-site
hosts. The PR amends the spec's **(assumption)** marks with what was measured.

## Phase 8: `enforce` on the deployment

Spec §9.1 (`enforce`), §10; D10.

- `enforce` accepted at start, with the start check of spec §9.1 (usable login
  configuration and a sign-in rule, or it refuses to start, saying which).
- README "Deploying" and "Running it" rewritten; break-glass documented; clusters'
  manifests move to `observe`, then `enforce` once the counter is zero for every caller.
- The default for a new install stays `off` (D10).

The CHIPS handoff (spec §6.3) is not planned (D2).
