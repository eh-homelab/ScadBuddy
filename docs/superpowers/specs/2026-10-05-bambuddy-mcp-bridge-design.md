# Bambuddy MCP bridge (#1466)

Status: designed with the owner in conversation on 2026-10-05, approved section by
section. This written spec is not yet approved; its review gates the implementation
plan. Part of epics #249 (AI agent integration) and #23 (Bambuddy integration).

## 1. What this changes

The assistant reaches Bambuddy today only through ScadBuddy's own tools: send, print,
library, projects and filaments. Spec D8 (`2026-09-27-ai-integration-design.md`) chose
that over third-party Bambuddy MCP servers.

This adds an **optional, off-by-default** bridge to
[`bambuddy-mcp`](https://github.com/MrMebelMan/bambuddy-mcp) (PyPI `bambuddy-mcp`,
GPL-3.0). That server turns Bambuddy's whole REST API, 430+ endpoints read from its
`/openapi.json`, into MCP tools. With the bridge, the assistant can answer questions
ScadBuddy's tools don't cover: printer status, archives and statistics, maintenance,
camera snapshots and spool inventory. It still cannot change anything in Bambuddy
without an approval.

Owner decisions:

- It is managed from ScadBuddy's Settings, not by the cluster (option A of two).
- It has **its own Bambuddy key, like the render key, and falls back to the default
  key** when that is unset.

Out of scope: OAuth, more than one instance, reaching the bridge from outside the pod,
and use from a Claude Code install outside ScadBuddy (that is the upstream package's
own stdio mode).

## 2. Facts this rests on

Each was read on 2026-10-05 from `origin/main` or the package's source at 0.2.0.

- `bambuddy-mcp` 0.2.0 speaks **stdio only** (`server.py`: `mcp.server.stdio`).
  - Configuration is by environment: `BAMBUDDY_URL`, `BAMBUDDY_API_KEY`,
    `BAMBUDDY_DIRECT_MODE`, and `BAMBUDDY_CENSOR_ACCESS_CODE`, `…_SERIAL` and
    `…_MODEL_FILENAME`.
  - In direct mode, each endpoint is one tool. Its description starts
    `[<tag>] <METHOD> <path>` (`openapi.py` `build_tool_description`).
  - Its dependencies are `mcp` and `httpx`.
- The agent accepts **remote HTTP plugins only**.
  - A plugin URL must be `https`, or plain `http` when every address it resolves to is
    loopback (`agent/src/plugins/registry.ts` `assertEndpointAllowed`, spec §8.4).
  - Stdio and other process-starting plugins are refused (`harness/plugins.ts`),
    because a child would inherit the credential env.
  - Unlisted plugin tools are `outward`.
  - The forwarder refuses redirects and 401/OAuth discovery
    (`plugins/forwarder.ts`).
- The agent and the backend **share the pod network** (README, "The agent sidecar").
  So `127.0.0.1` in the agent reaches a backend port that is bound to loopback.
- The render key is a secret setting with a fallback:
  - `settings_store.py` `render_bambuddy_key()` returns the render key, or else the
    full key with a fallback flag.
  - The Settings view exposes `has_render_api_key` and `render_key_fallback`
    (`api/settings.py`).
  - The page warns while the fallback is in use.
- The assistant's HTTP-request tool may reach loopback (`http/egress.ts` allows
  127/8). GET runs at once; POST waits for approval. Every MCP call is a POST, so that
  tool cannot skip the bridge's tiers. The bridge still requires a token (§4.3).
- ScadBuddy is Apache-2.0; `bambuddy-mcp` is GPL-3.0.

## 3. Settings

New fields in `StoredSettings` (key/value table, so no migration):

| Field | Kind | Default | Scope |
|---|---|---|---|
| `bambuddy_mcp_enabled` | bool | `false` | live |
| `bambuddy_mcp_api_key` | secret (like `bambuddy_render_api_key`) | unset | live |
| `bambuddy_mcp_token` | secret, generated, never set by the user | unset | live |

- `mcp_bambuddy_key() -> tuple[str | None, bool]` mirrors `render_bambuddy_key()`:
  - it returns the MCP key when set;
  - otherwise it returns `bambuddy_api_key` with `fallback=True`;
  - with no key at all it returns `(None, False)`.
- The settings view adds `has_mcp_api_key` and `mcp_key_fallback`. Both secrets join
  the list of fields `save()` treats as secrets (an empty string clears the field) and
  the redaction list. They are never returned.
- **The Settings page**, in the Bambuddy section beside the render key:
  - A key field: "Bambuddy MCP key".
  - Help text: "A Bambuddy API key for the assistant's Bambuddy tools. Give it only the
    scopes you want the assistant to have; Read Status is enough to look things up.
    Unset, it uses the main key."
  - While the fallback is in use, a warning: "The assistant's Bambuddy tools use the
    full Bambuddy key, which can print."
  - A switch: "Let the assistant use Bambuddy's API (bambuddy-mcp)", with the bridge
    state (§5) shown next to it.

