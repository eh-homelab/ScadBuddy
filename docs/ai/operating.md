# Operating the agent sidecar

How to run and configure ScadBuddy's AI agent service (`agent/`) as it exists on
`main`. Every fact names the file and function it was read from. Spec references are
to [`docs/superpowers/specs/2026-09-27-ai-integration-design.md`](../superpowers/specs/2026-09-27-ai-integration-design.md).
The README's "The agent sidecar (AI, #261)" section in [`README.md`](../../README.md)
covers the same ground more briefly.

> **Status.** This repository has no cluster manifests; the ScadBuddy pod and its
> ingress live in eh-homelab/clusters. §1.1 is what that ingress must route for the
> assistant to appear. With the routes missing, the UI hides the assistant and says the
> agent is unreachable (`useAiAvailability()`,
> [`frontend/src/agent/chat/availability.ts`](../../frontend/src/agent/chat/availability.ts)).

## 1. Layout

- **A separate image.** The Dockerfile's `agent` stage (`--target agent`) builds
  `ghcr.io/eh-homelab/scadbuddy-agent`, published by the `agent` job in
  `.github/workflows/build-image.yml` (README, "The agent sidecar";
  [`Dockerfile`](../../Dockerfile)).
- **A second container in the ScadBuddy pod** (spec §4.1). The two containers share
  the pod network, so the agent reaches the backend on `http://127.0.0.1:8080`
  (spec §4.3; `DEFAULT_BACKEND_URL` in [`agent/src/config.ts`](../../agent/src/config.ts)).
- **Port 8081**, fixed. The comment on `PORT` in [`agent/src/main.ts`](../../agent/src/main.ts)
  calls it "part of the pod contract with the ingress (spec §4.2)". The Dockerfile
  `EXPOSE`s 8081.
- **Ingress routing (spec §4.2).** `/mcp` and `/api/v1/ai/*` go to the agent,
  and everything else, including `/api/v1/ws`, goes to the backend. `/api/v1/ai/*` is
  under the backend's `/api/v1/*`, so the agent's rules must take precedence
  (longest-prefix match or explicit priority). Details and a check are in §1.1.
- **Runtime user and filesystem.** The image runs as uid 10001 with
  `HOME=/var/lib/scadbuddy-agent` and `CLAUDE_CONFIG_DIR=/var/lib/scadbuddy-agent/claude`
  ([`Dockerfile`](../../Dockerfile), `agent` stage). It writes only under
  `/var/lib/scadbuddy-agent` (`DEFAULT_STATE_DIR`,
  [`agent/src/harness/options.ts`](../../agent/src/harness/options.ts)). Mount an
  `emptyDir` there and run the root filesystem read-only (spec §4.4). At start,
  `ensureStateDirs()` in
  [`agent/src/harness/stateDirs.ts`](../../agent/src/harness/stateDirs.ts) recreates
  `claude/`, `work/` and `plugins/` (the plugin package cache) and checks all three are
  writable. If it cannot, the process exits 1
  with a message naming the directory (`main.ts`).
- **Pinned Claude Code.** The build runs `node dist/check-cli-version.js "$CLAUDE_CODE_VERSION"`
  (currently `2.1.283`) and fails when the SDK's bundled binary differs
  ([`Dockerfile`](../../Dockerfile); [`agent/src/check-cli-version.ts`](../../agent/src/check-cli-version.ts)).
  The measurement behind this is in spec §3.1 ("Measured in PR #319").
- **Container healthcheck.** `HEALTHCHECK` fetches `http://127.0.0.1:8081/healthz`
  every 30 s with node's `fetch` ([`Dockerfile`](../../Dockerfile)).
- **Shutdown.** On `SIGTERM`/`SIGINT` the listener drains for up to 10 s, then the
  database pool closes. The exit code is non-zero if the drain timed out (`main.ts`;
  [`agent/src/shutdown.ts`](../../agent/src/shutdown.ts)).

### 1.1 Routing the UI's origin to the agent

Spec §4.2 decides this: **the ingress** splits one origin between the two containers,
and neither container proxies for the other. That is what makes the assistant work
inside Bambuddy's iframe. The SPA, the backend API, the agent's routes and the
assistant's WebSocket (`/api/v1/ai/chat`) are then all on ScadBuddy's one public
origin:

- Bambuddy frames ScadBuddy with `allow-same-origin` in its sandbox (CLAUDE.md,
  "Bambuddy iframe facts"), so the page keeps that origin.
- The browser sends it as `Origin` on every write and on the WebSocket handshake.
- That value is the one the agent's allowlist accepts (`SCADBUDDY_PUBLIC_URL`, §6).

A second origin for the agent would need CORS and a second allowlisted origin. A
backend passthrough is the other alternative, and the spec rejects it because a
uvicorn hop risks buffering the SSE and WebSocket streams.

What the ingress must do:

| Path | Service | Notes |
|---|---|---|
| `/api/v1/ai` (prefix) | agent `:8081` | REST, SSE (`/api/v1/ai/sessions/{id}/events`) and the WebSocket `/api/v1/ai/chat` |
| `/mcp` (prefix) | agent `:8081` | MCP Streamable HTTP (SSE) |
| `/` (prefix) | backend `:8080` | everything else, including the backend's WebSocket `/api/v1/ws` |

