# Bambuddy MCP bridge (#1466)

Status: designed with the owner in conversation on 2026-10-05, approved section by
section. Revised twice after PR #1467 reviews:
- the first moved read tools to a tag allowlist, hid credential-bearing tags, and
  added the origin check, caps, licence, lockfile and reconciliation;
- the second classifies by all of an operation's tags from Bambuddy's own
  `openapi.json`, adds hidden path prefixes, token-named segments and parameters and
  streams, a 30 s call timeout, token rotation without a restart, and corrections to
  the install steps and field names. This written spec is not yet approved; its review gates the
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
    `[<tag>] <METHOD> <path>` (`openapi.py` `build_tool_description`). The tag shown is
    only the operation's **first** tag (`openapi.py:125`).
  - Its dependencies are `mcp` and `httpx`.
- **Live Bambuddy's `/openapi.json` has 618 paths: 779 operations in 60 tags.**
  - **24 operations carry more than one tag.**
  - §4.3's rules applied to it give 172 read, 404 write and 203 hidden operations (the
    numbers in §4.4's example).
  - Some **GETs return secrets**: `GET /api/v1/auth/tokens`, `/auth/smtp`,
    `/settings/backup`, `/cloud/settings`, `/notifications/{provider_id}`, and the
    `api-keys` tag.
  - Some calls **mint tokens**: `POST /api/v1/printers/camera/stream-token` (tag
    `camera`) and `POST /api/v1/auth/ws-token`.
  - Some **GETs have side effects**: `GET /api/v1/archives/confirm/{token}/{verdict}`,
    and `/auth/oidc/authorize/{provider_id}` and `/auth/oidc/callback`.
  - Some GETs carry a download token in the path: `…/dl/{token}/{filename}`.
  - `GET /api/v1/printers/{printer_id}/camera/stream` is a never-ending MJPEG stream.
  - The `/api/v1/auth/*` operations are tagged `authentication`, `2fa` or `oidc`.
  - So neither the HTTP method nor upstream's one displayed tag makes a call safe to run
    without approval.
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
  - The plugin view the browser reads (`pluginView`, `routes/plugins.ts`) exposes the
    secret's tail as `auth.last4`, or `auth: null` when there is no header.
    `last4()` (`agent/src/secrets.ts`) returns `''` for a secret shorter than 12
    characters.
  - Plugins are connected again for every turn. A plugin whose MCP server is not
    `connected` at the turn's init gets a `plugin_unavailable` session event ("not
    available in this turn"), and the next turn tries it again
    (`sessions/manager.ts`, `pluginCheck`).
  - Every `/api/v1` operation needs an agent tool or an `agent/src/tools/coverage.ts`
    entry, or `test/coverage.test.ts` fails.
- **Pod network.** The agent and the backend share it (README, "The agent sidecar"). So
  `127.0.0.1` in the agent reaches a backend port bound to loopback in **the same pod**.
- **Render key precedent.** `library/settings_store.py` `render_bambuddy_key()` returns the
  render key, or else the full key with a fallback flag. The Settings view exposes
  `has_render_api_key` and `render_key_fallback` (`api/settings.py`).
- **Settings events.**
  - They go out on the Postgres bus (`core/pg_events.py`: `pg_notify`, and every
    replica LISTENs).
  - `SettingsSection` (`core/events.py`) has no Bambuddy section; the Bambuddy URL
    and key emit `section="connection"`.
  - After a dropped LISTEN connection, each process gets a local resync event, because
    whatever was NOTIFYed meanwhile is lost.
- **Origin checks.** The backend has **no origin guard on ordinary POST routes**.
  `origin_allowed` (`api/realtime.py`) checks WebSocket upgrades only, against
  `public_url` and `allowed_origin_list`.
- **Loopback and the HTTP-request tool.** The assistant's HTTP-request tool may reach
  loopback (`http/egress.ts`). GET runs at once; POST waits for approval. MCP calls are
  POSTs, so that tool cannot skip the bridge's tiers. The bridge still requires a token
  (§4.3).
- **Licences.** ScadBuddy is Apache-2.0; `bambuddy-mcp` is GPL-3.0.
- **Image Python.** The Dockerfile installs uv-managed Python 3.12 under
  `UV_PYTHON_INSTALL_DIR=/opt/uv-python` in both the build and the runtime stages.

## 3. Settings

New fields in `StoredSettings` (key/value table, so no migration):

| Field | Kind | Default |
|---|---|---|
| `bambuddy_mcp_enabled` | bool | `false` |
| `bambuddy_mcp_api_key` | secret (like `bambuddy_render_api_key`) | unset |
| `bambuddy_mcp_token` | secret, `secrets.token_urlsafe(32)` (43 characters), generated by enable (§4.4), never set by the user | unset |

