package bus

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// PublishIn publishes the event kind with fields in the caller's transaction tx, through the
// database's scadbuddy_publish_event (#6949, design §4.8): the event log's lock, held to the
// commit, the row in events, and the NOTIFY that Postgres sends on commit. The event
// commits or rolls back with the write it tells of, so an error is returned, and the caller
// rolls the write back with it (Publish only logs). It returns the event's seq.
//
// scadbuddy_publish_event is the one size rule: an event over PayloadMax is logged whole,
// and its NOTIFY leaves out old and new and says values_in_log.
func PublishIn(ctx context.Context, tx pgx.Tx, kind string, fields ...httpx.Field) (int64, error) {
	if !validEnvelope(kind, fields) {
		return 0, errors.New("bus: the event's kind or topics are not ones Python can decode")
	}
	body, err := encode(fields)
	if err != nil {
		return 0, err
	}
	var seq int64
	err = tx.QueryRow(ctx, `SELECT scadbuddy_publish_event($1, $2::jsonb)`, kind, body).Scan(&seq)
	return seq, err
}
