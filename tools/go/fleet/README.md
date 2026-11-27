# fleet

ScadBuddy's agent fleet chat server: `fleetchat`'s MCP server (`tools/go/fleetchat`)
over ScadBuddy's own store and bus. The design is
`docs/superpowers/specs/2026-11-25-agent-fleet-messaging-design.md` §5 and §6.5-6.6.
It also holds the "ScadBuddy Fleet" GitHub App's parts (#8012, #8014; spec
`docs/superpowers/specs/2026-11-26-fleet-session-registry-and-rotator-design.md` §6.9):
the Actions secret writer (`ghsecrets`) and the webhook receiver (`ghwebhook`).

The image is `ghcr.io/eh-homelab/scadbuddy-fleet`, published by
`.github/workflows/build-go-image.yml`. It runs as uid 10003 and listens on 8080.

## Telemetry

The server exports traces, metrics and logs over OTLP/HTTP with the shared
`tools/go/telemetry` (service name `scadbuddy-fleet`), set by the standard `OTEL_*`
variables; with no endpoint every signal is off. Each request it serves is a SERVER span
(not `/healthz`), each request to core (the store, the bus stream and the credential
exchange) a CLIENT span whose traceparent core continues, and the Go runtime and process
are measured. Its JSON log lines stay on stderr; with logs on, each also goes over OTLP
without its path, error or credential fields. The `log` package's lines (net/http's) stay
on stderr only.
A setting telemetry cannot use (such as a malformed endpoint URL) is logged as
"telemetry is off", and the server runs with every signal off.

## Package `sbfleet`