- **Key resolution.** `mcp_bambuddy_key() -> tuple[str | None, bool]` mirrors
  `render_bambuddy_key()`:
  - it returns the MCP key when set;
  - otherwise it returns `bambuddy_api_key` with `fallback=True`;
  - with no key at all it returns `(None, False)`.
- **Secrets.** The settings view adds `has_mcp_api_key` and `mcp_key_fallback`. Both new
  secrets join `save()`'s secret list (an empty string clears the field) and the
  redaction list. Neither is ever returned by any route.
- **Events.** A new `SettingsSection` literal, `"bambuddy_mcp"`, is emitted by `save()`
  whenever any of the three fields changes. On `"bambuddy_mcp"`, `"connection"` (the URL
  or main key changed, which the fallback depends on), or the bus's resync event, the
  bridge re-reads settings and compares the **child's inputs** (enabled, URL, resolved
  key) with what the running child was started with:
  - it restarts the child only when those inputs differ;
  - a token-only change (enable while already running, or Reconnect) swaps the token the
    HTTP auth check uses and **leaves the child and in-flight calls running**.
- **The Settings page**, in the Bambuddy section beside the render key:
  - A key field: "Bambuddy MCP key".
  - Help text: "A Bambuddy API key for the assistant's Bambuddy tools. Give it only the
    scopes you want the assistant to have; Read Status is enough to look things up.
    Unset, it uses the main key."
  - While the fallback is in use, a warning: "The assistant's Bambuddy tools use the
    full Bambuddy key, which can print."
  - A switch: "Let the assistant use Bambuddy's API (bambuddy-mcp)", with the state
    from §4.4 and the registration check from §5 shown next to it.

## 4. The bridge (backend)

### 4.1 Packaging and licence

- **Lockfile.** `backend/bambuddy-mcp/requirements.txt` is a fully locked, hashed list
  of `bambuddy-mcp==0.2.0` and **every** transitive dependency (`mcp`, `httpx`,
  `anyio`, `pydantic`, …). It is generated by
  `uv pip compile --generate-hashes --python-version 3.12` from a one-line
  `requirements.in`, and committed.
- **Install.** The Dockerfile's `base`-derived build stage runs
  `uv venv --python /opt/uv-python/<the image's pinned 3.12> /opt/bambuddy-mcp`, then
  `uv pip install --require-hashes -r requirements.txt --python /opt/bambuddy-mcp/bin/python`.
  - **No `--no-deps`.** With `--require-hashes`, dependencies are still resolved, so a
    transitive dependency missing from the file has no hash and fails the build.
    `--no-deps` would skip it silently.
  - The build then runs `uv pip check --python /opt/bambuddy-mcp/bin/python`, which
    fails on a broken environment.
  - The venv's `bin/python` links to the uv-managed interpreter under `/opt/uv-python`.
    The runtime stage already provides that path (§2), so copying `/opt/bambuddy-mcp`
    into it is enough.
  - The runtime stage copies `/opt/bambuddy-mcp`, owned by root and read-only to the
    app user, which only executes it.
  - A smoke check in the runtime stage, `RUN ["/opt/bambuddy-mcp/bin/python", "-c",
    "import bambuddy_mcp.server"]`, proves the copied venv runs there. It is the image
    build importing GPL code in its own process, not ScadBuddy importing it.
- **Process boundary.**
  - The backend never imports `bambuddy_mcp`; it only runs
    `/opt/bambuddy-mcp/bin/bambuddy-mcp`. The GPL code and ScadBuddy's Apache code meet
    only at a process boundary (stdio).
  - The backend's own venv gains only the MIT-licensed `mcp` SDK (in `uv.lock` as
    usual) for the client and server sides of the bridge.
- **Version assertion.** A `uv venv` has no pip, so the Dockerfile asserts the version
  with the interpreter itself:
  `/opt/bambuddy-mcp/bin/python -c 'import importlib.metadata as m, sys; sys.exit(m.version("bambuddy-mcp") != sys.argv[1])' "$BAMBUDDY_MCP_VERSION"`
  (`BAMBUDDY_MCP_VERSION=0.2.0`). It has no pipe, as the `OPENSCAD_VERSION` check has
  none (hadolint DL4006).
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
  key resolves. Otherwise the state is `off`, `no_url` or `no_key` (§4.4).
- **Restarts.** On the events in §3, the bridge re-reads settings. It restarts the child
  only when the child's inputs changed (§3). A token-only change never restarts it. This
  happens in **every** replica, because each one hears the NOTIFY.
  - A call in flight when the child stops gets the fixed "restarting" MCP error (§4.4),
    never a hang or a partial result.
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
`{name, method, path}`. Upstream shows only the first tag (§2), so the bridge does not
classify by that tag.

