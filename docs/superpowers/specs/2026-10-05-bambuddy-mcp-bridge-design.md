# Bambuddy MCP bridge (#1466)

Status: designed with the owner in conversation on 2026-10-05, approved section by
section. Revised after the PR #1467 review: read tools moved to a tag allowlist,
credential-bearing tags are hidden, and the origin check, caps, licence, lockfile and
reconciliation are added. This written spec is not yet approved; its review gates the
implementation plan. Part of epics #249 (AI agent integration) and #23 (Bambuddy
integration).

## 1. What this changes

The assistant reaches Bambuddy today only through ScadBuddy's own tools: send, print,
library, projects and filaments. Spec D8 (`2026-09-27-ai-integration-design.md`) chose
that over third-party Bambuddy MCP servers.

This adds an **optional, off-by-default** bridge to
[`bambuddy-mcp`](https://github.com/MrMebelMan/bambuddy-mcp) (PyPI `bambuddy-mcp`,
GPL-3.0). That server turns Bambuddy's REST API, read from its `/openapi.json`, into MCP
tools. With the bridge, the assistant can answer questions ScadBuddy's tools don't
cover: printer status, archives and statistics, maintenance, camera snapshots and spool
inventory. It still cannot change anything in Bambuddy without an approval, and it
cannot see Bambuddy's credentials or account settings at all (§4.3).

Owner decisions:

- It is managed from ScadBuddy's Settings, not by the cluster (option A of two).
- It has **its own Bambuddy key, like the render key, and falls back to the default
  key** when that is unset.

