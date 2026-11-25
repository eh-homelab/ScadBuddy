// Package bus publishes an event on the API's event bus as core/pg_events.py write_event
// does: the event log's lock, the row in events, and the NOTIFY on scadbuddy_events, in a
// transaction of its own after the change it tells of has committed.
package bus

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"log/slog"
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
	// payloadMax is pg_events.py MAX_PAYLOAD_BYTES, half of NOTIFY's cap.
	payloadMax = 4000
)

// Publish logs and NOTIFYs the event kind with fields, which follow id, at and kind in its
// JSON. As core/events.py emit, a failure is logged and never reaches the caller: the change
// it tells of has happened, and a consumer can read again.
func Publish(ctx context.Context, pool *pgxpool.Pool, kind string, fields ...httpx.Field) {
	id := make([]byte, 16)
	_, _ = rand.Read(id)
	at := time.Now()
	event := append([]httpx.Field{
		{Key: "id", Value: hex.EncodeToString(id)},
		{Key: "at", Value: wire.Time(at)},
		{Key: "kind", Value: kind},
	}, fields...)
	payload, err := encode(event)
	if err == nil && len(payload) > payloadMax {
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