## 4. The bridge (backend)

### 4.1 Packaging

- `bambuddy-mcp==0.2.0` is installed in the image with
  `uv tool install --from bambuddy-mcp==0.2.0` into its own tool venv, outside the
  backend's venv. It is pinned by hash through a small `requirements` file with
  `--require-hashes`.
- The backend runs it only as an executable. **It never imports it**, so the GPL code
  and ScadBuddy's Apache code meet only at a process boundary.
- The Dockerfile asserts the installed version, as it does `OPENSCAD_VERSION`:
  `BAMBUDDY_MCP_VERSION=0.2.0`.
- A bump changes the pin and the assertion in the same commit, and re-runs §7's
  integration test.

### 4.2 Child process

`scadbuddy/bambuddy/mcp_bridge.py` owns one child process, `bambuddy-mcp`, started with
`asyncio.create_subprocess_exec`, as `library/lsp.py` starts `openscad-lsp`.

- **Environment: exactly this set.** Nothing is inherited, and a test asserts it.
  - `BAMBUDDY_URL`: the settings' `bambuddy_url`.
  - `BAMBUDDY_API_KEY`: from `mcp_bambuddy_key()`.
  - `BAMBUDDY_DIRECT_MODE=true`.
  - `BAMBUDDY_CENSOR_ACCESS_CODE=true` and `BAMBUDDY_CENSOR_SERIAL=true`.
  - `PATH`, a minimal value for the tool venv.
  - No `SCADBUDDY_*` variable, and no other secret.
- **Talking to it.** The bridge uses the `mcp` Python SDK's stdio client (MIT). It
  calls `tools/list` once after start, then `tools/call` per request.
- **Endpoint index.** Each listed tool is parsed into
  `{name, method, path, tag, description, input_schema}` from its `[tag] METHOD path`
  description.
  - A tool whose description does not parse is left out and counted in
    `unclassified`.
  - It is never guessed into the read set.
- **Start and stop.** It starts when the switch is on, a key resolves and
  `bambuddy_url` is set. It stops when any of those stops holding.
  - A `SettingsChanged` event for the Bambuddy section restarts it: a new key, URL or
    token.
  - On backend shutdown it is killed, as the LSP sessions are.
- **Crashes.**
  - Restart with backoff: 1, 2, 4… up to 60 s.
  - After 5 failures in 10 minutes it stays `failed` until the settings are saved
    again.
  - Stderr is kept as a bounded tail of 20 lines, with the key, the token and
    `access_code`-looking values scrubbed before it is stored or logged.

### 4.3 HTTP side

- **Server.** A second ASGI app in the backend process, on `127.0.0.1:8091`.
  - It is served by its own uvicorn server task, started in the app lifespan, and
    never mounted on the public 8080 app.
  - The port is a setting, `SCADBUDDY_BAMBUDDY_MCP_PORT`, default 8091, so tests and
    other deployments can move it.
  - If the port is taken, the bridge reports `failed: port in use`.
- **Transport.** Streamable HTTP at `/mcp`, from the `mcp` SDK's server side. JSON
  responses; no SSE stream is needed for these tools.
- **Auth.**
  - Every request needs `Authorization: Bearer <bambuddy_mcp_token>`, compared in
    constant time.
  - A missing or wrong token answers 401 with no `WWW-Authenticate` challenge. The
    forwarder treats it as a failing plugin, not as an OAuth sign-in.
  - Until the bridge is `ready`, it answers 503.
- **Tools.** The child's 430+ tools are not passed through. The bridge exposes three:

| Tool | Tier | Input | Does |
|---|---|---|---|
| `bambuddy_search` | `read` | `query` (string), optional `category`, optional `limit` (≤25) | Searches the endpoint index by name, path, tag and description. Returns name, method, path, summary and input schema. Answered by the bridge itself; the child is not called. |
| `bambuddy_get` | `read` | `name`, `arguments` | Calls the child's tool `name` **only if its method is GET**. Otherwise it is an error naming `bambuddy_call`. |
| `bambuddy_call` | `outward` | `name`, `arguments` | Calls a POST, PUT, PATCH or DELETE tool. A GET name is refused with a pointer to `bambuddy_get`, so a read never needs an approval it doesn't need. |

- **Tool descriptions** tell the model:
  - start with `bambuddy_search`;
  - printing or sending a ScadBuddy output goes through ScadBuddy's own
    print/send tools, not `bambuddy_call`, because those tools match spools, pick the
    rack hotend and file the result in the project.
- **Results.** Results pass through as the child returns them: text, or image content
  for a camera snapshot. Bambuddy HTTP errors keep their status and message.
  - A 403 is reworded by the existing scope mapping (`bambuddy/errors.py` `Scope`):
    "the Bambuddy MCP key lacks <scope>".
  - Results are capped at the agent's existing plugin result size. The bridge adds no
    cap of its own.

### 4.4 Status route

`GET /api/v1/bambuddy/mcp` answers:

```json
{ "state": "off" | "no_key" | "starting" | "ready" | "failed",
  "endpoints": 431, "reads": 210, "writes": 221, "unclassified": 0,
  "key_fallback": true, "reason": null, "url": "http://127.0.0.1:8091/mcp" }
```

- It carries no secret.
- `POST /api/v1/bambuddy/mcp/enable` and `…/disable` are behind the UI origin guard,
  like every other write.
  - Enable generates a new token, saves it, starts the bridge, and returns the token
    **once**, with the URL and the tool tiers, so the page can register the plugin
    (§5).
  - Disable clears the token and stops the bridge.

## 5. Registering with the assistant

The browser drives registration, through routes that already exist, so the agent gains
no new trust path.

1. The user turns the switch on. The page calls `POST /api/v1/bambuddy/mcp/enable`
   and gets `{url, token, tool_tiers}`.
2. The page creates the `bambuddy` plugin with `POST /api/v1/ai/plugins`. If it
   already exists (409), it updates it instead with
   `PATCH /api/v1/ai/plugins/bambuddy` (`agent/src/routes/plugins.ts`):
   - `url`: `http://127.0.0.1:8091/mcp`
   - `auth_header`: `Authorization`, with secret `Bearer <token>`
   - `tool_tiers`: `{bambuddy_search: read, bambuddy_get: read, bambuddy_call: outward}`
3. The page shows the plugin's state as the AI plugins list does.
4. Turning the switch off deletes the `bambuddy` plugin, then calls `…/disable`.

Notes:

- The token passes through the browser once, as any pasted plugin secret does. It only
  opens a loopback port inside the pod. **The Bambuddy key never reaches the browser.**
- If the plugin write fails after enable succeeded, the page shows the error and offers
  "Try again". The bridge is running but unreachable without the token, which is
  harmless.
- Every call through the plugin is audited as plugin calls already are: AI activity,
  tool name and outcome, without secrets.
- An outward `bambuddy_call` parks in `canUseTool`. Its approval shows the endpoint's
  method, path and arguments.

## 6. Amendment to spec D8

`2026-09-27-ai-integration-design.md` D8 gets this addendum:

> Amended 2026-10-05 (#1466): an optional, off-by-default bridge to the third-party
> `bambuddy-mcp` covers the parts of Bambuddy's API ScadBuddy's own tools do not
> (status, archives, maintenance, camera, inventory). It keeps D8's reasons:
> - the key stays server-side and is its own setting;
> - reads are `read` and every write is `outward`, which needs an approval;
> - errors use the same scope mapping;
> - calls are audited as plugin calls.
>
> Printing and sending ScadBuddy outputs stay with ScadBuddy's tools.

## 7. Testing

CI has no real Bambuddy.

**Backend unit tests:**
- `mcp_bambuddy_key()`: the MCP key, the fallback, and none. These mirror the
  render-key tests.
- The settings view flags.
- Secrets are never returned or logged.
- Description parsing: GET, POST, PUT, PATCH and DELETE, and an unparsable
  description counted as `unclassified`.
- `bambuddy_get` refuses a write; `bambuddy_call` refuses a GET.
- 401 without the token and with a wrong token; 503 before ready.
- The child's environment is exactly §4.2's set: the test spawns `env` through the same
  launcher.
- Restart, backoff and the 5-in-10-minutes stop, driven by a fake child script.
- The stderr tail is scrubbed of the key and the token.
- The 8080 app has no `/mcp` route for this bridge.

**Integration test** (`requires_bambuddy_mcp`; skips when the tool is not installed;
runs in the Dockerfile `test` image):
- The real pinned `bambuddy-mcp` runs against a uvicorn-served fake Bambuddy with a
  six-endpoint `openapi.json` (three GET, one each of POST, PATCH and DELETE).
- The bridge reaches `ready`, lists 3 tools, and makes a GET through `bambuddy_get`
  that reaches the fake.
- A POST through `bambuddy_call` reaches the fake.
- A camera endpoint returning PNG comes back as image content.

**Agent tests:**
- A plugin registered with the three tiers loads.
- `bambuddy_call` parks as `outward` and `bambuddy_get` does not. This reuses the
  existing plugin harness tests with a local fake MCP server.

**Frontend tests** (vitest + msw):
- The key field and the fallback warning.
- Switching on calls enable, then the plugin POST, or the PATCH on a 409.
- Switching off calls the plugin DELETE, then disable.
- A failed plugin write shows "Try again".

**Docker build:** the image asserts `bambuddy-mcp` 0.2.0.

## 8. Risks

- **The upstream package is small** (one maintainer, 5 stars on 2026-10-05).
  - Pinned by hash, and bumped only deliberately (§4.1).
  - The bridge's own tool set and tiers do not depend on upstream's meta-tools, so an
    upstream change to those cannot widen what is `read`.
- **Endpoint descriptions change format.** Unparsable tools drop out as `unclassified`.
  They are never guessed into the read set, and the status route shows the count.
- **The full-key fallback** lets `bambuddy_call` print after approval. The Settings
  warning and §3's help text steer users to a scoped key.