- **Precedence.** With a Kubernetes `Ingress`, the longest matching path wins,
  whatever order the rules are listed in, and on a tie `Exact` beats `Prefix`
  ([Ingress, "Multiple matches"](https://kubernetes.io/docs/concepts/services-networking/ingress/#multiple-matches)).
  Other routers (Traefik `IngressRoute`, Gateway API) have their own ordering; check
  it with the requests below instead of assuming.
- **WebSockets and SSE.** For ingress-nginx, "Support for websockets is provided by
  NGINX out of the box. No special configuration required." Its default
  `proxy-read-timeout` is 60 s
  ([ingress-nginx, WebSockets](https://kubernetes.github.io/ingress-nginx/user-guide/miscellaneous/#websockets)).
  The agent pings each chat socket every 25 s (`HEARTBEAT_MS`, `startHeartbeat`,
  [`agent/src/routes/chat.ts`](../../agent/src/routes/chat.ts)), and the session event
  stream sends a comment every 20 s with `X-Accel-Buffering: no` (`SSE_KEEPALIVE_MS`,
  [`agent/src/routes/sessions.ts`](../../agent/src/routes/sessions.ts)). Both
  therefore stay open under that default. The rule: both intervals must stay under
  the ingress's read and send timeouts. Lowering a timeout below 25 s, or raising
  either constant above it, closes idle assistant sockets and streams.
- **Forwarded headers.** The ingress must append `X-Forwarded-Proto` and
  `X-Forwarded-Host`, and its pod range goes in `SCADBUDDY_AGENT_TRUSTED_PROXIES`
  (§6). Otherwise every chat and session write is refused with 403.

An `Ingress` for ingress-nginx, as an example. It is not the clusters manifest, whose
names and TLS settings are its own:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: scadbuddy
  namespace: scadbuddy
spec:
  ingressClassName: nginx
  tls:
    - hosts: [scadbuddy.example]
      secretName: scadbuddy-tls
  rules:
    - host: scadbuddy.example
      http:
        paths:
          - path: /api/v1/ai
            pathType: Prefix
            backend: { service: { name: scadbuddy, port: { name: agent } } }     # 8081
          - path: /mcp
            pathType: Prefix
            backend: { service: { name: scadbuddy, port: { name: agent } } }     # 8081
          - path: /
            pathType: Prefix
            backend: { service: { name: scadbuddy, port: { name: http } } }      # 8080
```

**Checking it.** Every agent response carries `X-ScadBuddy-Service: agent`
(`createApp()`, [`agent/src/app.ts`](../../agent/src/app.ts)); the backend's
responses do not. One request per agent path, through the public URL:

```bash
base=https://scadbuddy.example
curl -sSI "$base/api/v1/ai/status" | grep -i '^x-scadbuddy-service: agent'   # agent
curl -sS  "$base/api/v1/ai/status"                                           # {"available":…,"state":…,"ai":…}
curl -sSI -X POST "$base/mcp" | grep -i '^x-scadbuddy-service: agent'        # agent (401/403/503 is fine)
curl -sSI "$base/api/v1/settings" | grep -ci '^x-scadbuddy-service'          # 0: the backend
```

The same checks as a Playwright spec are `frontend/e2e/real-agent.spec.ts`, added in
#533, run with `E2E_BASE_URL` pointing at the deployment and
`E2E_AGENT=1`. With the frontend's dev or
preview server, `frontend/vite.config.ts` routes the same way on one local origin
(`SCADBUDDY_BACKEND_URL` and `SCADBUDDY_AGENT_URL` override the two targets).

## 2. Environment variables

`ENV_VARS` in [`agent/src/config.ts`](../../agent/src/config.ts) lists the only
variables the service reads, and `agent/test/config.test.ts` asserts that nothing else
is read. Spec §9 explains the rule. There are no AI *configuration* variables:
credentials and AI settings live in the database. The ones below are infrastructure,
and Settings cannot hold them. `loadConfig()` treats an empty or whitespace-only value
as unset (`present()`).

| Variable | Default | Read by | Meaning |
|---|---|---|---|
| `SCADBUDDY_DATABASE_URL` | unset → AI disabled | `loadConfig()` | Postgres URL, shared with the backend (#241). It must be `postgres://` or `postgresql://`; anything else is a `ConfigError` at start. The URL is not echoed in the error because it normally carries the password. With it unset the service still runs, and `/healthz` reports `"ai": "disabled (no database)"`. |
| `SCADBUDDY_BACKEND_URL` | `http://127.0.0.1:8080` | `loadConfig()` | Where the backend listens. Must be `http(s)`. Trailing slashes are stripped. `/healthz` probes `GET {url}/healthz` with a 2 s timeout (`backendReachable()`, [`agent/src/api/backend.ts`](../../agent/src/api/backend.ts)). |
| `SCADBUDDY_SECRET_KEY_FILE` | unset → credentials cannot be saved | `loadKek()` in [`agent/src/secrets.ts`](../../agent/src/secrets.ts) | Path of the key-encryption key (KEK) file. See §3. |
| `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` | unset | `main.ts`, `loadKek()` | The old KEK during a rotation. See §3.3. |
| `SCADBUDDY_PUBLIC_URL` | unset → loopback only | `originPolicy()` in [`agent/src/http/origins.ts`](../../agent/src/http/origins.ts) | The UI's public URL. This is the same variable the backend reads (`public_url` in `backend/scadbuddy/core/settings.py`). Its origin is the only non-loopback origin allowed to write. Must be `http(s)`. See §6. |
| `SCADBUDDY_AGENT_TRUSTED_PROXIES` | unset → no proxy trusted | `parseCidrList()` in `origins.ts` | A comma-separated list of CIDRs or bare addresses whose `X-Forwarded-Proto`/`X-Forwarded-Host` are believed. A malformed entry is a `ConfigError` at start. See §6. |

`main.ts` logs one line at start naming the backend URL, whether a database is
configured, and where credential writes are accepted from.

Variables set in the image, not read by `config.ts`: `HOME`, `CLAUDE_CONFIG_DIR`,
`CLAUDE_CODE_VERSION` and `NODE_ENV` ([`Dockerfile`](../../Dockerfile)). The Claude Code
subprocess never inherits the service's environment. `buildQueryOptions()` in
[`agent/src/harness/options.ts`](../../agent/src/harness/options.ts) passes an explicit
`env` holding only `CLAUDE_CONFIG_DIR`, `HOME` and `PATH`. The pinned SDK's `sdk.d.ts`
says `env` "REPLACES the subprocess environment entirely" (quoted in that file).

## 3. The key-encryption key

The Claude credential is stored with envelope encryption (spec §9; see
[security.md](security.md#envelope-encryption-and-aad-binding)). The KEK is a file.

### 3.1 Generating it

The format comes from the header comment of
[`agent/src/secrets.ts`](../../agent/src/secrets.ts) and `kekFromBase64()`. The file
holds exactly 32 random bytes, base64-encoded with standard padding (44 characters).
Surrounding whitespace, including a trailing newline, is ignored. Hex, passphrases and
other lengths are refused rather than stretched into a key.

```bash
openssl rand -base64 32 > scadbuddy-secret.key
kubectl -n scadbuddy create secret generic scadbuddy-agent-kek \
  --from-file=secret.key=scadbuddy-secret.key
# container: volumeMount at /etc/scadbuddy/kek, readOnly;
# env SCADBUDDY_SECRET_KEY_FILE=/etc/scadbuddy/kek/secret.key
```

(The commands are the README's, "The agent sidecar".)

### 3.2 What happens without it

- `main.ts` reads the file **once at start**. Changing it means restarting the pod.
- A missing, unreadable or malformed file is not fatal. `loadKek()` never throws. It
  returns `{ ok: false, reason, detail }`. `reason` names the variable only, never the
  path or errno, and is what `/healthz` and `GET /api/v1/ai/credentials` show.
  `detail` goes to the start-up log only.
- Saving a credential then fails with `503`, "no key-encryption key is configured"
  (`planPut()` in [`agent/src/credentials.ts`](../../agent/src/credentials.ts)).
  `/healthz` reports `"ai": "disabled (no key-encryption key: …)"` (`aiStatus()` in
  [`agent/src/app.ts`](../../agent/src/app.ts)).
- **Keep a copy.** Each stored row records the KEK's id (the first 16 hex characters of
  SHA-256 over the key; `kekFromBase64()`). A credential sealed under a lost key cannot
  be opened. `/healthz` then reports "stored credential was sealed with a different
  key-encryption key", and the credential must be entered again.

### 3.3 Rotating it

Spec §9: "Rotating it re-wraps the data keys only." The procedure comes from the
comment above `previousKek` in `main.ts` and from `CredentialStore.rewrapFrom()` in
`credentials.ts`:

1. Generate a new key file (§3.1).
2. Mount the **new** key as `SCADBUDDY_SECRET_KEY_FILE` and the **old** one as
   `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE`, then restart.
3. Once migrations have applied (the `afterMigrate` hook of `connectDatabase()`),
   every row whose `kek_id` is the old key's has its data key re-sealed under the new
   key. The sealed secret and `updated_at` are not touched. The `UPDATE` is
   conditional on `kek_id`, so two pods rotating at once do not overwrite each other.
   The log line reads `secret key rotation: re-wrapped N credential(s) from key <old> to <new>`.
4. A row the old key cannot open (altered, or in the v1 format) is left as it is and
   counted as `failed` in the same log line.
5. Remove the previous-key mount and restart again.

If the previous file cannot be loaded, the log says so and nothing is re-wrapped
(`main.ts`).

## 4. Setting up the Claude credential

There is one credential row (`id = 'default'`, `ai_credentials`). It is of one of two
kinds (`CREDENTIAL_KINDS`, [`agent/src/credentials.ts`](../../agent/src/credentials.ts);
spec D2):

- `anthropic_api_key`: passed to Claude Code as `ANTHROPIC_API_KEY`.
- `gateway`: a `base_url` plus a token, passed as `ANTHROPIC_BASE_URL` and
  `ANTHROPIC_AUTH_TOKEN`.

The mapping is in `credentialEnv()` in [`agent/src/harness/run.ts`](../../agent/src/harness/run.ts).
Its sources are the [LLM gateway docs](https://code.claude.com/docs/en/llm-gateway-connect)
("`ANTHROPIC_AUTH_TOKEN` in `Authorization: Bearer`, `ANTHROPIC_API_KEY` in
`x-api-key`"), quoted in that file. The Agent SDK does not allow claude.ai
subscription login for third-party products ([Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview),
quoted in spec §3.1).

The routes are in `registerCredentialRoutes()` in
[`agent/src/routes/credentials.ts`](../../agent/src/routes/credentials.ts). Error
bodies are `{ "detail": "…" }`. There is no Settings UI for them on `main` yet, so
these are the interface.

| Route | Guarded | What it does |
|---|---|---|
| `GET /api/v1/ai/credentials` | No | Returns `configured`, `kind`, `base_url`, `last4`, `updated_at`, `usable`, `can_save` and `cannot_save_reason` (`view()`). It never returns the secret. `last4` is empty for a secret shorter than 12 characters (`last4()`, `secrets.ts`). |
| `PUT /api/v1/ai/credentials` | Yes | Body `{ kind, base_url?, secret? }`, strict (`PutBody`). A `gateway` needs `base_url`, and `anthropic_api_key` must not have one. `base_url` must be http(s), with no userinfo, query or fragment. It is normalised without a trailing slash (`normaliseBaseUrl()`). A gateway host is checked against the egress rules first (§5 of [security.md](security.md#egress-check-on-gateway-urls)). The secret must not contain whitespace. **Omitting `secret` keeps the stored one only if `kind` and `base_url` are unchanged**; otherwise the route answers `409` (`planPut()`). |
| `DELETE /api/v1/ai/credentials` | Yes | Deletes the row. |
| `POST /api/v1/ai/credentials/test` | Yes | Runs a one-turn query (`maxTurns: 1`, `maxBudgetUsd: 0.05`, 60 s timeout, prompt `Reply with the single word: ok`) and returns `{ ok, detail, duration_ms, model }` (`testConnection()`, [`agent/src/harness/testConnection.ts`](../../agent/src/harness/testConnection.ts)). Only one test runs at a time, with at most one per 10 s (`DEFAULT_TEST_COOLDOWN_MS`); otherwise it answers `429` with `Retry-After`. The first `api_retry` is treated as the verdict, so a bad key fails fast. The model comes from `ai_settings.model` when set (`main.ts`). A gateway host is re-checked at test time. |

"Guarded" means the request must pass `uiRequestProblem()` in
[`agent/src/routes/guard.ts`](../../agent/src/routes/guard.ts) or it gets `403` (§6).
Every route answers `503` while there is no database, or while migrations have not
applied (`store()` in `routes/credentials.ts`).

Example, from a loopback shell on the pod (for instance through `kubectl port-forward`):

```bash
curl -sS -X PUT http://localhost:8081/api/v1/ai/credentials \
  -H 'Origin: http://localhost:8081' -H 'Content-Type: application/json' \
  -d '{"kind":"anthropic_api_key","secret":"sk-ant-…"}'
curl -sS -X POST http://localhost:8081/api/v1/ai/credentials/test -H 'Origin: http://localhost:8081'
```

This works because the loopback pair is always accepted from a loopback peer
(`checkOrigin()`, `origins.ts`). Through the ingress, the browser's own `Origin` must
match `SCADBUDDY_PUBLIC_URL`.

### 4.1 MCP access tokens

An outside MCP client authenticates to `/mcp` with a bearer token (`bearer` auth mode,
spec §8.3). Tokens are managed in Settings → **MCP access tokens**, which calls the
routes in `registerMcpTokenRoutes()` in
[`agent/src/routes/mcpTokens.ts`](../../agent/src/routes/mcpTokens.ts). Tokens are
stored in `ai_mcp_tokens` (§7). The Settings section renders only where
`useAiAvailability()` ([`frontend/src/agent/chat/availability.ts`](../../frontend/src/agent/chat/availability.ts))
reports AI available, which today is the msw-mocked build: nothing routes
`/api/v1/ai/*` to the sidecar yet. Until then, the routes below are the interface.

| Route | Guarded | What it does |
|---|---|---|
| `GET /api/v1/ai/mcp-tokens` | Read guard | Returns `{ auth_mode, tokens }`. Tokens are newest first by `created_at` (two minted in the same microsecond come in no fixed order), each with `id`, `name`, `tier`, `created_at`, `expires_at`, `last_used_at`, `revoked_at`, `status` (`active`, `expired` or `revoked`) and `approval_grant`. It never returns the token or its hash. `auth_mode` is `null` when the auth settings cannot be read. |
| `POST /api/v1/ai/mcp-tokens` | Yes | Body `{ name, tier, expires_in?, approval_grant? }`, strict. `approval_grant: true` lets the token decide other agents' outward approvals (#300, [agent-sessions.md §3](agent-sessions.md#3-approvals-by-another-agent-the-per-token-grant)); only with `tier: "outward"`, else `400`. `name` is 1–100 characters after trimming, with no control characters. `tier` is `read`, `write` or `outward`. `expires_in` is whole seconds from now, 60 to ten years; leave it out for a token that never expires. Answers `201` with `{ token, record }` and `Cache-Control: no-store`. **`token` appears here only.** Answers `415` for a body that is not `application/json`. |
| `DELETE /api/v1/ai/mcp-tokens/:id` | Yes | Revokes the token: `204`. Answers `404` for an unknown id or one already revoked. The row stays, so the list shows when it was revoked. |

"Read guard" is `uiReadProblem()` in
[`agent/src/routes/guard.ts`](../../agent/src/routes/guard.ts). It requires HTTPS
transport as in §6. When an `Origin` is sent, it must be allowed. Otherwise the
request's own origin must be the public URL, or the loopback pair from a loopback peer,
and a `Sec-Fetch-Site` other than `same-origin` or `none` is refused. Every route
answers `503` while there is no database, or while migrations have not applied.

Tokens can be managed in every auth mode. In `disabled` mode `/mcp` ignores them, and
Settings warns about that; they take effect again when the mode returns to `bearer`.
The mode is not stored yet: `main.ts` passes `DEFAULT_MCP_AUTH` (`bearer`).

To give a client a token, create one with a name that says where it will live, copy it
from the panel, and paste it into the client's MCP configuration as
`Authorization: Bearer sbmcp_…`. Choose **Done** once it is saved. The token cannot be
shown again; if it is lost, revoke it and create another. Inside Bambuddy's iframe, the
browser may refuse the Clipboard API (the frame has no `allow="clipboard-write"`).
**Copy token** then falls back to `document.execCommand('copy')`. If that fails too,
the token is left selected for Ctrl+C
([`frontend/src/lib/clipboard.ts`](../../frontend/src/lib/clipboard.ts)).

From a loopback shell on the pod:

```bash
curl -sS -X POST http://localhost:8081/api/v1/ai/mcp-tokens \
  -H 'Origin: http://localhost:8081' -H 'Content-Type: application/json' \
  -d '{"name":"ops laptop","tier":"read","expires_in":2592000}'
curl -sS http://localhost:8081/api/v1/ai/mcp-tokens
curl -sS -X DELETE http://localhost:8081/api/v1/ai/mcp-tokens/<id> -H 'Origin: http://localhost:8081'
```

## 5. Health

`GET /healthz` is in `createApp()`, [`agent/src/app.ts`](../../agent/src/app.ts).

- **It is liveness, and always `200`** while the process serves HTTP. A missing or
  unreachable database or backend is *reported*, not failed on, so a Postgres blip
  does not restart the container (comment in `createApp()`).
- The body is `{ status: "ok", ai, database, backend, secret_key, credential }` (type `Health`).
- `ai` is `"enabled"` only when every prerequisite holds. Otherwise it names the
  **first** one missing, in the order an operator has to fix them (`aiStatus()`):

  | `ai` value | Meaning |
  |---|---|
  | `disabled (no database)` | `SCADBUDDY_DATABASE_URL` unset |
  | `unavailable (database unreachable)` | Ping failed, or the credential read threw |
  | `unavailable (database migrations failed)` | `ready()` returned false |
  | `unavailable (database timed out)` | `ready()` or the credential read took longer than 2 s (`DEFAULT_HEALTH_TIMEOUT_MS`) |
  | `disabled (no key-encryption key: <reason>)` | See §3.2 |
  | `disabled (no Claude credential)` | No row saved |
  | `unavailable (stored credential was sealed with a different key-encryption key)` | The row's `kek_id` does not match the mounted key |
  | `unavailable (stored credential is in an outdated format; save it again)` | A #354-era v1 row (see [security.md](security.md#envelope-encryption-and-aad-binding)) |

- Keep these strings stable. The CI smoke test asserts
  `.ai == "disabled (no database)"` on the image
  ([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)). That test is the only
  reader of `ai` today; no backend or frontend code on `main` reads it (the `AiStatus`
  comment in `agent/src/app.ts`).
- **Migrations** (one file each in `agent/src/db/migrations/`, applied by
  `agent/src/db/migrations.ts`) run in the background at start
  (`void database?.ready()` in `main.ts`). They run under a transaction-scoped advisory
  lock with `lock_timeout` 10 s and `statement_timeout` 60 s
  (`DEFAULT_LOCK_TIMEOUT_MS`, `DEFAULT_STATEMENT_TIMEOUT_MS`). A failure is retried on
  the next `ready()` call. An already-applied migration whose SQL was edited fails its
  recorded sha256 check (`MigrationChecksumError`), and `main.ts` exits 1 on it. So
  does a ledger still keyed by position (before #491) that holds a row main never had
  (`MigrationLedgerError`): that database ran an unmerged branch's migration.

## 6. Origin allowlist and trusted proxies

The guarded routes (§4) accept a request only when all of the following hold
(`uiRequestProblem()`, [`agent/src/routes/guard.ts`](../../agent/src/routes/guard.ts);
`checkOrigin()` and `isSecureTransport()`,
[`agent/src/http/origins.ts`](../../agent/src/http/origins.ts); spec §8.4):

1. **HTTPS transport.** Either the peer is in `SCADBUDDY_AGENT_TRUSTED_PROXIES` and its
   last `X-Forwarded-Proto` value is `https`, or the peer is loopback and no proxy is
   involved. From any other peer, forwarded headers are ignored and the request counts
   as plain HTTP.
2. **Origin allowlist.** `Origin` and the request's own origin must be **equal**, and
   both must be allowed. The request's own origin is built from the scheme plus the
   trusted proxy's last `X-Forwarded-Host`, or else `Host`. The allowed origins are:
   - the origin of `SCADBUDDY_PUBLIC_URL`; or
   - the loopback pair: host `localhost`, `127.0.0.1` or `[::1]` from a loopback peer,
     not through a proxy.

   Default ports are normalised through WHATWG `URL.origin`, so `https://x:443` equals
   `https://x`.
3. **`Content-Type: application/json`** on `PUT`.

A request without `Origin` is refused. The guard's comment gives the reason: browsers
send `Origin` on every POST, PUT and DELETE, so a write without one did not come from a
page.

**What to set in a deployment** (from the PR #379 body, "Configuration surface"):

- `SCADBUDDY_PUBLIC_URL`: the `https://` URL users open.
- `SCADBUDDY_AGENT_TRUSTED_PROXIES`: the ingress controller's pod CIDR, for example
  `10.42.0.0/16`.

Without them, credential writes are accepted from loopback only. When several
forwarded values are present, the **last** one is used: the one the nearest proxy
appended (`lastValue()`, `origins.ts`). The proxy must therefore append to these
headers rather than pass the client's through unchanged. This is **unverified** for any
particular ingress controller.

Why an allowlist rather than "Origin equals Host" is explained in
[security.md](security.md#dns-rebinding-defence). The backend's `/api/v1/ws` applies
the same rule to the stored public URL (`origin_allowed()` in
[`backend/scadbuddy/api/realtime.py`](../../backend/scadbuddy/api/realtime.py)).

## 6a. MCP sign-in with OIDC

Issue #262. With OIDC on, MCP clients such as Claude Code or Claude Desktop sign in
through the homelab IdP instead of being given a pasted `sbmcp_` token. Bearer tokens
keep working. The design and the specifications followed are in spec §8.3 (`oidc`) and
[security.md](security.md#mcp-oidc-access-tokens).

**Prerequisites.**

- `SCADBUDDY_PUBLIC_URL` is set (§2). The resource URI is its origin plus `/mcp`, e.g.
  `https://scadbuddy.example/mcp`; tokens must name it as their audience.
- The ingress sends `/.well-known/oauth-protected-resource` and
  `/.well-known/oauth-protected-resource/mcp` to the agent, besides `/mcp` (spec §4.2).
  Clients find the IdP through that document; a 401 names its URL.
- The IdP is reachable from the agent over https at an address that is not link-local
  or a cloud metadata service (the same rule as gateway URLs, §4; enforced on the
  connection, see security.md).

**In the IdP.**

1. Create three scopes (or reuse existing ones and rename them in Settings):
   `scadbuddy:read`, `scadbuddy:write`, `scadbuddy:outward`. Grant each user the
   ones they should have. A group claim works too: set "Also read tiers from claim" to
   `groups` and use group names as the three values.
2. Make access tokens JWTs whose `aud` is the resource URI. The MCP client sends it as
   the RFC 8707 `resource` parameter; IdPs that ignore that parameter need an audience
   mapper (Keycloak: an "Audience" protocol mapper; other IdPs have their own
   setting, not verified here). If the IdP cannot put a URL in `aud`, set **Audience** in Settings to
   what it does write, understanding that any client of that audience is then accepted.
   Tokens must then be RFC 9068 access tokens (header `typ` `at+jwt`, or a `client_id`
   claim), so the IdP's ID tokens for that client are not accepted.
3. Keep access-token lifetimes short (minutes): a JWT stays valid until `exp`.

**Client registration.** MCP clients register in one of two ways
([MCP authorization spec, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization),
"Client Registration Approaches"):

- **Dynamic client registration** (RFC 7591), when the IdP's metadata has a
  `registration_endpoint`. The Settings "Test discovery" result says whether it does.
  Clients then need nothing but the `/mcp` URL. Restrict what registered clients may
  request in the IdP, since anyone can register.
- **Pre-registered clients** otherwise: create a public client (PKCE, no secret) per
  MCP client with its redirect URI (Claude Code uses a loopback redirect), and give the
  client ID to the MCP client's configuration.

**In ScadBuddy.** Settings → "MCP sign-in (OIDC)" (the section appears where the agent
service is available), or the API it uses:

```bash
# see the defaults, the resource URI and the metadata URL
curl https://scadbuddy.example/api/v1/ai/mcp/oidc
# check an issuer without saving anything
curl -X POST -H 'content-type: application/json' -H 'origin: https://scadbuddy.example' \
  -d '{"issuer":"https://auth.example/application/o/scadbuddy/"}' \
  https://scadbuddy.example/api/v1/ai/mcp/oidc/test
```

Writes pass the same UI gate as credential writes (§6), so a `curl` needs the public
`Origin` and must come through the ingress. `PUT` takes the whole configuration:
`enabled`, `issuer` (exactly as the IdP writes `iss`, trailing slash included),
`audience` (null for the resource URI), `client_id` (kept for a future in-app login;
not used to check `/mcp` tokens), `scopes` `{read, write, outward}`, `tier_claim`
(null or a claim name) and `algorithms` (from `RS256 RS384 RS512 PS256 PS384 PS512
ES256 ES384 ES512 EdDSA`; default `RS256`, `ES256`). With `enabled: true` the discovery
check runs first, and on failure nothing is saved (`400`, naming the reason).

**Checking it.** `curl -i -X POST https://scadbuddy.example/mcp` should answer `401`
with `WWW-Authenticate: Bearer realm="scadbuddy", resource_metadata="…"`, and that URL
should return a document whose `authorization_servers` is the issuer. Common failures:

| Symptom | Cause |
|---|---|
| `401 … error_description="the token was issued for another audience (resource)"` | the IdP did not put the resource URI in `aud` (step 2) |
| `401 … "the token was issued by another issuer"` | the issuer in Settings differs from `iss` (often the trailing slash) |
| `401 … "signed with PS256, which is not allowed"` | add the IdP's algorithm in Settings |
| `403 … error="insufficient_scope"` | the user or client was granted none of the three scopes |
| `503` with `Retry-After: 30` | the agent cannot reach the IdP's metadata or JWKS |
| metadata URL answers 404 | OIDC is off, `SCADBUDDY_PUBLIC_URL` is unset, or the ingress does not route `/.well-known/oauth-protected-resource` to the agent |

A stored configuration that no longer parses (a hand-edited row) reads as "off" and is
logged as `mcp auth: ai_settings.mcp_oidc is not a valid OIDC configuration`.

## 7. Database tables

The agent owns and migrates its `ai_*` tables (spec §9;
[`agent/src/db/migrations/`](../../agent/src/db/migrations/), applied by
[`agent/src/db/migrations.ts`](../../agent/src/db/migrations.ts)):

- `ai_migrations`: the ledger, one row per applied file (its id is the file name
  without `.sql`), with a checksum each. `version` and `story` are set only for the
  two files that predate #491, so an older image can still read the ledger.
- `ai_credentials`: the sealed credential.
- `ai_settings`: non-secret key/value settings. `SettingsStore` in `credentials.ts`.
  The keys read today are `model` (`main.ts`); `session_max_turns` and
  `session_max_budget_usd` (`SETTING_SESSION_MAX_TURNS` and
  `SETTING_SESSION_BUDGET_USD` in [`agent/src/sessions/manager.ts`](../../agent/src/sessions/manager.ts));
  `approval_expiry_seconds` (`SETTING_APPROVAL_EXPIRY_SECONDS` in
  [`agent/src/approvals/service.ts`](../../agent/src/approvals/service.ts));
  `mcp_auth_mode` and `mcp_anonymous_cap` ([§10](#10-mcp-auth-mode)); and `mcp_oidc`, the
  OIDC configuration for `/mcp` (#262; see [§6a](#6a-mcp-sign-in-with-oidc)), which
  `PUT /api/v1/ai/mcp/oidc` writes.
  No route writes them yet.
- `ai_sessions`, `ai_session_entries` and `ai_session_events`: sessions (#377,
  `20260928T0107Z_sessions.sql`). `main.ts` builds the `SessionManager` and serves it
  through the chat socket, the session routes and the `sessions_*` tools
  ([agent-sessions.md](agent-sessions.md)). Every event-log append is also announced
  as `session.*` on `scadbuddy_events` (NOTIFY only, no table).
- `ai_approvals`: approvals of outward calls, from session turns and from `/mcp`
  prepares (#471, `20260928T0734Z_approvals.sql`; see
  [security.md](security.md#mcp-prepareconfirm-on-the-approval-store)).
- `ai_mcp_tokens`: MCP bearer tokens (#251, `20260928T0734Z_mcp_tokens.sql`), one row per token with its
  name, tier, `created_at`, `expires_at`, `revoked_at`, `last_used_at` and
  `approval_grant` (#300, `20260929T0249Z_mcp_token_approval_grant.sql`: off by default,
  and a `CHECK` allows it only on an `outward` token). Only the
  SHA-256 of the token is stored (`token_hash`, 64 hex characters, enforced by a
  `CHECK`); the plaintext is shown once when minted. `PostgresTokenStore` in
  [`agent/src/auth/tokens.ts`](../../agent/src/auth/tokens.ts). There is no file or
  in-memory store: without `SCADBUDDY_DATABASE_URL`, `/mcp` and the token routes
  (§4.1) answer 503. Settings writes this table through §4.1's routes.

- `ai_plugin_packages`: installed Claude plugin packages (#297,
  `20260928T0750Z_plugin_packages.sql`): the source, the pinned commit, the content
  hash, the review, the approval and the enabled flag. See §9.

The migration advisory lock key is "SCADAGNT", distinct from the backend's "SCADBDDY"
(the comment on `MIGRATION_LOCK` in `migrations.ts`).

## 8. Per-query limits

Every harness query gets `maxTurns` (default 25) and `maxBudgetUsd` (default 1 USD)
(`DEFAULT_MAX_TURNS` and `DEFAULT_MAX_BUDGET_USD` in
[`agent/src/harness/run.ts`](../../agent/src/harness/run.ts)). The comment there calls
them "placeholders until Settings stores per-session caps". Sessions read their caps
from `ai_settings` when they start, and spend the budget across the whole session (PR
#377 body, "Budget and turns").

## 9. Plugin packages (#297)

A plugin package is a Claude plugin (skills, subagents, hooks, `.mcp.json`) fetched from
a git repository, or from an entry of a marketplace repository, at a pinned commit. The
Agent SDK loads plugins by local path only: "To use a plugin distributed through a
marketplace or remote repository, download it first and provide the local directory
path" ([Agent SDK plugins](https://code.claude.com/docs/en/agent-sdk/plugins)).

- **Postgres is the record.** `ai_plugin_packages` holds the pin (commit SHA and content
  hash) and everything the admin reviewed
  ([`agent/src/plugins/packages/store.ts`](../../agent/src/plugins/packages/store.ts)).
- **Disk is a cache.** Packages are materialised under `<state dir>/plugins/<name>/<commit>-<hash prefix>`
  (`pluginCacheDir()` in [`agent/src/harness/options.ts`](../../agent/src/harness/options.ts)).
  Before each load, every file is hashed and compared with the pin. A missing, partial
  or altered copy is deleted and fetched again at the pinned commit. If the new files do
  not hash to the pin, the package is not loaded (`materialise()` in
  [`agent/src/plugins/packages/install.ts`](../../agent/src/plugins/packages/install.ts)).
  The cache can be the same `emptyDir` as the rest of the state directory.
- **git in the image.** The `agent` stage installs `git` and `ca-certificates`
  ([`Dockerfile`](../../Dockerfile)). git runs with an environment built by
  [`agent/src/plugins/packages/git.ts`](../../agent/src/plugins/packages/git.ts), so it
  never sees the service's own environment. It passes through only the proxy and CA
  variables (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, their lower-case forms,
  `GIT_SSL_CAINFO`, `SSL_CERT_FILE` and `SSL_CERT_DIR`).
- **Egress.** The pod needs outbound HTTPS to each git host it installs from. Fetches,
  and every URL a package declares, go through the same egress check as the gateway
  (see [security.md](security.md#plugin-packages)).

### The routes

These routes sit under `/api/v1/ai/plugin-packages` and use the same UI guard as the
other Settings writes
([`agent/src/routes/pluginPackages.ts`](../../agent/src/routes/pluginPackages.ts)):

| Route | What it does |
|---|---|
| `POST /` with `{ "source": { "kind": "git", "url", "ref"?, "path"? } }` or `{ "kind": "marketplace", "url", "ref"?, "entry" }` | Fetches, pins the commit, vets, and stores the package **unapproved and disabled** (201). A refused package gets 422 with every problem. |
| `GET /`, `GET /:name` | The pin, the review (skills as `<name>:<skill>`, commands, agents, hooks, MCP servers, files), and any pending re-pin with its file diff. |
| `POST /:name/approve` with `{ "commit_sha", "content_hash" }` | Approves exactly the pin the review showed. A mismatch is a 409. |
| `PATCH /:name` with `{ "enabled" }` | Enables an approved pin only; the table enforces this with a `CHECK` too. |
| `POST /:name/repin` with `{ "ref"? }` | Fetches the new commit into a *pending* pin. The current pin keeps loading until the pending one is approved. |
| `DELETE /:name/pending`, `DELETE /:name` | Drop the pending re-pin; uninstall and evict the cache. |

Plugins are never updated automatically (issue #297). At most two fetches run at a
time, and one per package; another request gets 429.

### Status

The session manager reads the enabled packages at the start of each turn
(`packagePlugins` in `SessionManagerDeps`,
[`agent/src/sessions/manager.ts`](../../agent/src/sessions/manager.ts)). A package that
cannot be loaded is reported in the session as a `plugin_unavailable` error, and the
turn goes ahead without it. `main.ts` passes `loadPackagesForRun(…)` to the
`SessionManager`, but nothing starts a session over HTTP yet (the comment on `sessions`
in `main.ts`).

Settings has an "Assistant plugins" area with two sections: "Plugin packages"
([`frontend/src/components/settings/PluginPackages.tsx`](../../frontend/src/components/settings/PluginPackages.tsx))
and "Plugin endpoints", the remote MCP plugins of `/api/v1/ai/plugins`
([`RemotePlugins.tsx`](../../frontend/src/components/settings/RemotePlugins.tsx)).
You install, review, approve and re-pin packages there. The approval dialog shows the
full commit SHA and content hash, and you must tick a confirmation before it sends
exactly those values. Every control that writes is user-only (`USER_ONLY`), so the
in-page agent's `click` and `fill` refuse it. The area follows the assistant's
availability (`useAiAvailability()`), so it is hidden in production builds until the
agent service is deployed.

## 10. MCP auth mode

The `/mcp` auth mode and the anonymous cap are `ai_settings` keys, never environment
variables (spec §8.3, §9). They are read on every `/mcp` request by
`mcpAuthSettings()` in [`agent/src/auth/authenticate.ts`](../../agent/src/auth/authenticate.ts),
so a change applies to the next request on every replica, with no restart:

| Key | Values | Unset |
|---|---|---|
| `mcp_auth_mode` | `"bearer"`, `"disabled"` | `"bearer"` |
| `mcp_anonymous_cap` | `"read"`, `"write"`, `"outward"` (used in `disabled` mode only) | `"outward"` |

`oidc` is not chosen with this key: it is on while the OIDC configuration (`mcp_oidc`,
[§6a](#6a-mcp-sign-in-with-oidc)) is enabled, and then it wins over `mcp_auth_mode`, even
over `"disabled"`. A stored `"oidc"` without an enabled configuration reads as `bearer`.

A value outside those lists fails closed: `bearer`, or a `read` cap. While the mode is
`disabled`, the agent logs `mcp auth: MCP auth is DISABLED ...` with the cap. It logs
this once, and again after any change to either key.

Change them in Settings → **MCP authentication** (shown where AI is available, beside
the access tokens), which calls `GET`/`PUT /api/v1/ai/mcp/auth`
([`agent/src/routes/mcpAuthMode.ts`](../../agent/src/routes/mcpAuthMode.ts)). The
choice is "Require an access token" (`bearer`) or "Allow calls without a token"
(`disabled`), plus the access an anonymous caller gets. Allowing calls without a token,
or raising the anonymous access while they are allowed, asks for a confirmation first.
While OIDC is enabled the section says so: OIDC applies whatever is stored here, and the
stored choice (still confirmed) applies once OIDC is turned off.
That confirmation is in the UI only; the route does not require it (a server-side
approval for settings writes is #258). `PUT` writes both keys in one transaction, only if
they still hold what the page showed (otherwise `409`, and the page reloads them), logs
`mcp auth: mcp_auth_mode set to … (was …; from <client> via <ingress>)` as soon as it
commits, and is guarded like the other Settings
writes (the UI's origin through the HTTPS ingress). It does not set `oidc`, which is
switched on with its own configuration once the discovery check passes (#262). The
database still works when the UI does not, e.g. to recover. Each value is a JSON
string:

```sql
INSERT INTO ai_settings (key, value) VALUES ('mcp_auth_mode', '"disabled"')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
-- back to the default:
DELETE FROM ai_settings WHERE key = 'mcp_auth_mode';
```

Outward tools still need a human approval in the UI in every mode (spec §8.2; see
[security.md](security.md#mcp-prepareconfirm-on-the-approval-store)).
