# claudecreds

A Go library for facts about Claude Code logins and their limits. It is part of the
fleet session registry and login rotator (#7981). The design is
`docs/superpowers/specs/2026-11-26-fleet-session-registry-and-rotator-design.md` §6.5
and §6.6.
The library has no binary and imports only the standard library.

## The reset parser

`ResetFromText(text, now)` reads the reset that a Claude Code usage-limit text names. It
returns the reset in UTC and `true`, or `false` when the caller must keep its default
cooldown.

- Accepted texts: "resets 10:30pm (UTC)", "resets Oct 21, 4am (UTC)" and
  "resets 1:30am (America/New_York)". The zone is `UTC` (any case) or an IANA name
  with at least one `/`, as written.
- A time alone is today's or tomorrow's. A date is this year's or, across the new year,
  next year's (`ResetHorizon`, 31 days). Today, tomorrow and the year are those of the
  named zone, with its DST rules.
- A time that occurs twice (DST ends) is the earlier. A time that does not occur (DST
  starts) is read with the offset before the gap.
- A time alone that passed less than `ResetGrace` (15 minutes) ago gives `false`. A
  dated reset that has passed gives `false`.
- An unknown zone gives `false`. The package imports `time/tzdata`, so a container with
  no zone files still knows every zone.

The agent's `credentialErrors.ts` `rateLimitResetFromText` and agent-durable's
`credentials.py` `rate_limit_reset` do the same parse. All three are held to
`agent/test/fixtures/rate-limit-reset-vectors.json`. A change to the rules changes all
three and adds vectors there, each with its provenance in `why`.

## The login and its locks

`login.go` replaces the login in a config dir (`~/.claude` or `CLAUDE_CONFIG_DIR`) under
Claude Code's own locks (#7989). The facts are design §3 and Claude Code 2.1.332's code
(read 2026-11-26 with `strings` on its binary).

- The login is `claudeAiOauth` in `<dir>/.credentials.json` (mode 0600).
- `AcquireLoginLocks(ctx, dir)` takes three proper-lockfile locks, in Claude Code's own
  order: `<dir>/.oauth_refresh.lock` and `<realpath(dir)>.lock` (stale 60 s), then
  `<dir>/.storage-write.lock` (stale 15 s; Claude Code locks the path `.storage-write`, and
  proper-lockfile adds `.lock`). Each lock is a directory made with mkdir. While it holds
  them, it updates their mtimes every 5 s. It waits for a fresh lock, takes over only a
  stale one, and gives back every lock when it fails (`LockTimeout`, 30 s, or the
  context's deadline). `Release` removes only a lock this process still holds. The
  convention is
  [proper-lockfile's](https://github.com/moxystudio/node-proper-lockfile#design).
- `ReadLogin(dir)` returns `claudeAiOauth` exactly as stored.
- `WriteLogin(locks, dir, login)` replaces only `claudeAiOauth`. The other top-level keys
  (such as `mcpOAuth`) keep their bytes and their order. It writes a 0600 temp file in the
  dir, syncs it and renames it over the file. It refuses (`ErrLocksNotHeld`) without the
  dir's locks, after their release, or after another process took one over.
- `Rotate(ctx, dir, writeBack, next)` does design §6.6 in the order of #8016: take the
  locks, read the live login, call `writeBack(old)`, write `next`, release. When
  `writeBack` fails, the file is not changed. It returns after the release, with the
  mtime of the file it wrote, so the caller then releases the old lease.
- `LoginChanged(dir, last)` tells whether the file's mtime changed since `last`. A change
  that the caller did not cause is a refresh by Claude Code, so the caller writes the
  login back. The caller polls it.
- `UpdateOAuthAccount(path, account)` sets `oauthAccount` in `~/.claude.json`, best
  effort. That file has no lock to share. It changes only that key, writes a temp file and
  renames it, reads the file again, and tries again at most 3 times. Then it returns
  `ErrRaceLost`.
- No error or log names a token or the file's contents; errors name the path and the
  step.

The tests use only `t.TempDir()`, never the real `~/.claude` or `~/.claude.json`.

## Commands

```bash
cd tools/go/claudecreds && go vet ./... && go test -race ./...
```

The test reads the vectors from the agent's tree, so `.github/scripts/select-go.sh`
selects the `go` job when that file changes.
