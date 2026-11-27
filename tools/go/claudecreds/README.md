# claudecreds

A Go library for facts about Claude Code logins and their limits. It is part of the
fleet session registry and login rotator (#7981). The design is
`docs/superpowers/specs/2026-11-26-fleet-session-registry-and-rotator-design.md` §6.5
and §6.6.
The library has no binary and imports only the standard library. Transcript events are §6.4.

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

## Transcript events

`ParseEntry(line)` reads one line of a Claude Code transcript
(`~/.claude/projects/<project>/<session>.jsonl`). The design is §6.4 of the spec above
(#7988). A line is an event when `isApiErrorMessage` is true and `error` is set. A line
without the text `"isApiErrorMessage":true` returns false before any JSON parse.

- `Kind` comes from `error` as the agent's `credentialErrors.ts` `classifyFailure` maps it:
  `rate_limit`; `permanent` for `authentication_failed`, `oauth_org_not_allowed`,
  `account_on_hold`, `verification_required`, `billing_error` and
  `cloud_credential_error`, and for `invalid_request`, `model_not_found` or
  `max_output_tokens` with a billing text; `transient` for the rest (`server_error`,
  `overloaded`, …).
- `Category` is the raw `error`, and `APIError` is `apiError` (such as
  `usage_limit_reached`).
- For a rate limit, `Reset` is `apiErrorParams.rate_limit_info.resetsAt` (epoch seconds),
  else `quotaLimits.resetsAt`, else `ResetFromText` on the message text with the entry's
  `timestamp` as now. `ResetSource` is `epoch`, `text` or `default` (no reset: the caller
  keeps its default cooldown).
- `LimitType` (`quotaLimits.rateLimitType`), `UUID`, `SessionID`, `Timestamp`, and
  `Text`, the message text cut to 500 bytes, for the operator. The event keeps no other
  field of the entry.

`Tail` follows one transcript file. It starts no goroutine: the caller calls `Poll()`,
which returns the events of the complete lines written since the last call, in order,
and keeps a partial last line. The first `Poll` starts at the file's end (or at 0 when
the file does not exist yet). Tail holds the file open by inode: after a rename it reads
the rest of the old file, then the new file from 0. After a truncate it reads from 0.
`Close()` closes the file.

The fixtures in `testdata/` are real transcript lines from one machine, redacted: they
keep only the keys `ParseEntry` reads plus `type`, `message.content[0].text`,
`timestamp`, `uuid`, `sessionId` and `cwd`, with fake ids and cwd `/home/user/repo`. The
normal assistant and user lines have their text replaced.

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