Out of scope: OAuth, reaching the bridge from outside the pod, and use from a Claude
Code install outside ScadBuddy (that is the upstream package's own stdio mode).

## 2. Facts this rests on

Each was read on 2026-10-05 from `origin/main`, from the package's source at 0.2.0, or
from live Bambuddy 1.2.6b1.

- `bambuddy-mcp` 0.2.0 speaks **stdio only** (`server.py`: `mcp.server.stdio`).
  - Configuration is by environment: `BAMBUDDY_URL`, `BAMBUDDY_API_KEY`,
    `BAMBUDDY_DIRECT_MODE`, and `BAMBUDDY_CENSOR_ACCESS_CODE`, `…_SERIAL` and
    `…_MODEL_FILENAME`.
  - In direct mode, each endpoint is one tool. Its description starts
    `[<tag>] <METHOD> <path>` (`openapi.py` `build_tool_description`).
  - Its dependencies are `mcp` and `httpx`.
- **Live Bambuddy's `/openapi.json` has 618 paths: 779 operations in 60 tags.** §4.3's
  rules applied to it give 173 read, 409 write and 197 hidden operations (the numbers
  in §4.4's example).
  - Some **GETs return secrets**: `GET /api/v1/auth/tokens`, `/auth/smtp`,
    `/settings/backup`, `/cloud/settings`, `/notifications/{provider_id}`, and the
    `api-keys` tag.
  - Some **GETs have side effects**: `GET /api/v1/archives/confirm/{token}/{verdict}`,
    and `/auth/oidc/authorize/{provider_id}` and `/auth/oidc/callback`.
  - Some GETs carry a download token in the path: `…/dl/{token}/{filename}`.
  - So the HTTP method alone does not make a call safe to run without approval.
- The agent accepts **remote HTTP plugins only**.
  - A plugin URL must be `https`, or plain `http` when every address it resolves to is
    loopback (`agent/src/plugins/registry.ts` `assertEndpointAllowed`, spec §8.4).
  - Stdio and other process-starting plugins are refused (`harness/plugins.ts`).
  - Unlisted plugin tools are `outward`.
  - The forwarder turns a plugin's 401/407 and its redirects into a 502, and
    refuses OAuth discovery (`plugins/forwarder.ts`).
  - Its `MAX_BODY_BYTES` (4 MiB) limits only the **request** body. Nothing caps a
    plugin's **response**.
  - Plugins are created with `POST /api/v1/ai/plugins` and updated with
    `PATCH`/`DELETE /api/v1/ai/plugins/:name` (`agent/src/routes/plugins.ts`). Writes
    go through the agent's UI-origin guard (`agent/src/http/origins.ts`).
  - A plugin summary carries `secret_last4`.
- **Pod network.** The agent and the backend share it (README, "The agent sidecar"). So
  `127.0.0.1` in the agent reaches a backend port bound to loopback in **the same pod**.
- **Render key precedent.** `settings_store.py` `render_bambuddy_key()` returns the
  render key, or else the full key with a fallback flag. The Settings view exposes
  `has_render_api_key` and `render_key_fallback` (`api/settings.py`).
- **Settings events.**
  - They go out on the Postgres bus (`core/pg_events.py`: `pg_notify`, and every
    replica LISTENs).
  - `SettingsSection` (`core/events.py:193`) has no Bambuddy section; the Bambuddy URL
    and key emit `section="connection"`.
  - After a dropped LISTEN connection, each process gets a local resync event, because
    whatever was NOTIFYed meanwhile is lost.
- **Origin checks.** The backend has **no origin guard on ordinary POST routes**.
  `origin_allowed` (`api/realtime.py:192`) checks WebSocket upgrades only, against
  `public_url` and `allowed_origin_list`.
- **Loopback and the HTTP-request tool.** The assistant's HTTP-request tool may reach
  loopback (`http/egress.ts`). GET runs at once; POST waits for approval. MCP calls are
  POSTs, so that tool cannot skip the bridge's tiers. The bridge still requires a token
  (§4.3).
- **Licences.** ScadBuddy is Apache-2.0; `bambuddy-mcp` is GPL-3.0.

## 3. Settings

New fields in `StoredSettings` (key/value table, so no migration):

| Field | Kind | Default |
|---|---|---|
| `bambuddy_mcp_enabled` | bool | `false` |
| `bambuddy_mcp_api_key` | secret (like `bambuddy_render_api_key`) | unset |
| `bambuddy_mcp_token` | secret, generated by enable (§4.4), never set by the user | unset |

- **Key resolution.** `mcp_bambuddy_key() -> tuple[str | None, bool]` mirrors
  `render_bambuddy_key()`:
  - it returns the MCP key when set;
  - otherwise it returns `bambuddy_api_key` with `fallback=True`;
  - with no key at all it returns `(None, False)`.
- **Secrets.** The settings view adds `has_mcp_api_key` and `mcp_key_fallback`. Both new
  secrets join `save()`'s secret list (an empty string clears the field) and the
  redaction list. Neither is ever returned by any route.
- **Events.** A new `SettingsSection` literal, `"bambuddy_mcp"`, is emitted by `save()`
  whenever any of the three fields changes. The bridge restarts on `"bambuddy_mcp"`, on
  `"connection"` (the URL or main key changed, which the fallback depends on), and on
  the bus's resync event.
- **The Settings page**, in the Bambuddy section beside the render key:
  - A key field: "Bambuddy MCP key".
  - Help text: "A Bambuddy API key for the assistant's Bambuddy tools. Give it only the
    scopes you want the assistant to have; Read Status is enough to look things up.
    Unset, it uses the main key."
  - While the fallback is in use, a warning: "The assistant's Bambuddy tools use the
    full Bambuddy key, which can print."
  - A switch: "Let the assistant use Bambuddy's API (bambuddy-mcp)", with the state
    from §4.5 and the registration check from §5 shown next to it.

## 4. The bridge (backend)

### 4.1 Packaging and licence

- **Lockfile.** `backend/bambuddy-mcp/requirements.txt` is a fully locked, hashed list
  of `bambuddy-mcp==0.2.0` and **every** transitive dependency (`mcp`, `httpx`,
  `anyio`, `pydantic`, …). It is generated by
  `uv pip compile --generate-hashes --python-version 3.12` from a one-line
  `requirements.in`, and committed.
- **Install.** The Dockerfile's `base`-derived build stage creates `/opt/bambuddy-mcp`
  with `uv venv` and runs
  `uv pip install --require-hashes --no-deps -r requirements.txt --python /opt/bambuddy-mcp/bin/python`.
  - `--no-deps` with `--require-hashes` means a dependency missing from the file fails
    the build rather than being resolved unpinned.
  - The runtime stage copies `/opt/bambuddy-mcp`, owned by root and read-only to the
    app user, which only executes it.
- **Process boundary.**
  - The backend never imports `bambuddy_mcp`; it only runs
    `/opt/bambuddy-mcp/bin/bambuddy-mcp`. The GPL code and ScadBuddy's Apache code meet
    only at a process boundary (stdio).
  - The backend's own venv gains only the MIT-licensed `mcp` SDK (in `uv.lock` as
    usual) for the client and server sides of the bridge.
- **Version assertion.** The Dockerfile asserts
  `/opt/bambuddy-mcp/bin/python -m pip show bambuddy-mcp` reports
  `BAMBUDDY_MCP_VERSION=0.2.0`, as it asserts `OPENSCAD_VERSION`.
- **GPL-3.0 compliance**, because the published GHCR image distributes the package:
  - Upstream's `LICENSE` and the **0.2.0 sdist** (`bambuddy_mcp-0.2.0.tar.gz`, hash
    pinned, the corresponding source) are copied into the image at
    `/usr/share/doc/bambuddy-mcp/`.
  - A new `THIRD_PARTY_NOTICES.md` and the README's "Deploying" section name it, its
    licence, upstream's URL and where the source is in the image.
