# claudecreds

A Go library for facts about Claude Code logins and their limits. It is part of the
fleet session registry and login rotator (#7981). The design is
`docs/superpowers/specs/2026-11-26-fleet-session-registry-and-rotator-design.md` §6.5.
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

## Commands

```bash
cd tools/go/claudecreds && go vet ./... && go test -race ./...
```

The test reads the vectors from the agent's tree, so `.github/scripts/select-go.sh`
selects the `go` job when that file changes.
