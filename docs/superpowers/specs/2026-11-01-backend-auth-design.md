# Backend authentication: ScadBuddy's own OIDC login, service credentials, and a rollout that cannot lock anyone out

Status: draft for review (#4975). Design only; no code. Builds on the main design spec
(`2026-09-22-scadbuddy-design.md`, §4 and §9), the AI spec
(`2026-09-27-ai-integration-design.md`, "the AI spec": §4.3 "Agent → backend", §8 auth,
§9 the KEK), the durable spec (`2026-10-01-durable-printing-agents-flows-design.md`, §5.2
on proxies) and the flow-grants spec (`2026-10-11-flow-grants-and-git-design.md`, §4.2 on
`started_by`). The open choices are collected in §13, each with a recommendation.

Every fact about the code was read from the tree at `773574ee3` on 2026-11-01. Facts
about browsers and standards are cited; the one thing measured is the Public Suffix List
check in §6.1. Nothing here was exercised inside a live Bambuddy or against a live IdP:
what this design assumes about them is marked **(assumption)** and is what phase 7's
e2e (§11) checks.

## 1. Why

The backend has no authentication. The code says so in four places (#4975):

- `backend/scadbuddy/api/internal_library.py:10`: "the API has no auth yet, so whoever
  reaches it reaches these".
- `backend/scadbuddy/api/realtime.py:86`: "the backend has no auth today (spec §4.3)". That
  is the AI spec's §4.3: "The backend has no auth today and this adds none. The trust
  boundary is the pod".
- `backend/scadbuddy/core/authorship.py:20`: "Neither header is authentication … the
  backend trusts its callers (§4.3), and a forged header can only mislabel a revision."
- `agent/src/auth/oidc.ts:93`: `client_id` is "The client id for a future in-app login;
  not used to validate `/mcp` tokens", shown in Settings as "Client ID for ScadBuddy's own
  login (optional)" (`frontend/src/components/McpOidcSettings.tsx:191-199`).

The README states it plainly ("**LAN only.** ScadBuddy has **no authentication**",
README "Running it") and so does the AI spec §8.3 ("anyone who can reach Settings can
change the MCP auth mode, mint tokens or approve actions … Putting the UI behind OIDC is
a separate issue"). This is that issue.

**Done when** (#4975): the browser signs in through the configured OIDC issuer with the
stored client id; the API checks a session on every `/api/v1` route; the internal routes
need a service credential; and `authorship` takes the author from the session, not a
header.

## 2. Threat model

### 2.1 Today: the network is the boundary

| Who can reach it | What they get today |
|---|---|
| Anyone who reaches the ingress (`scadbuddy.internal.nullreference.io`, the internal Envoy gateway, main spec §9) | Every `/api/v1` route: models, outputs, prints with the stored Bambuddy key, Settings (including the Bambuddy key write, the MCP auth mode, AI credentials through the agent's routes), approvals of the agent's outward calls (`agent/src/routes/guard.ts`: "this is not an approval and not authentication") |
| Anyone who reaches the API's Service (`SCADBUDDY_API_INTERNAL_URL`, e.g. `http://scadbuddy:8080`) | The same, plus the hidden `/api/v1/internal/*` routes: outputs (`api/internal.py`), the native library including `PUT /files` (`api/internal_library.py`), the agent's workspace blobs including `POST /uploads`, a token for one 128 MiB internal PUT (`api/internal_workspace.py`) |
| A page on another site, through a LAN user's browser | Nothing that writes: `CrossSiteGate` (`api/cross_site.py`, #962) refuses a write whose `Origin` is not the public URL's, one of `SCADBUDDY_ALLOWED_ORIGINS`, or loopback; the sockets check `Origin` the same way (`realtime.py` `refuse_foreign_origin`). Safe methods pass: the API sends no CORS headers, so the page cannot read the answer |
| A caller that sets `X-ScadBuddy-Agent-Author` | Mislabels a revision, an activity row's principal, or a flow run's `started_by`; and, by its presence, is refused the routes only a person may use (`api/flows.py` `_browser_only`) |

What already exists and stays: the `Origin` allowlist (one rule in both services,
`agent/src/http/origins.ts`, `realtime.py` `origin_allowed`), trusted-proxy handling
(`core/proxies.py`, a port of the agent's), the KEK (`core/secrets.py` `Kek`,
`SCADBUDDY_SECRET_KEY_FILE`), the slicer's transfer tokens (`slicing/transfer_token.py`:
one method on one key until one time, MACed under a key derived from the KEK), and `/mcp`'s
own auth modes (AI spec §8.3), which this design does not change.

### 2.2 What this design defends against

1. **Someone on the LAN (or the cluster network) who is not the owner** using the UI or
   the API: every `/api/v1` route needs a signed-in session or a service credential.
2. **A compromised or curious pod in the cluster** calling the API's Service: the
   internal routes need a credential scoped to the caller that uses them.
3. **A forged author header**: only a service credential that may assert an author is
   believed; from anyone else the header is refused.
4. **Cross-site request forgery** once a cookie authenticates: §5.3.
5. **A deploy or a bad IdP configuration locking everyone out**: §9.

### 2.3 Out of scope

- Roles beyond "a person who may use ScadBuddy" (§13 D3). Today there is one user, the
  "browser user" of the AI spec §8.1 (`agent/src/routes/approvals.ts` `BROWSER_USER`,
  `id: 'browser'`).
- Bambuddy's own authentication. ScadBuddy keeps calling it with its API key.
- `/mcp`, whose auth is the AI spec's §8.3; and the agent's loopback-only routes
  (`/internal/flows/tools`, `/internal/durable/subagent-mcp` with its KEK-minted per-turn
  token), which stay as they are.
- A hostile pod with the KEK or the database URL. Either already reads every secret
  ScadBuddy keeps; no API credential changes that.

## 3. Every caller of the backend

| # | Caller | How it reaches the API | Today | After |
|---|---|---|---|---|
| C1 | The browser (the SPA, in a tab or Bambuddy's iframe) | Ingress, same origin | Nothing; `Origin` on writes and sockets | Session cookie (§5) |
| C2 | The agent sidecar | `SCADBUDDY_BACKEND_URL`, pod-local `localhost` (AI spec §4.1, §4.3); `openapi-fetch` (`agent/src/api/backend.ts`) | Nothing; author headers (`agent/src/tools/authorship.ts`) | Service credential `agent`, which may assert an author (§7.3) |
| C3 | The agent's headless browser (#349) | Loads the SPA from `SCADBUDDY_BACKEND_URL` (AI spec §5.3) | Nothing | A delegated session the agent mints for the session it runs in (§7.4) |
| C4 | The print worker (`python -m scadbuddy.worker --queue bambuddy`) | `SCADBUDDY_API_INTERNAL_URL`: `/api/v1/internal/outputs/…`, `/api/v1/internal/library/…` (`worker.py` `build_print_deps`, `RemoteOutputs`, `RemoteLibrary`) | Nothing | Service credential `print`, internal routes only |
| C5 | The projects worker and flow host functions | `SCADBUDDY_API_INTERNAL_URL`: the public routes behind `render`, `save_output`, `queue_print`, `arrange` (`workflows/flow_routes.py`), with an `Idempotency-Key` per call; also `flows/component.py`, `api/flows.py:1120` | Nothing | Service credential `projects`, those routes only, asserting the run's starter (§7.3) |
| C6 | The Rust slicer worker | Transfer URLs: presigned S3 or the internal blob routes with `X-ScadBuddy-Transfer` (`slicing/transfer.py`, `workers/rust/crates/sb-transfer`) | Per-request MAC token | Unchanged: the transfer token is the credential for those two routes |
| C7 | `bambuddy-fs` / the Go `scadbuddy-client` | From a workstation through the ingress (`tools/go/bambuddy-fs/README.md`); Kiota with `AnonymousAuthenticationProvider` (`internal/api/api.go:88`) | Nothing | A personal access token (§7.5) |
| C8 | The browser telemetry relay `POST /telemetry/v1/traces` | Same origin | `Origin` required, `Sec-Fetch-Site`, rate limits (`telemetry/admission.py`) | Also the session (§5.5) |
| C9 | The language-server sockets `/api/v1/models/{slug}/lsp`, `/api/v1/lsp` | Same origin | `refuse_foreign_origin` (`api/lsp.py:160,174`) | Also the session (§5.4) |
| C10 | The realtime socket `/api/v1/ws` | Same origin | `refuse_foreign_origin`; no `Origin` is "as trusted as a REST call" | Also the session (§5.4) |
| C11 | Probes and Prometheus: `/healthz`, `/metrics` | Kubelet, clusters' `scadbuddy-verify-deploy.yml`, the scraper | Nothing | Unchanged and open (outside `/api/v1`; §13 D9) |
| C12 | The agent's own routes `/api/v1/ai/*` (the ingress sends them to the agent, AI spec §4.2) | Same origin | Origin and transport (`routes/guard.ts`) | The same session, checked by the agent (§5.6) |

The render worker and agent-durable do not call the API: the render worker reads the
blob store and Postgres, and agent-durable reaches ScadBuddy's tools through the agent's
`agent-tools` activities (read in `worker.py` and `agent-durable/src`; no use of
`SCADBUDDY_BACKEND_URL` or `SCADBUDDY_API_INTERNAL_URL` in either).

## 4. Design in one paragraph

The backend becomes a confidential OIDC client and a **backend for frontend** (BFF): it
runs the authorization code flow with PKCE against the configured issuer, keeps no IdP
tokens in the browser, and gives the browser one opaque, `HttpOnly` session cookie whose
hash names a row in Postgres. One ASGI gate, `AuthGate`, in front of every `/api/v1`
route, resolves each request to a **caller**: a person (from the cookie), a service (from
a service credential), or nobody; and each route class says which callers it takes. The
agent reads the same session rows to know who is on its chat socket. Author headers are
believed only from a service credential that may assert an author. A mode, `off`,
`observe` or `enforce`, set in the environment, decides whether a refusal is logged or
answered; a break-glass command run in the pod mints a short sign-in link.

## 5. Sessions

### 5.1 The login flow

OpenID Connect authorization code flow ([OIDC Core 1.0 §3.1][oidc-core]) with PKCE
`S256` ([RFC 7636][rfc7636]), which the OAuth security BCP recommends for confidential
clients too ("For confidential clients, the use of PKCE is RECOMMENDED",
[RFC 9700 §2.1.1][rfc9700]):

1. `GET /api/v1/auth/login?return_to=<path>` (a same-origin path only; anything else is
   `/`). The backend reads the issuer's metadata ([OIDC Discovery][oidc-discovery]),
   makes `state`, `nonce` and a PKCE verifier, seals the three with `return_to` under a
   key derived from the KEK (HKDF, as `transfer_key` derives its own), sets them as the
   login cookie `__Host-scadbuddy_login` (`HttpOnly; Secure; Path=/; SameSite=Lax;
   Max-Age=600`), and redirects to the issuer's `authorization_endpoint` with
   `response_type=code`, `scope=openid` (plus `profile email` for a display name),
   `redirect_uri=<public URL>/api/v1/auth/callback`, `state`, `nonce`,
   `code_challenge`, `code_challenge_method=S256`.
   `SameSite=Lax`, not `Strict`: the callback is a top-level navigation from the IdP's
   site, on which a `Strict` cookie is not sent and a `Lax` one is
   ([RFC 6265bis §5.6.7.1][rfc6265bis]: `Lax` cookies go with a cross-site request "if
   and only if they are top-level navigations" with a safe method).
2. `GET /api/v1/auth/callback?code&state`. The login cookie must open, its `state` must
   equal the query's, and it is cleared whatever happens. The backend redeems the code
   at the `token_endpoint` with the verifier and its client secret
   (`client_secret_basic`), validates the ID token ([OIDC Core §3.1.3.7][oidc-core]:
   signature by the issuer's JWKS, `alg` on the same allowlist as `/mcp`'s
   (`agent/src/auth/oidc.ts` `SUPPORTED_ALGORITHMS`; `none` and HMAC never), `iss`,
   `aud` containing the client id, `azp` when present, `exp`/`iat` with 30 s leeway,
   `nonce`), checks the sign-in rule (§13 D3), writes the session row, sets the session
   cookie, and answers a small same-origin HTML page that navigates to `return_to`
   (so the next request is same-site and carries a `Strict` cookie) or, in a popup,
   tells its opener (§6.2).
3. `POST /api/v1/auth/logout` deletes the row and the cookie; when the issuer publishes
   an `end_session_endpoint` it answers its URL with `id_token_hint` for the page to
   navigate to ([RP-Initiated Logout 1.0][oidc-logout]) (§13 D7).
4. `GET /api/v1/auth/session` answers `{signed_in, user: {principal, name, email},
   mode, expires_at}` (or `signed_in: false`). It is the one route every mode serves
   to anyone, and the SPA's first call.

The issuer metadata and JWKS are fetched as the agent fetches them for `/mcp`: https only
(http for loopback), no redirects, bounded size and time, cached 10 minutes, an unknown
`kid` refetching at most every 30 s (AI spec §8.3 "Fetching"). An IdP on the LAN is
normal here, so the URL import's public-address rule does not apply.

No access or refresh token is kept: the backend calls nothing on the user's behalf, so
the ID token's job ends at the callback. A session therefore outlives a change at the
IdP until it expires (§13 D5).

### 5.2 The session

- **Cookie** `__Host-scadbuddy_session` = 32 random bytes, base64url. `HttpOnly`,
  `Secure`, `Path=/`, no `Domain` (the `__Host-` prefix requires the three and forbids
  `Domain`, [RFC 6265bis §4.1.3.2][rfc6265bis]), `SameSite=Strict`. This is the BFF
  draft's cookie ([draft-ietf-oauth-browser-based-apps-27 §6.1.3.2][bba]: `Secure` and
  `HttpOnly` MUST, `SameSite=Strict` SHOULD, path `/`, no `Domain`). `Strict` costs
  nothing here because the SPA's shell (`/`, `/assets/*`) is served without a session
  and every API call is a same-origin `fetch` the shell makes; only the document request
  of a navigation from another site lacks the cookie, and it needs none.
- **Row** `auth_sessions` (a new backend migration): `id_hash` (SHA-256 of the cookie,
  the key; the cookie itself is never stored, as MCP bearer tokens are stored hashed, AI
  spec §9), `principal` (`user:<issuer>#<sub>`, the shape of `/mcp`'s
  `oidc:<issuer>#<sub>`, `agent/src/auth/principal.ts`), `issuer`, `subject`, `name`,
  `email`, `kind` (`login`, `delegated` §7.4, `break_glass` §9.3), `created_at`,
  `last_seen_at`, `idle_expires_at`, `absolute_expires_at`, `revoked_at`, and for the
  audit only the client address (`core/proxies.py` `client_address`) and user agent.
- **Lifetime**: an idle timeout, slid at most once a minute so a busy page does not
  write on every request, and an absolute one (§13 D5). Expired and revoked rows go to a
  `housekeeping` sweep on the prune Schedule (a new entry in `SWEEPS`, never a loop in
  the API, per CLAUDE.md).
- **Revocation** writes `revoked_at` and publishes `auth.session_revoked` on the event bus,
  which every replica's sockets hear (§5.4). Settings gets a "Signed-in sessions" list
  with revoke, and "Sign out everywhere".
- **Replicas** share nothing but Postgres: any replica checks any cookie.

### 5.3 CSRF

A cookie is sent by the browser on its own, so the API becomes a CSRF target the day it
starts honouring one. Three layers, of which the first two are already the rule:

1. **`SameSite=Strict`.** Not enough alone: the BFF draft notes it protects only while no
   other application is same-site with the BFF ([§6.1.3.3.1][bba]). Here Bambuddy is
   (§6.1): `bambuddy.internal.nullreference.io` is same-site with ScadBuddy, as is any
   other `*.nullreference.io` host.
2. **The `Origin` allowlist on every unsafe method** (`CrossSiteGate`, #962), unchanged,
   except that a write with no `Origin` must now carry a service credential or a
   personal token (a browser always sends `Origin` on a non-GET `fetch`, which is why
   `CrossSiteGate` lets a missing one through today).
3. **A required request header** on every cookie-authenticated `/api/v1` request,
   `X-ScadBuddy-Request: 1`, which a cross-origin page can only send after a CORS
   preflight the API never answers ([§6.1.3.3.2][bba]: the BFF "SHOULD require a custom
   request header" and "MUST check" it). The SPA's one `fetch` wrapper
   (`frontend/src/api/client.ts`, `request`/`send`) and the template-UI host add it.
   Safe methods need it too, so a cross-site `<img>` or form cannot fetch a GET's side
   effects or time it; the exceptions are what the browser loads without script:
   `<img>`/`<video>` media and thumbnails, downloads (`lib/embed.ts` opens a popup and
   fetches into it), and the template UI's `import()` of `ui/**`. Those are GETs that
   change nothing; they pass on the cookie alone, and they are listed in one allowlist
   (`auth/routes.py` `SCRIPTLESS_GETS`), not decided per route.

### 5.4 Websockets

Browsers send cookies on the WebSocket handshake and apply no same-origin policy to it,
which is why the server must check `Origin` ([RFC 6455 §10.2][rfc6455]), as
`refuse_foreign_origin` does. After the origin check, the realtime socket, the language
servers' and the agent's chat and bridge sockets resolve the cookie on the handshake:
none, or one not live, closes the socket before accepting it (code 1008, as the origin
refusal does) in `enforce`, and is logged in `observe`. An open socket re-checks its
session when it hears `auth.session_revoked` and at the session's idle deadline, and
closes with 4401 (an application code, [RFC 6455 §7.4.2][rfc6455]) so the SPA knows to
sign in again rather than reconnect in a loop. A socket with no `Origin` is no longer "as
trusted as a REST call": it needs a service credential, which nothing uses on a socket
today.

### 5.5 The telemetry relay

`POST /telemetry/v1/traces` is not under `/api/v1` but is the page's: it takes the
session too. A page not yet signed in sends no spans (`lib/tracing.ts` starts after
`/auth/session` answers signed in), and the relay's own origin and rate checks stay.

### 5.6 The agent's routes

The ingress sends `/api/v1/ai/*` to the agent (AI spec §4.2), so the agent must check the
same cookie. It reads `auth_sessions` itself, through a view the backend's migration
grants the agent's role (`id_hash`, `principal`, `name`, the expiries, `revoked_at`;
nothing else), with the same SHA-256 lookup: one row read per request, cached a few
seconds per id, and dropped on `auth.session_revoked` (the agent already listens to the
backend's bus, AI spec §7). Its `routes/guard.ts` gains the session check before its
existing transport and origin rules. The "browser user" (`BROWSER_USER`, `id:
'browser'`) becomes the signed-in person: `kind: 'browser'` stays, `id` becomes the
session's `principal`. §8.3 covers the rows that say `browser` today.

## 6. The Bambuddy iframe

### 6.1 What the frame is

ScadBuddy is an External Link in Bambuddy, rendered in an iframe with `sandbox="allow-scripts
allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"` (verified in
Bambuddy's 1.2.5.5 bundle, CLAUDE.md "Bambuddy iframe facts"). `allow-same-origin` keeps
the frame at ScadBuddy's own origin, so its cookies are ScadBuddy's
([MDN, `<iframe>` `sandbox`][mdn-iframe]: without it the document "is treated as being from
a special origin that always fails the same-origin policy (potentially preventing access
to data storage/cookies …)").

Whether its cookies are **third-party** depends on *site*, not origin: a site is the
scheme plus the registrable domain ([HTML, "Sites"][html-site];
[MDN, third-party cookies][mdn-3pc]). The deployed hosts are
`bambuddy.internal.nullreference.io` and `scadbuddy.internal.nullreference.io` (README and
the specs). **Measured** 2026-11-01: of `io`, `nullreference.io` and
`internal.nullreference.io`, only `io` is on the Public Suffix List
(`curl -s https://publicsuffix.org/list/public_suffix_list.dat | grep -nE
'^(io|nullreference\.io|internal\.nullreference\.io|.*\.nullreference\.io)$'` → `1523:io`),
so both hosts are the site `https://nullreference.io` and the frame is **same-site** with
its top-level page. A request from a same-site frame under a same-site top level is a
same-site request, so `SameSite=Strict` cookies are sent ([RFC 6265bis §5.2.1][rfc6265bis]:
the site for cookies is the top-level origin only while every ancestor is same-site with it),
and no third-party-cookie blocking applies: Chrome does not block third-party cookies by
default, Firefox's Total Cookie Protection keys a jar per top-level site, and Safari's
tracking prevention blocks cross-site ones ([MDN][mdn-3pc]); none of them treats a
same-site frame as third-party. **(Assumption, to be checked by phase 7's e2e in all three
engines: that each engine sends the `Strict` cookie in this sandboxed same-site frame.)**

### 6.2 Signing in from inside the frame

The login itself cannot happen in the frame. An IdP's login page commonly refuses to be
framed (`X-Frame-Options` or CSP `frame-ancestors`) **(assumption; it depends on the IdP,
§13 D1)**, and its own session cookie would be third-party there whenever the IdP is on
another site. So:

- **In a top-level tab** the SPA navigates to `/api/v1/auth/login?return_to=…`.
- **In a frame** (the SPA already knows: `lib/embed.ts`), the sign-in screen's button opens
  a popup on `/api/v1/auth/login?popup=1`. The sandbox allows it (`allow-popups`) and lets
  the popup run unsandboxed (`allow-popups-to-escape-sandbox`, which "will allow … a
  new browsing context without forcing the sandboxing flags upon it", [MDN][mdn-iframe]),
  as the download popup already does (`lib/embed.ts`, CLAUDE.md). The popup is opened in the
  click handler, before any `await`, as the download popup is. It is a top-level browsing
  context at ScadBuddy's origin, so the flow of §5.1 runs there first-party; the
  callback page posts `{type: "scadbuddy:signed-in"}` to `window.opener`, targeted at the
  public origin, and closes. The frame then calls `GET /auth/session`, which now finds
  the cookie, because the frame is same-site (§6.1). If the message never comes (the
  opener was lost), the frame polls `/auth/session` while the popup is open and once
  `popup.closed`. A blocked popup is an error the user sees, never a fallback, as with
  downloads (`DownloadBlockedError`).

### 6.3 A cross-site embedding

A deployment where Bambuddy and ScadBuddy are on different sites (an IP address, two
domains) gets none of §6.1. A `Strict` or `Lax` cookie is not sent in a cross-site frame,
and a `SameSite=None` one is blocked or partitioned by Safari and Firefox
([MDN][mdn-3pc]). Three ways out, of which this design ships the first and leaves the
others to §13 D2:

1. **Open ScadBuddy in its own tab.** The sign-in screen in a cross-site frame says why
   and links out (`window.open`, as deep links already do). Always available.
2. **A partitioned frame cookie (CHIPS).** The popup's callback mints a one-time handoff
   code (60 s, single use, hashed in `auth_sessions`), posts it to the opener, and the
   frame `POST`s it to `/api/v1/auth/handoff`, whose answer sets
   `__Host-scadbuddy_frame=…; Secure; Path=/; SameSite=None; Partitioned` in the frame's
   own partition. A partitioned cookie is keyed by the top-level site as well as the
   host, "must be set with `Secure`", and may use `__Host-`
   ([MDN, CHIPS][mdn-chips]); MDN lists CHIPS as Baseline 2025. With `SameSite=None` the
   cookie no longer helps against CSRF, so §5.3's layers 2 and 3 carry it alone, which the
   BFF draft accepts ([§6.1.3.3.2][bba]). **(Assumption: that Safari and Firefox accept
   the `Partitioned` cookie in a sandboxed frame with `allow-same-origin`. Unmeasured.)**
3. **The Storage Access API.** Ruled out for Bambuddy: a sandboxed frame needs
   `allow-storage-access-by-user-activation` for `requestStorageAccess()` to succeed
   ([MDN, Using the Storage Access API][mdn-saa]), which Bambuddy's sandbox does not
   carry (§6.1), and ScadBuddy does not ask third-party projects for changes.

## 7. Service credentials

### 7.1 Shape

A service credential is an opaque bearer token, `sbsvc_<id>.<secret>` (32 random bytes),
sent as `Authorization: Bearer …` ([RFC 6750 §2.1][rfc6750]). The API keeps
`service_credentials` rows: `id`, `caller` (`agent`, `print`, `projects`), `scopes`,
`secret_hash` (SHA-256), `sealed_secret` (AES-256-GCM under a per-row data key sealed by
the KEK, the AI spec §9 envelope, AAD binding the row's `id`, `caller` and `scopes` so a
sealed value cannot be moved to a wider row), `created_at`, `expires_at`, `revoked_at`,
`last_used_at`. Verification is the hash; the sealed copy exists only so the caller can be
handed its token (§7.2) without a plaintext secret in Postgres or its backups.

| Caller | Scopes (route classes, §8.1) | May assert an author |
|---|---|---|
| `agent` | `api` (every public `/api/v1` route), `internal.workspace` | yes, any principal (§7.3) |
| `print` | `internal.outputs`, `internal.library` | no |
| `projects` | the flow host routes (`render`, `outputs` create, the print queue, `arrange`, `operations` reads) | yes, only the run's `started_by` (§7.3) |

The Rust slicer keeps its transfer tokens (C6), which are already a credential per request
and need no caller identity.

### 7.2 Issue and rotation

The API issues and rotates them; nobody pastes a secret into a manifest (§13 D4).

- At start, and on the prune Schedule, the API makes sure each caller has a live
  credential with more than 7 days left, minting the next one 30 days ahead. Two are live
  in the overlap, so a rotation never cuts a caller off mid-call; one past `expires_at`
  is refused.
- A caller reads its current token at start and every hour from a view that its own
  Postgres role alone may select (`service_credentials_<caller>`, granted as
  `worker_role.py` grants `RENDER_GRANTS` to `scadbuddy_render`, #601), and unseals it
  with the KEK. The caller's Postgres role is therefore the root of its identity: which
  it already is, since each of these callers holds a database URL that could write the
  rows the routes guard. This costs the print worker the KEK, which it has only
  optionally today (`worker.py:1237`).
- Settings shows each caller's credentials (created, expires, last used) with revoke and
  "rotate now"; revoking the live one mints a replacement at once. #891 (rotate the
  render worker's key) is answered by the same page.

### 7.3 Authorship from the session, not a header

Today `core/authorship.py` believes `X-ScadBuddy-Agent-Author`, `-Author-Session` and
`X-ScadBuddy-Agent-Tool` from anyone. After:

- **From a person's session** the author is the session (`current_author()` returns
  none; activity's `attribute_request` records the session's `principal` instead of
  `"browser"`; a flow run's `started_by` is the principal). An author header on such a
  request is refused with 400 in `enforce` and logged in `observe`: a person's page has
  no reason to send one.
- **From the `agent` credential** the headers are read as today, and they are the only
  way that credential names whom it acts for. The agent sets them from what it knows:
  the principal of the turn's owner (the signed-in person of §5.6 for a browser session,
  or the `/mcp` token or OIDC subject), never a value a client sent it.
- **From the `projects` credential** the asserted principal must be the run's
  `started_by` (read from `workflow_runs` by the run id the host call names), or the
  call is refused.
- **From any other caller** an author header is refused.

`_browser_only` (`api/flows.py:866`) changes from "no author header" to "the caller is a
person's session", which is what it means; the routes it guards (a flow's answers,
approvals and grants) become unreachable by any service credential.

### 7.4 The headless browser's session

The agent's headless Chromium (C3) loads the SPA, so it needs a cookie. The `agent`
credential may call `POST /api/v1/auth/delegated-sessions` with `{principal,
ttl_seconds ≤ 3600}`, naming the principal of the agent session the browser serves; the
answer is a `kind: delegated` session the agent sets in that Playwright context. A
delegated session is refused on the routes `_browser_only` guards and on the auth and
service-credential routes, so the headless browser can never approve what its own
session asked for (the AI spec §8.6 row on the headless browser).

### 7.5 Personal tokens for `bambuddy-fs` and the Go client

`bambuddy-fs` runs on a person's workstation and reaches the API through the ingress (C7).
It gets a **personal access token**: minted by a signed-in person in Settings ("Access
tokens"), shown once, stored hashed, scoped `read` or `write`, with an expiry, a name and
last use, revocable; the principal is the person who minted it. The Go client takes it as
`Authorization: Bearer` (Kiota's `AnonymousAuthenticationProvider` becomes an API-key
provider in `internal/api/api.go`), from `--token-file` or `$BAMBUDDY_FS_TOKEN_FILE`. The
OAuth device grant ([RFC 8628][rfc8628]) against the IdP is the alternative (§13 D6).

## 8. The gate

### 8.1 Route classes

`AuthGate`, a pure ASGI middleware placed inside `CrossSiteGate`, resolves the caller once
and puts it on the request scope; routes read it through a dependency (`CallerDep`).
Every mounted route falls in exactly one class, and a test fails for a route in none, as
`tests/api/test_openapi.py` fails for a route missing from the schema:

| Class | Routes | Callers |
|---|---|---|
| `public` | `/healthz`, `/metrics`, the SPA shell and assets, `GET /api/v1/auth/session`, `/auth/login`, `/auth/callback` | anyone |
| `api` | every other `/api/v1` route | a person's session, a delegated session, a personal token within its scope, the `agent` credential, the `projects` credential for its routes |
| `person` | `_browser_only` routes, `/auth/logout`, session and token management, service-credential management, login configuration | a person's `login` or `break_glass` session only |
| `internal.*` | `/api/v1/internal/outputs/…`, `/internal/library/…`, `/internal/workspace-blobs/…` | the service credential with that scope |
| `transfer` | `GET`/`PUT /api/v1/internal/blobs/{key}` | a valid `X-ScadBuddy-Transfer` token (unchanged) |

### 8.2 Answers

A missing or dead credential is 401 with a problem body
(`https://scadbuddy.dev/problems/auth/signed-out`) and `WWW-Authenticate: Bearer
realm="scadbuddy"` on service and token routes; a live one without the class is 403
(`…/auth/forbidden`). The SPA turns a 401 into its sign-in screen, keeping the page's
state, so an expired session mid-edit loses nothing.

### 8.3 Migrating what says `browser` today

The rows that record the single anonymous user (`principal = 'browser'` in `activity`,
`started_by` in `workflow_runs`, the agent's `ai_sessions.owner`/`creator`, approvals'
`decided_by`, the audit log's `UI_ACTOR`) keep their value. A signed-in person may act on
a row owned by `browser` as on their own (today's behaviour: everyone was `browser`), and
new rows record the person (§13 D8). Git trailers and history are never rewritten.

## 9. Rollout

### 9.1 Modes

`SCADBUDDY_AUTH_MODE` = `off` | `observe` | `enforce`, read by both services. An
environment variable, not a stored setting: like `SCADBUDDY_TRUSTED_PROXIES` and the KEK
(AI spec §9), it decides who may write Settings, so Settings cannot hold it.

- **`off`** (the default until phase 8): today's behaviour. Sign-in works if configured
  and records the person as author; nothing is refused for lacking a credential.
- **`observe`**: every request is classed and its caller resolved; what `enforce` would
  refuse is served, logged once per caller and reason a minute, and counted as
  `scadbuddy_auth_would_refuse_total{class, reason}` (declared in `core/metrics.py`, so
  `lint-dashboard.sh` lets the dashboard read it). Services present their credentials,
  so a missed caller shows up here.
- **`enforce`**: refused requests are refused. The API refuses to start in `enforce`
  without a usable login configuration (issuer reachable at start or cached, client id,
  client secret, a sign-in rule), saying which, so a deploy with a broken config fails
  its readiness instead of serving a page no one can sign in to.

The order on a deployment: configure the login in Settings (off), sign in and check, roll
the services that present credentials, switch to `observe`, watch the counter fall to
zero for every caller, switch to `enforce`.

### 9.2 Saving the login configuration

Saving the login settings with sign-in on runs discovery against the issuer first and
saves nothing if it fails, as `/mcp`'s OIDC section does (`McpOidcSettings.tsx`: "a typo
cannot lock every client out"). In `enforce` a save also needs a fresh sign-in under the
new settings in a popup before it takes effect (the old ones stay until it succeeds).

### 9.3 Break-glass

`kubectl exec deploy/scadbuddy -- python -m scadbuddy.auth.break_glass` prints a sign-in
URL with a one-time code (10 minutes, single use, hashed in `auth_sessions`); opening it
gives a `kind: break_glass` session with a 1-hour absolute expiry, a banner, and an
audit row. Exec access to the pod is the proof: whoever has it already has the database.
It works with the IdP down or the login misconfigured. The other way back is
`SCADBUDDY_AUTH_MODE=observe` and a rollout.

## 10. What changes for existing headers and flags

| Today | After |
|---|---|
| `X-ScadBuddy-Agent-Author`, `-Author-Session`, `-Agent-Tool` | Same names; believed only from the `agent` and `projects` credentials (§7.3) |
| `_browser_only`: no author header | The caller is a person's session |
| `CrossSiteGate`: no `Origin` passes | No `Origin` needs a service credential or token |
| Sockets: no `Origin` is trusted | Needs a credential |
| `agent/src/routes/guard.ts`: origin and transport | Plus the session |
| `mcp_oidc.client_id` in the agent's `ai_settings` | Moves to the backend's login settings (§13 D1); the MCP section shows it read-only |
| `X-ScadBuddy-Transfer` | Unchanged |
| README "LAN only … no authentication" | Rewritten at phase 8 |

## 11. Tests

- **A fake OIDC issuer**, in-repo (`e2e-stack/fake-oidc/`, beside `fake-printer/`): OIDC
  discovery, a JWKS with a rotating key, an authorize endpoint that signs in a configured
  test user with no form (or shows one, for the popup test), a token endpoint that checks
  PKCE and the client secret, an `end_session_endpoint`, and switches to misbehave (wrong
  `iss`, `aud`, `nonce`, expired, `alg: none`, HS256, unknown `kid`). The backend's tests
  run it in-process; `e2e-stack/compose.yaml` runs it as a container.
- **Backend** (`tests/auth/`): the callback's every refusal, the cookie attributes, idle
  and absolute expiry, revocation closing a socket on another replica (two apps on one
  schema), every route classed (§8.1), each mode's answers, the author rules of §7.3,
  credential rotation with overlap, the break-glass code used twice.
- **Agent** (`test/pg.test.ts`): the session view lookup and its cache drop on revocation;
  `guard.ts` with and without a session; author headers set from the turn's owner.
- **Frontend**: msw handlers for `/auth/session` in `src/mocks/features/auth.ts` (signed
  in by default, so the existing suites run unchanged), the sign-in screen, a 401
  mid-session keeping the page, popup blocked.
- **Playwright against the real stack** (`e2e/real-auth.spec.ts`, run by `e2e-stack/run.sh`
  with `--auth`): top-level sign-in and out; sign-in from inside a replica of Bambuddy's
  frame with the exact sandbox attribute (as `e2e/downloads.spec.ts` builds one) on two
  same-site hosts, in Chromium, Firefox and WebKit (this is where §6.1's assumption is
  measured); the same on two cross-site hosts showing the open-in-a-tab path; a socket
  closed by revocation; `observe` serving and counting; `enforce` refusing an
  unauthenticated `curl`; the headless browser rendering with a delegated session.

## 12. Phases (one PR each)

1. **This spec.**
2. **Sessions and login, mode `off`.** Backend `auth/` feature (component, migration for
   `auth_sessions`, login settings with the client secret sealed under the KEK), the four
   `/auth` routes, `GET /auth/session`, the SPA's sign-in screen and popup, the fake issuer
   and backend tests. Nothing refused yet; a signed-in person is recorded as author.
3. **The gate in `observe`.** `AuthGate`, route classes and their completeness test, the
   counter, the request header in the SPA, socket checks logging.
4. **Service credentials.** `service_credentials`, issue and rotation, the per-role views,
   the agent, print and projects callers presenting them; Settings page.
5. **Authorship from the session.** §7.3 and §8.3, `_browser_only`, the agent's browser
   user as the person, the agent's session lookup and `guard.ts`.
6. **Delegated sessions and personal tokens.** §7.4, §7.5, `bambuddy-fs` and the Go client
   (regenerated by the `freshness` job).
7. **Real-stack e2e** of §11 in three engines; records what §6 assumed, and amends this
   spec with the results.
8. **`enforce` on the deployment.** README "Deploying" and "Running it" rewritten,
   clusters' manifests set the mode, the break-glass command documented. Then the default
   for a new install becomes `enforce` with a first-run screen, or stays `off` (§13 D10).
9. *(Only if §13 D2 asks for it.)* The CHIPS handoff for cross-site embedding.

## 13. Decisions for the owner

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Where the login configuration lives, and whether login shares `/mcp`'s issuer | (a) the backend's stored settings, issuer defaulting to `mcp_oidc.issuer`, the client id moved there; (b) keep it in the agent's `ai_settings.mcp_oidc` and have the backend read it; (c) environment | **(a)**: the backend owns the login and the client secret (sealed under the KEK, as #602 seals the Bambuddy key); one issuer for both by default, with a separate one allowed. Also name the IdP the homelab uses, so phase 7 can check its framing and logout behaviour |
| D2 | Cross-site embedding | (a) support only same-site embedding and top-level tabs, link out otherwise; (b) also build the CHIPS handoff (phase 9) | **(a)**: the deployment is same-site (§6.1, measured); build (b) only if a cross-site install appears |
| D3 | Who may sign in | (a) anyone the IdP authenticates for the client; (b) a required rule: a list of subjects and/or a group claim value; (c) roles (admin/user) | **(b)**, required before `enforce` starts, so an IdP shared with other apps cannot sign in its other users; roles later if there is ever a second person |
| D4 | How services get credentials | (a) API-issued, rotated, hashed and sealed under the KEK, read from a per-role view (§7.2); (b) Kubernetes projected service-account tokens ([Kubernetes docs][k8s-sa]), verified by TokenReview; (c) static secrets in Kubernetes Secrets | **(a)**: no manifest secrets, automatic rotation, works under compose; costs the print worker the KEK. (b) is stronger in-cluster but ties the API to Kubernetes and needs a fallback for compose anyway |
| D5 | Session lifetime | idle / absolute; keep a refresh token to re-check the account | **14 days idle, 30 days absolute, no refresh token**: a homelab owner should not sign in daily; revocation in Settings covers a lost device. Shorter if ScadBuddy is ever reachable off the LAN |
| D6 | `bambuddy-fs` credentials | (a) personal access tokens; (b) the device grant against the IdP, the backend validating access tokens as `/mcp` does | **(a)** now, (b) only if the IdP is set up for it |
| D7 | Logout | (a) ScadBuddy's session only; (b) also RP-initiated logout at the IdP | **(a)** by default with (b) as a setting: signing out of ScadBuddy should not sign the owner out of every other app on the IdP |
| D8 | Rows that say `browser` today | (a) every signed-in person may act on them (§8.3); (b) assign them to the first person who signs in | **(a)**: there is one person today, and (b) guesses |
| D9 | `/metrics` | (a) stays open, as now; (b) behind a service credential for the scraper | **(a)**, outside `/api/v1`, reachable only in-cluster through the Service; it carries no parameter values (`core/tracing.py` rules apply to metrics labels too) |
| D10 | The default mode for a new install after phase 8 | (a) stays `off`; (b) `enforce` with a first-run screen that configures the login | **(a)** until the first-run screen exists, so `docker run` keeps working on a laptop; the README then says to set `enforce` |
| D11 | The CSRF request header on safe methods | (a) required on every cookie GET except the listed scriptless ones (§5.3); (b) only on unsafe methods | **(a)**: the list is short, and it closes cross-site GETs whose timing or side effects (a render's cache, a preview) leak |

[oidc-core]: https://openid.net/specs/openid-connect-core-1_0.html
[oidc-discovery]: https://openid.net/specs/openid-connect-discovery-1_0.html
[oidc-logout]: https://openid.net/specs/openid-connect-rpinitiated-1_0.html
[rfc7636]: https://www.rfc-editor.org/rfc/rfc7636
[rfc9700]: https://www.rfc-editor.org/rfc/rfc9700
[rfc6750]: https://www.rfc-editor.org/rfc/rfc6750
[rfc6455]: https://www.rfc-editor.org/rfc/rfc6455
[rfc8628]: https://www.rfc-editor.org/rfc/rfc8628
[rfc6265bis]: https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-rfc6265bis
[bba]: https://datatracker.ietf.org/doc/html/draft-ietf-oauth-browser-based-apps
[html-site]: https://html.spec.whatwg.org/multipage/browsers.html#sites
[mdn-iframe]: https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe
[mdn-3pc]: https://developer.mozilla.org/en-US/docs/Web/Privacy/Guides/Third-party_cookies
[mdn-chips]: https://developer.mozilla.org/en-US/docs/Web/Privacy/Guides/Privacy_sandbox/Partitioned_cookies
[mdn-saa]: https://developer.mozilla.org/en-US/docs/Web/API/Storage_Access_API/Using
[k8s-sa]: https://kubernetes.io/docs/concepts/security/service-accounts/