**Classifying by all tags.** The bridge also fetches Bambuddy's `/openapi.json` itself.
- It does this in the backend process with its own httpx client and the same resolved
  key; this is ScadBuddy code, not the GPL package.
- It looks up each tool's operation by `(method, path)` and takes **all** of its `tags`.
- A tool with no matching operation is hidden and counted as `unclassified`.

Then the tool is classified, and the first matching rule wins:

1. **Hidden.** Never searchable and never callable. Counted as `hidden`.
   - **Any** of its tags is in `HIDDEN_TAGS`: `2fa`, `oidc`, `api-keys`,
     `authentication`, `users`, `groups`, `settings`, `cloud`, `orca-cloud`,
     `notifications`, `notification-templates`, `webhook`, `github-backup`,
     `local-backup`, `connected-apps`, `support`, `bug-report`, `updates`,
     `sponsor-prompt`, `user-notifications`.
   - Or its path starts with a hidden prefix. These hide by path, so a tag rename cannot
     unhide them: `/api/v1/auth/`, `/api/v1/api-keys`, `/api/v1/settings`,
     `/api/v1/cloud`, `/api/v1/orca-cloud`, `/api/v1/notifications`,
     `/api/v1/notification-templates`, `/api/v1/webhook`, `/api/v1/users`,
     `/api/v1/groups`.
   - Or its path has:
     - a segment `/dl/`, `/confirm/` or `/oidc/`;
     - a literal segment containing `token` (`/stream-token`, `/ws-token`, `/tokens`);
     - a `/stream` segment (streams never finish, §2);
     - or a path parameter whose name contains `token`, `secret`, `key`, `code` or
       `password` (case-insensitive).
   - Or the description does not parse, or no operation matches (also counted as
     `unclassified`).
