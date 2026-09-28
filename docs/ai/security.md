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
- **`/mcp`, the tool registry and approvals are on `main`** (#251's registry in
  [`agent/src/tools/`](../../agent/src/tools/) and
  [`agent/src/mcp/http.ts`](../../agent/src/mcp/http.ts); #258's approval store in
  [`agent/src/approvals/service.ts`](../../agent/src/approvals/service.ts)). `/mcp` is
  authenticated by bearer tokens (see [MCP bearer tokens](#mcp-bearer-tokens)) and, when
  enabled, OIDC access tokens (see [MCP OIDC](#mcp-oidc-access-tokens)). The
  other tool paths are the harness's in-process MCP servers (none registered in
  `main.ts`) and the browser bridge in the user's own tab
  ([browser-bridge.md](browser-bridge.md)).

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

## MCP OIDC access tokens

Issue #262, spec §8.3 (`oidc`). `/mcp` is an OAuth 2.1 *resource server*; the IdP
(Authentik, Keycloak, Pocket ID, …) is the authorization server. The code is
[`agent/src/auth/oidc.ts`](../../agent/src/auth/oidc.ts) (verifier, discovery, caches),
`authenticateOidc()` in [`agent/src/auth/authenticate.ts`](../../agent/src/auth/authenticate.ts),
and the metadata route in [`agent/src/mcp/http.ts`](../../agent/src/mcp/http.ts).
Specifications, as cited in the `oidc.ts` header:

- MCP authorization, revision 2025-11-25, the one the pinned `@modelcontextprotocol/sdk`
  1.30.x implements (`LATEST_PROTOCOL_VERSION`):
  <https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization>
- RFC 9728, Protected Resource Metadata: <https://www.rfc-editor.org/rfc/rfc9728>
- RFC 8707, Resource Indicators: <https://www.rfc-editor.org/rfc/rfc8707>
- RFC 6750, Bearer Token Usage: <https://www.rfc-editor.org/rfc/rfc6750>
- RFC 9068, JWT access tokens: <https://www.rfc-editor.org/rfc/rfc9068>

What is checked, in order:

- **The mode.** OIDC runs only while `ai_settings.mcp_oidc.enabled` is true (`main.ts`
  `authSettings`). `sbmcp_…` tokens still go to `PostgresTokenStore`; everything else
  must be a JWT.
- **`alg`** must be on the configured allowlist (default `RS256`, `ES256`). This is
  checked from the header *before* any key or metadata is fetched, so `none`, `HS256`
  and friends cost nothing. `OidcConfigSchema` cannot hold `none` or an HMAC algorithm
  at all, which also closes RSA-public-key-as-HMAC-secret confusion (tested).
- **`typ`**, when present, must be `at+jwt`, `application/at+jwt` or `JWT`. An ID token
  typed as such is refused.
- **Not an ID token** (`idTokenMarker()`): a token carrying `nonce` or `at_hash`, or
  Keycloak's payload `typ: "ID"`, is refused. When **Audience** is overridden (typically
  to the client id, which an ID token also names), the token must also be an RFC 9068
  access token: header `typ` `at+jwt`, or a `client_id` claim.
- **Signature** by a key from the issuer's JWKS (`jose` `jwtVerify` with a local JWK set).
  Private (`d`) and symmetric (`oct`) keys and keys with `use` other than `sig` are
  dropped from the fetched set.
- **Claims:** `iss` equal to the configured issuer, byte for byte; `aud` containing the
  resource URI `<origin of SCADBUDDY_PUBLIC_URL>/mcp` (RFC 8707), or the audience the
  operator set; `exp` required; `nbf`/`iat` checked; 30 s clock leeway; `sub` required
  (at most 255 characters).
- **Scopes → tier:** `scope`, `scp`, and the optional `tier_claim` are matched against
  the scope map. No match is `403 insufficient_scope`. The highest tier found includes
  the ones below it.

**Resource URI from configuration, never from the request.** The audience demanded is
built from `SCADBUDDY_PUBLIC_URL`. Taking it from `Host` would let a client present a
token minted for any other resource by pointing `Host` at that resource's name. Without
a public URL, JWTs are refused and Settings refuses to enable OIDC.

**No token passthrough** (MCP authorization spec, "Access Token Privilege Restriction"):
the principal handed to tools carries the subject and tiers, not the token
(`authInfoFor()` sets `token: ''`), and the backend is called without it.

**Fetching the IdP** (`OidcProvider`, `egressGetJson()` in
[`agent/src/http/egress.ts`](../../agent/src/http/egress.ts)):

- https only; plain http only to `localhost`/`127.0.0.0/8`/`::1` (development and the
  tests' fake IdP). No credentials in the URL.
- The same address rules as the gateway check (link-local, cloud metadata hosts and
  names refused), but **enforced on the connection**: the socket's `lookup` returns only
  the addresses that were checked, so DNS rebinding between check and connect cannot
  reach a refused address.
- Redirects are not followed; bodies over 512 KiB and requests over 5 s fail.
- **Caching:** metadata and JWKS for 10 minutes. An unknown `kid` triggers one JWKS
  refetch, at most every 30 s, so a flood of forged tokens cannot make the agent hammer
  the IdP. A failed fetch is remembered for 30 s; a failed refresh keeps serving the last
  good metadata and keys, but only until 1 hour after they were fetched (a failure never
  re-stamps them as fresh), so a key the IdP rotated out stops verifying even while the
  IdP stays unreachable. An unreachable IdP answers `503` (with `Retry-After`), not `401`, so clients do
  not discard good tokens.

**Enabling** (`PUT /api/v1/ai/mcp/oidc`, [`agent/src/routes/mcpAuth.ts`](../../agent/src/routes/mcpAuth.ts)):
`enabled: true` is saved only after a fresh discovery (metadata whose `issuer` matches
exactly, and a JWKS with at least one public signing key). The write passes the same
interim UI gate as credential writes (`uiRequestProblem()`), which is **not an approval**
(#258) and not authentication; see [Known limitations](#known-limitations).

**Audit.** OIDC principals are `oidc:<issuer>#<sub>` (a `sub` is unique only per
issuer, so a new issuer's colliding `sub` does not inherit anything) with `subject` (and `clientId` from `azp` or
`client_id`) on the principal. An approval records it as `requested_by` kind `oidc`, labelled
`MCP OIDC <sub> via <client>` (`ownerOf()` in
[`agent/src/approvals/mcp.ts`](../../agent/src/approvals/mcp.ts), an exhaustive switch).

**Tests.** [`agent/test/oidc.test.ts`](../../agent/test/oidc.test.ts) (bad issuer,
audience, expiry, `nbf`, missing claims, algorithm allowlist, `none`, HS256 confusion,
forged signature, key rotation and refetch limits, discovery refusals, the egress
fetcher), [`agent/test/mcpOidc.test.ts`](../../agent/test/mcpOidc.test.ts) (the 401/403
headers, metadata, bearer tokens alongside, Settings routes),
[`agent/test/oidc.e2e.test.ts`](../../agent/test/oidc.e2e.test.ts) (the MCP SDK client's
own OAuth flow: 401 → metadata → registration → PKCE login with `resource` → call), and
[`agent/test/oidc.pg.test.ts`](../../agent/test/oidc.pg.test.ts) (the configuration in
Postgres). The IdP is [`agent/test/support/fakeIdp.ts`](../../agent/test/support/fakeIdp.ts),
keys generated per run.

## Risk tiers and the permission seam

Tiers (spec §8.1): `read`, `write` (reversible through history) and `outward` (send,
print, delete, settings or credential writes). A tool ScadBuddy does not recognise is
`outward`.

The implementation is in [`agent/src/harness/permissions.ts`](../../agent/src/harness/permissions.ts):

- `decide(toolName, tierOf, input, guard)`: an optional `InputGuard` may deny a call
  by its arguments first, at any tier (the headless browser's origin and file-name
  checks, [headless-browser.md](headless-browser.md)). Then an unknown tool (`tierOf`
  returns `undefined`) is treated as `outward`. `read` and `write` are allowed.
  `outward` gets `needs_approval`.
- **Approvals** (#258): in a session, `needs_approval` parks the call until a human
  decides it in the UI (`approvalGate`, [`agent/src/approvals/service.ts`](../../agent/src/approvals/service.ts));
  with no gate (outside a session) it is answered with a **deny**, whose message tells
  the model to explain rather than retry. An outward tool therefore never runs
  unattended. External MCP clients use prepare/confirm instead (below).
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
- `strictMcpConfig: true` and `mcpServers: {}`. A query with the headless browser
  sets `strictMcpConfig: false`, because with it Claude Code 2.1.283 starts no plugin
  MCP server at all (measured, #349); `settingSources: []` still keeps settings-file
  MCP configs out, which the e2e test checks with a planted `.mcp.json`.
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

## MCP prepare/confirm on the approval store

Spec §8.2 gives external MCP clients "a two-step `prepare` (returns a pending action
id and a human-readable summary) then `confirm`, where the confirm completes only after
the UI approval". As built:

- **Prepare.** `runTool` ([`agent/src/tools/registry.ts`](../../agent/src/tools/registry.ts))
  does not run a gated outward tool. `ApprovalActions.prepare`
  ([`agent/src/approvals/mcp.ts`](../../agent/src/approvals/mcp.ts)) records a pending
  row in `ai_approvals` through `ApprovalService.create()`, with no session and no turn.
  The row holds the MCP principal (`requested_by`), the tool, the HMAC-SHA256 input hash
  under the KEK-derived key (`approvalHashKey`) and the scrubbed input summary. It never
  holds the full input. The row id is the `pending_action_id`.
- **Bounds.** A caller keeps at most 50 pending actions; a 51st cancels that caller's
  oldest. At 10,000 pending sessionless rows, a new prepare is refused and nobody's row
  is evicted. These are the in-memory store's bounds from #368, applied to the table.
  `ApprovalService.createPrepared` checks and applies both bounds in the insert's own
  transaction, under one `pg_advisory_xact_lock`, so concurrent prepares cannot
  overshoot them (covered by a concurrent-burst test in
  `agent/test/mcpConfirm.pg.test.ts`).
- **Anonymous callers.** In `disabled` mode the principal id is
  `anonymous:<Mcp-Session-Id>`, and the session id is that client's capability
  (`mcp/http.ts`). `requested_by` therefore stores `anonymous:` plus the first 128 bits
  of a SHA-256 of that id (`ownerOf`). The approval routes and the table never show the
  session id itself.
- **Decide.** The UI approves or denies it with `POST /api/v1/ai/approvals/:id/approve`
  or `/deny` ([`agent/src/routes/approvals.ts`](../../agent/src/routes/approvals.ts)), as
  the browser user. `authorize` refuses a principal deciding its own request even with
  an approval grant, so an MCP client cannot approve what it prepared (covered in
  `agent/test/mcpConfirm.pg.test.ts`).
- **Confirm.** `confirm_action` ([`agent/src/tools/approvals.ts`](../../agent/src/tools/approvals.ts))
  takes the `pending_action_id` and the same `arguments` again, because the table has
  only the hash. It answers `pending_approval` while the row is undecided. It runs the
  tool only when `ApprovalService.consumePrepared` marks the row used. That is one
  `UPDATE` requiring: no session, the same `requested_by`, the same input hash,
  `decision = 'approved'`, not consumed, not revoked, and `usable_until > now()`. Two
  confirms cannot both win. A replay, another principal, a changed input, a denial or an
  expiry is refused and nothing is sent. The approval is used up before the tool runs,
  so a call that then fails is not retried on the same approval.
- **No database.** `main.ts` falls back to the in-memory `PendingActionStore`
  ([`agent/src/tools/pending.ts`](../../agent/src/tools/pending.ts)). Nothing can approve
  its actions, so its `confirm_action` always refuses.

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

**The one exception is the headless browser** (#349). Its plugin is not read from
anyone's directory: `materializeHeadlessBrowser()` in
[`agent/src/harness/headlessBrowser.ts`](../../agent/src/harness/headlessBrowser.ts)
writes it per session from the vendored manifest, with a single stdio server that
starts under `/usr/bin/env -i` and gets `HOME`, `TMPDIR` and `PLAYWRIGHT_BROWSERS_PATH`
only, so the credential never reaches it or Chromium (measured from
`/proc/<pid>/environ`). `assertHeadlessPlugin()` checks that file instead of
`assertPluginAllowed()`, which would refuse it.

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

- **Dynamic context injection** (`` !`cmd` `` or a ```` ```! ```` block, anywhere in a
  line, as the CLI matches it) in any Markdown file. These run a shell "before the
  skill content is sent to Claude" ([skills](https://code.claude.com/docs/en/skills)).
  Every query also sets `disableSkillShellExecution` (`harness/options.ts`); measured on
  CLI 2.1.283, the CLI then puts a placeholder in place of both forms instead of running
  them (`test/pluginPackages.e2e.test.ts`). Without the setting, the harness denied the
  resulting Bash call.
- **Frontmatter** `hooks`, `mcpServers` or `permissionMode`, so every hook and server is
  in the vetted files and in the review. So that no key can hide from this check,
  frontmatter must be plain YAML (`frontmatter()`): the block is cut where the CLI cuts
  it (at the first `---`, even mid-line), and a block that is unterminated or ends on a
  `---` that is not a line of its own is refused. It must be one block mapping of plain
  keys at column 0. Quoted, explicit (`?`) and merge (`<<`) keys, flow mappings,
  anchors, aliases, tags, directives, a second document, and invalid or duplicate-key
  YAML are all refused. Those are the forms where our parser and the CLI's could read
  different keys.
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
  credential env (above). Hook **events** are allowlisted (`PACKAGE_HOOK_EVENTS`):
  `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PostToolUse`,
  `PostToolUseFailure`, `Notification`, `Stop`, `StopFailure`, `SubagentStart`,
  `SubagentStop`, `PreCompact` and `PostCompact`. A `PermissionRequest` hook of any
  type is refused. In CLI 2.1.283 it races the host's `can_use_tool` answer, and its
  `behavior: "allow"` wins, so it would approve an outward tool before a human could
  (spec §8.2). `PreToolUse` is refused too (`permissionDecision`, `updatedInput`), and so
  is any event not on the list, including one a later CLI adds.
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

## Headless browser (#349)

Spec §8.6's row "Headless browser used to click past an approval, or to reach other
origins or files", as built. Details and measurements are in
[headless-browser.md](headless-browser.md).

- **Off by default**: the `ai_settings` key `headless_browser_enabled` must be `true`.
  It is set only through `PUT /api/v1/ai/settings/headless-browser`, behind
  `uiRequestProblem` like the other AI settings writes, from a user-only switch in
  Settings.
- **Tools the model cannot see**: `browser_run_code_unsafe`, `browser_evaluate`,
  `browser_file_upload`, `browser_drop`, `browser_install` are in `disallowedTools`.
  Every other tool has an explicit tier; a new one is `outward`, so denied.
- **Origins**: the harness denies any `url` not on the backend's origin before the
  server sees it (`browserInputProblem()`, using `normaliseOrigin()` from
  `origins.ts`), and the server's `network.allowedOrigins` blocks navigations,
  subresources and page `fetch` to other origins. That list does not cover redirects
  (measured), so a **redirect guard** loaded on every page routes each request itself
  with `maxRedirects: 0` and refuses any hop off the origin, including one added by a
  proxy in front of the backend (measured: an off-origin redirect and an on→off chain
  reach nothing).
- **Files**: `filename` arguments must be plain names; the server's own
  workspace-root restriction refuses `../` and outside absolute paths too. No
  `--allow-unrestricted-file-access`, no downloads (`acceptDownloads: false`).
- **Approvals cannot be bypassed through the UI**: every request carries
  `X-ScadBuddy-Agent-Session` (a page `fetch` cannot replace it, measured), and
  `AgentActorGate` in
  [`backend/scadbuddy/api/agent_actor.py`](../../backend/scadbuddy/api/agent_actor.py)
  answers `403` to every non-safe route that is not on its allowlist of read/write
  routes. Default deny: a new route is refused until listed. The one exception is a
  **grant**: the model asks for one exact method and path with
  `mcp__scadbuddy_browser__authorize_request`, an outward tool that parks for a human
  approval (#258); the approved call writes a one-shot row to `ai_headless_grants`,
  and the backend's `GRANT_SQL` uses it once, only while the turn that made it is the
  session's live turn and only if its approval is approved and consumed. Any database
  problem refuses. The marker is not authentication; forging it can only get a request
  refused (a request without it is as trusted as today), and a forged marker cannot use
  a grant without knowing the session id, the grant's exact path, and landing inside its
  turn and two-minute window, for a request a human already approved.
- **A grant binds method and path, not the body** (review of #518), so settings routes
  (`/api/v1/settings` and below) are never grantable: a granted settings write could
  point `bambuddy_url` at another host and send the API key there. Both the agent's
  authorize tool and the backend gate refuse them. For print and send, the printer,
  plate and options in the body are not shown to the approver.
- **The grant check cannot be flooded** (review of #518): the marker is free for anyone
  to send, so the lookup it triggers is bounded. It runs on its own async pool of 2
  Postgres connections (`PostgresGrants`), at most 4 checks at once, and one more is
  refused at once without touching the database; a pooled connection that is not free
  within 2 s refuses too. Markers that are not UUIDs, and paths outside `/api/v1/`,
  are refused before any of that. Measured: 200 concurrent bogus markers opened at most
  2 connections, and a real grant still worked right after
  (`backend/tests/api/test_agent_actor_grants.py`).
- **No open redirects from the backend** either, as defence in depth:
  `backend/tests/api/test_no_open_redirect.py`
  ([headless-browser.md](headless-browser.md#redirects-guarded-in-the-browser-and-none-from-the-backend)).
- **Isolation**: `--isolated`, no profile on disk, one server per query, so no state
  crosses sessions (or turns).
- **Chromium's sandbox is on where the pod allows it**: the agent probes it once and
  asks for it (`chromiumSandbox: true`) when it starts; otherwise it warns and runs with
  `--no-sandbox`, where a renderer exploit would land in the agent container. It needs
  a seccomp profile that allows user namespaces, not `RuntimeDefault`
  ([headless-browser.md](headless-browser.md#sandbox)).

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
   `SessionManager` the enabled remote plugins, plugin packages and the headless
   browser for each turn, but nothing starts a session over HTTP yet (the comment on
   `sessions` in `main.ts`).
8. **Rotation leaves unopenable rows** as they are, and counts them in the log
   (`rewrapFrom()`).
9. **OIDC access tokens are JWTs only, and live until `exp`** (#262). There is no
   token introspection (RFC 7662), so an IdP that issues opaque access tokens is not
   supported, and revoking a session at the IdP does not stop a token already issued;
   keep access-token lifetimes short there. Turning OIDC off in Settings stops every
   JWT at the next request.
10. **The OIDC settings write is gated, not approved.** `PUT /api/v1/ai/mcp/oidc` uses
    the credential routes' interim gate (item 1). Someone who can reach Settings can
    point `/mcp` at an IdP they control, which is the "Stated plainly" caveat of spec
    §8.3.
11. **Under a `RuntimeDefault` seccomp profile the headless browser's Chromium runs
    without its sandbox** (see above).

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

The `LISTEN/NOTIFY` across replicas item is answered by #264 (spec §3.2): connect to
the primary, since a hot standby refuses `LISTEN` and `NOTIFY`
([PostgreSQL: Hot Standby](https://www.postgresql.org/docs/current/hot-standby.html));
see [mcp-resources.md](mcp-resources.md#the-event-source).

#349 measured the headless-browser rows and moved them to spec §3.1 (see
[headless-browser.md](headless-browser.md), "Measured"), including how the backend
matches the agent-actor marker to an approved outward action (a one-shot grant in the
shared database).