- **Bumps.** A bump regenerates the lockfile, changes `BAMBUDDY_MCP_VERSION` and the
  sdist in the same commit, and re-runs §7's integration test.

### 4.2 Child process

`scadbuddy/bambuddy/mcp_bridge.py` owns one child process per backend process, started
with `asyncio.create_subprocess_exec` as `library/lsp.py` starts `openscad-lsp`.

- **Environment: exactly this set.** Nothing is inherited, and a test asserts it.
  - `BAMBUDDY_URL`: the settings' `bambuddy_url`.
  - `BAMBUDDY_API_KEY`: from `mcp_bambuddy_key()`.
  - `BAMBUDDY_DIRECT_MODE=true`.
  - `BAMBUDDY_CENSOR_ACCESS_CODE=true` and `BAMBUDDY_CENSOR_SERIAL=true`.
  - `PATH=/opt/bambuddy-mcp/bin:/usr/bin:/bin`.
  - `HOME` set to a private temporary directory.
  - No `SCADBUDDY_*` variable, and no other secret.
- **Talking to it.** The bridge uses the `mcp` SDK's stdio client: `tools/list` once
  after start, then `tools/call` per request.
- **Start.** It starts when `bambuddy_mcp_enabled` is on, `bambuddy_url` is set, and a
  key resolves. Otherwise the state is `off`, `no_url` or `no_key` (§4.5).
- **Restarts.** On the events in §3, the bridge stops the child and re-reads settings,
  then starts it again if the conditions still hold. This happens in **every** replica,
  because each one hears the NOTIFY.
- **Crashes.**
  - Restart with backoff: 1, 2, 4… up to 60 s.
  - After 5 failures in 10 minutes it stays `failed` until the next §3 event.
- **Shutdown.** The child is killed on backend shutdown, as the LSP sessions are.
- **Stderr.** It is kept as a bounded tail of 20 lines, each at most 500 characters,
  scrubbed before it is stored or logged:
  - the resolved key, the main key and the token are replaced by `***`;
  - so is any `access_code`/`api_key`/`token`-looking `key=value` or JSON field.

### 4.3 Endpoint index and the three tools

Each tool the child lists is parsed from its `[tag] METHOD path` description into
`{name, method, path, tag, description, input_schema}`. Then it is classified. The
first matching rule wins:

1. **Hidden.** Never searchable and never callable. Counted as `hidden`.
   - Tag in `HIDDEN_TAGS`: `2fa`, `api-keys`, `authentication`, `users`, `groups`,
     `settings`, `cloud`, `orca-cloud`, `notifications`, `notification-templates`,
     `webhook`, `github-backup`, `local-backup`, `connected-apps`, `support`,
     `bug-report`, `updates`, `sponsor-prompt`, `user-notifications`.
   - Or a path containing `/dl/`, `/confirm/`, `/oidc/` or `{token}`.
   - Or a description that does not parse (also counted as `unclassified`).
2. **Read.** The method is `GET`, the tag is in `READ_TAGS`, and the path does not end
   in `/download` or `/export`.
   - `READ_TAGS`: `printers`, `archives`, `queue`, `library`, `library-tags`,
     `library-variants`, `projects`, `inventory`, `spoolman-inventory`, `maintenance`,
     `camera`, `camwall`, `ams-history`, `printer-sensor-history`, `print-log`,
     `filament-catalog`, `kprofiles`, `Local Presets`, `Slicer Presets`, `firmware`,
     `system`, `metrics`, `smart-plugs`, `pending-uploads`, `slice-jobs`.
