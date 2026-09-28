# AI security: the threat model as built

The spec's threat model is in §8.6 of
[`docs/superpowers/specs/2026-09-27-ai-integration-design.md`](../superpowers/specs/2026-09-27-ai-integration-design.md).
This page covers what `main` actually implements, where it lives, and what is still
open. Anything the spec plans but `main` does not have is marked **not built**.

## Trust boundaries today

- **The backend has no auth.** The trust boundary is the pod (spec §4.3). The UI has no
  login of its own either. Spec §8.3, "Stated plainly": "anyone who can reach Settings
  can change the MCP auth mode, mint tokens or approve actions."
- **The agent service has no authentication.** Its credential writes are *gated*, but
  not authenticated (see [Origin gate](#dns-rebinding-defence)). The limitation is
  stated in the header of [`agent/src/routes/guard.ts`](../../agent/src/routes/guard.ts).
- **No `/mcp`, no tool registry, no approvals** exist on `main`. Those are open PRs
  #368 and #471. The only tool paths today are the harness's in-process MCP servers
  (none registered in `main.ts`) and the browser bridge in the user's own tab
  ([browser-bridge.md](browser-bridge.md)).

## Risk tiers and the permission seam

Tiers (spec §8.1): `read`, `write` (reversible through history) and `outward` (send,
print, delete, settings or credential writes). A tool ScadBuddy does not recognise is
`outward`.

The implementation is in [`agent/src/harness/permissions.ts`](../../agent/src/harness/permissions.ts):

- `decide(toolName, tierOf)`: an unknown tool (`tierOf` returns `undefined`) is treated
  as `outward`. `read` and `write` are allowed. `outward` gets `needs_approval`.
- **Approvals are not built** (#258, open PR #471). Until then `needs_approval` is
  answered with a **deny**, whose message tells the model to explain rather than retry.
  An outward tool therefore never runs unattended.
- **It is enforced twice**, as spec §8.2 asks:
  - `makePreToolUseHook()` runs first. The [Agent SDK permissions docs](https://code.claude.com/docs/en/agent-sdk/permissions)
    say "a hook deny applies even in bypassPermissions mode" (quoted in the file).
  - `makeCanUseTool()` returns the same decision, and is what *allows* read and write
    tools.
- **Wiring.** `buildHarness()` in [`agent/src/harness/run.ts`](../../agent/src/harness/run.ts)
  sets both on every query, together with `permissionMode: 'default'`. If `tierOf`
  is omitted, every tool resolves to `outward`.

**Least privilege** (spec D7, §4.4). `buildQueryOptions()` in
[`agent/src/harness/options.ts`](../../agent/src/harness/options.ts) sets:

- `tools: []`, which removes all built-in tools: no Bash, file or web tools.
- `settingSources: []`, so nothing is read from a host `~/.claude` or a project
  `.claude/`.
- `strictMcpConfig: true` and `mcpServers: {}`.
- A service-owned `CLAUDE_CONFIG_DIR` and `cwd`.
- An explicit `env`, so the service's own environment (the database URL above all) does
  not reach the Claude Code subprocess.

The option semantics are quoted from the pinned SDK's `sdk.d.ts` 0.3.283 in that file.
`run.ts` also sets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, citing the
[gateway docs](https://code.claude.com/docs/en/llm-gateway-connect) ("Turn off traffic
outside the gateway path").

**Runaway limits.** Each query gets `maxTurns` (25) and `maxBudgetUsd` (1 USD), plus an
abort signal (`run.ts`). Sessions spend one budget across all their turns, and any
watcher can interrupt (PR #377 body, "Budget and turns", "Interrupt").

## Envelope encryption and AAD binding

Spec §9. The implementation is in [`agent/src/secrets.ts`](../../agent/src/secrets.ts)
and [`agent/src/credentials.ts`](../../agent/src/credentials.ts).

- **Envelope.** `sealSecret()` draws a random 32-byte data key per secret, seals the
  secret under it with AES-256-GCM, then seals the data key under the KEK (the key file,
  see [operating.md](operating.md#3-the-key-encryption-key)). The row stores
  `secret_sealed`, `dek_sealed` and `kek_id` (`Envelope`).
- **Sealed format.** `version (1) | IV (12) | GCM tag (16) | ciphertext`, from the
  `secrets.ts` header. `seal()` writes version `0x02`, whose AAD is `v2|` + context, so
  the version byte is authenticated too. Version `0x01` (#354) is opened only by the
  generic `open()`, and is never written.
- **AAD binding.** `credentialAad(kind, baseUrl)` is
  `ai_credentials:default:` + `JSON.stringify({kind, base_url})`, and the data key's
  AAD is `dek:` + that. Someone with write access to the table but without the KEK
  therefore cannot re-point `base_url` to their own host, or change `kind`: the edited
  row fails GCM authentication instead of sending the token elsewhere. The comment on
  `credentialAad()` explains, and the PR #379 findings table (row 2) lists the tests.
- **No legacy fallback.** A v1 row (#354, whose AAD did not bind `kind` and `base_url`)
  is refused (`openCredential()`). `/healthz` reports it as "outdated format". A
  fallback would "keep the unbound form valid for exactly the rows an attacker would
  target" (comment on `credentialAad()`; PR #379 body, "Finding 2").
- **Re-sending a secret.** A `PUT` that omits `secret` but changes `kind` or `base_url`
  gets `409` (`planPut()`). A stored secret is never sent to a destination its owner did
  not enter it for.
- **Never returned.** No route returns the secret. Reads give `kind`, `base_url` and the
  last four characters, and none for a secret under 12 characters (`view()` in
  [`agent/src/routes/credentials.ts`](../../agent/src/routes/credentials.ts); `last4()`).
- **Wrong key vs. tampering.** `openSecret()` compares `kek_id` first, so "wrong key"
  is reported as such. Anything else that fails authentication raises `SealError`,
  which never carries plaintext.
- **Rotation** re-wraps data keys only (`rewrap()`, `CredentialStore.rewrapFrom()`).
  See [operating.md](operating.md#33-rotating-it).
- **Plaintext in memory.** The Buffers the module creates are zeroed after use. JS
  strings cannot be zeroed, and the header states it: "Zeroing the Buffers shortens the
  exposure; it does not remove it."
- **Per-query delivery.** The decrypted credential reaches one Claude Code process
  through the SDK's `env` option only, never the container environment
  (`credentialEnv()` and `buildHarness()` in `run.ts`; spec §4.4).

## DNS-rebinding defence

Spec §8.4: "An `Origin` check on `/mcp`, the agent's sockets and the backend's
`/api/v1/ws` prevents DNS rebinding."

The implementation is `checkOrigin()` and `effectiveRequest()` in
[`agent/src/http/origins.ts`](../../agent/src/http/origins.ts), applied to every
`PUT`/`DELETE`/`POST` under `/api/v1/ai/credentials` by `uiRequestProblem()` in
[`agent/src/routes/guard.ts`](../../agent/src/routes/guard.ts).

- **Why an allowlist.** Under DNS rebinding, the attacker's page at
  `http://evil.test:8081` has its name re-pointed at this service. The browser then
  sends `Host: evil.test:8081` *and* `Origin: http://evil.test:8081`. They match, and the
  browser treats the request as same-origin: no CORS preflight, and JSON bodies are
  allowed. Only a list of the names the service is really served under tells the two
  apart. This is paraphrased from the `origins.ts` header.
- **The rule.** `Origin` and the request's own origin must be equal, and both must be
  on the list. The list is the origin of `SCADBUDDY_PUBLIC_URL`, plus the loopback pair
  from a loopback peer.
- **Forwarded headers.** They are trusted only from `SCADBUDDY_AGENT_TRUSTED_PROXIES`,
  and the last value wins. HTTPS is required, except on loopback (`isSecureTransport()`).
- **Tests.** The review's rebinding `PUT` is refused with 403 from a LAN peer, from
  loopback, from the trusted proxy, and with no public URL set (`agent/test/app.test.ts`
  "origin allowlist and DNS rebinding"; PR #379, row 1).
- **The backend's socket.** `/api/v1/ws` applies the same allowlist to the stored public
  URL (`origin_allowed()` in [`backend/scadbuddy/api/realtime.py`](../../backend/scadbuddy/api/realtime.py)).
  A frame with no `Origin` is treated like a REST call there, because the backend has
  no auth (module docstring, "Security").

## Egress check on gateway URLs

A `gateway` credential's `base_url` is a server-side request that whoever reaches
Settings chooses. `assertGatewayHostAllowed()` in
[`agent/src/http/egress.ts`](../../agent/src/http/egress.ts) runs on `PUT` and again
before `/test`:

- **Refused:**
  - link-local `169.254.0.0/16` and `fe80::/10`;
  - `100.100.100.200`, `fd00:ec2::254`, `0.0.0.0` and `::`;
  - the names `metadata`, `metadata.google.internal`, `metadata.goog`, `instance-data`
    and `instance-data.ec2.internal`;
  - a name that does not resolve.
- **Allowed:** public, private (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`) and
  loopback addresses, because "a LiteLLM or similar gateway on the LAN" is the normal
  reason to use `gateway`.
- **Resolved, not literal.** Every address the name resolves to is checked, including
  IPv4-mapped IPv6 (`plainAddress()`).
- **Limitation (stated in the file).** Claude Code resolves the name again when it
  connects, so a name re-pointed afterwards is not caught. "It narrows the SSRF, it is
  not a network policy; an egress NetworkPolicy on the pod is the boundary."

## Stderr redaction

Claude Code's stderr arrives in arbitrary chunks, so a per-chunk replace could miss a
secret split across two of them. `lineRedactor()` in
[`agent/src/harness/redactLines.ts`](../../agent/src/harness/redactLines.ts) handles this:

- it buffers to line boundaries and redacts whole lines with the shared `redact()`
  (`secrets.ts`);
- it cuts a runaway line (over 64 KiB) without splitting any occurrence or prefix of a
  secret.

`runHarness()` flushes the buffer when the stream ends, returns early or throws. The
connection test also redacts the credential from its `detail` (`testConnection()`).
`redact()` ignores secrets shorter than 4 characters.

## Plugin vetting

The credential reaches Claude Code through `env`, and every process Claude Code starts
for a plugin inherits that environment. `tools: []` removes Bash, but not these
processes. So `assertPluginAllowed()` / `pluginProblems()` in
[`agent/src/harness/plugins.ts`](../../agent/src/harness/plugins.ts) refuse **the whole
plugin** if it declares any of the following:

- a hook whose `type` is not `http`, `mcp_tool`, `prompt` or `agent`, so `command` and
  missing types are refused;
- an MCP server that is not `type: "http"`/`"sse"` with a `url` and no `command`, or any
  `.mcpb`/`.dxt`/URL bundle;
- LSP servers;
- monitors.

It checks the default files (`hooks/hooks.json`, `.mcp.json`, `.lsp.json`,
`monitors/monitors.json`) and the manifest fields. The sources are the
[plugin manifest reference](https://code.claude.com/docs/en/plugins-reference) and the
[hooks reference](https://code.claude.com/docs/en/hooks), cited in the file. `bin/` is
not checked, because it only extends the Bash tool's PATH. `buildHarness()` calls
`assertPluginAllowed()` for every `pluginPaths` entry. ScadBuddy's own plugin passes
(PR #379, row 8). Plugin packages add the rules in the next section.

## Plugin packages

Packages (#297) are Claude plugins that ScadBuddy fetches from a git URL or a marketplace
entry. They are someone else's code from the network, so they get more checks than the
harness's own rules above. The code is in
[`agent/src/plugins/packages/`](../../agent/src/plugins/packages/), and
[operating.md](operating.md#9-plugin-packages-297) describes the flow.

**Approval (spec §8.2).** Installing is an outward settings write. An install or re-pin
only fetches, vets and stores the pin with its review. Nothing loads until the admin
approves that exact `commit_sha` and `content_hash` through
`POST /api/v1/ai/plugin-packages/:name/approve`. Enabling needs an approved pin (also a
`CHECK` on `ai_plugin_packages`). A re-pin stays pending, and the old pin keeps loading,
until the admin approves the new one after seeing its file diff. The routes use the
same UI guard as credential writes, with the same limitation (Known limitations, 1).

**Pin and cache.** The content hash is SHA-256 over a sorted list of path, executable bit
and file SHA-256 (`hashTree()`, `hash.ts`). The cached copy is hashed again before every
load. If it does not match, it is deleted and fetched again at the pinned commit, and the
package loads only if the new files hash to the pin (`materialise()`, `install.ts`). The
package is also vetted again at every load, so rules that have tightened since approval
still apply. A process that writes the cache between the check and Claude Code's read is
not caught; such a process already controls the pod.

**Fetching** (`git.ts`):

- The source URL must be https, or http to a loopback address. It may not carry
  credentials, a query or `$` (`normaliseGitUrl()`, `source.ts`). It must pass the
  egress check (`assertEndpointAllowed()`: no link-local or cloud-metadata address)
  before git runs.
- git gets an environment of its own: no database URL, no key path, no credential.
  `GIT_ALLOW_PROTOCOL` is `https:http`, and no system or global config is read.
  `http.followRedirects=false`, `core.hooksPath=/dev/null`, `transfer.fsckObjects` and
  `GIT_TERMINAL_PROMPT=0` are set. The fetch is shallow, takes no tags or submodules,
  and has a time limit.
- Refs and paths are held to an alphabet that cannot start with `-` or contain `..`.
- A symlink or submodule anywhere in the plugin's directory is refused, from `git
  ls-tree`, before any file is used. `hashTree()` refuses any non-regular file again.
  A package may have at most 2000 files and 20 MB.
- Like the gateway check, this is point-in-time: git resolves the name again itself.

**Vetting** (`vetPackage()`, `vet.ts`, on top of `pluginProblems()`). The whole package
is refused, with every problem listed, if it has any of the following:

- **Dynamic context injection** (`` !`cmd` `` or a ```` ```! ```` block) in any Markdown
  file. These run a shell "before the skill content is sent to Claude"
  ([skills](https://code.claude.com/docs/en/skills)). Measured on CLI 2.1.283, the
  harness already denies it ("Permission to use Bash has been denied", even with
  `allowed-tools: Bash(...)`; `test/pluginPackages.e2e.test.ts`).
- **Frontmatter** `hooks`, `mcpServers` or `permissionMode`, so every hook and server is
  in the vetted files and in the review.
- **Tools outside the allowlist.** `allowed-tools` and a subagent's `tools` may name MCP
  tools only (`mcp__…`). The tier seam decides each MCP tool's tier, and an unknown
  plugin tool is `outward`. Built-ins such as `Bash(...)` or `Write` are refused.
- **MCP servers** that are not `type: "http"`, that have a `headersHelper` (a command;
  [MCP](https://code.claude.com/docs/en/mcp)), or that contain a `$` anywhere. `${...}`
  resolves in an http server's `url` and `headers`
  ([plugins reference](https://code.claude.com/docs/en/plugins-reference), "Where each
  variable resolves"), which could send the credential to the plugin's server.
- **Hooks.** `mcp_tool` hooks are refused: the
  [hooks reference](https://code.claude.com/docs/en/hooks) ("MCP tool hook fields")
  does not say their call is permission-checked. `http` hooks may not use `$` or
  `allowedEnvVars` ("HTTP hook fields"). `pluginProblems()` already refuses command
  hooks. There is deliberately no switch to allow one, because it would inherit the
  credential env (above).
- **Manifest fields** that a headless run cannot honour: `dependencies`, `userConfig`,
  `channels`, `settings` or a root `settings.json` (their `agent` key replaces the main
  agent), and `workflows` (JavaScript).
- **A name** that is not 2–32 lower-case letters, digits and single hyphens, or that is
  reserved. The name namespaces the skills (`/<name>:<skill>`).

Every URL a package declares (MCP servers, http hooks) goes through the egress check at
install and again at every load. A marketplace entry must have a git source: a relative
path, `github`, `url` or `git-subdir`. `archive`, `npm` and `command` sources are
refused, because they have no commit to pin or they run a command. An entry that
declares components of its own is refused too
([marketplaces](https://code.claude.com/docs/en/plugin-marketplaces)).

Not done yet:

- per-part enabling. The review shows each part, but a package is enabled as a whole.
- tier maps for a package's own MCP tools. They stay `outward`.

## Event-log scrubbing (#377)

Each session's panel events go into `ai_session_events` and are replayed to every
watcher (spec §6). `scrubForLog()` in
[`agent/src/sessions/sdkEvents.ts`](../../agent/src/sessions/sdkEvents.ts) runs before
they are stored:

- the turn's credential is redacted from every string, in every event;
- in `tool.call` inputs, any argument whose name matches `SENSITIVE_KEY` is replaced
  with `[redacted]`, at any depth. `SENSITIVE_KEY` covers `secret`, `token`,
  `password`, `passphrase`, `api_key`, `authorization`, `credential`, `cookie` and
  `private_key`, case-insensitively;
- a `tool.call` input whose JSON exceeds 4096 characters (`INPUT_MAX`) is cut to
  `{ truncated, preview }`;
- `tool.result` summaries are capped at 500 characters (`SUMMARY_MAX`).

Full payloads stay only in the SDK transcript (`ai_session_entries`), which is never
sent to watchers.

## Known limitations

From the merged code and PR bodies:

1. **The credential gate is not authentication and not an approval**
   ([`agent/src/routes/guard.ts`](../../agent/src/routes/guard.ts), "LIMITATION").
   Anything that can open a TCP connection to port 8081 can set `Host` and `Origin`.
   What a non-trusted peer cannot do is claim HTTPS. The gate therefore does not stop a
   hostile process on the trusted proxy or on the pod's loopback. The human approval
   comes with #258.
2. **`GET /api/v1/ai/credentials` is unguarded.** It returns only the non-secret
   summary (`view()`). It and the `PUT` route await `ready()` without a route-level
   timeout; that wait is bounded by `lock_timeout` (10 s) (PR #379, "Also checked").
3. **The gateway SSRF check is point-in-time.** Claude Code re-resolves at connect
   (`egress.ts`; PR #379, row 6).
4. **Secrets as JS strings** stay in the heap until garbage-collected (`secrets.ts`,
   "PLAINTEXT IN MEMORY").
5. **Scrubbing is name-based.** A tool that takes a secret under a name
   `SENSITIVE_KEY` does not match would log it. The registry must let such tools
   declare it before #251/#258 wire in real outward tools (spec §6; seam comment in
   [`agent/src/sessions/manager.ts`](../../agent/src/sessions/manager.ts)).
6. **Session store items still to verify** (PR #377, "To verify"):
   - `SessionStore` is `@alpha` in SDK 0.3.283;
   - concurrent `append` under two project keys is not measured;
   - a fork during a running turn is allowed but not tested;
   - `mirror_error` is not surfaced;
   - `waiting_input`, `waiting_approval` and `done` are never set yet.
7. **Plugins are vetted, but no production turn runs yet.** `main.ts` gives the
   `SessionManager` the enabled remote plugins and plugin packages for each turn, but
   nothing starts a session over HTTP yet (the comment on `sessions` in `main.ts`).
8. **Rotation leaves unopenable rows** as they are, and counts them in the log
   (`rewrapFrom()`).

## Spec §3.2 items still open

On `main`, spec §3.2 still lists every item below as "to verify". None is used by
merged code.

| Item (abridged) | Owner |
|---|---|
| Whether `canUseTool` can wait on an asynchronous human decision, or a `PreToolUse` hook must deny and the session resume after approval | #255, #258 |
| Bambuddy 1.2.5.5 routes for print archive outcomes and stats | #251 |
| Whether the #241 Postgres needs anything for `LISTEN/NOTIFY` across replicas | #264 |
| Bambu Studio's "Open in Bambu Studio" hand-off | #284 |
| Keys accepted by `filament_overrides` on `PrintQueueItemCreate` | #284 |
| Whether `/local-presets/` can create a derived process preset | #284 |
| Whether a BambuStudio-claimed 3MF's `project_settings.config` overrides the pipeline preset | #284 |
| Which process preset each level of detail maps to | #284 |
| Whether `network.allowedOrigins` in `@playwright/mcp` blocks page-JS requests | #349 |
| How off-allowlist navigation and redirects fail | #349 |
| Whether `extraHTTPHeaders` reaches every request and survives page JS | #349 |
| Whether `--isolated` gives one context per session | #349 |
| Tool names of a plugin's MCP server, and whether deny rules remove the four dangerous Playwright tools | #349 |
| Whether `outputDir` confines every write | #349 |
| How the backend matches the agent-actor marker to an approved outward action | #349 |
| Chromium on the agent image: install, non-root, read-only root, size | #349 |

The headless browser (spec §5.3, merged as a spec in #363) stays off until its rows are
verified (spec §8.2).