`sbfleet` imports `fleetchat` and nothing from `tools/go/core`: it reaches core only
over HTTP (`TestNoCoreImports` checks it; the binary also imports `tools/go/telemetry`).
`WrapTransport` wraps the transport of its requests to core (the binary passes
telemetry's `Transport`); `(*Exchange).WrapTransport` does the same for the exchange.

- `NewStore(coreURL, token)`: a `fleetchat.Store` over core's routes under
  `/api/v1/internal/crud/fleet/` (caller `fleet`; `tools/go/core/internal/fleet`):

  | Method | Route |
  |---|---|
  | `Touch` | `POST /agents/{session_id}/touch` |
  | `Agents` | `GET /agents` (`{"agents"}`) |
  | `Join` / `Leave` | `PUT` / `DELETE /channels/{name}/members/{session_id}` |
  | `Channels` | `GET /agents/{session_id}/channels` (`{"channels"}`) |
  | `Send` | `POST /messages` `{"from", "channel" or "agent", "text"}` (`{"seq", "recipients"}`) |
  | `Inbox` | `GET /agents/{session_id}/inbox?limit=&peek=` (`{"messages", "more"}`; a page holds at most 4 MiB of text, and at least one message) |
  | `Rewind` | `PUT /agents/{session_id}/cursor` `{"seq"}` |

  A problem's `reason` maps to an error that `errors.Is` finds: `unknown-agent` (404)
  to `fleetchat.ErrUnknownAgent`, `unknown-channel` (404, a post to or a leave of a
  channel nobody has joined) to `fleetchat.ErrUnknownChannel`, `not-member` (403) to
  `fleetchat.ErrNotMember`, and
  `handle-taken` (409, two session ids that share their first 8 hex characters) to
  `sbfleet.ErrHandleTaken`, `seq-ahead` (422, a rewind past the cursor) to
  `fleetchat.ErrBadSeq`, and `text-too-long` (422, a text over core's 16 MiB, detail
  `text is longer than 16777216 bytes`) to `fleetchat.ErrTooLong`. `virtual-sender` (422,
  a DM to a virtual sender's name such as `rotator`) is `fleetchat.ErrVirtualSender`, as a
  `fleetchat.VirtualSenderError` whose text is core's detail, the sender's reject message
  alone, so `fleet_send` shows that text. Any other answer is an error that names the status. A 403
  with no `not-member` reason (the CRUD guard's refusal) is not `ErrNotMember`. Every
  request has a 30 s timeout.
- `NewBus(coreURL, token)`: a `fleetchat.Bus`. `Publish` does nothing: core publishes
  `fleet.message` itself when a send commits. `Subscribe` follows the change stream
  `GET /api/v1/internal/crud/fleet/changes` (served by the Python API,
  `backend/scadbuddy/api/crud_fleet.py`; core forwards it), a server-sent event
  stream whose frames are `backend/scadbuddy/agent_changes/hub.py`'s:
  - `change`, data `{"c", "p"}`, where `p` is the stored event as a JSON string. A
    `fleet.message` event's `recipients` (session ids) become
    `Event{SessionIDs: recipients}`; other kinds are skipped.
  - `ready`, data `{"resync", "reconnect"}`. It becomes `Event{Resync: true}` when
    `resync` is true (the server could not place `Last-Event-ID`: pruned, unknown, or
    too far behind) or `reconnect` is false (a stream sent no `Last-Event-ID`, the first
    one or any before a frame was seen, starts at the newest event, so what came before
    was never seen). With `reconnect` true and no `resync`, the replay before it lost
    nothing (every `fleet.*` event is logged), so it is no event.
  - `reconnected` (the server's own reader fell behind and replayed): `Event{Resync: true}`.
  - `: keepalive` comments, every 15 s.
  - A `ready` or `change` frame that cannot be read is logged and becomes
    `Event{Resync: true}`.

  Each stream logs a line once core answers 200, and each frame that becomes a resync
  logs one too.

  Every frame's `id` is kept and sent as `Last-Event-ID` on each reconnect. A stream
  that fails, is refused (a 401 or 403 too: the credential rotates), ends, or sends
  nothing for 45 s is followed again after a back-off of 500 ms doubling to 30 s,
  reset after a stream that delivered a frame. `Subscribe` returns only when its
  context ends.

The token function is called for every request. A read error fails that request (and,
for the stream, that attempt). Both take `OnUnauthorized(f)`: a request core refuses
with 401 calls `f` with the refused credential and is sent once more with the next one
(core's CRUD guard refuses before a route runs, so no route runs twice).

- `NewExchange(coreURL, proofFile, proof)`: the `fleet` service credential from the
  service-token exchange (root `README.md`, "The service-token exchange"). `Token`
  reads the proof file again and posts it to `POST /api/v1/auth/service-token`
  (`client_id` `fleet`) when it holds no credential, at half-life (7.5 of the 15
  minutes), and after `Invalidate`, which drops a credential core refused; it exchanges
  again at once, but never under 10 s after the last exchange. A failed exchange is
  tried again after 5 s, doubling to 60 s, and a 429 after its `Retry-After`; meanwhile
  the credential held serves until it expires. The proof is `ProofKubernetes` (a
  projected ServiceAccount token, ServiceAccount `scadbuddy-fleet`, audience
  `scadbuddy`) or `ProofBootstrap` (a secret whose hash the API holds as the file
  `fleet` in `SCADBUDDY_SERVICE_BOOTSTRAP_DIR`). It mirrors the agent's
  `ExchangeCredential` (`agent/src/api/serviceCredential.ts`), and also resends a request
  once after a 401 with a renewed credential. A refused connection (the API not up yet)
  sets no back-off. It never logs the proof
  or the credential.

## Package `ghsecrets`

`ghsecrets` writes GitHub Actions secrets as a GitHub App. It imports nothing else from
this repository, so fleetd can use it later. No route calls it yet: the routes that set a
secret come with #8011's rotation targets.

- `New(Config{AppID, InstallationID, PrivateKey, …})` and `ParsePrivateKey` (PEM,
  PKCS #1 as GitHub gives it, or PKCS #8).
- `Target{Owner, Repo, Name, Visibility, SelectedRepositoryIDs}`: a repository secret
  when `Repo` is set, else an organisation secret. `Visibility` (`all`, `private`, the
  default, or `selected` with `SelectedRepositoryIDs`) is for an organisation secret only.
  A name must match GitHub's rule (letters, digits and `_`, not a digit first, not
  `GITHUB_` first).
- `Set(ctx, target, value)`:
  1. It signs the App's JWT: RS256 over `iat` (now minus 60 s), `exp` (now plus 9
     minutes; GitHub refuses more than 10) and `iss` (the App id)
     ([GitHub's JWT page](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)).
  2. It mints an installation token for this one write:
     `POST /app/installations/{id}/access_tokens` with `permissions`
     `{"secrets":"write"}` and `repositories` `[repo]` for a repository target, or
     `{"organization_secrets":"write"}` for an organisation target
     ([create an installation access token](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app)).
     An organisation permission gives no repository's contents, so that token names no
     repository. A token whose answer holds a permission that was not asked for (other
     than `metadata`, which GitHub can add) is refused and not used.
  3. `GET …/actions/secrets/public-key`, a sealed box with that key
     (`golang.org/x/crypto/nacl/box` `SealAnonymous`), then
     `PUT …/actions/secrets/{name}` with `encrypted_value` and `key_id`, and for an
     organisation secret `visibility` (and `selected_repository_ids`)
     ([repository secrets](https://docs.github.com/en/rest/actions/secrets#create-or-update-a-repository-secret),
     [organisation secrets](https://docs.github.com/en/rest/actions/secrets#create-or-update-an-organization-secret)).
- `UpdatedAt(ctx, target)`: the secret's `updated_at`, with a token minted the same way at
  `read`. GitHub sends no webhook when a secret changes, so a scheduler compares this with
  its last write to find an overwrite. A missing secret is `ErrNotFound`.

The writer never caches, logs or returns a token or the JWT. It never logs the key, the
value or the sealed value either. It follows no redirect. An error names the method, the
path, the status and GitHub's `message`, never a header or a body that was sent. Each
write logs one line, "github secret set", with the owner, repository, name and key id.

## Package `ghwebhook`

`ghwebhook.New(secret, Options)` is the receiver of the App's webhook deliveries, served
at `/github/webhook`. Pomerium sends the public
`https://scadbuddy.sso.nullreference.io/fleet/github/webhook` to it and rewrites the path
(eh-homelab/clusters#4432). That route is public, so the handler is its only guard:

| Delivery | Answer |
|---|---|
| A method other than `POST` (Pomerium cannot limit the method on a public route) | 405, `Allow: POST` |
| No `X-Hub-Signature-256: sha256=<64 hex>` | 401; the body is not read |
| A body over 1 MiB | 413 |
| A signature that is not the HMAC-SHA256 of the raw body with the secret (compared in constant time, [GitHub's page](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)) | 401; the body is not parsed |
| `X-GitHub-Event` `installation` or `installation_repositories` | 204, after one log line and one count |
| Any other event | 204; the body is not parsed |
| One of the two events with a body that is not JSON | 400 |

The log line, "github app installation event", holds the event, the action, the delivery
id, the installation id and, for `installation`, the account id and the count of
repositories; for `installation_repositories`, the repository selection and the counts
and ids of the repositories added and removed. It holds no name, login or secret. The
counter `fleet.github.webhook.installation_events` has the attributes `event` and
`action`. Marking a `github_secret` target broken when the App loses its repository or
organisation is #8011's work, with its targets table.

## Binary

`main.go` serves `/mcp` (fleetchat's streamable HTTP handler, server name
`scadbuddy-fleet`, its version from the build info or `dev`) and `GET /healthz`
(`ok`), and `/github/webhook` (`ghwebhook`) when a webhook secret file is set. On SIGTERM it takes no new request and lets the open ones finish for 5 s,
then ends those still open (the MCP GET streams) and stops within 10 s. Apart from the
webhook's signature check, the server has no auth of its own. Anything on the LAN may call it, directly or through ScadBuddy's `/mcp`
(the owner's decision of 2026-11-26): its NetworkPolicy admits ScadBuddy's pods and the LAN's Gateway. The
internet reaches only `/github/webhook`, through the SSO host's one public Exact route
(eh-homelab/clusters#4432).

| Flag | Environment | Default |
|---|---|---|
| `-listen` | `FLEETCHAT_LISTEN` | `:8080` |
| `-core-url` | `FLEETCHAT_CORE_URL` | required: core's URL, such as `http://scadbuddy-core:8080` |
| `-service-token-file` | `FLEETCHAT_SERVICE_TOKEN_FILE` | a projected ServiceAccount token, exchanged for the `fleet` credential |
| `-service-bootstrap-file` | `FLEETCHAT_SERVICE_BOOTSTRAP_FILE` | a bootstrap secret, exchanged for the `fleet` credential |
| `-credential-file` | `FLEETCHAT_CREDENTIAL_FILE` | a fixed `fleet` credential, read again at every request and never renewed (tests, local runs) |
| `-max-text` | `FLEETCHAT_MAX_TEXT` | `16777216` (16 MiB) |
| `-page-size` | `FLEETCHAT_PAGE_SIZE` | `50` |
| `-max-page-size` | `FLEETCHAT_MAX_PAGE_SIZE` | `200` |
| `-session-timeout` | `FLEETCHAT_SESSION_TIMEOUT` | `24h`: a Go duration; `0` never closes a session |
| `-github-app-id` | `FLEETCHAT_GITHUB_APP_ID` | none: the App's id (5941183 for "ScadBuddy Fleet") |
| `-github-installation-id` | `FLEETCHAT_GITHUB_INSTALLATION_ID` | none: the installation's id (186236249 on eh-homelab) |
| `-github-app-key-file` | `FLEETCHAT_GITHUB_APP_KEY_FILE` | none: the App's private key (PEM). Unset, the secret writer is off |
| `-github-webhook-secret-file` | `FLEETCHAT_GITHUB_WEBHOOK_SECRET_FILE` | none: the webhook secret. Unset, `/github/webhook` is not served |

Exactly one of the three credential settings is required. The App key file, the App id and
the installation id go together, or none is given; the webhook secret file needs none of
them. Both files are read once, at start, with the space around them trimmed; an empty or
unreadable file, or a key that is not RSA PEM, stops the start. The start logs one
"github app" line that says which parts are on, with the two ids, never a file's
content. An exchanged credential is
renewed in the process (`NewExchange`); a fixed one lasts 15 minutes unless something
rewrites the file. A flag wins over its variable, which wins over the default. A bad
value, from either, stops the start. `--help` and `-version` need no core URL.

`-session-timeout` closes an MCP session, with its inbox subscriptions, after that long
without a request. Claude Code never sends `DELETE /mcp` when it exits (measured
2026-11-25 with Claude Code 2.1.332: four `claude -p` runs through a logging proxy
against a local stack sent none), so with `0` every run would leave its session
and its subscription open for the life of the process. The MCP SDK counts only POSTs as activity, so a timeout also ends a
session that only holds its GET stream open.

Core caps a send's body at twice its own 16 MiB text limit plus 1 MiB, and refuses a
longer text with a 422 that names the limit, so a `-max-text` above 16 MiB does not let
longer texts through.

## Deploying

Nothing deploys it yet. The Kubernetes objects will be eh-homelab/clusters'
`applications/scadbuddy/scadbuddy-fleet.yaml` and `networkpolicy-fleet.yaml` (#7903). Once
that file exists, ScadBuddy's deploy (`deploy.reusable.yml`) moves the image pin with each
build that published a fleet image, and keeps it otherwise. Before a deploy, they need:

- Core serves the fleet's routes only when `SCADBUDDY_CORE_AREAS` names `fleet`
  (`tools/go/core/README.md`). Without it the routes go to Python, which has none of them
  but `GET …/fleet/changes`.
- The pod runs as the ServiceAccount `scadbuddy-fleet`, which the API maps to caller
  `fleet` (`backend/scadbuddy/auth/exchange.py` `SERVICE_ACCOUNTS`).
- The proof is a projected ServiceAccount token, audience `scadbuddy`, from a projected
  volume (not `subPath`), named by `FLEETCHAT_SERVICE_TOKEN_FILE`. On compose or a laptop,
  use `FLEETCHAT_SERVICE_BOOTSTRAP_FILE` and the file `fleet` in the API's
  `SCADBUDDY_SERVICE_BOOTSTRAP_DIR` instead. Never deploy `FLEETCHAT_CREDENTIAL_FILE`: a
  fixed credential is not renewed.
- A route on ScadBuddy's LAN-only hostname, `https://scadbuddy.internal.nullreference.io/fleet/mcp`
  rewritten to the server's `/mcp`, so anything on the LAN can call it (the owner's decision of 2026-11-26).
  The hostname's DNS record is DNS-only, so `/mcp` is never on the internet.
- A NetworkPolicy that admits, on 8080, ScadBuddy's pods and that Gateway's proxy pods (the
  server has no auth of its own), and lets the pod reach only DNS and core (and
  api.github.com once the secret writer is on). The SSO host's Pomerium proxy reaches it
  for `/github/webhook` only (eh-homelab/clusters#4432).
- For the GitHub App: the webhook secret from 1Password `homelab - prod` /
  `scadbuddy-fleet-github-webhook` as `FLEETCHAT_GITHUB_WEBHOOK_SECRET_FILE`, and, for the
  writer, the key from `scadbuddy-fleet-github-app` as `FLEETCHAT_GITHUB_APP_KEY_FILE`
  with its `app-id` and `installation-id` (#8014).
- `FLEETCHAT_SESSION_TIMEOUT`: keep the 24 h default, or set a shorter one; never `0`
  while Claude Code is a client.

## Commands

```bash
cd tools/go/fleet && go vet ./... && go test -race ./...
docker build -f tools/go/fleet/Dockerfile -t scadbuddy-fleet:dev .   # repo root as context
```