3. **Write.** Everything else. Callable only through `bambuddy_call`, which is
   `outward`. This includes GETs outside `READ_TAGS` and the download/export GETs, so
   any GET we have not vetted needs an approval.

The lists are constants in `mcp_bridge.py`, with a comment citing this section and the
Bambuddy version they were checked against. A tag Bambuddy adds later lands in
**write**, never in read.

The bridge exposes three tools, never the child's whole list:

| Tool | Tier | Input | Does |
|---|---|---|---|
| `bambuddy_search` | `read` | `query`, optional `tag`, optional `limit` (≤25) | Searches the read and write sets (never hidden) by name, path, tag and description. Returns name, class, method, path, summary and input schema. Answered by the bridge itself. |
| `bambuddy_get` | `read` | `name`, `arguments` | Calls a **read** tool. A write tool is refused with a pointer to `bambuddy_call`; a hidden or unknown one with "not available". |
| `bambuddy_call` | `outward` | `method`, `path`, `name`, `arguments` | Calls a **write** tool. The bridge checks that `method` and `path` match the indexed `name`. They are there so the approval card shows them (§5). A read tool is refused with a pointer to `bambuddy_get`. |

- **Tool descriptions** tell the model:
  - start with `bambuddy_search`;
  - printing or sending a ScadBuddy output goes through ScadBuddy's own print/send
    tools, not `bambuddy_call`.
- **Result caps**, enforced by the bridge (the agent has no response cap, §2):
  - text results are cut at **256 KiB**, with a marker naming the original size;
  - image content larger than **2 MiB** is replaced with a text note and its size;
  - a call returning more than **one image** keeps the first.
- **Errors.**
  - Bambuddy HTTP errors keep their status and message.
  - A 403 is reworded by the existing scope mapping (`bambuddy/errors.py` `Scope`):
    "the Bambuddy MCP key lacks <scope>".
  - Error text passes through the same scrubber as stderr.

### 4.4 HTTP side and routes

- **MCP server.** A second ASGI app in the backend process, on
  `127.0.0.1:${SCADBUDDY_BAMBUDDY_MCP_PORT:-8091}`.
  - It is served by its own uvicorn server task in the app lifespan, and never mounted
    on the public 8080 app.
  - The port belongs to each pod, so **each pod's agent reaches only its own pod's
    bridge**. All replicas share the one token, and each bridge reads it from settings.
  - If the port is taken, the state is `failed: port in use`.
- **Transport.** Streamable HTTP at `/mcp` (`mcp` SDK server side), JSON responses.
- **Auth.**
  - Every request needs `Authorization: Bearer <bambuddy_mcp_token>`, compared in
    constant time.
  - A missing or wrong token answers 401 with no challenge. The forwarder makes that a
    502, so the turn reports a failing plugin.
- **Not ready.** While the bridge is not `ready`, the server answers 503. The body is a
  fixed MCP error: "Bambuddy tools are starting or restarting; try again in a minute."
  It never contains the reason, the stderr tail or any setting. During a restart
  triggered by a settings save, the model therefore sees a retryable, plain error.
- **Status route.** `GET /api/v1/bambuddy/mcp` answers:

  ```json
  { "state": "off" | "no_url" | "no_key" | "starting" | "ready" | "failed",
    "reason": null,
    "endpoints": 779, "reads": 173, "writes": 409, "hidden": 197, "unclassified": 0,
    "key_fallback": true,
    "url": "http://127.0.0.1:8091/mcp",
    "token_last4": "a1b2" }
  ```

  - `reason` is a fixed phrase plus the scrubbed stderr tail, for the UI only.
  - It never carries the key or the token. `token_last4` is the last four characters
    of the plugin secret `Bearer <token>`, for comparing with the plugin's
    `secret_last4` (§5).
