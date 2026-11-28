# Fleet session registry and Claude credential rotator: design

Status: draft, 2026-11-26. Owner decisions are marked **(owner)**. Calls the author made are
marked **(call)** and stay open to correction. Facts measured for this design say where and
how they were measured.

## 1. Intent

The owner runs many Claude Code sessions on several Linux machines, under several Claude
accounts. When one account reaches a usage limit, a person today finds the reset time, logs
in to the next account, and tells each stopped session to continue. This design automates
that loop **(owner)**:

1. A **stand-alone Go service on each Linux machine** finds every Claude Code session there and
   reads their transcripts for rate-limit events **(owner)**.
2. It **rotates the machine's Claude login** to the next usable account from a list of
   credentials and kinds kept in the database on the primary cluster **(owner)**.
3. It **sends "continue" to the stopped sessions** through the fleet. The next turn of each
   session is also the check that the rotation worked **(owner)**.
4. The fleet can show **which session uses which credential**, and **which machine, path and
   other local facts** belong to each fleet session id **(owner)**.

Rate limits are per account, whoever uses it. So the fleet shares ScadBuddy's credential pool
(`ai_credentials`), through core's interfaces, as all data access does **(owner)**. A cooldown
the fleet sees also makes ScadBuddy's agent skip that account, and the reverse.

The touched items of a session (issues, PRs, runs, repos and files) are a **server-side**
concern **(owner)**: #7971 defines them, and the fleet server derives them. The machine service
only reports raw facts about a session (machine, cwd, git remote and branch, transcript). The
server turns those facts into touched items with #7971's logic (§6.3).

## 2. Goals, non-goals, anti-goals

Goals:
- A session id in the fleet maps to its machine, pid, cwd, config dir, transcript path, git
  facts, Claude Code version, kind and name, and to the credential it uses now.
- A rate limit is detected within seconds of its transcript entry, with the reset time.
- The machine's login moves to the next usable account, with no restart of any session.
- Every session that stopped on the limit is told to continue, and its next turn is checked.
- Every rotation, rate limit and check is recorded, with its time.

Non-goals:
- macOS or Windows machines. Claude Code stores the login in the keychain there. Linux (and WSL)
  only **(owner)**.
- Cloud sessions and GitHub-hosted runners. They do not use a local login.
- Choosing a model or plan. The service only picks an account.
- Changing ScadBuddy's agent rotation (`agent/src/harness/fallback.ts`). It keeps its gateway.

Anti-goals (a failure even if it works):
- A secret on a path with no authentication. The fleet chat server has no auth (its README),
  so no credential secret ever passes through it or through fleet chat.
- A second database client. The service and the fleet server stay HTTP clients of core (root
  `CLAUDE.md`, "Database access").
- Two machines on one subscription login at once. Claude Code rotates the refresh token at each
  refresh (§3), so a second machine would sign the first one out.
- A guessed reset time recorded as a measured one.

## 3. Measured facts

From Claude Code 2.1.332's embedded JavaScript, read on 2026-11-26 with `strings` on
`~/.local/share/claude/versions/2.1.332` (this session; the offsets were kept in the reading
notes, not here). "Code" means read in the code, not run.

- The login is `<config dir>/.credentials.json`. The config dir is `CLAUDE_CONFIG_DIR`, else
  `~/.claude`. It is written with mode 0600 (also seen on disk). Its `claudeAiOauth` object
  has `accessToken`, `refreshToken`, `expiresAt`, `refreshTokenExpiresAt`, `accountUuid`,
  `organizationUuid`, `rateLimitTier`, `scopes`, `subscriptionType` and
  `subscriptionTypeSource` (key names read on disk, values not read).
