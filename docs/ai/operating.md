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
- **Roles (#2726, plan Step 6.1).** The same image runs as one of three roles,
  `SCADBUDDY_AGENT_ROLE` (§2; what each starts is `roleParts` in
  [`agent/src/runtime/role.ts`](../../agent/src/runtime/role.ts)):
  - `all` (the default): everything, as every pod ran before the roles.
  - `api`: the HTTP server (the routes, the chat and bridge sockets, `/mcp`, the
    loopback `/internal/*` routes), its sweeps and start-time passes, and its own
    `agent-tools.<hostname>` queue (browser calls for its tabs, a classic session's
    Monitor commands, a DurableSession's pinned calls). It polls neither
    `agent-sessions` nor the shared `agent-tools`, nor a session host queue, so it runs
    no `AgentSession` turn. Classic sessions still run where they are sent, until Step 6.3.
  - `worker`: the Temporal workers that run turns (`agent-tools`, `agent-sessions`,
    `agent-sessions.<hostname>`, `agent-tools.<hostname>`), and over HTTP only
    `/healthz` and `/readyz` on port 8081 (§5). A worker pod is not beside the backend,
    so its `SCADBUDDY_BACKEND_URL` must name the backend's cluster address. A worker runs
    none of the HTTP side's sweeps (the session lease reaper, the durable running and
    pending-input orphan sweeps, approval and question expiry, audit retention), so keep
    at least one `api` (or `all`) replica running whenever workers run.
- **Port 8081**, fixed. The comment on `PORT` in [`agent/src/main.ts`](../../agent/src/main.ts)
  calls it "part of the pod contract with the ingress (spec §4.2)". The Dockerfile
  `EXPOSE`s 8081.
- **Ingress routing (spec §4.2).** `/mcp`, `/api/v1/ai/*` and, for MCP OIDC (§6a),
  `/.well-known/oauth-protected-resource` go to the agent, and everything else, including `/api/v1/ws`, goes to the backend. `/api/v1/ai/*` is
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
- **Pinned Claude Code.** `agent/package.json` pins the Agent SDK exactly and its lockfile
  pins the Claude Code binary the SDK bundles, for each platform. The build runs
  `node dist/check-cli-version.js`, which fails when that binary is missing or its
  `--version` differs from the version the SDK declares
  ([`Dockerfile`](../../Dockerfile); [`agent/src/check-cli-version.ts`](../../agent/src/check-cli-version.ts)).
  The measurement behind this is in spec §3.1 ("Measured in PR #319").
- **Container healthcheck.** `HEALTHCHECK` fetches `http://127.0.0.1:8081/healthz`
  every 30 s with node's `fetch` ([`Dockerfile`](../../Dockerfile)).
- **Shutdown.** On `SIGTERM`/`SIGINT` the listener drains for up to 10 s, then the
  database pool closes. The exit code is non-zero if the drain timed out (`main.ts`;
  [`agent/src/shutdown.ts`](../../agent/src/shutdown.ts)).
  A restart ends every turn the replica was running, and with it every question and
  attention request those turns were waiting on: none is asked again. That includes an
  attention request with `on_timeout: 'wait'`, which otherwise waits up to a day
  (`WAIT_CEILING_S`, [`agent/src/harness/attention.ts`](../../agent/src/harness/attention.ts))
  while holding the session's lease and its Claude Code process (#1220).
- **Activity heartbeats across a rollout.** Since #4200 every activity on the agent's
  Temporal worker beats every 10 s (`heartbeatingActivities` in
  [`agent/src/temporal/worker.ts`](../../agent/src/temporal/worker.ts)), and a flow's
  calls to the agent take a 30 s heartbeat timeout
  ([`backend/scadbuddy/workflows/flow_tools.py`](../../backend/scadbuddy/workflows/flow_tools.py)
  `_agent_heartbeat`). An agent replica from before #4200 sends no beats, and
  `agent-tools` is unversioned, so while one still polls (during the API Deployment's
  rolling update, or after rolling the agent back past #4200) any attempt may land on
  it. A `turn_tool_tier` whose last attempt missed its beats (the failure the call ends
  with, whatever the earlier attempts did) is asked once more with no heartbeat timeout
  (#4262), so it cannot fail the run's tier check for that; it only takes longer. A
  tier whose attempts are all lost (recorded as started but never delivered, or taken
  by a replica that died) now reaches the script, at the default 30 s heartbeat, after
  450 s, about 7.5 min (3 × 30 s of missed beats, then up to 3 × 120 s, the
  `_TOOL_TIMEOUT`, plus the retries' backoff), not 90 s; one whose last attempt ends on
  its 120 s start-to-close instead takes no fallback and fails there. A flow's tool
  calls have no such fallback: one slower than 30 s that lands on an old replica times
  out, a read up to three times and a write at once, and the script sees the error
  (`flow_session_check` retries without limit).
- **Browser calls across a rollout of #4780.** Since #4780 a browser_* call for a tab on
  another replica is a `BridgeCall` workflow on `agent-tools`, whose one activity runs
  on the queue of the replica that holds the tab (`ai_tab_owners`;
  [browser-bridge.md](browser-bridge.md#replicas)). The Postgres relay of #1916 ran
  beside it for one release, and #4868 removed it: every replica since then claims its
  tabs in `ai_tab_owners`, so a call finds the owner there or answers "not connected".
  A new replica's tab has no row until its host worker polls, so for those first seconds
  of its start a call from another replica answers "not connected" (an old replica could
  reach it through the Postgres relay before).
  `ai_bridge_messages` stays until #5109, because a replica of the build before #4868
  still writes it for a tab it finds no live owner for. `agent-tools` is unversioned,
  so while an old replica still polls it, it can take a `BridgeCall` workflow task. It
  has no such workflow, so it fails the task, and Temporal gives the task again to a
  replica that may be another old one. The workflow's execution timeout leaves 15 s
  past the call's own time for that (`WORKFLOW_SLACK_MS`,
  [`agent/src/bridge/relay.ts`](../../agent/src/bridge/relay.ts)), about one failed task
  and its retry. A call whose workflow still times out is logged and answers
  `no_answer`; it never fails the tool, and it is never sent again, since
  the owner may already have run it. With two or more old replicas polling, the retries'
  backoff can use up those 15 s before a new replica takes the task, so a call that never
  ran can still answer `no_answer` during the rollout. And a bridge call's deleted key is
  gone everywhere only once every replica runs this build: an old replica's
  `PgPayloadKeys` caches any key it opens for 30 s, a `bridge-<uuid>` one included, and
  this build caches none. Once no replica
  runs a build before #4780, nothing takes those tasks but replicas that have the
  workflow.

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
| `/api/v1/ai` (prefix) | agent `:8081` | REST, SSE (`/api/v1/ai/sessions/{id}/events`) and the WebSockets `/api/v1/ai/chat` and `/api/v1/ai/bridge` (the tab's socket for the `browser_*` tools, #254, [browser-bridge.md](browser-bridge.md#the-tabs-socket)) |
| `/mcp` (prefix) | agent `:8081` | MCP Streamable HTTP (SSE) |
| `/.well-known/oauth-protected-resource` (prefix) | agent `:8081` | The protected-resource metadata MCP clients read when `/mcp` is in `oidc` mode (§6a; `RESOURCE_METADATA_PATH`, [`agent/src/auth/oidc.ts`](../../agent/src/auth/oidc.ts)). Unrouted, it falls through to the backend, whose SPA fallback answers `200` with `index.html` |
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
  The agent pings each chat and bridge socket every 25 s (`HEARTBEAT_MS`, `startHeartbeat`,
  [`agent/src/routes/chat.ts`](../../agent/src/routes/chat.ts)), and the session event
  stream sends a comment every 20 s with `X-Accel-Buffering: no` (`SSE_KEEPALIVE_MS`,
  [`agent/src/routes/sessions.ts`](../../agent/src/routes/sessions.ts)). Both
  therefore stay open under that default. The rule: both intervals must stay under
  the ingress's read and send timeouts. Lowering a timeout below 25 s, or raising
  either constant above it, closes idle assistant sockets and streams.
- **Request bodies.** An image upload (`POST /api/v1/ai/attachments`, #1941) is a
  multipart body of up to about 3.8 MiB (`ATTACHMENT_BODY_MAX`,
  [`agent/src/routes/attachments.ts`](../../agent/src/routes/attachments.ts)): one
  image's bytes and its preview's (#2479). ingress-nginx refuses bodies over its default
  `proxy-body-size` of 1m with 413, so the agent's paths need at least
  `nginx.ingress.kubernetes.io/proxy-body-size: 8m`
  ([ingress-nginx, custom max body size](https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/#custom-max-body-size)).
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
          - path: /.well-known/oauth-protected-resource
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
curl -sSI "$base/.well-known/oauth-protected-resource/mcp" | grep -i '^x-scadbuddy-service: agent'  # agent (404 while OIDC is off is fine)
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
| `SCADBUDDY_DATABASE_URL` | unset → AI disabled | `loadConfig()` | Postgres URL, shared with the backend (#241). It must be `postgres://` or `postgresql://`; anything else is a `ConfigError` at start. The URL is not echoed in the error because it normally carries the password. With it unset the service still runs, and `/healthz` reports `"ai": "disabled (no database)"`. Each replica opens up to 4 connections in its shared pool and about 4 more of its own for plugin connection-test locks (briefly more while a failed lock connection is replaced) (`TEST_LOCK_CONNECTIONS` in [`agent/src/db.ts`](../../agent/src/db.ts), #4994): count both when sizing Postgres `max_connections`. |
| `SCADBUDDY_BACKEND_URL` | `http://127.0.0.1:8080` | `loadConfig()` | Where the backend listens. Must be `http(s)`. Trailing slashes are stripped. `/healthz` probes `GET {url}/healthz` with a 2 s timeout (`backendReachable()`, [`agent/src/api/backend.ts`](../../agent/src/api/backend.ts)). |
| `SCADBUDDY_SECRET_KEY_FILE` | unset → credentials cannot be saved | `loadKek()` in [`agent/src/secrets.ts`](../../agent/src/secrets.ts) | Path of the key-encryption key (KEK) file. See §3. |
| `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` | unset | `main.ts`, `loadKek()` | The old KEK during a rotation. See §3.3. |
| `SCADBUDDY_PUBLIC_URL` | unset → loopback only | `originPolicy()` in [`agent/src/http/origins.ts`](../../agent/src/http/origins.ts) | The UI's public URL. This is the same variable the backend reads (`public_url` in `backend/scadbuddy/core/settings.py`). Its origin is the only non-loopback origin allowed to write. Must be `http(s)`. See §6. |
| `SCADBUDDY_AGENT_TRUSTED_PROXIES` | unset → no proxy trusted | `parseCidrList()` in `origins.ts` | A comma-separated list of CIDRs or bare addresses whose `X-Forwarded-Proto`/`X-Forwarded-Host` are believed. A malformed entry is a `ConfigError` at start. See §6. |
| `SCADBUDDY_AGENT_ROLE` | `all` | `loadConfig()` | `all`, `api` or `worker` (§1, "Roles"). Any other value stops the process with exit code 1, naming the value. |
| `SCADBUDDY_SESSION_DIR` | unset → `work/sessions` under the state directory | `loadConfig()`, `sessionsRoot()` in [`agent/src/harness/stateDirs.ts`](../../agent/src/harness/stateDirs.ts) | Where each session's working directory, `<dir>/<session id>/`, is: the `cwd` of its Claude Code process, a working copy of its workspace (§13). Set it on the workers to a capped emptyDir of the pod's own, never the NAS share (the copies are plaintext, and the share's backup cannot leave a subPath out); every placement refills it from the workspace, so a turn placed on another pod still finds the files the last one wrote back. Must be an absolute path. The process checks at start that it can write there. |

`main.ts` logs one line at start naming the backend URL, whether a database is
configured, and where credential writes are accepted from.

Variables set in the image, not read by `config.ts`: `HOME`, `CLAUDE_CONFIG_DIR` and
`NODE_ENV` ([`Dockerfile`](../../Dockerfile)). The Claude Code
subprocess never inherits the service's environment. `buildQueryOptions()` in
[`agent/src/harness/options.ts`](../../agent/src/harness/options.ts) passes an explicit
`env` holding only `CLAUDE_CONFIG_DIR`, `HOME` and `PATH`. The pinned SDK's `sdk.d.ts`
says `env` "REPLACES the subprocess environment entirely" (quoted in that file).

Tracing (#988) is configured by the standard OpenTelemetry variables only, read by
[`agent/src/telemetry/setup.ts`](../../agent/src/telemetry/setup.ts) when the process
starts under `node --import ./dist/telemetry.js` (the image's `CMD`):
`OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (the latter used
verbatim, the former with `/v1/traces` appended; `OTEL_EXPORTER_OTLP_HEADERS` and
`OTEL_EXPORTER_OTLP_TRACES_HEADERS` add collector headers; neither endpoint set, or
`OTEL_TRACES_EXPORTER=none`: spans are created, so context propagates, and dropped;
`OTEL_TRACES_EXPORTER` unset, empty or a list containing `otlp`, any case, exports as
configured, and any other value, `console` say, is off with one warning naming it), `OTEL_SDK_DISABLED=true` (the kill switch: no spans at all),
`OTEL_TRACES_SAMPLER` (replaces the default, which drops parentless client spans) and
`OTEL_RESOURCE_ATTRIBUTES`. `SCADBUDDY_VERSION` and `SCADBUDDY_REVISION` are stamped
into the image by `build-image.yml` and become `service.version` and
`scadbuddy.revision`. None of these reach the Claude Code subprocess, whose `env` is
explicit (above). Only the backend client sends `traceparent`; plugins, `http_request`
and Anthropic never get one. Design:
`docs/superpowers/specs/2026-10-01-distributed-tracing-design.md` §5.4.

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

There can be several credentials, one row each in `ai_credentials`, in priority order
(#1093). Each is of one of three kinds (`CREDENTIAL_KINDS`,
[`agent/src/credentials.ts`](../../agent/src/credentials.ts); spec D2):

- `anthropic_api_key`: a Console key (`sk-ant-api03-…`), passed to Claude Code as
  `ANTHROPIC_API_KEY`.
- `claude_oauth_token`: the token `claude setup-token` prints (`sk-ant-oat01-…`),
  passed as `CLAUDE_CODE_OAUTH_TOKEN`. Saved as an API key instead, it goes in
  `x-api-key` and Anthropic answers 401. Spec D2 records this kind as a reversal;
  read Anthropic's terms for subscription credentials before using it.
- `gateway`: a `base_url` plus a token, passed as `ANTHROPIC_BASE_URL` and
  `ANTHROPIC_AUTH_TOKEN`.

The mapping is in `credentialEnv()` in [`agent/src/harness/run.ts`](../../agent/src/harness/run.ts).
Its sources are the [LLM gateway docs](https://code.claude.com/docs/en/llm-gateway-connect)
("`ANTHROPIC_AUTH_TOKEN` in `Authorization: Bearer`, `ANTHROPIC_API_KEY` in
`x-api-key`"), quoted in that file. `CLAUDE_CODE_OAUTH_TOKEN` is the variable Claude
Code reads a `claude setup-token` token from. Anthropic does not allow claude.ai login
in third-party products ([Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview),
quoted in spec §3.1). `claude_oauth_token` is for a deployment owner using their own
token on their own deployment (spec D2).

Each query uses the first credential that is usable now, and falls back to the next
when a call fails for a reason that is the credential's (`runWithFallback()` in
[`agent/src/harness/fallback.ts`](../../agent/src/harness/fallback.ts); the classifier
is `classifyFailure()` in
[`agent/src/harness/credentialErrors.ts`](../../agent/src/harness/credentialErrors.ts)):

| Failure | Examples | What happens |
|---|---|---|
| Permanent | 401, 403, 402, a billing or credit message | First confirmed: the agent asks the endpoint once more with that credential (`probeCredential()`, a one-token request on `claude-haiku-4-5`, not the turn's model, so a refusal of that model or of a feature the turn used does not confirm itself). Refused again with a 401, a 402, or a 400/403 that names the key, the organisation or billing (`refusesTheKey()`), the credential becomes `disabled`, with the reason in `last_error`, and is not tried again until someone resets it or saves a new secret for it. Answered, the refusal was about the request (a gateway or WAF refusing one body, say), so the turn ends there with no fallback and nothing recorded. With no clear answer it falls back for this call without being marked. |
| Rate limited | 429 | The credential is `cooling_down` until the time the endpoint names, then usable again on its own. It is recorded at once with the default 60 s and the turn moves on; Claude Code does not pass the response headers on, so the agent asks the endpoint once more with that credential in the background (`probeCredential()`, a one-token request; a refused one is not billed) and replaces that time with what its 429 names (`retry-after`, then the `anthropic-ratelimit-*-reset` headers). A probe that is answered makes it usable again in 1 s; one refused the same way as above (`refusesTheKey()`) disables it instead. Every probe stops with the turn: when the user stops it, the agent does not wait for a probe still running, and records nothing from it. |
| Transient | 5xx, 529, network errors | Claude Code retries on the same credential, at most twice when there is another to fall back to (`CLAUDE_CODE_MAX_RETRIES`), then the turn moves on for this call only. The credential is not marked. |
| Not the credential's | 400 (other than billing), a turn or budget limit | No fallback: the next credential would fail the same way. Through a gateway, Claude Code first sends a refused 400 or 422 once more on the same credential without its `thinking.display` field (and that field's `anthropic-beta` value), so a gateway that refuses only that field still answers the turn, at the cost of one refused request a turn. |

No further attempt starts once the turn's `maxTurns` or budget is used up. A turn that
fails mid-way (after tool calls ran) resumes the session on the next credential with a
short "continue" prompt, so no tool runs twice and the prompt is not sent twice. Each
credential is tried at most once per turn. In a durable session, while another
credential could still take the segment over, the assistant's text reaches the
transcript only at the next tool call or at the end of the segment, not block by
block, so a refused credential's notice never shows (#2945). A durable segment records
what its credential met as a classic turn does, so Settings shows the same "used" time
and state in both modes (#2320): an answer stamps it used, a 429 cools it down for the
default 60 s, and a refusal or a 5xx is its `last_error`. It runs no probe, so it never
disables a key and never learns a rate limit's real reset time; a classic turn's probe
does both. Every disable, cooldown,
recovery and fallback is an audit row of kind `credential` (`CredentialPool`), named
like `credential 2 (API key …abcd)`, never with the secret. When no credential is
usable, a turn fails with a message naming each one's state and the soonest time one
is usable again, and `GET /api/v1/ai/status` answers `state: "unavailable"` with `ai`
set to one of:

- `unavailable (every Claude credential is rate limited)`, with `recovers_at`: every
  credential is cooling down and will be usable again on its own;
- `unavailable (no Claude credential is usable now)`: some are disabled or cannot be
  opened with the mounted key-encryption key (sealed under another one, or in the old
  format, which a Reset does not fix: save the secret again); `recovers_at` is set when
  any of the others is cooling down;
- `unavailable (every Claude credential is disabled)`: every one is disabled and opens
  with the mounted key, so a Reset of any of them makes it usable.

A reset, or a new secret, clears `last_error` and `last_error_at` as well as the status.

Several pods share this state. A cooldown that has passed reads as `active` on the
database's clock without a write; a rate limit never downgrades a disabled credential;
of two cooldowns the later wins; and a failure is recorded only against the `epoch`
the query read, so a query that started with an old secret cannot disable a new one.

The routes are in `registerCredentialRoutes()` in
[`agent/src/routes/credentials.ts`](../../agent/src/routes/credentials.ts). Error
bodies are `{ "detail": "…" }`, plus `code` on the no-database `503`. The normal way
to use them is Settings → Assistant → **Claude credentials** (`AiCredentialSection`,
[`frontend/src/components/assistant/AiCredentialSection.tsx`](../../frontend/src/components/assistant/AiCredentialSection.tsx),
#1000, #1093), a list over the `/entries` and `/order` routes in the order queries try
the credentials.
Each row shows the kind, base URL, last four and status (active, rate limited until a
time, or disabled with the last error), and can be moved up or down, tested (showing
`Retry-After` on a `429`), reset when not active, given a new key, or deleted after a
confirmation; a form adds one last. The list is read again just after the earliest
cooldown ends, since the status is worked out at read time. The `curl` below is the
fallback when there is no UI.

The routes without `/entries` are the single-credential routes from #1000. They act on
the first credential by priority, for scripts written against them. They are
**deprecated** (#2768): the UI no longer uses them, and every answer carries
`Deprecation: @1791936000` (RFC 9745: deprecated at 2026-10-14T00:00:00Z) and a `Link`
header naming `/api/v1/ai/credentials/entries` as the
successor. The `/entries` and `/order` routes manage every credential (#1093); use them
in anything new.

| Route | Guarded | What it does |
|---|---|---|
| `GET /api/v1/ai/credentials` | Read guard (#989) | Returns `configured`, `kind`, `base_url`, `last4`, `updated_at`, `usable`, `can_save` and `cannot_save_reason` (`view()`). It never returns the secret. `last4` is empty for a secret shorter than 12 characters (`last4()`, `secrets.ts`). |
| `PUT /api/v1/ai/credentials` | Yes | Body `{ kind, base_url?, secret? }`, strict (`PutBody`). A `gateway` needs `base_url`, and `anthropic_api_key` and `claude_oauth_token` must not have one. `base_url` must be http(s), with no userinfo, query or fragment. It is normalised without a trailing slash (`normaliseBaseUrl()`). A gateway host is checked against the egress rules first (§5 of [security.md](security.md#egress-check-on-gateway-urls)). The secret must not contain whitespace. **Omitting `secret` keeps the stored one only if `kind` and `base_url` are unchanged**; otherwise the route answers `409` (`planPut()`). |
| `DELETE /api/v1/ai/credentials` | Yes | Deletes the first credential and answers with the one that moved up into its place (`configured: false` when none is left). |
| `GET /api/v1/ai/credentials/entries` | Read guard (#989) | Every credential in priority order, as `{ credentials, usable_now, recovers_at, can_save, cannot_save_reason }`. Each entry has `id`, `priority`, `kind`, `base_url`, `last4`, `updated_at`, `usable`, `status` (`active`, `cooling_down` or `disabled`), `cooldown_until`, `last_error`, `last_error_at` and `last_used_at` (`entryView()`). |
| `POST /api/v1/ai/credentials/entries` | Yes | Body `{ kind, base_url?, secret }`; adds a credential last in priority and answers `201` with its entry. At most 100 are stored (`MAX_CREDENTIALS`); past that it answers `409`. |
| `PUT /api/v1/ai/credentials/order` | Yes | Body `{ ids }`, every credential's id exactly once, first to last; otherwise `409`. Answers with the list. |
| `PUT /api/v1/ai/credentials/entries/{id}` | Yes | Same body and rules as `PUT /api/v1/ai/credentials`, for one credential. A new secret makes it `active` again. |
| `DELETE /api/v1/ai/credentials/entries/{id}` | Yes | Deletes one credential and answers with the list. |
| `POST /api/v1/ai/credentials/entries/{id}/reset` | Yes | Makes a `disabled` or `cooling_down` credential `active` again. |
| `POST /api/v1/ai/credentials/entries/{id}/test` | Yes | Tests one credential, as below. A test does not change its status. |
| `POST /api/v1/ai/credentials/test` | Yes | Runs a one-turn query (`maxTurns: 1`, `maxBudgetUsd: 0.05`, 60 s timeout, prompt `Reply with the single word: ok`) and returns `{ ok, detail, duration_ms, model }` (`testConnection()`, [`agent/src/harness/testConnection.ts`](../../agent/src/harness/testConnection.ts)). Only one test runs at a time, with at most one per 10 s (`DEFAULT_TEST_COOLDOWN_MS`); otherwise it answers `429` with `Retry-After`. The first `api_retry` is treated as the verdict, so a bad key fails fast. The model comes from `ai_settings.model` when set (`main.ts`). A gateway host is re-checked at test time. |

"Guarded" means the request must pass `uiRequestProblem()` in
[`agent/src/routes/guard.ts`](../../agent/src/routes/guard.ts) or it gets `403` (§6).
Every route, the `/entries` and `/order` ones included, answers `503` while there is no
database, or while migrations have not applied (`store()` in `routes/credentials.ts`).
The no-database `503` also carries
`"code": "no_database"` (`NO_DATABASE_CODE`), which is what the Settings section hides on;
the not-yet-migrated one has no `code`, and the section shows it with a Retry.

Example, from a loopback shell on the pod (for instance through `kubectl port-forward`).
It reads the new entry's `id` with `jq`; where the shell has no `jq`, read `id` from
the `POST`'s JSON answer by hand. The `POST` adds a credential every time it runs, so
running it again leaves a duplicate: to replace an existing key, `PUT` it to
`/api/v1/ai/credentials/entries/{id}` instead, taking its `id` from
`GET /api/v1/ai/credentials/entries`. `jq -e` fails when the `POST` was refused (a
`403`, `400` or `503` has no `id`), so the test runs only for a credential that was
added; rerun the `POST` without `| jq` to read why it was refused.

```bash
id=$(curl -sS -X POST http://localhost:8081/api/v1/ai/credentials/entries \
  -H 'Origin: http://localhost:8081' -H 'Content-Type: application/json' \
  -d '{"kind":"anthropic_api_key","secret":"sk-ant-…"}' | jq -er .id) &&
  curl -sS -X POST "http://localhost:8081/api/v1/ai/credentials/entries/$id/test" -H 'Origin: http://localhost:8081'
# Replacing that key later, rather than adding another:
curl -sS -X PUT "http://localhost:8081/api/v1/ai/credentials/entries/$id" \
  -H 'Origin: http://localhost:8081' -H 'Content-Type: application/json' \
  -d '{"kind":"anthropic_api_key","secret":"sk-ant-…"}'
```

This works because the loopback pair is always accepted from a loopback peer
(`checkOrigin()`, `origins.ts`). Through the ingress, the browser's own `Origin` must
match `SCADBUDDY_PUBLIC_URL`.

### 4.1 MCP access tokens

An outside MCP client authenticates to `/mcp` with a bearer token (`bearer` auth mode,
spec §8.3). Tokens are managed in Settings → MCP access → **Tokens**, which calls the
routes in `registerMcpTokenRoutes()` in
[`agent/src/routes/mcpTokens.ts`](../../agent/src/routes/mcpTokens.ts). Tokens are
stored in `ai_mcp_tokens` (§7). The Settings section renders only where
`useAiAvailability()` ([`frontend/src/agent/chat/availability.ts`](../../frontend/src/agent/chat/availability.ts))
reports AI available: the ingress routes `/api/v1/ai/*` to the sidecar **and** a Claude
credential is saved (`GET /api/v1/ai/status` answers `configured`). The credential
section itself always shows, so it can be saved first.

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
The mode is stored in `ai_settings` and changed in Settings (§10, `GET`/`PUT /api/v1/ai/mcp/auth`, `agent/src/routes/mcpAuthMode.ts`); `bearer` is the default until it is set.

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
A `worker` process (§1, "Roles") serves its own instead
([`agent/src/runtime/health.ts`](../../agent/src/runtime/health.ts)): `GET /healthz`,
always `200`, with `{ status, role: "worker", database, secret_key, temporal, queues }`
(each worker's state by the queue it polls, and `temporal` the worst of them), and
`GET /readyz`, `200` only once Temporal lists this process as a fresh poller of each of
its four queues (`queuesPolled`), else `503` with the reason. Every other path is `404`.
In an `api` process, `temporal` reports its own `agent-tools.<hostname>` worker.

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
- **The agent migrates before agent-durable writes.** agent-durable runs no migrations
  of its own but writes columns the agent's create, such as
  `ai_input_responses.response_hash` (`20261015T1500Z_input_response_hash.sql`, #3724),
  which `resolve_input` sets on every respond. So roll the agent out first. If
  agent-durable runs ahead of that migration, every respond fails in its handler: the
  route answers 503, the entry stays pending, and it goes through once the agent has
  migrated.

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

**In ScadBuddy.** Settings → MCP access → **OIDC sign-in** (the section appears where the agent
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
| metadata URL answers 404 (with `X-ScadBuddy-Service: agent`) | OIDC is off, or `SCADBUDDY_PUBLIC_URL` is unset |
| metadata URL answers `200` with `text/html` and no `X-ScadBuddy-Service: agent` | the ingress does not route `/.well-known/oauth-protected-resource` to the agent, so the backend's SPA fallback answers with `index.html`, which an MCP client cannot parse (§1.1) |

A stored configuration that no longer parses (a hand-edited row) reads as "off" and is
logged as `mcp auth: ai_settings.mcp_oidc is not a valid OIDC configuration`.

## 7. Database tables

The agent owns and migrates its `ai_*` tables (spec §9;
[`agent/src/db/migrations/`](../../agent/src/db/migrations/), applied by
[`agent/src/db/migrations.ts`](../../agent/src/db/migrations.ts)):

- `ai_migrations`: the ledger, one row per applied file (its id is the file name
  without `.sql`), with a checksum each. `version` and `story` are set only for the
  two files that predate #491, so an older image can still read the ledger.
- `ai_credentials`: the sealed credentials, with their priority and health (#1093).
- `ai_settings`: non-secret key/value settings. `SettingsStore` in `credentials.ts`.
  The keys read today are `model`, the Claude model every turn and the connection test
  use (`SETTING_MODEL`; unset or `null` is Claude Code's default), which Settings →
  Assistant model writes through `PUT /api/v1/ai/settings/model`
  ([`agent/src/routes/model.ts`](../../agent/src/routes/model.ts), #1917); `session_max_turns` and
  `session_max_budget_usd` (`SETTING_SESSION_MAX_TURNS` and
  `SETTING_SESSION_BUDGET_USD` in [`agent/src/sessions/manager.ts`](../../agent/src/sessions/manager.ts)),
  which Settings writes through `PUT /api/v1/ai/settings/session-limits`
  ([§8](#8-per-query-limits));
  `approval_expiry_seconds` (`SETTING_APPROVAL_EXPIRY_SECONDS` in
  [`agent/src/approvals/service.ts`](../../agent/src/approvals/service.ts));
  `mcp_auth_mode` and `mcp_anonymous_cap` ([§10](#10-mcp-auth-mode)); `browser_origins`, the headless browser's allow and deny
  lists (`{ allow, deny }`, the defaults when unset), which Settings → Tools & plugins → AI headless browser writes
  through `PUT /api/v1/ai/settings/browser-origins`
  ([headless-browser.md](headless-browser.md#where-it-may-go-the-allow-and-deny-lists-982), #982);
  `image_long_edge`, the long edge in pixels the
  assistant panel scales an attached image to (default 1568, the Messages API's
  standard-tier edge; 200 to 2576, the high-resolution tier's edge for Claude 4.7 and
  later), which Settings → Assistant images writes through
  `PUT /api/v1/ai/settings/images` ([`agent/src/routes/imageSettings.ts`](../../agent/src/routes/imageSettings.ts));
  `memory_recall_max_query_chars`, the longest automatic Hindsight recall query in
  characters (default 800, 0 for no cut, at most 100000), which Settings → Assistant →
  Hindsight memory writes through `PUT /api/v1/ai/settings/memory`
  ([§11](#11-automatic-hindsight-memory));
  `session_mode` is no longer read: since plan Step 6.3 every session is an
  `AgentSession` (spec 2026-10-13 §10.1), and its route and Settings section are gone;
  `durable_subagents` is no longer read: it configured the retired Python turn's
  subagent loopback, which plan Step 8.2 removed with its route and Settings section;
  and `mcp_oidc`, the
  OIDC configuration for `/mcp` (#262; see [§6a](#6a-mcp-sign-in-with-oidc)), which
  `PUT /api/v1/ai/mcp/oidc` writes. `approval_expiry_seconds` has no route yet.
- `ai_sessions`, `ai_session_entries` and `ai_session_events`: sessions (#377,
  `20260928T0107Z_sessions.sql`). `main.ts` builds the `SessionManager` and serves it
  through the chat socket, the session routes and the `sessions_*` tools
  ([agent-sessions.md](agent-sessions.md)). Every event-log append is also announced
  as `session.*` on `scadbuddy_events` (NOTIFY only, no table).
- `ai_approvals`: approvals of outward calls, from session turns and from `/mcp`
  prepares (#471, `20260928T0734Z_approvals.sql`; see
  [security.md](security.md#mcp-prepareconfirm-on-the-approval-store)).
  `traceparent` is the parked call's tool span and `decision_traceparent` the decision's
  `agent.approval` span (#988); both are internal and never in an approval view.
- `ai_mcp_tokens`: MCP bearer tokens (#251, `20260928T0734Z_mcp_tokens.sql`), one row per token with its
  name, tier, `created_at`, `expires_at`, `revoked_at`, `last_used_at` and
  `approval_grant` (#300, `20260929T0249Z_mcp_token_approval_grant.sql`: off by default,
  and a `CHECK` allows it only on an `outward` token). Only the
  SHA-256 of the token is stored (`token_hash`, 64 hex characters, enforced by a
  `CHECK`); the plaintext is shown once when minted. `PostgresTokenStore` in
  [`agent/src/auth/tokens.ts`](../../agent/src/auth/tokens.ts). There is no file or
  in-memory store: without `SCADBUDDY_DATABASE_URL`, `/mcp` and the token routes
  (§4.1) answer 503. Settings writes this table through §4.1's routes.

- `ai_browser_pairings`: MCP clients the user paired with a tab by typing a code
  (#254, spec §8.5, `20260929T1330Z_browser_pairings.sql`): who asked, the SHA-256 of
  the code (never the code), the status (`pending`, `paired`, `denied`, `ended`), the
  tab, wrong tries and the expiry. `PostgresPairingStore` in
  [`agent/src/bridge/pairings.ts`](../../agent/src/bridge/pairings.ts); see
  [browser-bridge.md](browser-bridge.md#pairing-spec-85). Without a database only chat
  sessions reach a tab.

- `ai_session_files`: the assistant's workspace (#2329,
  `20261011T1502Z_session_files.sql`; [§13](#13-the-workspace-2329)): files keyed by
  `workspace` (`session:<id>`, deleted with the session, or `principal:<kind>:<id>` for
  an `/mcp` caller outside a session, swept 24 h after its last write) and `path`, with
  the media type, sha256, size and `source`, and the bytes: a sealed blob of the
  backend's store (`blob_key`, `key_sealed`, #4789, `20261027T0114Z_session_files_blobs.sql`)
  or, where the backend keeps no blobs, `data`. `SessionFiles` in
  [`agent/src/workspace/store.ts`](../../agent/src/workspace/store.ts).

- `ai_plugin_packages`: installed Claude plugin packages (#297,
  `20260928T0750Z_plugin_packages.sql`): the source, the pinned commit, the content
  hash, the review, the approval and the enabled flag. See §9.

The migration advisory lock key is "SCADAGNT", distinct from the backend's "SCADBDDY"
(the comment on `MIGRATION_LOCK` in `migrations.ts`).

The agent also reads the backend's flow tables, which the backend migrates. Its
database role needs `UPDATE` on `workflow_runs`, not only `SELECT`: every flow tool call
reads its run `FOR SHARE` (`PgFlowRuns.startedBy`, `agent/src/temporal/flowRuns.ts`), and
Postgres requires `UPDATE` for that lock. A role narrowed to `SELECT` fails every flow
tool call, as a lookup error the activity retries until it gives up.

## 8. Per-query limits

Every harness query gets `maxTurns` (default 25) and `maxBudgetUsd` (default 1 USD)
(`DEFAULT_MAX_TURNS` and `DEFAULT_MAX_BUDGET_USD` in
[`agent/src/harness/run.ts`](../../agent/src/harness/run.ts)). Sessions read their caps
from `ai_settings` when they start, and spend the budget across the whole session (PR
#377 body, "Budget and turns"): each turn gets what is left as `maxBudgetUsd`.

Settings → Assistant → **Assistant chat limits** sets both (#790): "Session budget
(USD)", 0.01 to 100, stored in cents, and "Max turns per reply", 1 to 200
(`GET`/`PUT /api/v1/ai/settings/session-limits`,
[`agent/src/routes/sessionLimits.ts`](../../agent/src/routes/sessionLimits.ts), behind
`uiReadProblem`/`uiRequestProblem`). Each key written is a `settings` audit row as the
browser user. A change applies to sessions created after it; existing sessions keep
their budget and turn cap.

When a session's budget runs out, the assistant panel says so once ("This chat used
its $1.00 budget.") and offers:

- **Continue in a new chat**: `POST /api/v1/ai/sessions/:id/fork` makes a new session
  with the transcript so far and the current Settings budget, owned by the browser user,
  and the panel switches to it. It counts against the new-session limit (`429`). With
  `up_to` (a reply's panel message id, #793) the child has the conversation only through
  that reply. The transcript is copy-on-write (plan 2026-10-13 Step 6.5, #4212): the
  child records where it cut its parent's (`ai_sessions.fork_parent_id`, `fork_cut`),
  reads the parent's entries through that cut under the parent's key, and stores only
  what it adds. Forgetting the parent (`forgetSubject`) first copies the range each fork
  reads into that fork, sealed under the fork's own key, so it needs the KEK mounted
  while the parent has forks. Forks made before this hold a copy. A fork runs in its parent's mode: a durable parent's conversation is read
  from its workflow (the `fork_point` Query) and the child's workflow starts with a copy,
  sealed under the child's own key (#2688); its replies are matched to the transcript by
  their text, and a parent whose turn is running forks at its last finished reply.
  Each fork, from this route or the `sessions_fork` tool, is a `resource`
  audit row (action `session_fork`), written once the fork is complete; a rename or
  mark-done (`PATCH /api/v1/ai/sessions/:id`) is one with action `session_update`.
- **Raise this chat's budget**: `POST /api/v1/ai/sessions/:id/budget` `{add_usd}`
  (`SessionManager.raiseBudget`). Only the browser user, only on a session it owns
  (take one over first), and never past $100. No tool calls it, and
  the panel's button carries `data-agent-user-only`, so the browser bridge cannot press
  it. A raise is a `settings` audit row with action `session_budget_usd` and the
  session id; refused and failed attempts are recorded from the route's status.
- **Start a new chat.**

The panel's session header shows "$0.74 of $1.00" and warns from 80%. The numbers come
from the session's events: `session.started` and `session.result` carry `budgetUsd`,
and `session.budget` is sent after a raise and before a send is refused because the
budget is spent. Money is shown in cents, in the panel and in the agent's error text.

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
- **Built-in plugins.** The plugin in the image is listed first by
  `GET /api/v1/ai/plugin-packages`, with `built_in: true`
  ([`agent/src/plugins/packages/builtins.ts`](../../agent/src/plugins/packages/builtins.ts)):
  `scadbuddy`, ScadBuddy's own plugin (on unless its source switch, `ai_access` row
  (`builtin:own-plugin`, `*`), is `off`; [`agent/src/access/model.ts`](../../agent/src/access/model.ts)). A browser (Playwright MCP) is not built in
  any more; it is added as a remote plugin ([headless-browser.md](headless-browser.md)).
  Settings sets that switch on the source "ScadBuddy skills and subagents" (#4757).
  The old `PATCH /plugin-packages/scadbuddy` with `{ enabled }` is gone (#4828); use
  `PUT /api/v1/ai/capabilities/builtin:own-plugin`. Approve, re-pin and `DELETE` answer 409. An
  install whose package names one answers 409 with `built_in: true` instead of
  refusing it, and Settings shows that as a notice.

### The routes

These routes sit under `/api/v1/ai/plugin-packages` and use the same UI guard as the
other Settings writes
([`agent/src/routes/pluginPackages.ts`](../../agent/src/routes/pluginPackages.ts)):

| Route | What it does |
|---|---|
| `POST /` with `{ "source": { "kind": "git", "url", "ref"?, "path"? } }` or `{ "kind": "marketplace", "url", "ref"?, "entry" }` | Fetches, pins the commit, vets, and stores the package **unapproved and disabled** (201), with what the rules refuse in `review.refused`. A package no approval can allow (its name, a path outside it, a symlink, an endpoint the egress check refuses) gets 422 with every problem. |
| `GET /`, `GET /:name` | The pin, the review (skills as `<name>:<skill>`, commands, agents, hooks, MCP servers, files), and any pending re-pin with its file diff. |
| `GET /:name/files`, `GET /:name/file?path=` (each with `pending=true` for the re-pin's) | Every file of the pin with its size, and one file's content for Settings to show before approval (#1029). Only a path in the pin's own file list is read, from the cached copy that hashes to the pin (fetched again when it does not; never vetted for loading, so a refused package can be read). Text is cut at 64 KiB unless `full=true`; a file that is not UTF-8 is `binary` with no `content`. A built-in's files are read from the image. |
| `POST /:name/approve` with `{ "commit_sha", "content_hash", "allow_refused"? }` | Approves exactly the pin the review showed. A mismatch is a 409. So is a pin whose `review.refused` is not empty, unless `allow_refused` is `true`: that loads the package as it is, its code running with the Claude credential in its environment ([security.md](security.md#plugin-packages)). |
| `PATCH /:name` | Gone (#4828). A package's switch is `PUT /api/v1/ai/capabilities/package:<name>` (#4757), which Settings uses; it enables an approved pin only, and the table enforces this with a `CHECK` too. |
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
`SessionManager`, so every turn gets them, whether it was started from the panel's chat
socket, `POST /api/v1/ai/sessions` or the `sessions_*` tools (the comment on `sessions`
in `main.ts`).

A vetted package's http MCP servers are reached the way a remote plugin is, through the
loopback forwarder
([`agent/src/plugins/packages/mcp.ts`](../../agent/src/plugins/packages/mcp.ts)
`forwardPackageServers`, #2320). Each is registered as a plugin named
`plugin_<package>_<server>`, the name Claude Code gives a plugin's server. The forwarder
connects to the address the egress check passed, follows no redirect, starts no OAuth
discovery, adds the declared header itself (one at most) and marks results untrusted.
Before #2320, Claude Code started none of them, because `strictMcpConfig` was on. Their
URL and their one header (at most one) are checked as a remote plugin's registration is
(`normalisePluginUrl`, `validateHeaderName`, `validateSecret`), and `X-Forwarded-*` and
`Proxy-*` are refused. Claude Code loads every package without its own http entries: a
copy under `<state dir>/plugins-local/<hex>/<name>/` whose `.mcp.json` and manifest keep only
local servers, and that leaves out what the user switched off (`derivedPackage`, see
"Items and switches" below). A query that reads plugins' MCP configs (with
`strictMcpConfig` off, for an allowed package) refuses any
loaded plugin that still declares an http server (`harness/run.ts`). An allowed
package's local servers still start as declared.

**Turn tools (#2320).** A flow run's `tool('<server>__<tool>', …)`, and a session turn's
calls with `tools_as_activities` on (unified runtime plan Step 9.3), reach a package's MCP
servers as turn tools, as a remote plugin's
([`agent/src/temporal/packageServers.ts`](../../agent/src/temporal/packageServers.ts)). The
copies durable sessions shared on the payload volume (`<SCADBUDDY_PAYLOAD_DIR>/plugins/`)
are swept until none is left. They are
named `plugin_<package>_<server>` and are outward. http ones go through the same
forwarder (above), and their forwarder URLs and headers are redacted like a plugin's. A
local one runs only for a package approved with `allow_refused`, started in the
package's folder with `PATH`, `HOME` and its declared `env`.

**Claude Code's own built-ins** (plan 2026-10-13 Step 9.6, decisions Q2 to Q4). Every
session's Claude Code process offers its own built-ins, each an item of the access source
`builtin:claude-code` in Settings → Tools & plugins
([`agent/src/access/nativeBuiltins.ts`](../../agent/src/access/nativeBuiltins.ts)):

| Tool | Default access |
|---|---|
| `Read`, `Glob`, `Grep`, `TaskCreate`, `TaskGet`, `TaskList`, `TaskUpdate` | `read` |
| `Write`, `Edit`, `NotebookEdit` | `write` |
| `Bash`, `PowerShell` | `off` |

- They run in the session's working directory (§13), the process's cwd. The permission
  seam refuses a file tool's path outside it: an absolute path elsewhere, `..`, or a link
  whose target is outside
  ([`agent/src/harness/confine.ts`](../../agent/src/harness/confine.ts), an input guard in
  both canUseTool and the PreToolUse hook). A link swapped in between the check and the
  tool's own open is still followed; only a process the session runs (a Monitor command, an
  allowed package's `Bash`) can make one.
- `Bash` and `PowerShell` are `off` because Claude Code's sandbox cannot run in the worker
  pod. Measured on 2026-11-01 in `scadbuddy-agent-worker` (read-only): `unshare --user
  --map-root-user --pid --fork id` works, but with `--mount` it fails with "cannot change
  root filesystem propagation: Permission denied" (AppArmor `cri-containerd.apparmor.d`,
  enforce), and the image has no `bwrap`. Unsandboxed, `Bash` could read the KEK mount and
  every session's directory. A sandbox pod (#779) comes first.
- An `allow_refused` package's `builtinTools` only switch an `off` built-in on, at the
  package's own tier for it (`outward` by default, so each call parks). They no longer
  decide which built-ins exist.
- The task list. Measured on the bundled Claude Code (SDK 0.3.310): `TaskCreate` keeps it
  in `$CLAUDE_CONFIG_DIR/tasks/<session id>/<n>.json`, in neither the transcript nor the
  cwd. The config directory is the pod's own, so that path is a link to the session
  directory's `_claude/tasks/` (`stateDirs.ts` `linkTaskList`). The task files are then
  workspace files, written back with the directory and refilled at any pod's placement
  (`test/nativeBuiltins.sdk.test.ts`).
- **Breaking for package authors.** The retired durable turn's `builtin__<Name>` file and
  task tools (`Read`, `Write`, `Edit`, `Glob`, `Grep`, `NotebookEdit`, the `Task*` tools,
  `ReportFindings`, `EnterWorktree`, `ExitWorktree`, `SendMessage`) and their `/workspace`
  paths are gone, and so is the old task list file `_claude/tasks.json`. A skill names
  Claude Code's own tools, with paths in the session directory.

The agent still provides these in place of Claude Code's, from the in-process `builtin`
server (`agent/src/builtins/server.ts`, `mcp__builtin__<Name>`), each outward:

- `WebFetch` fetches through `http_request`'s exchange (every hop egress-checked and
  pinned; a cross-host redirect is returned, as Claude Code returns it; http is not
  upgraded), converts the page with turndown and asks a small model (`haiku`) about it in
  a one-shot Claude Code query with no tools, on the session's credential (#2684). Claude
  Code's own WebFetch skips the egress check (decision Q2);
- `WebSearch` runs Claude Code's own WebSearch in a one-shot query that may make only the
  search asked for, and returns its result as it is.

The one-shot queries' cost is added to the session's `cost_usd`, which its budget reads,
as a turn counts Claude Code's own calls.

`CronCreate`, `CronDelete`, `CronList`, `ScheduleWakeup`, `Monitor`, `Workflow` and `TaskStop` are kept by the
session, in both modes (`agent/src/background/`). Claude Code keeps its own jobs in its
process ("Session-only (not written to disk, dies when Claude exits)"), which a turn
ends, so a classic turn gets these in place of its own too (`mcp__builtin__<Name>`, the
`builtin` in-process server, `sessions/manager.ts`). Each task is a row of
`ai_background_tasks` and what fires it on `agent-tools`, which carries only the id: a
recurring cron job is a Temporal Schedule `bgtask-<session>-<id>` (its cron in the task's
timezone, ending at its expiry) whose every fire is a short `BackgroundFire` workflow
(`background/temporal.ts`, unified runtime plan Step 9.8; jobs made before it are converted
at start, and an hourly pass ends any job whose last occurrence before its expiry was
missed). A job with no occurrence before its expiry (a monthly one, say) is not kept.
A cron takes numbers, `*`, ranges, steps, lists and month or day names. `L`, `W`, `#` and
`?` are refused, because Temporal refuses them or reads them differently. A job stored with
them before keeps its timer workflow until it expires. A job whose day
fields are both restricted is two Schedule expressions, so it fires when either day field
matches, as standard cron does (`background/schedule.ts` `scheduleCrons`). A fire's
payloads are its task id and outcome only, so they are not sealed. Any other task is a `BackgroundTask` workflow `bgtask-<session>-<id>`
(`temporal/backgroundTask.ts`). When the task is due it sends
the prompt as the session's next message, as the one who made it and only while they
still own the session, once the session is idle (asked again every 30 s; a cron
occurrence is given up after an hour busy). Bounds: 10 active tasks per session;
recurring cron jobs expire after 7 days, as Claude Code's; one wakeup at a time, clamped
to [60, 3600] s. Cron times are in the agent's timezone (`TZ`, UTC unless the
deployment sets one), which every answer names. The panel lists a session's running tasks
above the conversation and its owner cancels them (`routes/backgroundTasks.ts`). A
task's workflow is `bgtask-<session id>-<task id>`, which both payload codecs seal under
the session's data key; archiving the chat or marking it done ends its tasks, and
`forgetSubject` ends them and terminates their workflows before it deletes the key.

A `Monitor` is a task of the same kind whose source runs as one heartbeating activity
where the session's Bash runs: a classic session's command in its session directory on
the replica that ran the turn (`agent-tools.<hostname>`, `background/monitor.ts`), a
durable session's in agent-durable's working directory (`scadbuddy_durable/background.py`,
on the `agent` queue), a WebSocket on any agent (egress-checked, loopback refused, and connected only to the addresses the
check passed, with no redirect). A line is cut at 8000 characters and the rest of it
dropped, and so is a batch, so a command that never writes a newline holds no more. The
command gets a shell's environment only (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TZ`, `TERM`),
never the service's. As Claude Code's: stdout lines within 200 ms are one event, exit ends
the watch with its code, it expires after `timeout_ms` (default 5 minutes, at most 30),
and more than 200 events stop it. Each event is a row of `ai_background_events`; the
source signals the workflow, which sends the undelivered ones, as Claude Code's
`<task-notification>` blocks, redacted of every saved credential and inside the
untrusted-data envelope every tool result gets
(what a command prints or a socket sends is data, never the user's words), as the
session's next message once it is idle (Claude Code
shows them mid-turn; here they wait for the turn to end), and the watch's end last.
One message carries at most 64k characters of events (`DELIVER_MAX_CHARS`, `store.ts`)
and says how many follow; the rest go in the next once the session is idle again.
A classic session's command runs on one replica's queue, so a rollout that replaces that
pod ends it as `failed` (its heartbeat stops and it is not retried): re-arm it after a
deploy. A durable session's command and any WebSocket are not tied to one pod this way.
A `Workflow` is a task of the same kind too (Claude Code's tool, its texts measured on
2.1.296). Its script runs as one heartbeating activity on any agent (`wf_run`), in
QuickJS compiled to WebAssembly (`background/workflowRunner.ts`), never in the service's
own JavaScript: a script is model-written code, and Node's `vm`, Temporal's workflow
sandbox included, is not a boundary. Inside it there is no `process`, `require`, `fetch`,
file or clock (`new Date()`, `Date.now()` and `Math.random()` throw, as Claude Code's
do), memory is capped at 64 MiB, and a stretch that runs 5 s without awaiting is
stopped. Each `agent()` is a chat of its own under the session (`SessionManager.startChild`,
`background/workflowAgents.ts`): the session's mode, its budget (the lineage's, as a
fork's), owned by whoever started the workflow, so its tool calls park in the gate as
theirs do. Its task is framed as Claude Code's harness frames it, its instructions are
Claude Code's for a workflow subagent, and its result is its final reply; with
`schema`, the JSON object it replies with, checked (`z.fromJSONSchema`) and asked for
again twice at most. At most 4 run at once and 50 in all, and a run expires after 6 hours.
An agent's `model` and `effort` are its chat's own (`ai_sessions.agent_model`,
`agent_effort`, read by a classic turn and by the durable runner in place of Settings');
`agentType` names the plugin's subagent definition (`agents/<name>.md`) the chat's turns run
as: the definition passed in `agents` and named by `agent` (`harness/agentDefinitions.ts`
in a classic turn, `--agents`/`--agent` in a durable segment; Claude Code 2.1.296 does not
take a plugin's own agent for the main thread, measured); `general-purpose` is none, and an
unknown name fails the call with the list there is; `isolation: 'worktree'` fails
the call as `EnterWorktree` does, the workspace being no git repository. Each result is
journalled (`ai_workflow_calls`, by the call's key and occurrence), so a retry of the
activity after its worker died, and `resumeFromRunId`, answer the calls already made.
Every child chat a run starts is recorded (`ai_workflow_children`); an attempt first ends
(interrupts and marks done) the ones an earlier attempt left open and no journal row names,
since a worker that died never ran its agents' cleanup (#3395).
The script is kept in the workspace (`/workspace/_claude/workflows/<name>-wf_<id>.js`),
which `scriptPath` and `workflow({scriptPath})` read. Its end is Claude Code's
`<task-notification>`, sent as a monitor's events are.
`PushNotification` is not provided yet, and the session says so. The
Temporal plugin owns the engine's settings, so a durable turn cannot set
`disableSkillShellExecution`, and skill shell injection follows Claude Code's default
there. Only an allowed package can carry one (`vet.ts` refuses it everywhere else), and
it runs only with `Bash` offered, as in a classic turn (run.ts `builtinTools`). This was
not measured in a durable engine. Each listing touches the copies it shares, and each
query the `plugins-local/` copy it loads; every replica removes, once an hour, the copies
on the volume and its own `plugins-local/` copies that nothing has touched for 7 days,
and copies left half-made for an hour (`sweepPackageCopies`, #2395; a copy is moved aside and checked again before it goes, so a turn sharing it at that moment keeps it). An enabled package
is shared again at its next turn.

### Items and switches (#4757)

Each part of a package is an item of its capabilities source `package:<name>`, with its
own `ai_access` row (spec
[§4.2](../superpowers/specs/2026-10-18-settings-and-tool-access-design.md), §4.4). The
user sets them in Settings → Tools & plugins → Packages
([`frontend/src/components/settings/PluginPackages.tsx`](../../frontend/src/components/settings/PluginPackages.tsx)).

- **Item ids come from the source**, as the runtime keys them
  ([`agent/src/access/items.ts`](../../agent/src/access/items.ts),
  `packageItems` in [`agent/src/plugins/packages/vet.ts`](../../agent/src/plugins/packages/vet.ts)):
  `skill:<name>`, `subagent:<name>` (a nested agent is `dir:name`),
  `hook:<event>:<id>` and `builtin:<Tool>`. A hook's id is the first 16 hex digits of the
  sha256 of the canonical JSON of its event, matcher and handler (`hookId`). Two
  byte-identical hooks under one event and matcher share one id and one access. A package
  that is one skill (no `skills` path) is the skill `rootSkillName(<package name>)`.
- **Commands are skill items.** Claude Code runs a plugin's slash command through the Skill
  tool, so a skill and a command of one name are one item. Measured on Claude Code 2.1.310
  (the `claude` binary that SDK 0.3.310 bundles, read with `strings -n 20`; the comment on
  `skills` in `vet.ts`).
- **Names over 1024 UTF-16 units are refused.** A skill or subagent name, or a frontmatter
  `name`, longer than `ALIAS_KEY_CAP` (1024) is a fatal vet problem (`listingProblems` in `vet.ts`):
  no approval allows it. A pin approved before this check, with such a name, stops loading
  until it is re-pinned. So the call rule below never compares a cut name.
- **Listing budgets.** One package's items and aliases may take at most 256 KiB of UTF-8
  (`LISTING_ITEMS_BUDGET`), else vet refuses it. All of a turn's packages may take at most
  1 MiB (`LISTING_PACKAGES_BUDGET`, half Temporal's payload limit). Packages are taken in
  name order: one that would take the total past 1 MiB is not loaded, and the turn says
  so; the others still are. `loadPackagesForRun`
  ([`install.ts`](../../agent/src/plugins/packages/install.ts)) decides this once for
  classic and durable turns, so both leave out the same packages.
- **The derived copy.** A turn loads a copy of each package that leaves out its http
  servers and every hook, skill, subagent and local server that is off
  (`derivedPackage` in [`agent/src/plugins/packages/derived.ts`](../../agent/src/plugins/packages/derived.ts)).
  The copy is first checked against the pin's hash, then made, then named by its own hash:
  `<root>/<hex>/<package name>`. So two turns with different access to one pin each get
  their own copy. The folder carries the package name because Claude Code 2.1.310 names a
  root skill with no frontmatter `name`, and a plugin with no manifest name, by its folder
  (the same `strings` read, comment at the top of `derived.ts`). A copy already there is
  kept only while it hashes to the fresh copy.
- **Re-pins.** A row is kept while the new pin has its item. A hook keeps its id, and so its
  access, while its event, matcher and handler do not change. A changed or new hook gets a
  new id, so it is back at its default (`on`), and the re-pin's review marks it "New since
  you last reviewed". `pruneStaleRows`
  ([`agent/src/plugins/packages/store.ts`](../../agent/src/plugins/packages/store.ts)) runs
  in every package write's transaction. It deletes the rows of items and servers the pin
  lost, and the listings of servers it lost. A review stored by an older vet cannot name
  its items, so for it no item row is deleted. Rows an older replica's approval leaves go
  with the next write to that package by this code; there is no sweep.
- **Skill, Agent and Task calls are judged per call, in both runtimes.** A call that names
  a skill or subagent that is off, or one of a package that is switched off, is refused
  from its next call. A classic turn refuses it at the permission seam
  ([`agent/src/access/live.ts`](../../agent/src/access/live.ts)). A durable turn refuses it
  in the segment runner's own `PreToolUse` hook (`CallAccess` in
  `agent-durable/src/scadbuddy_durable/session/runner.py`), which reads `ai_access` per
  call and refuses the call when the read fails. One rule (`offCallItem`,
  `off_call_item`) is pinned for both languages by the `calls` set of
  `agent/test/fixtures/access-vectors.json`. The call's name is read as Claude Code
  2.1.310 reads it (the comments on `calledName` and `agentTypeForm` in `items.ts`):
  - Skill: trimmed by JavaScript's `trim()` set, one leading `/` dropped, then matched
    exactly, by name or alias.
  - Agent and Task: matched exactly; else every subagent whose name is equal after NFKC,
    lower case and without white space, dashes or underscores. The call is refused when
    any match it reaches is off.
- **Hooks change per turn, in both runtimes.** No SDK option drops one plugin hook from a
  running query (spec §2), so a hook switched off is left out of the next turn's copy.
- **Built-ins.** Claude Code's own built-ins are items of `builtin:claude-code` (§9). For a
  pin approved with `allow_refused`, each Claude Code built-in it names is also an item
  `builtin:<Tool>`, with a tool's access; for one of `builtin:claude-code`'s it counts only
  while that item is `off`, as the switch that turns it on. Where several packages of the
  turn name one built-in, the strictest wins (`off` > `outward` > `write` > `read`), and a
  package that names it with no row counts as `outward` (`strictest` in `items.ts`). Each
  call takes its access as it is then:
  - A classic turn decides at the permission seam.
  - A durable turn asks the `builtin_tier` activity
    ([`agent/src/temporal/toolTier.ts`](../../agent/src/temporal/toolTier.ts)) for `Bash`
    and `PowerShell`, at most 3 attempts, then parks
    (`agent-durable/src/scadbuddy_durable/session/workflow.py`, behind
    `workflow.patched("builtin-access-4757")`). The other built-ins are the `builtin` turn
    tools, judged by the turn server's tier.
  - Skill, Agent and Task as built-ins run inside the segment: an outward call parks
    through the segment runner's hook and the workflow's `builtin_call` Update, behind the
    same patch. So an outward package built-in parks for approval in both modes.
    A large input parks too (#5000). The Update's argument goes through the client's
    data converter, and Temporal's External Storage moves every payload at or over
    `OFFLOAD_BYTES` (128 KiB) out of history
    ([`payload_store.py`](../../agent-durable/src/scadbuddy_durable/payload_store.py)).
    In temporalio 1.34.0 (the version `agent-durable/uv.lock` pins),
    `_build_update_workflow_execution_request` in
    [`temporalio/client/_impl.py`](https://github.com/temporalio/sdk-python/blob/1.34.0/temporalio/client/_impl.py)
    encodes the arguments with `DataConverter.encode` in
    [`temporalio/converter/_data_converter.py`](https://github.com/temporalio/sdk-python/blob/1.34.0/temporalio/converter/_data_converter.py).
    That step seals each payload, then stores the large ones. History then holds a
    reference only.
    `test_a_hooked_call_over_the_offload_size_parks_with_history_holding_a_reference`
    in `agent-durable/tests/test_session_workflow.py` measures this on a dev server.
    Only a worker without `SCADBUDDY_PAYLOAD_DIR` (so with no packages) still refuses an
    input over 128 KiB unasked (`BUILTIN_CALL_INPUT_MAX`), so its history stays bounded.
- **Package servers.** Each server is a child source `package:<name>/mcp:<server>`,
  on by default while the package is approved. A server switched off is hidden at turn
  start and refused per call, fail closed: by the forwarder
  ([`agent/src/plugins/forwarder.ts`](../../agent/src/plugins/forwarder.ts)), by
  `turn_tool_tier` and by the turn activity
  ([`agent/src/temporal/turnTools.ts`](../../agent/src/temporal/turnTools.ts)). A read
  that fails refuses the call. Every path refuses it in one sentence, a classic turn's
  (`toolOffReason` in [`agent/src/access/items.ts`](../../agent/src/access/items.ts)). A
  tool set off, a served built-in switched off, or a tool its server no longer lists is
  refused at `turn_tool_tier`, never parked. An allowed package's local (stdio) servers
  take the same switch and stored tool access in both runtimes (`localPackageServers` in
  `packageServers.ts`; a turn's copy leaves an off one out). Their tools are not listed on
  the page, since that would start the package's process outside a turn; until set, they
  are `outward`. "List tools" lists an http server's tools
  (`POST /api/v1/ai/capabilities/{source}/refresh`).
- **Server names.** Claude Code names a package server's tools after
  `plugin_<package>_<server>`. Two servers that give one name are both refused, and so is
  one whose name holds `__`, which splits a tool name: "…has a name that cannot be
  offered" ([`agent/src/plugins/packages/mcp.ts`](../../agent/src/plugins/packages/mcp.ts)
  `collisionProblem`, `unofferableNameProblem`).
- **Capabilities.** A source's switch folds onto its items: while it is off, each item
  reads `off`, saying why, and keeps its own choice in `own_access`
  ([`agent/src/access/capabilities.ts`](../../agent/src/access/capabilities.ts) `switched`).
  A package that a turn would not load reads off, with `problem` saying why (refused at
  load, past the listing budget, named as a built-in). One whose load check has not
  answered yet reads `checking`, with every item off
  ([`agent/src/access/packageProvider.ts`](../../agent/src/access/packageProvider.ts)
  `stateOf`). The page reads the tree again while anything is `checking`, after 1, 2, 4
  and 8 s, then every 10 s, 125 s in all; then it says "Still checking…" with a Retry
  button (`POLL_DELAYS` in
  [`frontend/src/components/capabilities/useCapabilities.ts`](../../frontend/src/components/capabilities/useCapabilities.ts)).

**Rolling deploy.** Every write lands in `ai_access` or `ai_plugin_packages`, which old
and new replicas read, so none is lost. An older replica ignores the new switches and
items until it is replaced. Roll the agent before agent-durable:

- An older agent-durable refuses the `<hex>/<name>` folders the new agent shares, so
  packages are left out of durable turns until agent-durable rolls (fail closed).
- A listing from a pre-#4757 agent names no items, so a new durable worker lets an off
  package skill load until that agent is replaced.
- An older agent-durable has no `own_off` and no `CallAccess`. A skill or subagent of
  ScadBuddy's own plugin that is switched off on the new page still loads and runs in
  durable turns until agent-durable rolls. A classic turn refuses it at once.

Settings has an "Assistant plugins" area with two sections: "Plugin packages"
([`frontend/src/components/settings/PluginPackages.tsx`](../../frontend/src/components/settings/PluginPackages.tsx))
and "Plugins", the list of `/api/v1/ai/plugins`
([`RemotePlugins.tsx`](../../frontend/src/components/settings/RemotePlugins.tsx)): ScadBuddy's
own tool set first (#1953, `built_in: true`; `scadbuddy` is every registry tool, from
[`agent/src/plugins/builtInTools.ts`](../../agent/src/plugins/builtInTools.ts)), then the
remote MCP plugins. A built-in tool's access is changed in Settings → Tools & plugins
(`/api/v1/ai/capabilities`, below); `PATCH /plugins/{name}` takes none of it (#4828). The overrides are
`ai_access` rows (`builtin:scadbuddy`, or `builtin:browser` for the browser tools; see
"Tool access" below), applied at the
harness seam, on `/mcp` and in the durable `agent-tools` activities. Under the patch
`tool-access-2171`, every registry call in a durable session stops at the gate, which asks
`tool_tier` for the tool's access as it is then and lets the call through unless it is
`outward` (`agent-durable/src/scadbuddy_durable/session/workflow.py`, module docstring).
So a tool raised to `outward` parks, and one lowered stops parking, from its next call.
Adding, removing,
testing or re-pointing a built-in answers 409 with `built_in: true`.
You install, review, approve and re-pin packages there. The approval dialog shows the
full commit SHA and content hash, and you must tick a confirmation before it sends
exactly those values. Install, approve, re-pin and remove are user-only (`USER_ONLY`), so
the in-page agent's `click` and `fill` refuse them: they decide which code is trusted. A
package's switch and its items' selects are not, as every access control on the page is
not (#4208). The area follows the assistant's
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

Change them in Settings → MCP access → **Auth mode** (shown where AI is available, beside
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

## 11. Automatic Hindsight memory

When a session's turn loads an enabled remote MCP plugin named `hindsight` (#297,
`ai_plugins`), the agent recalls and retains memory itself instead of leaving it to the
model. In production the model called `recall` once and `retain` never across four
sessions. The code is [`agent/src/memory/hindsight.ts`](../../agent/src/memory/hindsight.ts),
a port of `create_memory_hooks` in Hindsight's Claude Agent SDK integration
([`hooks.py` @ eb021da3](https://github.com/vectorize-io/hindsight/blob/eb021da3b2501911e4b57c82b3de1123572a200e/hindsight-integrations/claude-agent-sdk/hindsight_claude_agent_sdk/hooks.py)),
registered as in-process SDK hooks by `harness/run.ts`. The API
base and bank come from the plugin's URL (`…/mcp/<bank>/` gives
`/v1/default/banks/<bank>/…`), and requests carry the plugin's stored header to the
address the plugin check pinned, with no redirects (`agent/src/http/pinned.ts`, shared
with the forwarder).

- **Recall** (`UserPromptSubmit`): the prompt is the query, and the top five memories
  are added as `additionalContext`, wrapped as untrusted data (#258). It gives up after
  3 s (`RECALL_TIMEOUT_MS`); on a timeout or error nothing is added and the failure is
  logged.
- **The recall query's length** (#2655). Only the start of the redacted query is sent:
  `recallMaxQueryChars` characters (code points, so a non-BMP character is never split),
  800 by default and `0` for all of it. This is the Claude Code Hindsight plugin's
  setting of the same name (env `HINDSIGHT_RECALL_MAX_QUERY_CHARS`, `query[:max]`).
  Settings → Assistant → Hindsight memory writes it to `ai_settings`
  `memory_recall_max_query_chars` through `PUT /api/v1/ai/settings/memory`
  ([`agent/src/routes/memorySettings.ts`](../../agent/src/routes/memorySettings.ts)), and
  both session modes read it at each turn. The bank's own limit is in tokens,
  `HINDSIGHT_API_RECALL_MAX_QUERY_TOKENS` on the Hindsight server (default 500, counted
  with tiktoken), which refuses a longer query with HTTP 400 `Query too long: N tokens
  exceeds maximum of M`. Since the two are set apart (and punctuation-dense text such as
  pasted OpenSCAD runs about one token per two characters), such a refusal is retried
  once with the query cut to its length × M/N, less a fifth, or to half when the reply
  does not give both numbers, and logged. A second refusal is an ordinary recall failure.
- **Retain** (`Stop`): the session's user and assistant text (tool results left out,
  secrets redacted) is upserted as one document, `conversation:<session_id>`, so every
  turn replaces the same document. It runs in the background and never fails or delays
  the turn; a failure is logged. On a session's first turn the transcript file does not
  exist yet when Stop fires, so that turn retains the prompt and the final reply, and
  the next turn's upsert carries the whole session.
- **Tool results** (`PostToolUse`): off unless `retainOnTools` names tools.

Upstream's knobs are kept as `MemoryHookConfig` (`DEFAULT_MEMORY_HOOK_CONFIG`). One is
ScadBuddy's own: `retainMode` is `'transcript'` (the default, above) or `'result'`,
upstream's behaviour of retaining only the last result with no document id.

Every session is an AgentSession (plan 2026-10-13 Step 6.3), whose turn runs in the
agent with these hooks, as above. The retired Python turn's `memory_recall` and
`memory_retain` activities (#2320) were removed in plan Step 8.2.

## Tool access (`ai_access`, #2171)

One table holds what the user set for every tool, hook, skill and subagent the assistant
can use ([spec §4](../superpowers/specs/2026-10-18-settings-and-tool-access-design.md), §4.1 to §4.3). The user sets it in Settings → Tools & plugins.

- One row per (`source`, `item`). The sources are `builtin:scadbuddy`, `builtin:browser`,
  `builtin:http`, `builtin:own-plugin`, `remote:<name>`, `package:<name>` and
  `package:<name>/mcp:<server>` (`agent/src/access/model.ts`).
- Item `*` is the source switch: `on` or `off`. Built-in sources and package servers
  (`package:<name>/mcp:<server>`) are on until turned off; a server is offered only while
  its package is approved and enabled. Remote plugins are off until turned on
  (`enabledByDefault` in `agent/src/access/resolve.ts`). A package's own switch is not a
  `*` row: it is `ai_plugin_packages.enabled`, served as the package source's `enabled`
  (#4757, [`agent/src/access/packageProvider.ts`](../../agent/src/access/packageProvider.ts)).
- `builtin:browser`'s switch holds at every gate (#4757): while it is off, every browser
  tool is `off` at the classic seam, on `/mcp`, in durable turns and in flow runs
  (`registryOverrides` in `agent/src/access/resolve.ts`; `BROWSER_SOURCE` in
  `agent-durable/src/scadbuddy_durable/session/access.py`).
- In `package:<name>` and `builtin:own-plugin`, an item's kind is its prefix
  (`agent/src/access/items.ts`): `skill:<name>`, `subagent:<name>` and
  `hook:<event>:<id>` take `on` or `off`; `builtin:<Tool>` takes a tool's access. A
  plugin's commands are `skill:` items, since Claude Code runs them through the Skill
  tool (measured on Claude Code 2.1.310; see §9, "Items and switches"). A built-in named
  by several packages of a turn takes the strictest of their accesses, and a package that
  names it with no row counts as `outward`. So this rule is stricter than a tool's: an
  unset built-in asks first.
- Any other item is a tool, with `off`, `read`, `write` or `outward`. A tool with no row has
  its declared tier. The user may set it above or below that tier, with one exception.
  On the registry sources (`builtin:scadbuddy`, `builtin:browser`), a row that the copy
  migration wrote (`updated_by` is `migration:2171`) counts only when it is `off` or above
  the declared tier. A copied lower tier is ignored, because the old registry overrides
  could only raise (`toolAccess` in `agent/src/access/resolve.ts`;
  `tool_access` in `agent-durable/src/scadbuddy_durable/session/access.py`). A row the
  user writes later counts in either direction.
- `on` is also allowed on a single item, but only in `package:*` sources and in
  `builtin:own-plugin` (its skills, subagents and hooks). The table's CHECK enforces this
  (`agent/src/db/migrations/20261026T2200Z_access.sql`).
- A source's switch folds onto its items when they are read: while it is off, each reads
  `access: off` with a reason, and `own_access` keeps the item's own choice, which its
  select shows and edits and Reset resets (`switched` in `agent/src/access/capabilities.ts`).
  A write sets the item's own row; it may be written while its source is off.
- Only overrides are stored. The resolver reads the rows at each call, so a change applies
  from the next call, in either direction, also in a turn or flow run under way
  (spec §4.4; `agent/src/access/resolver.ts`).
- The same access holds from every caller: main agent, subagent, classic or durable turn,
  flow run and `/mcp` (spec §4.1, §4.5). agent-durable reads the same rows
  (`agent-durable/src/scadbuddy_durable/session/access.py`).
- The old keys no longer decide. These are `ai_settings` `builtin_tools.scadbuddy`,
  `scadbuddy_plugin_enabled`, `http_request_enabled` and `printer_camera_enabled`, and
  `ai_plugins` `enabled`, `tool_tiers` and `disabled_tools`. Migration
  `agent/src/db/migrations/20261026T2200Z_access.sql` made the table.
  `20261026T2201Z_access_copy.sql` copied the old values into it (`updated_by` is
  `migration:2171`). `20261030T1020Z_access_drop_mirrors.sql` (#4828) dropped the two mirror
  triggers and deleted the four `ai_settings` keys (and the stray `builtin_tools.claude`). The agent writes `ai_access` only.
  `20261101T1527Z_access_drop_columns.sql` (#5014) dropped the three `ai_plugins` columns and
  deleted the four keys again, for those that older replicas wrote after the first delete.
  Rolling the agent back past #4838 is not supported: that build
  reads the deleted keys as absent, which means on. Rolling back to a build that names the
  dropped columns (any build before #4828) is not supported either: its plugin reads and writes
  fail. Build `sha-7626da9` and later are safe. The copied `migration:2171` rows stay.
  See [`20261026T2201Z_access_copy.sql`](../../agent/src/db/migrations/20261026T2201Z_access_copy.sql),
  [`20261030T1020Z_access_drop_mirrors.sql`](../../agent/src/db/migrations/20261030T1020Z_access_drop_mirrors.sql)
  and [`20261101T1527Z_access_drop_columns.sql`](../../agent/src/db/migrations/20261101T1527Z_access_drop_columns.sql).
- The printer camera is the access of `get_printer_camera` (`builtin:scadbuddy`): `off`
  replaces the old switch (spec §4.2, #1911). The old route `/api/v1/ai/settings/printer-camera`
  is gone (#4828): change it in Settings → Tools & plugins, or with
  `PUT /api/v1/ai/capabilities/builtin:scadbuddy/items/get_printer_camera`.

### The capabilities API

Settings → Tools & plugins reads and writes `ai_access` through seven agent routes
([`agent/src/routes/capabilities.ts`](../../agent/src/routes/capabilities.ts), over
`agent/src/access/capabilities.ts` and `agent/src/access/providers.ts`; spec §4.6):

| Route | |
|---|---|
| `GET /api/v1/ai/capabilities` | `{ sources }`: every source with its items (`id`, `kind`, `name`, `harness_name`, `label`, `description`, `default`, `access`, `own_access`, `overridden`, `allowed`, `fixed`, `reason`, …), its `enabled` and `switchable`, its `problem` and `checking` (a package's load check), its `children` (a package's servers), its `options_route` and, for a remote plugin or a package server, `listed_at`. The sources are the five built-ins (`builtin:claude-code` among them, Step 9.6), each remote plugin, and each installed package `package:<name>` with a child `package:<name>/mcp:<server>` per server; a package is `switchable` only once its pin is approved |
| `GET /api/v1/ai/capabilities/{source}` | one source |
| `PUT /api/v1/ai/capabilities/{source}` | `{ "enabled": true\|false }`: the source switch (item `*`); 409 on a source that is not `switchable` |
| `GET /api/v1/ai/capabilities/{source}/items/{item}` | the item with its input and output schemas, annotations, the raw listed entry and what the caps `omitted` |
| `PUT /api/v1/ai/capabilities/{source}/items/{item}` | `{ "access": … }`: `off`, `read`, `write` or `outward` for a tool or a built-in; `on` or `off` for a hook, skill or subagent. Choosing the default deletes the row. 400 on an access the kind does not take, 409 on a `fixed` item or one outside `allowed` |
| `DELETE /api/v1/ai/capabilities/{source}/items/{item}` | back to the default |
| `POST /api/v1/ai/capabilities/{source}/refresh` | `{ test, source }`: a remote plugin's connection test (without its `tools`: they are the source's items) and its source after it. A good test stores the listing (`testAndList` in `agent/src/routes/plugins.ts`, shared with `POST /plugins/{name}/test`, one at a time per plugin across replicas; a second is 429 with `Retry-After`, as is a fifth test at once on one replica; 503 when the database is unreachable). For a package's http server (#4757), the same test through the forwarder, stored in `ai_tool_listings` (`listPackageServer` in `agent/src/plugins/packages/listing.ts`); refused, with no connection, for a server a turn would not offer (its package off, unapproved, not loadable or still `checking`, the server switched off, or its name refused; `listingRefusal` in `agent/src/access/packageProvider.ts`), and 409 for a local server, which only a turn starts. 409 on a source that lists no tools |

- A source or item id is one percent-encoded path segment (`remote%3Ahindsight`). An
  unknown one is 404. Every other write answers the source as it is after the write.
- The refusals: 400 (a bad body or access), 403 (a guard), 404, 409, 413 (a body over
  4 KiB), 429 (a test already running), 502 (a refresh whose test failed with any other status,
  `refresh` in `agent/src/access/providers.ts`) and 503 (no database, or its migrations have not applied), each as `{ detail }`.
- The guards are the plugin routes' guards (`agent/src/routes/guard.ts`): reads behind
  `uiReadProblem`, writes and the refresh behind `uiRequestProblem`, JSON bodies of at
  most 4 KiB (`jsonBodyLimit`). These are the user's settings writes. No agent tool
  calls them (`AGENT_ROUTES` in `agent/src/tools/coverage.ts`): the in-page assistant
  changes an access only by working the page a person has open, through the browser
  bridge (#4208).
- Built-in writes go through `AccessStore` (`agent/src/access/store.ts`), which audits
  them and sends the warm eviction. Remote writes go through `PluginRepo`
  (`agent/src/plugins/registry.ts` `setToolAccess`) under the plugin's row lock. They record the writer in
  `ai_access.updated_by` (`browser:browser` from the page, `updatedBy` in
  `agent/src/access/store.ts`) and are not audited, as remote plugin writes are not.
- A remote tool's label is its MCP `title`, else `annotations.title`, else its name
  ([MCP tools, "Tool"](https://modelcontextprotocol.io/specification/2025-06-18/server/tools#tool)).
  Claude Code renames a tool name's characters outside `[A-Za-z0-9_-]` to `_`
  (`harnessToolName`), so a renamed tool is `allowed: ["off", "outward"]`, and tools
  that collide on one harness name are both `fixed` `off`.
- A listing is kept in `ai_tool_listings`: each `tools/list` entry once, as the server
  sent it, with its parts derived when read (`agent/src/access/listings.ts`). The caps
  (`LISTING_CAPS`): 512 tools, a name of at most 128 characters (a longer one is
  dropped), a title of 1,024, a description of 8,192, 64 KiB of JSON per schema, 16 KiB
  for the annotations and for the other fields together, and 2 MiB per listing. Over the
  listing cap the other fields go first, then the output schemas, the input schemas,
  the annotations and the descriptions. If the listing is still over the cap with only
  names and titles left, then from the first tool whose title no longer fits, every tool
  keeps only its name. Each removal is named in the tool's `omitted`.
- The page asks one confirmation line when a choice ranks below the item's default
  (`off` < `read` < `write` < `outward`; a by-call default such as `http_request`'s ranks
  as `outward`), and nothing more. The routes ask nothing: a write takes effect from the
  next call.

## 12. The HTTP request tool (#827)

Session turns get an `http_request` tool ("curl"; `agent/src/harness/httpRequest.ts`)
unless Settings → Tools & plugins turns it off. It is on by default; the
switch is the `ai_access` row (`builtin:http`, `*`) (`off` turns it off),
written by `PUT /api/v1/ai/capabilities/builtin:http`
([`agent/src/routes/capabilities.ts`](../../agent/src/routes/capabilities.ts)); the old
`/api/v1/ai/settings/http-request` route is gone (#4828). The turn's tool
list is fixed when the turn starts (`agent/src/sessions/turnRunner.ts`), so turning the
tool on or off changes what the next turn offers. The access of each call is read per
call (`agent/src/access/live.ts`), so `off` refuses a call in a turn under way.

- **Reach is open until the sandbox** (decided 2026-09-30): the internet, the LAN and
  cluster services, plain `http:` included. Only link-local and cloud metadata
  addresses are refused. If the pod must not reach something, an egress NetworkPolicy
  is the way to say so; see [security.md](security.md#http-request-tool-827).
- `GET` and `HEAD` run at once; `POST`, `PUT`, `PATCH` and `DELETE` wait for an
  approval in the UI.
- Limits: 30 s per request by default (the model may ask for up to 120 s), 5
  redirects, 1 MiB inline and 20 MiB read. A binary body is a workspace file (§13,
  #2511), never base64 in the result. A longer text body is saved in
  Postgres (`ai_session_http_bodies`, #2374), the 10 newest per session, classic and
  durable alike, so any replica pages it with `http_response_read`. That is up to
  10 × 20 MiB of plaintext `bytea` a session: size the database for it beside the other
  per-session storage. The rows go with the session. A flow run's bodies stay on the
  replica that ran the call, under `durable-turns/flow-<id>/http/`, removed by every
  pod's hourly sweep after 7 days (#2496); that sweep also clears a `durable-turns/<id>/`
  directory a pod left before #2374.
- Every request is listed in Settings → Audits → **AI activity** under **HTTP requests**, with
  its method, host, status and size. A request whose body was saved has a **View /
  download body** link (#1292): plain text and JSON open as text, anything else
  downloads. It works while the body is among the session's 10 newest, on the replica
  that saved it.

## 13. The workspace (#2329)

The assistant keeps files of its own in `ai_session_files`, in Postgres rather than on
the pod's disk, so every replica, classic and durable sessions and `/mcp` see the same
files ([`agent/src/workspace/store.ts`](../../agent/src/workspace/store.ts), tools in
[`agent/src/tools/workspace.ts`](../../agent/src/tools/workspace.ts)).

- Where the bytes are (#4789): a blob of the backend's content store (`store_backend` s3
  or bambuddy), `workspace-<sha256 of the sealed bytes>`. The agent reads which store the
  backend runs on from its `/healthz` (`store.backend`), and asks its hidden
  `/api/v1/internal/workspace-blobs` routes for a URL to PUT the bytes to (the internal
  blob route) and one to GET them (S3's presigned URL on s3); it holds no store credential
  and mints no token. The bytes are sealed under a key of the file's own, in 1 MiB segments
  ([`agent/src/workspace/sealing.ts`](../../agent/src/workspace/sealing.ts)); that key is
  wrapped under the session's data key (`ai_payload_keys`), or under the KEK for a
  principal's file (re-wrapped at a rotation). A fork's row names the parent's blob with
  the key wrapped again for the child: no bytes are copied. At start the agent moves rows
  still in `data` to blobs (`SessionFiles.moveToBlobs`); a later migration drops `data`.
- Only on `store_backend` local (no content store; the routes answer 404) do the bytes
  stay in `data`, in plaintext, as before. On any other backend a write is refused
  (`unavailable`, HTTP 503 on the panel's Files routes) and nothing is kept in plaintext
  when the agent has no `SCADBUDDY_SECRET_KEY_FILE`, when the backend cannot mint the URLs
  (no `SCADBUDDY_API_INTERNAL_URL` or KEK: its routes answer 503 saying which), when the
  backend is older than the routes (404), or when it cannot be reached. `moveToBlobs`
  refuses the same way and moves nothing. The agent logs the cause once at start, and
  `GET /api/v1/ai/status` shows it as `workspace_files` (`{store: "blobs" | "postgres" |
  "refused", ...}`, checked at most once a minute).
- Rows of both kinds read back only between blob backends (s3 and bambuddy, through
  `/store/migrate`). After a switch back to `store_backend` local the backend's URL route
  answers 404, so every file already in the blob store cannot be read (`unavailable`)
  until the backend is on a blob store again; new writes go to `data`. `workspace_files`
  then carries `unreadable_files`, the count of such rows (also when writes are refused).
  Do not switch a deployment that holds workspace blobs back to local.
- The holder: a trigger on `ai_session_files` (migration
  `20261027T0114Z_session_files_blobs.sql`) writes and removes each row's holder
  `(ai_session_file, <workspace>/<path>)` in the backend's `blob_refs`, in the
  transaction that writes or removes the row (a delete, the sweep, a session's cascade,
  `forgetSubject`), so the blob sweep keeps a blob while a row names it. This assumes
  **one database and one role for both services**: the agent's
  `SCADBUDDY_DATABASE_URL` is the backend's, and its role may write `blob_refs` (the
  agent already reads the backend's `workflow_runs` the same way). Giving the agent a
  database or role of its own breaks every workspace write on s3.
  `agent/test/workspaceBlobs.backend.e2e.test.ts` pins the trigger to the backend's
  table: it migrates both services into one database, runs the real API on moto's S3
  (`backend/tests/support/workspace_backend.py`), and checks the `blob_refs` row; a
  change to that table the trigger does not follow fails it.
- The working directory (plan 2026-10-13 Step 9.5, decision Q5,
  [`agent/src/workspace/directory.ts`](../../agent/src/workspace/directory.ts)): a
  session's Claude Code runs in `<SCADBUDDY_SESSION_DIR>/<session id>/` (§2), a working copy
  of its workspace; `ai_session_files` stays the record. Before each turn the worker makes
  the directory what the workspace holds: it writes each file whose bytes differ and removes
  everything else (a file the workspace no longer holds, what a turn that died never wrote
  back, and every entry that is not a regular file at a workspace path, such as a symlink or a
  dot-file). At each park, every minute of a running turn, and at the turn's end (a released
  or shut-down turn's too), it writes each changed file back and deletes each removed one, by
  sha256 against what it last synced, with the source `{kind: "session_dir"}`. A placement on
  the worker that synced last writes back first what changed since, such as a Monitor
  command's output between turns or a file whose write-back failed, before it removes
  anything. A file the record cannot take now (`unavailable`) is never removed or
  overwritten: placements, the eviction and the sweep keep it until a write-back succeeds.
  While a Monitor command runs, nothing in the directory is removed. A panel upload or a `workspace_*` write during the turn wins over the
  directory's change to the same file, and reaches the directory at the next turn. A turn
  cut off because another placement took it writes nothing back. A worker that dies
  mid-turn loses only that turn's changes since its last sync.
  - As built, against Q5: Q5 put the directory on the NAS share. It is a capped emptyDir of
    each worker instead, refilled at every placement, because the copies are plaintext and the
    share's one backup (`nas-share-backup.yaml`, Velero 1.18's kopia fs-backup) carries the
    whole share and cannot leave a subPath out. A separate NAS share outside that backup stays
    possible if the refill costs too much.
  - Caps: the workspace's. A file past `workspace_file_max_bytes`, or one the
    `workspace_max_bytes` or 5,000-file quota refuses, is removed from the directory at the
    write-back, as is every entry that cannot be synced. Once the workspace holds 5,000 files, a
    write-back offers no new file at all. Each file brings at most three parent directories, so a
    placement makes at most 20,000 entries and never makes a directory the entry check stops. A turn whose directory grows past `workspace_max_bytes`
    is stopped, as is one that holds more than 20,000 entries (`MAX_ENTRIES`; empty files and
    directories cost inodes and walk time but no bytes). The check runs every 15 s
    (`DIR_CHECK_MS`) and is never skipped. A tick is skipped only while the previous walk still
    runs. A walk that takes longer than 10 s (`DIR_WALK_TIMEOUT_MS`) counts as past the cap and
    stops the turn (fail closed), as does a walk that fails. A Monitor command's directory is
    checked the same way (`watchCaps`), between turns too: past a cap, the command is killed and
    the monitor ends failed. A directory or file that a process made unreadable to its owner
    (`chmod 000`) gets its owner's permissions back before it is walked, read or removed. The
    minute's write-back runs beside the walks and never holds
    them up. A walk uses lstat only, and it stops counting once past either cap. So a turn can overrun its cap by at most its
    write rate times 15 s, and the emptyDir's `sizeLimit` must leave room for that. Each refusal logs a line starting
    `session directory cap:`. A session's directory is removed once its warm process on that
    worker closes, unless a Monitor command still runs in it, and only after what changed since
    the last sync is written back. So a worker's disk holds at most `warm_max_per_replica`
    directories, plus those of running monitors. The
    worker's emptyDir `sizeLimit` must be at least `workspace_max_bytes` times
    `warm_max_per_replica`.
  - Isolation: each directory is 0700 and its files 0600. Nothing is read or written through a
    symlink, and no path leaves the directory. All sessions on a worker share one uid, so
    that uid is not a boundary between them: the boundary is these checks, and the
    file-tool seam that Step 9.6 adds.
  - The directory holds the files in plaintext (the record's bytes are sealed):
    `forgetSubject` removes the copy on its own pod, and each worker removes, every hour, the
    directories of sessions whose row is gone. That sweep is what finally removes a copy: the
    `forget-subject` CLI runs outside the worker's own serialisation, so a turn placed there at
    that moment can write the directory again.
  - A write-back at a park or on the minute's timer only offers files. Pruning (strays, capped
    files) happens at the turn's end, at an eviction and at a placement, so a turn keeps its
    `.git` or a large intermediate file while it runs. Claude Code's own file tools (§9) and
    a Monitor's command use the directory.
- A session's files go with the session (deleting it, or `forgetSubject`, deletes them,
  and forgetting deletes the key that opens its blobs); a fork copies them. A call outside any session (an `/mcp` client, a flow run) uses its
  principal's own workspace, whose files are swept 24 h after their last write (every
  10 minutes, `WORKSPACE_SWEEP_MS`).
- Caps: 25 MiB a file, 200 MiB and 5,000 files a workspace by default (the file count is
  fixed, `WORKSPACE_MAX_FILES`), the byte caps the `ai_settings` keys
  `workspace_file_max_bytes` and `workspace_max_bytes` (bytes), which Settings →
  Assistant workspace writes through `PUT /api/v1/ai/settings/workspace`
  `{file_max_bytes, workspace_max_bytes}` (1 MiB to 100 MiB a file, 1 MiB to 2 GiB a
  workspace, the file cap no larger; both in one transaction, each a `settings` audit row;
  [`agent/src/routes/workspaceSettings.ts`](../../agent/src/routes/workspaceSettings.ts)).
  Each is read at every write. Size the database for them.
- A file and a directory never share a name. A write of `notes` while `notes/a.txt` exists, or
  of `notes/a.txt` while the file `notes` exists, is refused `conflict`, because a session's
  working directory cannot hold both. In an older workspace that holds both, a placement leaves
  the file `notes` out of the directory and out of its write-back, so it is never read as deleted.
- Tools, with their tiers: `workspace_list` and `workspace_read` (read),
  `workspace_write`, `workspace_edit` and `workspace_delete` (write), and
  `workspace_save` (read: it saves an output's 3MF, a model's file, thumbnail or media,
  a print's image, timelapse or file, or a Bambuddy library file into the workspace
  through the backend's own routes, so the bytes never pass through the model).
  `workspace_read` pages text as `read_file` does, returns an image as an image (a
  durable session gets its name, to see it with `view_tool_images`, #2402), and only the
  metadata of other binary files. `workspace_edit` is made against the sha256 the model read.
- `http_request` takes `save_to_workspace: "<path>"` (classic and durable sessions, #2496;
  a flow run's starter's own workspace, #2511): the whole body
  becomes a workspace file instead of a saved body (§12). A binary body (anything not
  text, an image included) is stored the same way without it (#2511), at
  `http/<first 12 hex of its sha256>-<the URL's file name>`, so the same body from the
  same URL is one file; the result answers `workspace: {name, mediaType, size, sha256}`
  and never the bytes, and `workspace_read` shows an image as an image. A body cut
  at the 20 MiB read cap, or one over the workspace caps (`workspace_file_max_bytes`,
  `workspace_max_bytes`), is not stored, and the result says why (`workspace.error`);
  a binary one is then kept as a saved body only for the **View / download body**
  link, which `http_response_read` refuses to page.
- Uploads send workspace files (#2329 part 2,
  [`agent/src/tools/workspaceUploads.ts`](../../agent/src/tools/workspaceUploads.ts)):
  `upload_asset`, `add_model_media`, `set_model_thumbnail`, `set_output_thumbnail`,
  `create_model`, `import_model`, `write_source_file`, `write_file` and `set_readme` take
  `workspace_path` with its `workspace_sha256`. The text tools (`create_model`,
  `import_model`, `write_source_file`, `write_file`, `set_readme`) take it instead of
  their inline text (exactly one of the two); the binary ones (`upload_asset`,
  `add_model_media`, `set_model_thumbnail`, `set_output_thumbnail`, and
  `create_model_from_mesh`'s upload) take it only (#2510): no tool takes bytes as a
  base64 argument, and `workspace_write` writes text only. Binary reaches the workspace
  by the user's upload in the panel (`routes/sessionFiles.ts`), `http_request`
  `save_to_workspace` or `workspace_save`. The sha256 is the one the model read the file at; a file no longer at
  it is refused, so an approval card and an audit row (the call's input) name the path
  and the exact content, and never carry the bytes. Each tool keeps its tier
  (`import_model` from a file is still `outward`). A binary file is streamed from
  its blob, a sealed segment at a time, or from Postgres, to the backend's multipart route a 1 MiB slice at a time
  (`SessionFiles.chunks`; the column is stored uncompressed, `20261011T1521Z_session_files_streaming.sql`);
  a text file is decoded as UTF-8 and refused when it is not text. Every upload, from
  the workspace or inline, text or binary (a byte search), is refused when it contains
  one of the agent's credentials (`ToolServices.secrets`, below): `workspace_read`
  redacts them, and an upload would otherwise carry them into a README, a source file,
  an asset or Bambuddy. The refusal names the upload, never the credential.
- `workspace_read` redacts every saved Claude credential's secret from text (and, in a
  classic turn, the turn's plugin tokens) before the model sees it
  (`ToolServices.secrets`, `main.ts`).
- Paths are at most four `/`-separated segments of `[A-Za-z0-9_][A-Za-z0-9_.-]*`, checked
  by the store and by the table. No Claude Code built-in reaches the workspace.
- The panel's **Files** (part 3,
  [`agent/src/routes/sessionFiles.ts`](../../agent/src/routes/sessionFiles.ts)) is a
  session's workspace over HTTP, for the browser user, guarded as the session routes
  are: reads need a session it may see (`uiReadProblem`), writes the UI's origin
  (`uiRequestProblem`) and a session it owns that is not archived (403, 409).
  - `GET /api/v1/ai/sessions/:id/files`: `{files, usage: {files, bytes}, limits:
    {file_max_bytes, workspace_max_bytes}}`, each file with its `path`, `media_type`,
    `size`, `sha256`, `source` and times.
  - `GET /api/v1/ai/sessions/:id/files/<path>`: the bytes, always
    `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, a sandboxing
    CSP and `Cache-Control: private, no-store`. The stored media type is sent, except
    HTML, SVG, XML and scripts, which go as `application/octet-stream`. The panel fetches
    it and saves it through `downloadBlob`, or previews an image through a blob URL.
  - `POST /api/v1/ai/sessions/:id/files`: multipart `file`, `path` and, to replace a
    file, `expected_sha256` (the sha256 it has now) → `201 {file}` (`200` when it
    replaced one). A path that exists without it, or with a stale one, is `409`; past
    either cap, `413` (the body is capped at the file cap plus 64 KiB before it is read).
    The file's `source` is `{kind: "upload", by: "browser", filename}`.
  - `DELETE /api/v1/ai/sessions/:id/files/<path>[?expected_sha256=]`: `204`, `404` when
    there is none, `409` on a stale sha256.

  An upload or a delete is a `resource` audit row (`workspace_upload` or
  `workspace_delete`, refusals included: a bad path, a cap, a stale sha256, and a
  session the browser user does not own or that is archived) and appends a
  `workspace.file` event (`path`, `change`, `by`, and an upload's `size`, `mediaType`
  and `sha256`) to the session's log in the same transaction as the file, so the two
  commit together: the panel's Files list reads again on it, and the feed shows it as a
  line where it happened (#2399). An upload whose numbered
  `uploads/<name>` was taken meanwhile reads the list again and takes the next number,
  once. A running turn is not interrupted;
  the panel adds `[workspace file <path>]` to the next message, and the model finds the
  file with `workspace_list` / `workspace_read`.

## 14. The slicer setting (#3822)

`get_settings` shows `slicer_backend` (`bambuddy` or `native`) with the other stored
settings, so an agent can say which slicer the print dialog and print runs use. No
ScadBuddy tool changes it: `PUT /api/v1/settings`, where `slicer_backend` is written, is
Settings-only (`agent/src/tools/coverage.ts`), and switching slicer is an operator's
decision (`README.md` "Rolling out the native slicer"). Two other paths do write the
settings store, and neither is a way to this setting by design: `set_print_options`
writes the print options (`PUT /api/v1/settings/print-options`,
`agent/src/tools/settings.ts`), an outward call; and `http_request` (§12), once enabled
and each call approved, can reach any LAN URL, ScadBuddy's own `PUT /api/v1/settings`
included, so approving such a call is approving a settings write. A print an agent starts slices
on whichever slicer is set when its run is planned.
