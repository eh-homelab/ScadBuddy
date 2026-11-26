// Package claudecreds reads facts about Claude Code logins and their limits.
package claudecreds

import (
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	// The zone data goes into the binary, so a container with no /usr/share/zoneinfo
	// still knows the zone a text names.
	_ "time/tzdata"
)

// Claude Code's usage-limit text names its reset, "resets 10:30pm (UTC)" or "resets
// Oct 21, 4am (UTC)" (#4088), or, on a machine that is not pinned to UTC, in its IANA
// zone, "resets 1:30am (America/New_York)" (#7986). The same parse as the agent's
// credentialErrors.ts rateLimitResetFromText and agent-durable's credentials.py
// rate_limit_reset; all three are held to agent/test/fixtures/rate-limit-reset-vectors.json.
var resetRE = regexp.MustCompile(`(?i)\bresets\s+(?:([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\((UTC|[a-z][a-z0-9_+-]*(?:/[a-z0-9_+-]+)+)\)`)

// ResetGrace is how long after a named minute it still counts as passing now.
const ResetGrace = 15 * time.Minute

// ResetHorizon is how far ahead a dated reset may roll into next year (weekly limits
// name days ahead).
const ResetHorizon = 31 * 24 * time.Hour

var months = []string{"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"}

// ResetFromText returns the reset a rate-limited turn's text names, in UTC, as the
// next such time after now. A time alone is today's or tomorrow's. A date is this
// year's or, across the new year, next year's. Today, tomorrow and the year are those
// of the zone the text names, with its DST rules.
//
// It returns false when the text names no reset, one that is not a time or a date, or
// a zone it does not know. The caller then keeps its default cooldown.
//
// The text names the minute, never the second. So a reset within ResetGrace before
// now is one that is passing now, not tomorrow's (or next year's): false, not a day's
// lock-out. A dated reset that has passed has no grace.
func ResetFromText(text string, now time.Time) (time.Time, bool) {
	m := resetRE.FindStringSubmatch(text)
	if m == nil {
		return time.Time{}, false
	}
	monthName, dayText, hourText, minuteText, half, zone := m[1], m[2], m[3], m[4], m[5], m[6]
	hour12, _ := strconv.Atoi(hourText)
	minute := 0
	if minuteText != "" {
		minute, _ = strconv.Atoi(minuteText)
	}
	if hour12 < 1 || hour12 > 12 || minute > 59 {
		return time.Time{}, false
	}
	hour := hour12 % 12
	if strings.EqualFold(half, "pm") {
		hour += 12
	}
	loc, ok := location(zone)
	if !ok {
		return time.Time{}, false
	}
	local := now.In(loc)
	if monthName == "" {
		y, mo, d := local.Date()
		at, _ := zoned(loc, y, mo, d, hour, minute)
		if at.After(now) {
			return at, true
		}
		if at.After(now.Add(-ResetGrace)) {
			return time.Time{}, false
		}
		// Tomorrow is the zone's next date, which is not always 24 hours later (DST).
		next := time.Date(y, mo, d+1, 0, 0, 0, 0, time.UTC)
		return zoned(loc, next.Year(), next.Month(), next.Day(), hour, minute)
	}
	month := slices.Index(months, strings.ToLower(monthName))
	if month < 0 {
		return time.Time{}, false
	}
	day, _ := strconv.Atoi(dayText)
	at, ok := zoned(loc, local.Year(), time.Month(month+1), day, hour, minute)
	if !ok {
		return time.Time{}, false
	}
	if at.After(now) {
		return at, true
	}
	// Only a date just across the new year is next year's ("Jan 2" on Dec 31). One
	// earlier this year has passed, however recently.
	later, ok := zoned(loc, local.Year()+1, time.Month(month+1), day, hour, minute)
	if !ok || later.Sub(now) > ResetHorizon {
		return time.Time{}, false
	}
	return later, true
}

// location loads the zone the text names. "UTC" is read in any case; an IANA name only
// as written, as Python's zoneinfo reads it.
func location(name string) (*time.Location, bool) {
	if strings.EqualFold(name, "UTC") {
		return time.UTC, true
	}
	loc, err := time.LoadLocation(name)
	if err != nil || !writtenAsFiled(name) {
		return nil, false
	}
	return loc, true
}

// zoneDirs are the directories time.LoadLocation reads before its embedded data
// (zoneinfo_unix.go's platformZoneSources, and zoneinfo_ios.go's for darwin).
var zoneDirs = []string{"/usr/share/zoneinfo", "/usr/share/lib/zoneinfo", "/usr/lib/locale/TZ", "/etc/zoneinfo"}

// writtenAsFiled reports whether no zone directory holds name in another case. On a
// case-insensitive filesystem (macOS) time.LoadLocation opens "america/new_york" from
// /usr/share/zoneinfo, which Linux, the embedded data and the other two ports refuse
// (#8023). It reads each directory's entries, so the answer is the same on any
// filesystem.
func writtenAsFiled(name string) bool {
	for _, dir := range zoneDirs {
		for _, part := range strings.Split(name, "/") {
			entries, err := os.ReadDir(dir)
			if err != nil {
				break
			}
			if !slices.ContainsFunc(entries, func(e os.DirEntry) bool { return e.Name() == part }) {
				if slices.ContainsFunc(entries, func(e os.DirEntry) bool { return strings.EqualFold(e.Name(), part) }) {
					return false
				}
				break
			}
			dir = filepath.Join(dir, part)
		}
	}
	return true
}

// zoned returns that date and wall clock time in loc, in UTC. It returns false when
// the date does not exist (Feb 30). A time that occurs twice (DST ends) is the
// earlier. A time that does not occur (DST starts) is read with the offset before the
// gap. Python's fold=0 does the same, and so do the other two ports.
func zoned(loc *time.Location, year int, month time.Month, day, hour, minute int) (time.Time, bool) {
	naive := time.Date(year, month, day, hour, minute, 0, 0, time.UTC)
	if naive.Month() != month || naive.Day() != day {
		return time.Time{}, false
	}
	before := naive.Add(-offset(loc, naive.Add(-24*time.Hour)))
	after := naive.Add(-offset(loc, naive.Add(24*time.Hour)))
	if wall(loc, before).Equal(naive) || !wall(loc, after).Equal(naive) {
		return before, true
	}
	return after, true
}

// offset is loc's offset from UTC at the instant t.
func offset(loc *time.Location, t time.Time) time.Duration {
	_, seconds := t.In(loc).Zone()
	return time.Duration(seconds) * time.Second
}

// wall is the wall clock time in loc at the instant t, read as a UTC time.
func wall(loc *time.Location, t time.Time) time.Time {
	l := t.In(loc)
	return time.Date(l.Year(), l.Month(), l.Day(), l.Hour(), l.Minute(), l.Second(), 0, time.UTC)
}
