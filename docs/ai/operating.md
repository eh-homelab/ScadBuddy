# Operating the agent sidecar

How to run and configure ScadBuddy's AI agent service (`agent/`) as it exists on
`main`. Every fact names the file and function it was read from. Spec references are
to [`docs/superpowers/specs/2026-09-27-ai-integration-design.md`](../superpowers/specs/2026-09-27-ai-integration-design.md).
The README's "The agent sidecar (AI, #261)" section in [`README.md`](../../README.md)
covers the same ground more briefly.

> **Status.** Nothing deploys the sidecar yet, and there are no ingress routes for
> `/mcp` or `/api/v1/ai/*` (README, "The agent sidecar"). The assistant panel is
> hidden in production builds (`useAiAvailability()`,
> [`frontend/src/agent/chat/availability.ts`](../../frontend/src/agent/chat/availability.ts)).
> This page is for operators who stand the service up ahead of that.

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
- **Ingress routing (planned, spec §4.2).** `/mcp` and `/api/v1/ai/*` go to the agent,
  and everything else, including `/api/v1/ws`, goes to the backend. `/api/v1/ai/*` is
  under the backend's `/api/v1/*`, so the agent's rules must take precedence
  (longest-prefix match or explicit priority).
- **Runtime user and filesystem.** The image runs as uid 10001 with
  `HOME=/var/lib/scadbuddy-agent` and `CLAUDE_CONFIG_DIR=/var/lib/scadbuddy-agent/claude`
  ([`Dockerfile`](../../Dockerfile), `agent` stage). It writes only under
  `/var/lib/scadbuddy-agent` (`DEFAULT_STATE_DIR`,
  [`agent/src/harness/options.ts`](../../agent/src/harness/options.ts)). Mount an
  `emptyDir` there and run the root filesystem read-only (spec §4.4). At start,
  `ensureStateDirs()` in
  [`agent/src/harness/stateDirs.ts`](../../agent/src/harness/stateDirs.ts) recreates
  `claude/` and `work/` and checks both are writable. If it cannot, the process exits 1
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
  ([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)). The `AiStatus` comment
  also says the backend's health report will read `ai` to decide whether the UI shows
  AI (#261). No backend code on `main` reads it yet.
- **Migrations** (`agent/src/db/migrations.ts`) run in the background at start
  (`void database?.ready()` in `main.ts`). They run under a transaction-scoped advisory
  lock with `lock_timeout` 10 s and `statement_timeout` 60 s
  (`DEFAULT_LOCK_TIMEOUT_MS`, `DEFAULT_STATEMENT_TIMEOUT_MS`). A failure is retried on
  the next `ready()` call. An already-applied migration whose SQL was edited fails its
  recorded sha256 check (`MigrationChecksumError`), and `main.ts` exits 1 on it.

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

## 7. Database tables

The agent owns and migrates its `ai_*` tables (spec §9;
[`agent/src/db/migrations.ts`](../../agent/src/db/migrations.ts)):

- `ai_migrations`: the ledger, with a checksum per entry.
- `ai_credentials`: the sealed credential.
- `ai_settings`: non-secret key/value settings. `SettingsStore` in `credentials.ts`.
  The keys read today are `model` (`main.ts`), and `session_max_turns` and
  `session_max_budget_usd` (`SETTING_SESSION_MAX_TURNS` and
  `SETTING_SESSION_BUDGET_USD` in [`agent/src/sessions/manager.ts`](../../agent/src/sessions/manager.ts)).
  No route writes them yet.
- `ai_sessions`, `ai_session_entries` and `ai_session_events`: sessions (#377,
  migration 2). The session manager is not wired into `main.ts` yet (PR #377 body,
  "HTTP routes").

The migration advisory lock key is "SCADAGNT", distinct from the backend's "SCADBDDY"
(the comment on `MIGRATION_LOCK` in `migrations.ts`).

## 8. Per-query limits

Every harness query gets `maxTurns` (default 25) and `maxBudgetUsd` (default 1 USD)
(`DEFAULT_MAX_TURNS` and `DEFAULT_MAX_BUDGET_USD` in
[`agent/src/harness/run.ts`](../../agent/src/harness/run.ts)). The comment there calls
them "placeholders until Settings stores per-session caps". Sessions read their caps
from `ai_settings` when they start, and spend the budget across the whole session (PR
#377 body, "Budget and turns").