- Every write of that file takes a proper-lockfile lock on `<config dir>/.storage-write` and
  re-reads the file inside the lock. proper-lockfile adds `.lock`, so the lock on disk is the
  directory `<config dir>/.storage-write.lock`, stale after 15 s (Claude Code 2.1.332's code,
  read with `strings` in #8047; [proper-lockfile](https://github.com/moxystudio/node-proper-lockfile#lockfile-options)). A token refresh also takes
  `<config dir>/.oauth_refresh.lock` and the legacy `<realpath(config dir)>.lock` (code).
- A running session checks the file's mtime before each model request, and on a change drops
  its cached tokens and reads the file again. It also re-reads the file after a 401 and adopts a
  token that differs from the one refused (code). So a login written by another process is
  used on the session's next request, with no restart (code; measured live in #7982, below).
- Measured live (#7982, 2026-11-26, Claude Code 2.1.332, this machine): two long-lived
  `claude -p --input-format stream-json` processes shared a scratch `CLAUDE_CONFIG_DIR`. Between
  turns, `.credentials.json` was replaced through a 0600 temp file and a rename, with no
  `~/.claude.json` change and no lock. Each copied login had its refresh token removed, so the
  originals could not rotate. Login A: both processes answered. An invalid access token: both
  failed on their next turn with `authentication_failed` (401). Login B (a second account, at its
  weekly limit): both got `rate_limit` (429) with "You've hit your weekly limit · resets Nov 29,
  8am (America/New_York)", so B's token was used. Login A again: both answered. The
  file was read again within a second of the rename, and a 401 left no state that blocked the
  next login. Not measured: interactive sessions (the same request path in code), and a
  `claude setup-token` token as `accessToken`.
- `/login` also writes `oauthAccount` in `~/.claude.json` (with `accountUuid`,
  `organizationUuid`, `emailAddress` and `profileFetchedAt`). A running session re-checks at 1 s,
  3 s and 10 s after that entry changes, and on a change clears its caches and refreshes its
  account settings (code, behind a flag that defaults to on).
- A refresh starts 5 minutes before `expiresAt`, and writes the new tokens back to the file.
  The refresh token changes at a refresh, so a copy kept elsewhere goes stale (code).
- `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_AUTH_TOKEN` are fixed for the life of the process,
  and `apiKeyHelper` sends its value as an API key, not as a subscription login (code). None of
  them can switch a running session's subscription account.
- A rate-limit entry in a transcript has `isApiErrorMessage: true` and `error: "rate_limit"`.
  A usage limit also has `apiError: "usage_limit_reached"` and the reset as epoch seconds in
  `apiErrorParams.rate_limit_info.resetsAt` and `quotaLimits.resetsAt`, with
  `quotaLimits.rateLimitType` (such as `five_hour`) (one transcript, Claude Code 2.1.310). A plain
  429 ("API Error: Request rejected (429) …") has no epoch.
- The text names the reset in the machine's time zone: across this machine's transcripts
  (`~/.claude/projects/*/*.jsonl`, counted 2026-11-26) every usage-limit text ends in
  `(America/New_York)`, such as "You've hit your session limit · resets 1:30am
  (America/New_York)" and "You've hit your weekly limit · resets Nov 29, 8am
  (America/New_York)". ScadBuddy's reset parser accepts only `(UTC)`
  (`agent/src/harness/credentialErrors.ts` `rateLimitResetFromText`, held to
  `agent/test/fixtures/rate-limit-reset-vectors.json`).
- Other errors in the same transcripts: `billing_error` ("Credit balance is too low") and
  `server_error` (529 overloaded, a cut-off response).
- Each running session writes `~/.claude/sessions/<pid>.json` with `pid`, `sessionId`, `cwd`,
  `startedAt`, `procStart`, `version`, `kind`, `entrypoint`, `name`, `status`, `updatedAt` and
  `messagingSocketPath` (read on disk, 2026-11-26).

## 4. Approaches

How the machine's sessions move to the next account:

- **(a) Rewrite the login file (picked, owner: "look at what happens after a /login").** Write
  the next account's `claudeAiOauth` into `.credentials.json` the way `/login` does, under
  Claude Code's own locks, and update `oauthAccount` in `~/.claude.json`. Every session in the
  config dir uses it on its next request (§3). It works for sessions already running.
- (b) A loopback gateway, as ScadBuddy's agent uses. Sessions would need `ANTHROPIC_BASE_URL`
  and a fixed env token at start, so running sessions would not move, and subscription
  refresh is skipped behind a non-first-party base URL (code). Not picked.
- (c) `apiKeyHelper`. It sends an API key, not a subscription login (§3). Not picked.

Where the accounts live:

- **(a) ScadBuddy's pool, `ai_credentials`, through core's routes (picked, owner).** One
  cooldown state per account for every user of it.
- (b) A separate fleet table. Not picked: two cooldown states for one account.

## 5. Parts

| Part | Where | What |
|---|---|---|
| 1 | `backend/scadbuddy/migrations_agent/`, `tools/go/core/internal/secrets` | The pool learns a subscription-login kind, a scope and a machine lease (§6.1) |
| 2 | `tools/go/core` (new `internal/fleetrotator`), the API's auth | A `fleet-rotator` caller and its routes (§6.2) |
| 3 | `tools/go/core/internal/fleet`, a migration | The session registry (§6.3) |
| 4 | `tools/go/claudecreds` (new library module) | Reading transcripts, the reset parser, and the login file writer (§6.4, §6.5) |
| 5 | `tools/go/fleetd` (new binary module) | The machine service (§6.6) |
| 6 | `tools/go/fleet`, `tools/go/fleetchat` | The registry and rotations in the fleet's HTTP API and MCP tools, and the continue message (§6.7, §6.8) |
| 7 | eh-homelab/clusters, the project-ops plugin | Routes, proofs and the install (§7) |

## 6. Design

### 6.1 The pool

- A new kind, `claude_login` **(call: name)**: a whole `claudeAiOauth` object, sealed like every
  other secret (envelope encryption, context bound to row id and kind). Its non-secret fields
  (`accountUuid`, `organizationUuid`, `subscriptionType`, `rateLimitTier`, the email) are
  stored in clear columns, so the registry and the UI can name the account without a reveal.
- A `scope` column: `agent`, `fleet` or `both`, default `agent` **(call)**, so no existing row
  changes meaning. ScadBuddy's agent reads `agent` and `both`; the rotator reads `fleet` and
  `both`. ScadBuddy's agent never picks a `claude_login` row: it cannot use one (§4 (b)).
- A machine lease on `claude_login` rows: `leased_to` (machine id), `leased_at`,
  `lease_epoch`. At most one machine holds a login **(call, from the anti-goal in §2)**. The
  other kinds take no lease.
- Health writes stay the same conditional update the agent uses (`/credentials/events`): a
  rate limit from a transcript is a `cooldown` with the epoch reset, so both users skip it.
- A usage limit belongs to the account, not to one login. Two machines that each ran `/login`
  for one account hold two token families, so two rows with one `account_uuid` (measured
  2026-11-26: this machine and spark-c9d9 have the same account uuid hash). A cooldown of a
  `claude_login` row therefore cools every row with the same `account_uuid` **(call)**. The
  lease is per row (one token family), not per account.

### 6.2 The `fleet-rotator` caller

- A new service caller `fleet-rotator`, separate from `fleet` **(call)**. The `fleet` caller
  runs the unauthenticated chat server and must never reveal a secret. The `fleet` PAT scope
  below is a third principal: a person's token for one machine, which these routes accept and
  the chat server never does.
- Proof on a machine: a personal access token of the scope `fleet`, read from `--token-file`
  and sent as `Authorization: Bearer`, as `bambuddy-fs` does (owner, 2026-11-26; #8041). One
  live token is one machine: the machine row records the token's id, and a person stops a
  machine's writes by revoking its token in Settings. A row moves to a new owner only when its
  owner is dead: to a new `fleet` token of the same person once the old token is revoked or
  expired, or, for a proof's machine, to a token once its proof file is gone. A proof never
  takes a token's machine, and a machine whose token row is gone is refused to every token. A `fleet` token can call only these routes. #8003's
  per-machine bootstrap proofs (`fleet-rotator.<machine>`) still work beside the tokens; #8367
  retires them for machines.
- Proof in the cluster: the `github` mode (#8012) keeps a `fleet-rotator` service credential from
  a Kubernetes ServiceAccount proof, renewed like the other callers'
  (`tools/go/fleet/sbfleet` `NewExchange`).
- Routes under `/api/v1/internal/crud/fleet-rotator/`, each requiring the `fleet-rotator` caller
  or a `fleet`-scope token:
  - `PUT /machines/{id}`: register or refresh the machine (hostname, OS, user, service version).
  - `PUT /machines/{machine_id}/sessions/{session_id}`: write this machine's row for a session
    (§6.3).
  - `POST /leases`: lease the best usable `claude_login` row with scope `fleet` for this machine
    (priority order, no cooldown, not disabled, not leased elsewhere), and reveal it. Only
    `claude_login` rows are leased: `fleetd` writes only a `claudeAiOauth` login (§4, §6.6).
    Answers `recovers_at` when none is usable.
  - `PUT /leases/{credential}`: write back the current login (the refresh token changes) while
    the lease is held, keyed by `lease_epoch`.
  - `DELETE /leases/{credential}`: release. A lease whose login was installed is released after a
    final write-back; a lease that was never installed (§6.6 step 3) is released without one.
  - `POST /events`: a rate limit, auth failure or recovery, with its source (transcript, session,
    entry uuid), deduplicated on the entry uuid.
  - `POST /rotations`, `PUT /rotations/{id}`: a rotation (from, to, reason, time) and its check
    (each session's result, `verified_at`).
- Secrets travel only on these routes, on core's authenticated path, never through the fleet
  chat server.

### 6.3 The session registry

- Tables (each with `created_at`, and `updated_at` where the row changes, #7968):
  - `fleet_machines`: id (the hash of `/etc/machine-id` and the user, never the raw id)
    **(call)**, the id of the `fleet` token that registered it (§6.2; #8041 adds `token_id`
    beside #8003's `proof_name`, and each row names one of them; #8367 drops `proof_name`), hostname, OS, user, service version, first and last seen.
  - `fleet_session_hosts`: `session_id` (a fleet agent), machine, pid, `procStart`, cwd, config
    dir, transcript path, git remote, branch, worktree root and HEAD, Claude Code version,
    kind, entrypoint, name, status, the credential in use (id), the account uuid, the last rate
    limit and its reset, last seen. A row is keyed by session and machine: one session id can run
    on several machines at once (measured 2026-11-26: fleet session `b79d1dcc` posts from both
    spark and a laptop), and each machine writes only its own row
    (`PUT …/fleet-rotator/machines/{machine_id}/sessions/{session_id}`). A session that moves
    (`/clear` gives a new id under the same pid) gets a new row on that machine, linked to the
    old one by pid and `procStart`.
- The service writes through `fleet-rotator`'s routes; the fleet server reads through new
  `fleet` routes (`GET /sessions`, `GET /sessions/{id}`). They return each session once, with
  one host per machine, and give the credential's id, name, kind and last four characters, never
  the secret.
- **Touched items stay server-side (owner).** The registry feeds #7971: the session's git remote
  and worktree become its repo context (#7971 part 4, step 2), and its cwd links its files to a
  repo (part 5). The service sends no touched items of its own.

### 6.4 Reading transcripts

- The service follows each live session's transcript (from `~/.claude/sessions/<pid>.json` and
  the project dir) from its end, by inode and offset, and reads only new lines.
- An entry with `isApiErrorMessage` and `error` is an event:
  - `rate_limit`: a cooldown until `apiErrorParams.rate_limit_info.resetsAt`, else
    `quotaLimits.resetsAt`, else the reset in the text (§6.5), else the default cooldown.
    The source of the time is recorded (`epoch`, `text`, `default`).
  - `billing_error`, `authentication_failed` and the agent's other permanent categories
    (`credentialErrors.ts` `classifyFailure`): a failure of that credential, sent as an event;
    the row is disabled only as the agent's rules allow.
  - `server_error` and the like: ignored.
- Each event names the session, the machine and the credential in use at that time.

### 6.5 The reset parser

- A Go port of `rateLimitResetFromText`, held to the shared vectors, in `tools/go/claudecreds`.
- It also reads an IANA zone in the parentheses (`America/New_York`), with the zone's rules for
  the date. New vectors, from the texts measured in §3, go into
  `rate-limit-reset-vectors.json`, and the TypeScript and Python ports pass them too, as the
  fixture requires.

### 6.6 The machine service, `fleetd`

- A Go binary run as a systemd user service on each Linux machine **(call)**, for the user who
  runs Claude Code. Config dirs to watch: `~/.claude`, and any more named in its settings.
- Every 10 s **(call)** and on a change in `~/.claude/sessions`, it reads the sessions, checks
  each pid is alive with the same `procStart`, and sends the registry rows.
- On a rate limit of the config dir's current credential:
  1. Send the event (§6.2). Ask for a lease on the next usable credential.
  2. Take Claude Code's locks: `.oauth_refresh.lock`, the legacy `<dir>.lock` and
     `.storage-write.lock`, with the proper-lockfile convention, and refresh their mtimes every
     5 s while they are held. While they are held, no session can
     refresh or write the login (§3).
  3. Inside the locks, read the current `claudeAiOauth` back. It holds the live refresh token.
     Write it to the old row (`PUT /leases/{credential}`), with a 10 s timeout, below the 15 s
     stale time of `.storage-write.lock`. If that write fails, release the locks, release the
     new lease (`DELETE /leases/{new}`) and stop: the file is not changed, the old lease is
     kept, and the new row is free for other machines. If that release fails too (the API is
     down), the service writes the new lease's id to a 0600 file beside its state, per config
     dir, and on its next attempt for that dir releases that lease before it asks for another.
     This state is the service's own; the API keeps no "installed" mark.
  4. Still inside the locks, write the new `claudeAiOauth` to a 0600 temp file in the dir and
     rename it over `.credentials.json`.
  5. Release the locks. Only then release the old lease (`DELETE /leases/{credential}`), so no
     other machine can take that login while this machine's sessions can still use or refresh
     it.
  6. Update `oauthAccount` in `~/.claude.json`, best effort (below).
  7. Record the rotation, then send continue (§6.7).
- `~/.claude.json` has no lock that the service can share. No lock around its writes was found
  in Claude Code 2.1.332's code (read 2026-11-26), and Claude Code rewrites the file often. The
  service reads the file, changes only `oauthAccount` (other keys kept), writes a temp file and
  renames it. Then it reads the file again, and tries again (at most 3 times) if `oauthAccount`
  is not the new one. If it still loses the race, it logs that and goes on: sessions switch on
  the `.credentials.json` mtime alone, as #7982 measured, and the `oauthAccount` change only
  starts the faster sibling-login check (§3).
- While it holds a lease it writes the login back after each refresh (a change of
  `.credentials.json`'s mtime that it did not cause), so the pool keeps the live refresh token.
- When no credential is usable, it records that, waits until the soonest `recovers_at`, and
  then rotates. It never loops faster than once a minute per config dir **(call)**.
- It never logs a token, and it reads secrets only into memory.

### 6.7 Continue, and the check

- After a rotation, the service asks the fleet server to send each session that stopped on the
  limit a message from a virtual sender `rotator` (#7971 part 6): "The Claude login on <machine>
  moved to <credential name>. Continue the task you were doing." Fleet chat delivers it to the
  session's inbox, and the plugin's `fleet-watch` monitor raises it in the session.
- A person can send the same message to every live session, or to one machine's, from the fleet
  HTTP API (and later the UI, #7972).
- The check: the service watches each told session's transcript. The first assistant entry after
  the message with no error passes it; a new `rate_limit` fails it, cools that credential too, and
  starts the next rotation. A session that does not answer in 10 minutes **(call)** is marked
  `no-answer`. The rotation's `verified_at` is set when one session passes.
- A session not in the fleet (no fleet chat) cannot be told. The registry marks it
  `unreachable`, and the UI lists it for a person.

### 6.8 The fleet's API and tools

- The fleet server (`tools/go/fleet`) adds HTTP routes beside `/mcp`: `GET /api/sessions` (the
  registry, with credential names), `GET /api/rotations`, and `POST /api/continue` (all, one
  machine or one session).
- Their auth is that of `/mcp`, which is none: every path but `/github/webhook` is open to the
  LAN and the cluster only, never the internet, by the owner's decision of 2026-11-26
  (`tools/go/fleet/README.md`, "Binary"). The internet reaches only `/github/webhook`, through
  the SSO host's public Exact route (eh-homelab/clusters#4432), which the webhook's signature
  check (`X-Hub-Signature-256`, an HMAC with the webhook secret) guards; the NetworkPolicy also
  admits the SSO host's Pomerium proxy for that path. So these routes are held to what is safe to show on the LAN:
  - Reads give the session's machine, paths, git facts, and the credential's id, name, kind and
    last four characters. Never a secret, a token or an email.
  - `POST /api/continue` takes only a target (all, a machine id, or a session id). The text is
    the fixed continue message from `rotator` (§6.7); a caller cannot supply text. It is rate
    limited to once a minute per target **(call)**.
  - When sender authentication lands (#7927), these routes take it too.
- MCP tool `fleet_sessions`: the same registry, so a session can see who runs where and on which
  account.
- No route of the fleet server reveals or accepts a secret.

## 7. Deploying

- The `fleet-rotator` routes must be reachable from the machines, on ScadBuddy's LAN host, with a
  `fleet` token (§6.2) required. Machines do not use the exchange route; only the in-cluster
  `github` mode does, inside the cluster. The ingress change is in
  eh-homelab/clusters, dry-run on the server before merge.
- Each machine gets a `fleet` token (§6.2), made in Settings → System → Access tokens and
  written to a 0600 file on the machine.
- `fleetd` is published as a binary for linux/amd64 on a release tag, beside its image (every Go
  CLI has a Dockerfile, root `CLAUDE.md`). The project-ops plugin documents the systemd user unit.

## 8. What is left to the builder

- Names of tables, routes and the kind, where they read better.
- Any proper-lockfile detail not named in §3 and §6.6, matched to what Claude Code does.
- How the service learns a transcript's credential when the session started before the service.
  The account uuid in `.credentials.json` at the time is enough.

## 9. Issues
Epic: #7981. Item 12 is under the fleet UI epic #7972.


1. #7982: Measure: two live sessions in one config dir; rewrite the login from outside; check both use
   the new account on their next request. Also measure a `claude setup-token` token written as
   `accessToken` with no refresh token. This gates part 5's rotation.
2. #7983: The pool: `claude_login` kind, `scope`, machine lease (§6.1).
3. #7984: The `fleet-rotator` caller, proofs and routes (§6.2).
4. #7985: The session registry tables and routes (§6.3).
5. #7986: The reset parser in Go with IANA zones, and the shared vectors in all three ports (§6.5).
6. #7987: `fleetd`: session discovery and the registry (§6.6, first half).
7. #7988: `fleetd`: transcript events (§6.4).
8. #7989: `fleetd`: rotation and write-back (§6.6, second half).
9. #7990: Continue and the check (§6.7), after #7971's virtual senders.
10. #7991: The fleet's HTTP API and `fleet_sessions` (§6.8).
11. #7993: Deploying (§7).
12. #7994: The UI's session, machine and credential view, under the fleet UI epic #7972.