2. **Read.** The method is `GET`, **every** one of its tags is in `READ_TAGS`, and the
   path does not end in `/download` or `/export`.
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
- **Timeout.** Each `tools/call` to the child has a **30 s** timeout in the bridge.
  - Past that, the call answers "Bambuddy did not answer within 30 s".
  - The bridge restarts the child, since httpx in the child may still hold the
    response. That is the one bound on the child's memory for a response that never
    ends.
  - Streams are hidden (rule 1), so this is a guard, not the expected path.
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
  - **The token's secrecy rests on this bind.** `require_ui_origin` (below) stops
    cross-site *browser* requests, but any non-browser client that reaches the backend
    can send `Origin: <public_url>` and get a token. That is acceptable only because the
    token opens nothing outside the pod's loopback. A change that exposes the port, or
    binds anything but `127.0.0.1`, breaks this model and needs a fresh design.
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
  - **If a turn starts while the bridge answers 503**, Claude Code's connection to the
    plugin fails. The existing `pluginCheck` then appends `plugin_unavailable` ("plugin
    bambuddy is not available in this turn") to the session, and the panel shows it.
  - The next turn connects again (§2), so no retry logic is added.
- **Status route.** `GET /api/v1/bambuddy/mcp` answers:

  ```json
  { "state": "off" | "no_url" | "no_key" | "starting" | "ready" | "failed",
    "reason": null,
    "endpoints": 779, "reads": 172, "writes": 404, "hidden": 203, "unclassified": 0,
    "key_fallback": true,
    "url": "http://127.0.0.1:8091/mcp",
    "token_last4": "a1b2" }
  ```

  - `reason` is a fixed phrase plus the scrubbed stderr tail, for the UI only.
  - It never carries the key or the token.
  - `token_last4` is the last four characters of the token. Those are also the last four
    of the plugin secret `Bearer <token>` (50 characters, well over `last4()`'s
    12-character floor). The page compares it with the plugin view's `auth.last4` (§5).
- **Enable and disable.**
  - `POST /api/v1/bambuddy/mcp/enable` and `POST /api/v1/bambuddy/mcp/disable` get a new
    dependency, `require_ui_origin`. It needs an `Origin` header and accepts it only
    when `origin_allowed(origin, public_url, allowed_origin_list)` holds, the same rule
    `api/realtime.py` uses for WebSockets. Otherwise it answers 403.
  - **These are the backend's first origin-guarded POSTs.** The dependency is written
    to be reused, but this spec applies it only to these two routes, because enable is
    the first backend route that returns a secret.
  - Enable sets `bambuddy_mcp_enabled`, generates a new token (`secrets.token_urlsafe(32)`), saves both, and
    returns `{url, token, tool_tiers}` **once**. `url` is built from the configured
    port.
  - Disable sets `bambuddy_mcp_enabled=false` and clears the token. Both emit
    `"bambuddy_mcp"`, so every replica's bridge follows.
- **Agent coverage.** The three routes get `NOT_A_TOOL` entries in
  `agent/src/tools/coverage.ts`:
  - `GET /api/v1/bambuddy/mcp`: Settings-page status, not useful to the assistant.
  - `POST …/enable`: returns a bearer token, so it must never be an agent tool.
  - `POST …/disable`: a Settings action the user takes.

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
reads the status route and the `bambuddy` entry from `GET /api/v1/ai/plugins` (the
plugin view):

- `enabled`, and the plugin is missing, or its `auth` is `null`, or
  `auth.last4 ≠ token_last4`: the page shows
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
- **Classification**, against a fixture of real Bambuddy operations, with their full
  tag lists, from 1.2.6b1's `openapi.json`:
  - every `HIDDEN_TAGS` tag, every hidden prefix and every hidden path rule hides:
    `/dl/`, `/confirm/`, `/oidc/`, a `token` segment, `/stream`, and a
    `{…token|secret|key|code|password…}` parameter;
  - these are hidden: `GET /api/v1/auth/tokens` (tag `authentication`, and also by
    prefix), `/settings/backup`, `/cloud/settings`,
    `/archives/confirm/{token}/{verdict}`, `POST /printers/camera/stream-token` and
    `GET /printers/{printer_id}/camera/stream`;
  - a path under `/api/v1/auth/` with an invented, non-hidden tag is still hidden
    (prefix);
  - **multi-tag:** an operation tagged `["printers", "settings"]` is hidden, and a GET
    tagged `["printers", "finance"]` is a write, not a read;
  - a GET in an unknown tag is a write;
  - `…/download` and `…/export` are writes;
  - a GET whose tags are all in `READ_TAGS` is a read;
  - an unparsable description, and a tool with no matching operation, are hidden and
    counted as `unclassified`;
  - the live fixture classifies as 172 read, 404 write and 203 hidden.
- **Tools.**
  - `bambuddy_search` never returns a hidden tool.
  - `bambuddy_get` refuses a write and a hidden tool.
  - `bambuddy_call` refuses a read, a hidden tool, and a `method`/`path` that don't
    match `name`.
- **Caps.** Text over 256 KiB is cut; an image over 2 MiB is replaced; only the first
  image is kept.
- **Timeout.** A fake child that never answers a `tools/call` gets "did not answer
  within 30 s" after the timeout (shortened in the test), and the child is restarted.
- **Auth and state.**
  - 401 without the token and with a wrong token.
  - 503 before ready, with the fixed body and no stderr text.
  - The `off`, `no_url`, `no_key` and `failed` states.
  - A taken port yields `failed` with the fixed reason "port in use".
- **Child.**
  - Its environment is exactly §4.2's set; the test spawns `env` through the same
    launcher.
  - Restart, backoff and the 5-in-10-minutes stop, driven by a fake child script.
  - A `"connection"` event that changes the resolved key, and a resync event after one,
    each restart it.
  - **A token-only change leaves the child running** (same pid). The new token is
    accepted and the old one gets 401.
  - **A call in flight when the child restarts** gets the fixed "restarting" error, not
    a hang.
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
  `openapi.json` has:
  - a `printers` GET;
  - an `authentication` GET under `/api/v1/auth/` (hidden);
  - a `/dl/{token}` GET (hidden);
  - a GET tagged `["printers", "settings"]` (hidden);
  - a `queue` POST, a `printers` PATCH and a `queue` DELETE;
  - a `camera` GET returning PNG.
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
- With the fake MCP server answering 503 to `initialize`/`tools/list`, the turn
  records `plugin_unavailable` for `bambuddy`. The next turn, with the server
  answering, has the tools.
- `test/coverage.test.ts` passes with the three new `NOT_A_TOOL` entries.

**Frontend tests** (vitest + msw):
- The key field and the fallback warning.
- Switching on calls enable, then the plugin POST, or the PATCH on a 409, using the
  returned `url`.
- Switching off calls the plugin DELETE, then disable, even when the DELETE fails, and
  shows "Remove the plugin" then.
- Reconciliation: a missing plugin, `auth: null`, or an `auth.last4` that differs from
  `token_last4` shows "Reconnect"; a leftover plugin while disabled shows "Remove the
  plugin".

**Docker build:**
- The image asserts `bambuddy-mcp` 0.2.0 with the `importlib.metadata` check (§4.1).
- `uv pip install --require-hashes` fails when a resolved dependency, listed or not,
  has no hash. That's checked once, by hand, when the lockfile is generated: removing
  one line must break the build.
- `uv pip check` passes.
- The runtime-stage smoke import succeeds.

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
