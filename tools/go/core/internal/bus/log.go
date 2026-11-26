package bus

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
)

// ReplayMax is core/pg_events.py MAX_REPLAY_LIMIT: the most rows one Replay reads.
const ReplayMax = 1000

// Logged is one row of the event log: its place and its payload as the jsonb gives it back.
type Logged struct {
	Seq  int64
	Text string
}

// Newest is the id of the newest logged event, "" while the log is empty
// (PgNotifyEventBus.newest).
func (h *Hub) Newest(ctx context.Context) (string, error) {
	var id string
	err := h.pool.QueryRow(ctx, `SELECT event_id FROM events ORDER BY seq DESC LIMIT 1`).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil
	}
	return id, err
}

// Position is the seq of the event with id; ok is false when it was never logged or has
// been pruned (PgNotifyEventBus.position).
func (h *Hub) Position(ctx context.Context, id string) (seq int64, ok bool, err error) {
	err = h.pool.QueryRow(ctx, `SELECT seq FROM events WHERE event_id = $1`, id).Scan(&seq)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, false, nil
	}
	return seq, err == nil, err
}

// Positions is Position for several ids in one query; an unknown or pruned id is absent.
func (h *Hub) Positions(ctx context.Context, ids []string) (map[string]int64, error) {
	rows, err := h.pool.Query(ctx, `SELECT event_id, seq FROM events WHERE event_id = ANY($1)`, ids)
	if err != nil {
		return nil, err
	}
	out := map[string]int64{}
	var id string
	var seq int64
	_, err = pgx.ForEachRow(rows, []any{&id, &seq}, func() error {
		out[id] = seq
		return nil
	})
	return out, err
}

// Replay is the logged events with seq > after, oldest first, at most limit, and whether
// some of what followed after was pruned (or may have been: a rolled-back publish also skips
// a number). The gap check and the page read one snapshot (PgNotifyEventBus.replay).
func (h *Hub) Replay(ctx context.Context, after int64, limit int) (page []Logged, gap bool, err error) {
	if limit < 1 || limit > ReplayMax || after < 0 {
		return nil, false, errors.New("replay out of range")
	}
	err = pgx.BeginTxFunc(ctx, h.pool, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly},
		func(tx pgx.Tx) error {
			var floor int64
			if err := tx.QueryRow(ctx, `SELECT coalesce((SELECT min(seq) FROM events),
				coalesce(pg_sequence_last_value(pg_get_serial_sequence('events', 'seq')), 0) + 1)`).Scan(&floor); err != nil {
				return err
			}
			gap = after < floor-1
			rows, err := tx.Query(ctx, `SELECT seq, payload::text FROM events WHERE seq > $1 ORDER BY seq LIMIT $2`,
				after, limit)
			if err != nil {
				return err
			}
			var row Logged
			_, err = pgx.ForEachRow(rows, []any{&row.Seq, &row.Text}, func() error {
				page = append(page, row)
				return nil
			})
			return err
		})
	return page, gap, err
}
