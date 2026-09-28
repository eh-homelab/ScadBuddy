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
- **`/mcp` and the tool registry** are on `main` (#368, [`agent/src/mcp/http.ts`](../../agent/src/mcp/http.ts),
  [`agent/src/tools/`](../../agent/src/tools/)), authenticated by bearer tokens (see
  [MCP bearer tokens](#mcp-bearer-tokens)). **Approvals are not built** (open PR #471).
  The other tool paths are the harness's in-process MCP servers and the browser bridge
  in the user's own tab ([browser-bridge.md](browser-bridge.md)).

## MCP bearer tokens

Spec §8.1 ("minted in Settings, stored hashed") and §9 ("MCP auth mode, tokens
(hashed)" live in the database). The implementation is `PostgresTokenStore` in
[`agent/src/auth/tokens.ts`](../../agent/src/auth/tokens.ts), over `ai_mcp_tokens`
([`agent/src/db/migrations/20260928T0734Z_mcp_tokens.sql`](../../agent/src/db/migrations/20260928T0734Z_mcp_tokens.sql)).

- **Format.** `sbmcp_` plus 32 bytes from `crypto.randomBytes`, base64url. The prefix
  makes a leaked token recognisable to secret scanners.
- **Hash only.** The row holds `token_hash`, the SHA-256 (hex) of the token, and never
  the plaintext; a `CHECK` rejects anything that is not 64 hex characters. An unsalted
  fast hash is enough because the token is 256 random bits: there is nothing to
  brute-force that a slow KDF would protect (comment on `hashToken()`). A read of the
  table therefore yields no usable token. The plaintext is returned once, by `mint`.
- **Verify** is a single `UPDATE … RETURNING` that matches the hash, skips revoked and
  expired rows, and stamps `last_used_at` (only forwards, with `GREATEST`). Revoking on
  one replica takes effect on every replica at the next request.
- **No other store.** There is no file or in-memory persistence. Without a database,
  `/mcp` answers 503 before any token is looked at (`app.ts`), and `main.ts` wires
  `FailClosedTokenStore`, which verifies nothing. The same store is the fallback when
  the auth settings cannot be read (`resolveAuth()` in `mcp/http.ts`).
- **Minting, listing and revoking** are Settings → "MCP access tokens" (shown only
  where `useAiAvailability()` says AI is available, so not in a production build yet)
  ([`frontend/src/components/McpTokensSection.tsx`](../../frontend/src/components/McpTokensSection.tsx))
  over `/api/v1/ai/mcp-tokens`
  ([`agent/src/routes/mcpTokens.ts`](../../agent/src/routes/mcpTokens.ts);
  routes in [operating.md §4.1](operating.md#41-mcp-access-tokens)):
  - `GET` returns metadata only (name, tier, created, expires, last used, revoked and a
    derived `status`), never the token or its hash. The store keeps no last-4 hint, so
    none is shown. It passes `uiReadProblem()` (HTTPS, and an `Origin` or `Host` on the
    allowlist; a cross-site `Sec-Fetch-Site` is refused).
  - `POST` returns the plaintext once, in the `201` body, with `Cache-Control:
    no-store`. The route never logs it. `DELETE /:id` sets `revoked_at`; a revoked
    token stays listed and never verifies again.
  - Both writes pass `uiRequestProblem()`, as credential writes do: minting a
    token is an outward write (spec §8.1). `POST` also needs `Content-Type:
    application/json` (`415` otherwise). The same limitation applies: this is a gate, not
    authentication (spec §8.3, "Stated plainly").
  - In the browser, the plaintext is held only in the section's React state until
    **Done**. It is rendered as text in a `<code>` element, not as a field value, so the
    browser agent's snapshot ([`frontend/src/agent/snapshot.ts`](../../frontend/src/agent/snapshot.ts)),
    which reads field values and `role=status`/`alert` text, never sees it. Create,
    Copy and Revoke are `USER_ONLY`, so the browser agent cannot press them.
  - Every auth mode allows managing tokens. In `disabled` mode `/mcp` does not check
    them; they are kept for when the mode returns to `bearer`, and Settings shows a
    warning. In `oidc` mode (#262) bearer tokens keep working alongside the IdP
    (spec §8.3). `GET` reports `auth_mode` for this.

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
(PR #379, row 8). Fetching, pinning and reviewing user plugins (spec §10) is **not
built** (open PR #464).

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

## Audit log (#258)

Spec §8.3 ("the audit log records the client IP"), §8.6 ("audit log"; credentials
"redacted in logs and audit") and §9 ("the audit log" is AI state in Postgres). The MCP
spec asks the same of a client: "Log tool usage for audit purposes"
([MCP tools, Security Considerations](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)).
The code is [`agent/src/audit/`](../../agent/src/audit/), over `ai_audit`
([`agent/src/db/migrations/20260928T0950Z_audit.sql`](../../agent/src/db/migrations/20260928T0950Z_audit.sql)).

| Kind | Recorded by | When |
|---|---|---|
| `tool_call` | `TurnAuditor` (`audit/turn.ts`), fed by `sessions/manager.ts` | every tool call a session turn makes: ScadBuddy's in-process tools and remote plugin tools, including calls refused or denied at the approval gate |
| `tool_call` | `createExternalServer()` (`tools/projections.ts`) | every call over `/mcp`, with the client address |
| `resource` | `installResources()` (`resources/server.ts`) | every `/mcp` `resources/read`, `subscribe` and `unsubscribe` (#264), with the URI; a tier refusal is `refused` |
| `approval` | `ApprovalService` (`approvals/service.ts`) | approved, denied, expired, cancelled, and approved-but-voided |
| `credential`, `plugin` | `auditWrites()` (`audit/writes.ts`, mounted in `app.ts`) | `PUT`/`DELETE /api/v1/ai/credentials`, `POST`/`PATCH`/`DELETE /api/v1/ai/plugins…`, refused attempts included; bodies are never read |
| `settings` | `SettingsStore.set()` (`credentials.ts`) | every `ai_settings` write, with the key and value (the table holds no secrets by contract) |
| `token` | `auditedTokenStore()` (`audit/writes.ts`), around the one store `main.ts` gives both the Settings token routes (#517) and `/mcp` | MCP token mint and revoke, with the token's id and name; never the token. A refused or failed `POST`/`DELETE /api/v1/ai/mcp-tokens…` is recorded by `auditWrites()` (failures only, so a mint is one row) |

Each row has who (principal kind, id and label; session and turn), the tool and tier,
the input as a **keyed HMAC** (the approvals' own key, so a row matches its approval's
`input_hash`) and a **scrubbed summary** (`summariseInput()`: `scrubForLog()`, capped),
the approval id, the outcome (`ok`, `error`, `refused`, `denied`) and timings. A turn's
credential and plugin secrets are redacted from the summary and the detail.

**Who ran it and who approved it, on one row.** A `tool_call` row with an approval id
also has `approved_by_*`: the principal that approved it, copied from
`ai_approvals.decided_by_*` in the same `INSERT` when the approval's decision is
`approved` (`AuditLog.record()`). Copied, not joined at read time, because an approval
row goes with its session (`ON DELETE CASCADE`) and the audit row must outlive it. A
denied, expired or cancelled call has none; the approval's own `approval` row names
its decider as the principal.

- **Append-only.** Triggers refuse `UPDATE`, `TRUNCATE` and any `DELETE` except the
  retention sweep's, which sets `scadbuddy.audit_prune` for its own transaction only
  (`set_config(…, true)`). This stops the service's own code, not a database superuser.
- **Retention.** `audit_retention_days` in `ai_settings` (default 90, 1–3650), pruned
  hourly by `main.ts`. Changing it is itself a `settings` row.
- **Reading it.** `GET /api/v1/ai/audit` (`routes/audit.ts`) behind `uiReadProblem`,
  newest first, filtered by kind, outcome, surface, action, session, principal and time,
  paged by an id cursor (`next` → `before`). `PUT /api/v1/ai/audit/settings` behind
  `uiRequestProblem`. The UI is Settings → **AI activity**
  ([`frontend/src/components/assistant/AiAuditSection.tsx`](../../frontend/src/components/assistant/AiAuditSection.tsx)).
- **Recording never blocks the action.** A failed insert goes to the log
  (`onError`); refusing to act would let a database blip stop every session, and the
  table lives in the same database as what the actions touch.

## Prompt-injection hardening (#258)

Spec §8.6: "Prompt injection via model READMEs, upstream sources, library code, plugin
output, Bambuddy data" is mitigated by "Tool results wrap such content as untrusted;
outward actions always need a human approval; `tools: []`". Anthropic's guidance on
indirect prompt injection
([Mitigate jailbreaks and prompt injections](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/mitigate-jailbreaks))
decides the details, and Anthropic's own research write-up says "prompt injection is far
from a solved problem"
([Mitigating the risk of prompt injections in browser use](https://www.anthropic.com/research/prompt-injection-defenses)),
so the marking is defence in depth and the approval gate is the boundary.

- **Untrusted-data envelope** ([`agent/src/safety/untrusted.ts`](../../agent/src/safety/untrusted.ts)).
  `runToolWithOutcome()` re-encodes every text block a handler returns as
  `{"untrusted_data": {"tool", "source", "content"}}`. The guidance: "Put untrusted
  content only in tool results", "Tell Claude what the content is and where it came
  from", and "JSON-encode untrusted content ... so an attacker cannot close a quote or
  tag to 'break out' into an instruction context". Each tool may declare its `source`
  (the README, OpenSCAD source and comments, render logs, upstream libraries, Google
  Fonts, Bambuddy data); the rest get a default. Following "Don't put your own
  instructions in tool results", the envelope carries no instruction. ScadBuddy's own
  messages (tier refusals, the pending-approval notice, a `ToolError` summary) are not
  wrapped. The panel's `tool.result` summary shows the content, unwrapped.
- **Images, audio and blobs** (`markUntrustedContent()`). Bytes cannot be wrapped, and
  an image can carry text as well as a README can; OWASP lists "multimodal" injection,
  instructions hidden in images, among its scenarios
  ([LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)).
  So each image, audio or embedded-blob block is preceded by a text preamble,
  `{"untrusted_data": {"tool", "source", "content_follows": {"type", "mime_type"}}}`,
  which says only where the next block came from ("Tell Claude what the content is
  and where it came from"), again with no instruction. An embedded *text* resource has
  its text wrapped like any other text. A `resource_link` is a URI and is left alone.
- **Plugin tool results** (`plugins/forwarder.ts` `rewriteMessages()`). Claude Code puts
  a remote plugin's result straight into the model's context, and every plugin call
  already passes through the loopback forwarder, so the forwarder rewrites each
  `tools/call` response it relays (JSON or SSE) with `markUntrustedContent()`, under the
  name the model knows the tool by (`mcp__<plugin>__<tool>`) and a source naming the
  plugin as a third-party MCP server. A JSON-RPC error's message is wrapped too, since
  Claude Code hands it to the model as the tool's error. `tools/list` is not: it is the
  tool catalogue, which the registry already filters. Measured end to end in
  `agent/test/plugins.e2e.test.ts` (the bundled Claude Code sends the model the wrapped
  result) and `agent/test/plugins.untrusted.test.ts`.
- **MCP resources** (#264, `resources/server.ts`, `markUntrustedResourceContents()`).
  The resources serve the same READMEs, sources and Bambuddy data as the tools, so a
  `resources/read` answer is marked as well: text contents become the envelope (with
  the resource's own MIME type as `mime_type` inside it, and `application/json` as the
  item's `mimeType`, which is what the text now is); a blob (a thumbnail, a 3MF) is
  preceded by a preamble item. Every item also carries
  `_meta["scadbuddy/untrusted"]` (tool, source, original MIME type) for a client that
  reads metadata; `_meta` is the field MCP reserves "to allow clients and servers to
  attach additional metadata to their interactions"
  ([MCP basic protocol, `_meta`](https://modelcontextprotocol.io/specification/2025-06-18/basic)).
  `resources/list` still names each resource's underlying MIME type.
- **The boundary, stated where instructions belong.** `UNTRUSTED_CONTENT_POLICY` is
  appended to Claude Code's system prompt on every session turn
  (`sessions/manager.ts`, `systemPromptAppend`), per "State the policy in your system
  prompt", modelled on the page's `<untrusted_content_policy>` example. `/mcp`'s server
  `instructions` carry the same statement for an external client's model.
- **Content can never approve an outward call.** Tiers are decided from the tool
  *name* only (`decide()` in `harness/permissions.ts`; an unknown tool is `outward`),
  and an outward call is approved only by a decision in the ScadBuddy UI
  (`ApprovalService.decide()`, behind the origin gate); no tool argument or result is
  read by either. Over `/mcp` an outward call only prepares a pending action.
- **Replayed in tests.** `agent/test/injection.e2e.test.ts` runs the real SDK and
  bundled Claude Code against the fake Anthropic endpoint, with the real registry over
  an msw backend. A model README (the injection of #521's `readme-prompt-injection`
  eval scenario) orders a `delete_model`, and an OpenSCAD comment orders a
  `print_output`, and the fake model obeys each. The tests assert that the content
  reached the model inside the envelope, the system prompt carried the policy, the
  outward call parked for approval, the backend never received it (while parked, after
  a denial, after an interrupt), and the audit log shows it `denied` / `refused` with
  its approval id. `agent/test/untrusted.test.ts` covers the envelope (a README that
  tries to close it stays inside the JSON string) and that tier decisions ignore
  content.
- **Not covered.** Tool results are not screened by a classifier (the guidance's
  "Screen tool outputs" step). The preamble marks an image's provenance; it cannot
  stop a model from reading text inside the image, which is why the approval gate,
  not the marking, is the boundary.

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
7. **Plugins are vetted, never loaded in production.** No plugin path is passed in
   `main.ts` today.
8. **Rotation leaves unopenable rows** as they are, and counts them in the log
   (`rewrapFrom()`).

## Spec §3.2 items still open

On `main`, spec §3.2 still lists every item below as "to verify". None is used by
merged code.

| Item (abridged) | Owner |
|---|---|
| Whether `canUseTool` can wait on an asynchronous human decision, or a `PreToolUse` hook must deny and the session resume after approval | #255, #258 |
| Bambuddy 1.2.5.5 routes for print archive outcomes and stats | #251 |
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

The `LISTEN/NOTIFY` across replicas item is answered by #264 (spec §3.2): connect to
the primary, since a hot standby refuses `LISTEN` and `NOTIFY`
([PostgreSQL: Hot Standby](https://www.postgresql.org/docs/current/hot-standby.html));
see [mcp-resources.md](mcp-resources.md#the-event-source).

The headless browser (spec §5.3, merged as a spec in #363) stays off until its rows are
verified (spec §8.2).
