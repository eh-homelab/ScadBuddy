// Package bus publishes an event on the API's event bus as core/pg_events.py write_event
// does: the event log's lock, the row in events, and the NOTIFY on scadbuddy_events, in a
// transaction of its own after the change it tells of has committed.
package bus

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"log/slog"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

const (
	// channel is core/events.py PG_CHANNEL.
	channel = "scadbuddy_events"
	// logLock is core/pg_events.py EVENT_LOG_LOCK.
	logLock = int64(0x5343_4144_4556_4E54)
	// PayloadMax is pg_events.py MAX_PAYLOAD_BYTES, half of NOTIFY's cap.
	PayloadMax = 4000
	// TopicsMax is the most topics an event's envelope may carry (core/events.py).
	TopicsMax = 64
)

// Publish logs and NOTIFYs the event kind with fields, which follow id, at and kind in its
// JSON. As core/events.py emit, a failure is logged and never reaches the caller: the change
// it tells of has happened, and a consumer can read again. An event whose `topics` field
// names a topic outside its kind's namespace (the kind up to its first dot) is refused, and
// nothing is written.
func Publish(ctx context.Context, pool *pgxpool.Pool, kind string, fields ...httpx.Field) {
	if !inNamespace(kind, fields) {
		slog.ErrorContext(ctx, "refused to publish an event with a topic outside its kind's namespace", "kind", kind)
		return
	}
	id := make([]byte, 16)
	_, _ = rand.Read(id)
	at := time.Now()
	payload, err := encode(envelope(hex.EncodeToString(id), at, kind, fields))
	if err == nil && len(payload) > PayloadMax {
		slog.ErrorContext(ctx, "refused to publish an event over the NOTIFY payload cap", "kind", kind)
		return
	}
	if err == nil {
		err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
			if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, logLock); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `INSERT INTO events (event_id, kind, at, payload)
				VALUES ($1, $2, $3, $4::jsonb)`, hex.EncodeToString(id), kind, at, payload); err != nil {
				return err
			}
			_, err := tx.Exec(ctx, `SELECT pg_notify($1, $2)`, channel, payload)
			return err
		})
	}
	if err != nil {
		slog.ErrorContext(ctx, "could not publish an event", "kind", kind, "error", err.Error())
	}
}

// Fits says whether an event of kind with fields stays within PayloadMax, whatever its id
// and time.
func Fits(kind string, fields ...httpx.Field) bool {
	// A time with microseconds is the longest that wire.Time writes.
	longest := time.Date(2000, 1, 1, 0, 0, 0, 1000, time.UTC)
	payload, err := encode(envelope(strings.Repeat("0", 32), longest, kind, fields))
	return err == nil && len(payload) <= PayloadMax
}

func envelope(id string, at time.Time, kind string, fields []httpx.Field) []httpx.Field {
	return append([]httpx.Field{
		{Key: "id", Value: id},
		{Key: "at", Value: wire.Time(at)},
		{Key: "kind", Value: kind},
	}, fields...)
}

// inNamespace says whether every topic in fields starts with the kind's namespace and a colon.
// A topics field that is not a list of strings is refused.
func inNamespace(kind string, fields []httpx.Field) bool {
	namespace, _, _ := strings.Cut(kind, ".")
	for _, f := range fields {
		if f.Key != "topics" {
			continue
		}
		topics, ok := f.Value.([]string)
		if !ok {
			return false
		}
		for _, topic := range topics {
			if !strings.HasPrefix(topic, namespace+":") {
				return false
			}
		}
	}
	return true
}

func encode(fields []httpx.Field) (string, error) {
	out := []byte{'{'}
	for i, f := range fields {
		if i > 0 {
			out = append(out, ',')
		}
		key, err := httpx.Marshal(f.Key)
		if err != nil {
			return "", err
		}
		value, err := httpx.Marshal(f.Value)
		if err != nil {
			return "", err
		}
		out = append(append(append(out, key...), ':'), value...)
	}
	return string(append(out, '}')), nil
}