- **Enable and disable.**
  - `POST /api/v1/bambuddy/mcp/enable` and `POST /api/v1/bambuddy/mcp/disable` get a new
    dependency, `require_ui_origin`. It needs an `Origin` header and accepts it only
    when `origin_allowed(origin, public_url, allowed_origin_list)` holds, the same rule
    `api/realtime.py` uses for WebSockets. Otherwise it answers 403.
  - **These are the backend's first origin-guarded POSTs.** The dependency is written
    to be reused, but this spec applies it only to these two routes, because enable is
    the first backend route that returns a secret.
  - Enable sets `bambuddy_mcp_enabled`, generates a new 32-byte token, saves both, and
    returns `{url, token, tool_tiers}` **once**. `url` is built from the configured
    port.
  - Disable sets `bambuddy_mcp_enabled=false` and clears the token. Both emit
    `"bambuddy_mcp"`, so every replica's bridge follows.

## 5. Registering with the assistant

The browser drives registration through the agent's existing plugin routes, so the
agent gains no new trust path. The Bambuddy key never reaches the browser.

**Switching on:**
1. The page calls `POST /api/v1/bambuddy/mcp/enable` and gets `{url, token, tool_tiers}`.
2. It calls `POST /api/v1/ai/plugins` with:
   - `name: "bambuddy"`
   - `url`: the returned `url`, not a literal
   - `auth_header: "Authorization"`, `secret: "Bearer <token>"`
   - `tool_tiers`: `{bambuddy_search: read, bambuddy_get: read, bambuddy_call: outward}`

   On a 409 (the plugin exists) it sends the same fields with
   `PATCH /api/v1/ai/plugins/bambuddy`.

**Switching off:**
1. The page calls `DELETE /api/v1/ai/plugins/bambuddy`.
2. Then it calls `…/disable` **whether or not the delete succeeded**. A plugin left
   behind points at a stopped bridge with a cleared token, so it fails harmlessly with
   401 or connection refused.
3. If the delete failed, the page says so and offers "Remove the plugin".

**Reconciliation**, every time the Settings page loads and after each switch. The page
reads the status route and the `bambuddy` plugin summary:

- `enabled` and the plugin is missing, or `secret_last4 ≠ token_last4`: the page shows
  "Not connected to the assistant" with a **Reconnect** button. Reconnect re-runs
  "switching on", which rotates the token.
  - This covers a page closed between enable and the plugin POST, and two browsers
    enabling at once: the last enable's token wins, and the other browser's PATCH may
    carry an older one.
- Not `enabled`, and a `bambuddy` plugin still exists: the page offers "Remove the
  plugin".

**Approvals and audit:**
- An outward `bambuddy_call` parks in `canUseTool`. The approval card shows the tool
  input, which includes `method` and `path` (§4.3). §7 asserts that they appear in the
  approval payload.
- Every call is audited as plugin calls already are: AI activity, tool name and
  outcome, without secrets.

## 6. Amendment to spec D8

`2026-09-27-ai-integration-design.md` D8 gets this addendum:

