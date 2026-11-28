package publish

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"slices"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

// Execer is a transaction, or anything else that runs a statement.
type Execer interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

var _ Execer = pgx.Tx(nil)

// Notify sends a bus event of kind with fields on EventsChannel in q, as core/events.py
// encodes one: {"id", "at", "kind", then fields by name}, written by ASCII, so decode_event
// reads it. In a transaction, Postgres sends it at the commit, and never on a rollback. It
// is a notice only, with no row in the event log, as the agent's ai_operation.changed is
// (agent_changes/hub.py NOTIFY_ONLY).
func Notify(ctx context.Context, q Execer, kind string, fields map[string]any) error {
	id := make([]byte, 16)
	_, _ = rand.Read(id)
	text, err := payload(hex.EncodeToString(id), time.Now(), kind, fields)
	if err != nil {
		return err
	}
	_, err = q.Exec(ctx, `SELECT pg_notify($1, $2)`, EventsChannel, text)
	return err
}

// payload is the event's JSON: id, at and kind first, then fields by name.
func payload(id string, at time.Time, kind string, fields map[string]any) (string, error) {
	out := []byte(`{"id":`)
	for i, f := range append([]httpx.Field{{Key: "id", Value: id}, {Key: "at", Value: wire.Time(at)}, {Key: "kind", Value: kind}}, sorted(fields)...) {
		if i > 0 {
			out = append(out, `,"`+f.Key+`":`...)
		}
		value, err := httpx.Marshal(f.Value)
		if err != nil {
			return "", err
		}
		out = append(out, value...)
	}
	return ASCII(append(out, '}')), nil
}

func sorted(fields map[string]any) []httpx.Field {
	out := make([]httpx.Field, 0, len(fields))
	for k, v := range fields {
		out = append(out, httpx.Field{Key: k, Value: v})
	}
	slices.SortFunc(out, func(a, b httpx.Field) int {
		if a.Key < b.Key {
			return -1
		}
		if a.Key > b.Key {
			return 1
		}
		return 0
	})
	return out
}
