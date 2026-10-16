# Bambuddy MCP bridge Implementation Plan (DRAFT OUTLINE)

> Status: outline only. The full task-by-task plan with code is not written yet. These
> are the decisions and facts gathered while planning, so the full plan can be written
> without re-deriving them.

**Spec:** `docs/superpowers/specs/2026-10-05-bambuddy-mcp-bridge-design.md` (#1466)

## Deviations from the spec (record in spec §9 when the plan is final)

1. **Hand-rolled JSON-RPC instead of the `mcp` SDK**, on both the stdio side and the HTTP
   side. Only a few messages are needed (initialize, notifications/initialized,
   tools/list with nextCursor, tools/call, ping), and the SDK's 2.x API changed. The
   backend gains no new dependency.
2. **Package `backend/scadbuddy/bambuddy_mcp/`**, not `bambuddy/mcp_bridge.py`.
   `discover_components` finds only top-level `scadbuddy/<feature>/component.py`. Its
   modules:
   - `classify.py`
   - `results.py`
   - `tools.py` (holds `SEARCH`, `GET`, `CALL`, `TOOL_TIERS` and `NOT_READY`)
   - `child.py`
   - `bridge.py`
   - `server.py`
   - `component.py`
3. **Multipart operations are hidden.** Upstream's `http.py` uploads with
   `os.path.isfile(value)`, so a string argument would read files from the backend
   container. 19 live operations are newly hidden.
4. **Action GETs inside READ_TAGS count as writes.** Any GET path with a literal segment
   in {stop, test, check-plate, prepare, discover, diagnostic, runtime-debug} is a write:
   - `/camera/stop`, `/camera/test`, `/camera/check-plate`
   - `/firmware/updates/{id}/prepare`
   - `/smart-plugs/discover/*`
   - `/diagnostic`, `/runtime-debug`

   That is 8 operations.
5. **Path-argument check.** Upstream's `build_url` substitutes `str(value)` unescaped,
   then calls `urljoin`, so `../` could reach hidden endpoints. The bridge refuses a
   path parameter that:
   - is not a str or int (bool is refused too);
   - is empty, `.` or `..`;
   - contains `/ \ ? # %`, whitespace or control characters.
6. **The child gets two extra settings.** Its environment adds `TMPDIR=<home>/tmp`, and
   every call passes `embed_image: true`, so upstream never writes image files to /tmp.
   `BAMBUDDY_CENSOR_MODEL_FILENAME` stays unset (false), or embedding is blocked.
7. **Secret-looking JSON keys are redacted in results.** Keys matching
   access_code|api[_-]?key|token|secret|password|passwd|private[_-]?key have string
   values replaced by `***`.
8. **Settings layout:**
   - `bambuddy_mcp_api_key` is an env-seeded secret. Add it to:
     - `Settings`;
     - `SECRET_FIELDS`;
     - `APPLIES` as "live";
     - `SettingsPatch`;
     - the secrets tuple in `save()`.

     A save emits the existing `"connection"` section.
   - `bambuddy_mcp_enabled` and `bambuddy_mcp_token` are `StoredSettings`-only.
     - They are written by the new `SettingsStore.set_bambuddy_mcp(*, enabled, token)`,
       which emits `"bambuddy_mcp"`.
     - `SettingsPatch` refuses both (`extra="forbid"`).
   - `bambuddy_mcp_port: int = 8091` goes in `BOOTSTRAP_FIELDS`, with a reason.
     `0` means an ephemeral port, for tests.
   - The view adds `has_mcp_api_key` and `mcp_key_fallback`. `test_settings_coverage`
     requires the `has_mcp_api_key` name.
9. **The bridge listens for three events:** `SettingsChanged` "connection" and
   "bambuddy_mcp", plus `BusResync`. It uses `events.add_listener` with
   `loop.call_soon_threadsafe`, like `api/runtime.py` `follow_changes`.
10. **The HTTP side binds only while enabled.** Otherwise every API test would bind
    8091. The `LoopbackServer` mirrors `worker.py`'s `_HealthServer`: it overrides
    `capture_signals`, binds its own socket with `SO_REUSEADDR`, and calls
    `serve(sockets=[sock])`. If `bind` raises OSError, the state is `failed` with reason
    "port in use".
11. **The status adds an `enabled` field**, and disable returns the status.
12. **The lockfile pins `mcp==1.26.0`** in `requirements.in`, next to
    `bambuddy-mcp==0.2.0`. That is upstream's own uv.lock version; its `mcp>=1.0.0`
    would otherwise take 2.x.
13. **The plugin is registered with `enabled: true`**, because the agent registers a
    plugin disabled by default. Reconciliation also treats `enabled: false` as "not
    connected".

## Live numbers (Bambuddy 1.2.6b1, rules above)

779 operations: **164 read, 393 write, 222 hidden**, 0 unclassified.

The slim fixture is committed at `backend/tests/fixtures/bambuddy-openapi-1.2.6b1.json`.
It keeps paths → methods → {tags, operationId, multipart marker}.

## Upstream facts (bambuddy-mcp 0.2.0 source)

- **Tool descriptions.** The first line is `[<first tag or empty>] METHOD path`, then the
  summary and description.
- **Tool names.** A name is `clean_tool_name(operationId)`; duplicates get a `_N`
  suffix.
- **Startup.** The child fetches `urljoin(url, "/openapi.json")` and exits 1 on failure.
  It authenticates with the `X-API-Key` header and uses `httpx` with `timeout=30`.
- **Errors** come back as the text `HTTP {status} Error:\n{json or text}`.
- **Images and binaries.**
  - Images are base64 `ImageContent` only when `embed_image` is set and filename
    censoring is off.
  - octet-stream, video and audio responses come back as base64 text.
- **Arguments.** `embed_image` is popped from the arguments in direct mode.
- **Console script.** `bambuddy-mcp = "bambuddy_mcp:run"`.

## Task list (each with TDD steps in the full plan)

1. **Settings** (backend):
   - fields, `mcp_bambuddy_key()`, `set_bambuddy_mcp`, the `"bambuddy_mcp"`
     SettingsSection, and the view flags;
   - tests in `test_settings_store.py` and `tests/api/test_settings.py` (DEFAULTS);
   - add a README env table row.
2. **`classify.py`:**
   - `Operation(tags, multipart)`, `operations(spec)`, `classify`, `parse_description`,
     `Endpoint`, `Index`, `build_index`, `search` (limit ≤ 25) and
     `path_argument_problem`;
   - fixture tests for the counts, every hidden rule, multi-tag cases, action GETs and
     traversal.
3. **`results.py`:**
   - `Scrubber`, `Tail` (20 lines × 500 characters), `redact`/`redact_text`, `cap_text`
     (256 KiB, measured in bytes), `shape` (one image ≤ 2 MiB, a 403 reworded with
     `Scope.READ_STATUS` for reads), `text_result` and `error_result`.
4. **`child.py`:**
   - `child_env`, `spawn`, `StdioChild` (start, initialize, list_tools, call_tool, wait,
     kill, stop);
   - `ChildGone` on exit or on a line over the limit; stderr read in chunks;
   - fake child `tests/support/fake_bambuddy_mcp.py`. It is stdlib only and driven by a
     JSON config: tools, results, hang, exit, exit_at_start, stderr, pids.
5. **`bridge.py`:**
   - `Inputs` (child tuple, blocked), `BambuddyMcpStatus`, `fetch_openapi`, `Bridge`;
   - `Bridge` members: `apply` under a lock, the supervisor (backoff 1, 2, 4… capped at
     60; failed after 5 failures in 600 s), `call` (30 s timeout, then a restart without
     counting a failure), `running()` (listener plus the first apply) and `close`.
6. **`server.py`:**
   - `build_app(McpBackend)`;
   - auth: 401 with no challenge, checked with `hmac.compare_digest`;
   - 503 with a fixed JSON-RPC error; notifications get 202; non-POST gets 405;
   - methods: initialize (version negotiation), ping, tools/list, tools/call;
   - `LoopbackServer`.
7. **Component and routes:**
   - `component.py`: `Bridge`, `COMMAND=/opt/bambuddy-mcp/bin/bambuddy-mcp`;
   - `api/origin_guard.py`: `require_ui_origin`;
   - `api/bambuddy_mcp.py`: `GET /bambuddy/mcp`, `POST …/enable` returning
     `BambuddyMcpEnabled{url, token, tool_tiers}`, and `POST …/disable`;
   - API tests: the module overrides the `settings` fixture with a free port.
8. **Packaging:**
   - Dockerfile stage `bambuddy-mcp` (from base): `uv venv --python 3.12`,
     `--require-hashes`, `uv pip check`, the importlib.metadata version check, and the
     sdist plus LICENSE in `/usr/share/doc/bambuddy-mcp/`;
   - the app stage copies both and runs the smoke import;
   - `backend/bambuddy-mcp/requirements.{in,txt}`, via `uv pip compile --generate-hashes
     --python-version 3.12 --universal`;
   - a `THIRD_PARTY_NOTICES.md` section and a README "Deploying" note;
   - the `requires_bambuddy_mcp` marker and skip fixture;
   - integration test: a fake Bambuddy on a `LoopbackServer(port=0)` with 8 operations,
     expecting 2 read, 3 write and 3 hidden.
9. **Agent:**
   - three `NOT_A_TOOL` entries;
   - a `tools` option for `test/support/fakeMcp.ts`;
   - `test/bambuddyPlugin.e2e.test.ts`:
     - the harness runs `bambuddy_get` without a decision;
     - `bambuddy_call` gets needs_approval;
     - with Postgres, the approval's `inputSummary` has method and path;
     - a 503 on turn one gives `plugin_unavailable` matching
       `/^plugin bambuddy is not available in this turn/`, and turn two has the tools.

     The session manager takes `remotePlugins: () => Promise<PluginsForRun>`.
10. **Frontend:**
    - the types and three `api` methods (use `request`, not `command`);
    - add `enabled`, `tool_tiers` and `disabled_tools` to `RemotePluginCreate`;
    - in `fields.ts`, add `HAS_KEY` and `serverValue` entries;
    - in `SettingsPage`, add the key to `SECRETS` and `HAND_LAID.connection`, a key
      FieldRow with the `mcp-key-fallback` warning, and
      `{ai.available && <BambuddyMcpPanel/>}`;
    - new `components/settings/BambuddyMcp.tsx` covering switch on (with PATCH on a
      409), switch off (delete, then disable even if the delete failed) and
      reconciliation;
    - mocks:
      - `mocks/aiPlugins.ts` must allow loopback http and honour enabled, tiers and
        secret on create and PATCH;
      - a new `mocks/bambuddyMcp.ts`, registered in `handlers.ts` and reset there.
11. **Docs:** the D8 addendum in `2026-09-27-ai-integration-design.md`, and a spec §9
    listing the deviations above.

## Review Focus

1. Path-parameter traversal (`../`) in a read call reaching a hidden endpoint (Tasks 2, 5).
2. A side-effect GET inside the read tags (Task 2).
3. A child stdout line over the reader limit must end the call with the restarting
   error, not hang (Task 4).
4. A multipart upload reading a local file in the container (Task 2).
5. Enable succeeds but the agent's plugin POST fails: the page shows the error, and
   Reconnect on reload (Task 10).