> Amended 2026-10-05 (#1466): an optional, off-by-default bridge to the third-party
> `bambuddy-mcp` covers parts of Bambuddy's API that ScadBuddy's own tools do not
> (status, archives, maintenance, camera, inventory). It keeps D8's reasons:
> - the key stays server-side and is its own setting;
> - credential and account endpoints are hidden;
> - vetted reads are `read`, and every other call is `outward`, which needs an
>   approval;
> - errors use the same scope mapping;
> - calls are audited as plugin calls.
>
> Printing and sending ScadBuddy outputs stay with ScadBuddy's tools.

## 7. Testing

CI has no real Bambuddy.

**Backend unit tests:**
- **Key and settings.**
  - `mcp_bambuddy_key()`: the MCP key, the fallback, and none. These mirror the
    render-key tests.
  - The view flags.
  - `save()` emits `"bambuddy_mcp"` for each of the three fields.
- **Classification**, against a fixture of real Bambuddy tags and paths:
  - every `HIDDEN_TAGS` tag and each hidden path pattern (`/dl/`, `/confirm/`,
    `/oidc/`, `{token}`) is hidden;
  - `GET /api/v1/auth/tokens`, `/settings/backup`, `/cloud/settings` and
    `/archives/confirm/{token}/{verdict}` are hidden;
  - a GET in an unknown tag is a write;
  - `…/download` and `…/export` are writes;
  - a `READ_TAGS` GET is a read;
  - an unparsable description is hidden and counted as `unclassified`.
- **Tools.**
  - `bambuddy_search` never returns a hidden tool.
  - `bambuddy_get` refuses a write and a hidden tool.
  - `bambuddy_call` refuses a read, a hidden tool, and a `method`/`path` that don't
    match `name`.
- **Caps.** Text over 256 KiB is cut; an image over 2 MiB is replaced; only the first
  image is kept.
- **Auth and state.**
  - 401 without the token and with a wrong token.
  - 503 before ready, with the fixed body and no stderr text.
  - The `off`, `no_url`, `no_key` and `failed` states.
- **Child.**
  - Its environment is exactly §4.2's set; the test spawns `env` through the same
    launcher.
  - Restart, backoff and the 5-in-10-minutes stop, driven by a fake child script.
  - A `"connection"` event and a resync event each restart it.
- **No secret reaches a route.**
  - The bodies of `GET /bambuddy/mcp` and `POST …/enable`, and the stored and logged
    stderr tail, contain no Bambuddy key (main or MCP) and no access code.
  - The status body never contains the token.
- **Origin guard.** `POST …/enable` with no `Origin`, or with a foreign `Origin`,
  answers 403 and returns no token. With the public origin it succeeds.
- **No public route.** The 8080 app has no `/mcp` route for this bridge.

**Integration test** (`requires_bambuddy_mcp`; skips when `/opt/bambuddy-mcp` is
absent; runs in the Dockerfile `test` image):
- The real pinned `bambuddy-mcp` runs against a uvicorn-served fake Bambuddy. Its
  `openapi.json` has: a `printers` GET, an `auth` GET (hidden), a `/dl/{token}` GET
  (hidden), a `queue` POST, a `printers` PATCH, a `queue` DELETE, and a `camera` GET
  returning PNG.
- The bridge reaches `ready` and lists 3 tools.
- `bambuddy_search` shows no hidden tool.
- `bambuddy_get` reaches the fake for the `printers` GET.
- `bambuddy_call` reaches the fake for the POST.
- The camera GET comes back as image content.

**Agent tests**, using the existing plugin harness with a local fake MCP server that
offers the three tools:
- A plugin registered with the three tiers loads.
- `bambuddy_get` runs without parking.
- `bambuddy_call` parks as `outward`, and the parked approval's payload contains the
  call's `method` and `path`.

**Frontend tests** (vitest + msw):
- The key field and the fallback warning.
- Switching on calls enable, then the plugin POST, or the PATCH on a 409, using the
  returned `url`.
- Switching off calls the plugin DELETE, then disable, even when the DELETE fails, and
  shows "Remove the plugin" then.
- Reconciliation: a missing plugin, or a mismatched `secret_last4`, shows "Reconnect";
  a leftover plugin while disabled shows "Remove the plugin".

**Docker build:** the image asserts `bambuddy-mcp` 0.2.0, and the build fails if the
lockfile is missing a hash.

## 8. Risks

- **The upstream package is small** (one maintainer, 5 stars on 2026-10-05).
  - It is fully locked and bumped only deliberately (§4.1).
  - The bridge's own tool set, classification and tiers do not depend on upstream's
    meta-tools, so an upstream change cannot widen what is `read`.
- **Bambuddy adds endpoints or tags.** New tags land in **write** (approval needed),
  never in read. A new credential-bearing tag is not hidden until it is added to
  `HIDDEN_TAGS`, but it still needs an approval to call. The status counts make drift
  visible.
- **Credential exposure through an approved call.** A user could approve a
  `bambuddy_call` that returns something sensitive from a tag not in `HIDDEN_TAGS`. The
  approval card shows the method and path. Hidden tags remove the known
  credential-bearing endpoints outright.
- **The full-key fallback** lets an approved `bambuddy_call` print. The Settings
  warning and §3's help text steer users to a scoped key.
- **Bridge writes are not operations.** Since #1063, ScadBuddy's own Bambuddy writes run
  as durable `Operation` workflows, with an `Idempotency-Key` and an `operations`
  record. A `bambuddy_call` goes straight from the child to Bambuddy. It is not
  idempotent, not retried, and not in `operations`; only AI activity records it. That is
  why ScadBuddy-owned writes (send, print, projects) stay with ScadBuddy's tools, and the
  tool descriptions say so. Moving bridge writes onto `Operation` is out of scope.
- **GPL-3.0 obligations** are met by shipping the licence and the corresponding source
  in the image (§4.1). Changing the process boundary, for example importing the package,
  would change that analysis and needs a fresh look.
