// Package wire holds the values the routes write as pydantic writes them, and the warm
// process cache's eviction (agent_data/access.py notify_warm_evict).
package wire

import (
	"context"
	"crypto/rand"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// Time is a timestamptz as pydantic writes a datetime: UTC with Z, microseconds only when
// there are any.
type Time time.Time

// Scan takes a timestamptz.
func (t *Time) Scan(src any) error {
	v, ok := src.(time.Time)
	if !ok {
		return fmt.Errorf("a %T is not a time", src)
	}
	*t = Time(v)
	return nil
}

// MarshalJSON writes the time as pydantic does, in UTC: the database's TimeZone is UTC.
func (t Time) MarshalJSON() ([]byte, error) {
	v := time.Time(t).UTC().Truncate(time.Microsecond)
	layout := `"2006-01-02T15:04:05Z"`
	if v.Nanosecond() != 0 {
		layout = `"2006-01-02T15:04:05.000000Z"`
	}
	return []byte(v.Format(layout)), nil
}

// UUID4 is a random UUID, as Python's uuid4().
func UUID4() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

// PyUUID is s as Python's UUID(s) reads it, in its canonical form; ok is false when Python
// raises ValueError.
func PyUUID(s string) (string, bool) {
	hex := strings.ReplaceAll(strings.ReplaceAll(s, "urn:", ""), "uuid:", "")
	hex = strings.ReplaceAll(strings.Trim(hex, "{}"), "-", "")
	if len(hex) != 32 {
		return "", false
	}
	for _, c := range hex {
		if !strings.ContainsRune("0123456789abcdefABCDEF", c) {
			return "", false
		}
	}
	hex = strings.ToLower(hex)
	return hex[0:8] + "-" + hex[8:12] + "-" + hex[12:16] + "-" + hex[16:20] + "-" + hex[20:], true
}

// WarmEvictChannel is sessions/warmEvict.ts WARM_EVICT_CHANNEL.
const WarmEvictChannel = "scadbuddy_warm_evict"

// NotifyWarmEvict tells every replica, on commit, to evict every warm process.
func NotifyWarmEvict(ctx context.Context, tx pgx.Tx) error {
	_, err := tx.Exec(ctx, `SELECT pg_notify($1, $2)`, WarmEvictChannel, "*")
	return err
}
