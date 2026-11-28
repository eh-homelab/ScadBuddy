# fleetd

The fleet's machine service (#7987, part of #7981). It runs on each Linux machine that runs
Claude Code sessions. It finds the live sessions and keeps ScadBuddy's session registry up
to date (#7985). Design: `docs/superpowers/specs/2026-11-26-fleet-session-registry-and-rotator-design.md`
§6.3 and §6.6.

This first step only reports. Transcript events (#7988) and login rotation (#7989) come
later.

## Build and install

The module is in the Go workspace at the repo root. Build it from there:

```bash
go install ./tools/go/fleetd     # into $(go env GOPATH)/bin; do it again after a pull
fleetd scan                      # a dry run: print what run would send, and send nothing
```

Run it as a systemd **user** service, for the user who runs Claude Code. Make a personal
access token of scope `fleet` in ScadBuddy (Settings → System → Access tokens, #8041). Put
it in a file that only you can read:

```bash
install -d -m 0700 ~/.config/fleetd
install -m 0600 /dev/null ~/.config/fleetd/token
$EDITOR ~/.config/fleetd/token    # paste the token
```

Write `~/.config/systemd/user/fleetd.service`:

```ini
[Unit]
Description=ScadBuddy fleet machine service (fleetd)
After=network-online.target

[Service]
ExecStart=%h/go/bin/fleetd run
Environment=FLEETD_TOKEN_FILE=%h/.config/fleetd/token
# Environment=FLEETD_API=https://scadbuddy.example.com/api/v1
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

Then start it, and keep it running when you are not logged in:

```bash
systemctl --user daemon-reload
systemctl --user enable --now fleetd
loginctl enable-linger "$USER"
journalctl --user -u fleetd -f
```

## Commands and flags

```
fleetd run  [--api <url>] [--token-file <file>] [--config-dir <dir>]... [--interval 10s] [--refresh 5m] [--otel-endpoint <url>] [--no-telemetry] [--update-interval 15m] [--no-update]
fleetd scan [--config-dir <dir>]...
fleetd version
```

`fleetd --help` prints the usage and exits 0. An unknown command exits 2.

| Flag | Meaning |
|---|---|
| `--api` | ScadBuddy's API root, like `https://host/api/v1`. Default `$FLEETD_API`, else the deployed ScadBuddy. It must be https. Plain http is allowed only to localhost (`localhost`, `127.0.0.1`, `::1`). |
| `--token-file` | A file that holds a personal access token of scope `fleet` (#8041). fleetd sends it as `Authorization: Bearer`. It reads the file again for each request. Default `$FLEETD_TOKEN_FILE`. `run` needs it. Use one token per machine: core records the token that registered a machine first and refuses another token's writes to it while that token lives (403 `machine-of-another-proof`). To replace a token, revoke the old one (or let it expire) and put a new `fleet` token of the same person in the file, with no restart: core moves the machine to it at fleetd's next machine `PUT`, at most `--refresh` later; until then session writes get 403. A token of another person never takes the machine (403 `machine-of-another-person`), nor does any token while the old token lives. A machine a bootstrap proof registered never moves to a token (403 `machine-of-a-proof`); ask an admin to delete the machine's row. If the old token was deleted, core cannot tell whose the machine was and refuses every token (403 `machine-of-unknown-owner`); ask an admin to delete the machine's row. A revoked token gets 401; a `read` or `write` token gets 403 `out_of_scope` (core's README, "fleet-rotator"). |
| `--config-dir` | A Claude Code config dir to watch. Give it again for each other dir. Default `$CLAUDE_CONFIG_DIR`, else `~/.claude`. |
| `--interval` | How often `run` scans. Default `10s`. |
| `--refresh` | How often `run` sends every row again, changed or not, so the registry's `last_seen` stays fresh. Default `5m`. |
| `--otel-endpoint` | OTLP/HTTP collector base URL for traces, metrics and logs (see "Telemetry"). |
| `--no-telemetry` | Send no traces, metrics or logs. |
| `--update-interval` | How often `run` looks for a new signed release (see "Auto-update"). Default `$FLEETD_UPDATE_INTERVAL`, else `15m`. At least `1m`. |
| `--no-update` | Never replace this binary. |

## What `run` does

1. At start, it reads the machine's facts and registers the machine:
   `PUT /api/v1/internal/crud/fleet-rotator/machines/{machine_id}` with `hostname`, `os`,
   `os_user` and `service_version`.
2. It scans every `--interval`. It also scans 250 ms after a change in any
   `<config dir>/sessions` (inotify). A dir that does not exist yet is watched when it
   appears.
3. For each live session it sends
   `PUT …/machines/{machine_id}/sessions/{session_id}`, but only when the row changed since
   it was last sent. Every `--refresh` it sends the machine and every row again.

Core's answers:

- **404 with no problem `reason`** (or a plain not-found text): the route is not there. An
  older core pod answers so during a rollout. fleetd stops that scan, logs it once, and
  tries again at the next scan.
- **404 `unknown-machine`**: fleetd registers the machine again, then sends the row again.
- **Any other failure**: fleetd logs it and sends the row again at the next scan.

`fleetd scan` prints the same machine and rows as JSON and exits. It sends nothing and
needs no token.

## What it reads

- **The machine id:** `/etc/machine-id`. fleetd sends only `sha256(<machine id> ":" <OS user>)`,
  in hex. It never sends or logs the raw id.
- **The machine's facts:** the hostname, `/etc/os-release`'s `PRETTY_NAME`, the OS user and
  fleetd's version.
- **Sessions:** `<config dir>/sessions/<pid>.json` (`pid`, `sessionId`, `cwd`, `procStart`,
  `version`, `kind`, `entrypoint`, `name`, `status`). It ignores the other fields, such as
  `messagingSocketPath`.
- **Liveness:** `/proc/<pid>/stat`. A session is live only when its process exists and the
  process's start time (field 22) equals the file's `procStart`. So a reused pid is not taken
  for the session.
- **The transcript path:** only file names. The path is
  `<config dir>/projects/<slug>/<sessionId>.jsonl`. The slug is the cwd with each character
  that is not an ASCII letter or digit changed to `-` (Claude Code 2.1.332's code, read
  2026-11-26; it agrees with each name under this machine's `~/.claude/projects`). Claude
  Code cuts a slug longer than 200 characters and adds `-` and a hash. fleetd finds that
  directory by its first 200 characters. It sends the path only when the file exists.
- **Git facts:** it runs `git -C <cwd> rev-parse` and `git -C <cwd> config --get-regexp`,
  each with a 5 s timeout. It sends the worktree root, HEAD, the branch (none when detached)
  and the URL of `origin` (else the first remote).
- **The account:** `<config dir>/.credentials.json`. fleetd decodes only
  `claudeAiOauth.accountUuid`.

## What it never reads or sends

- It never reads a transcript's contents.
- It never reads, sends or logs a token from `.credentials.json` (`accessToken`,
  `refreshToken`), and no other field of that file but `accountUuid`.
- It never sends the raw machine id.
- It never sends a secret in a git remote. A URL remote loses its user info, query and
  fragment (`https://user:token@host/r.git` becomes `https://host/r.git`). Only http(s),
  ssh and git URLs are sent. The scp-like form `user@host:path` keeps its user name (such
  as `git@`), which core requires and which holds no password. A remote in another form,
  or with a password in the scp-like form, is not sent.
- A value that core would refuse (a control character, too long) is left out of the row,
  so one odd field does not lose the whole row.

## Auto-update

fleetd updates itself from signed GitHub releases (#8588, refs #7993).

**The releases.** `.github/workflows/fleetd-release.yml` runs on each push to main that
changes fleetd or a module it builds from, and on a dispatch of main. It cross-compiles
fleetd for linux/amd64 and linux/arm64, stamped with the full commit sha (`fleetd version`
prints it). It publishes the release `fleetd-<sha7>` with `fleetd-linux-amd64`,
`fleetd-linux-arm64`, `SHA256SUMS` and `SHA256SUMS.sig`. The release is never the repo's
"latest". Its tag is a lightweight tag at the commit. `SHA256SUMS`'s first line is
`commit <full sha>`. Then comes one `sha256sum` line per binary.

The workflow has two jobs. `build` holds no secret and only reads the repo. It builds,
writes `SHA256SUMS` and uploads both as an artifact. `release` runs in the environment
`fleetd-release`. It checks the artifact's sha256 against the one `build` output. It checks
the commit line and the binaries' sums. Then it signs and publishes. Only this job may
write releases.

**The key.** `SHA256SUMS.sig` is an ed25519 signature of `SHA256SUMS`: the raw 64 bytes, in
base64. The private key is `FLEETD_RELEASE_SIGNING_KEY` (PEM, PKCS#8). It is a secret of the
environment `fleetd-release`, not of the repo. That environment's deployment branch policy
allows only `main`, so a workflow on another branch cannot read the key. The job also
checks `github.ref` itself. The signature covers the commit line, so a signature for one
commit cannot vouch for the release of another commit. The public key is compiled into fleetd (`internal/update`, `publicKey`):
`XLVoFjOOmaMxLpAg41zfmZ661BVZkwb3MSOMTGXP0h0=`. The workflow fails, and publishes nothing,
when the secret is empty or its public half is not that key. To change the key, change both
in one PR. A fleetd with the old key refuses releases signed by the new one, so install the
new build by hand on each machine.

**What `run` does.** At start and every `--update-interval`:

1. It lists the repo's releases and takes the newest `fleetd-` release by `created_at`.
2. It does nothing when that release is the running commit. Else it resolves the release's
   tag with GitHub's git refs API. The tag must be a lightweight tag of the release's
   target commit (`target_commitish`, which the tag name also names). Then it asks GitHub's
   compare API whether that commit is ahead of the running one. A release that is behind,
   or on another line, is never taken.
3. It downloads `SHA256SUMS` and `SHA256SUMS.sig` and verifies the signature. The signed
   first line must be `commit <that commit>`. It refuses a manifest signed for another
   commit, or one with no commit line.
4. It downloads `fleetd-<GOOS>-<GOARCH>` and checks its sha256 against the signed list.
   Downloads go only to `github.com`, `objects.githubusercontent.com` and
   `release-assets.githubusercontent.com`, over https, redirects included.
5. It writes the binary next to the running one, with mode 0755, and runs `<new> version`
   (10 s). It must print the release's commit.
6. It renames the running binary to `<name>.old` and the new one to `<name>`.
7. It stops its loops and telemetry, and re-execs the new binary with the same arguments and
   environment. The pid stays the same, so systemd keeps the service.

A failure at any step keeps the running binary. fleetd logs it once per release and reason,
and tries again at the next check. A network error or a 5xx is retried twice, with backoff.
A GitHub rate limit (`Retry-After`, or `X-RateLimit-Remaining: 0`) stops all checks until it
resets. The calls are unauthenticated and carry no trace headers.

A dev build (`go install`, so `fleetd version` prints `dev…`) never updates. To get updates,
install a release binary:

```bash
tag=$(gh release list -R eh-homelab/ScadBuddy --json tagName,createdAt \
  -q '[.[] | select(.tagName | startswith("fleetd-"))] | sort_by(.createdAt) | last | .tagName')
gh release download "$tag" -R eh-homelab/ScadBuddy -p "fleetd-linux-$(dpkg --print-architecture)" -p 'SHA256SUMS*'
head -n1 SHA256SUMS   # commit <the full sha of $tag>
grep -v '^commit ' SHA256SUMS | sha256sum --ignore-missing -c -
install -m 0755 "fleetd-linux-$(dpkg --print-architecture)" ~/.local/bin/fleetd
```

Then point the unit's `ExecStart` at `%h/.local/bin/fleetd run`. The directory must be
writable by the service's user. Else each check logs why and fleetd keeps running.

**Roll back.** The binary before the last update is `<name>.old`:

```bash
systemctl --user stop fleetd
mv ~/.local/bin/fleetd.old ~/.local/bin/fleetd
# Add --no-update to ExecStart, or the next check takes the newest release again.
systemctl --user daemon-reload && systemctl --user start fleetd
```

## Telemetry

Telemetry is on by default, as for bambuddy-fs. fleetd sends OpenTelemetry traces, metrics
and logs, with this machine's host name as `service.instance.id`, to eh-homelab's collector
(`https://otlp.internal.nullreference.io`). Pass `--no-telemetry` (or set
`OTEL_SDK_DISABLED=true`) to send nothing. The endpoint rules and the `OTEL_*` variables are
those of the shared `tools/go/telemetry` module; bambuddy-fs's README describes them. The
resource is `service.name=fleetd`.

- **Spans:** `fleetd.scan` (INTERNAL) for each scan, with `fleetd.sessions`, `fleetd.sent`
  and `fleetd.failed` (counts). Each request to core has a CLIENT span with the method, the
  status code and the API's host, never the URL's path.
- **Metrics:** `fleetd.sessions` (a gauge, the live sessions at the last scan) and
  `fleetd.registry.writes` (by `result`, `ok` or `failed`), and the Go runtime's and the
  process's metrics.
- **Logs:** a text line on stderr for each record. The OTLP copy keeps only the attributes
  `session` (an id), `sessions`, `sent`, `failed`, `config_dirs` (counts) and `version`.
  A path, a remote, a token and an error's text stay on stderr.

No span or metric attribute holds a path, a token, a remote or an account id.

## Container image

`build-go-image.yml` publishes `ghcr.io/eh-homelab/scadbuddy-fleetd`, as it does for each Go
CLI. fleetd is meant to run on the machine itself, as a systemd user service (above): in a
container it would need the user's config dirs, the host's `/proc` and `/etc/machine-id`.
Build it from the repo root:

```bash
docker build -f tools/go/fleetd/Dockerfile -t scadbuddy-fleetd:dev .
```
