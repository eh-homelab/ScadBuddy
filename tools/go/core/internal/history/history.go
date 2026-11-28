// Package history is the history subscriber (#6949, design §4.8 "the subscriber's
// cursor"). The event stream is the history: a database trigger publishes each property
// set and remove, and each property schema change, in the writer's transaction. This
// subscriber reads the event log from its durable cursor (event_subscribers) and inserts
// one history row per event, keyed by the event's seq, so a replay inserts nothing twice.
// It never skips a history event: a payload that does not fit its table stops the cursor
// just before it, and event_subscribers.stopped_seq names that event until the cursor
// passes it (#8074), so the API's scrape can tell a stopped cursor from a slow one.
package history

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Options say which cursor to move and how. The zero value takes the defaults.
type Options struct {
	// Name is the subscriber's row in event_subscribers; "history" when empty.
	Name string
	// Batch is the most events one Apply reads; 500 when 0.
	Batch int
	// Poll is how long Run waits after a short batch or an error; 2 s when 0.
	Poll time.Duration
}

func (o Options) withDefaults() Options {
	if o.Name == "" {
		o.Name = "history"
	}
	if o.Batch <= 0 {
		o.Batch = 500
	}
	if o.Poll <= 0 {
		o.Poll = 2 * time.Second
	}
	return o
}

// The history kinds this subscriber writes, each to its table. handler_value.changed gets
// its table with handler_values in K2 (design §4.7); until then the cursor passes it, as
// any other kind.
const (
	propertyChanged = "entity_property.changed"
	schemaChanged   = "entity_property_schema.changed"
	// configChanged is the inventory's parameter history (R-K1-13): a step's resolved
	// handler and parameters, before and after a save (inventory/store publishConfig).
	configChanged = "handler_config.changed"
)

// Apply reads, in one transaction, at most Batch events past the cursor, inserts a row for
// each history event, and moves the cursor past every event it read. It returns how many
// events the cursor passed: 0 when another replica holds the cursor. A payload that does
// not fit its table is an error that names its seq; the events before it are applied, the
// cursor stops just before it, and stopped_seq is set to its seq. stopped_seq is cleared
// once an Apply stops at no event.
func Apply(ctx context.Context, pool *pgxpool.Pool, o Options) (applied int, err error) {
	o = o.withDefaults()
	tx, err := pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(ctx) // a no-op after the commit
	var cursor int64
	var wasStopped *int64
	err = tx.QueryRow(ctx, `SELECT seq, stopped_seq FROM event_subscribers WHERE name = $1
		FOR UPDATE SKIP LOCKED`, o.Name).Scan(&cursor, &wasStopped)
	if errors.Is(err, pgx.ErrNoRows) {
		var exists bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM event_subscribers WHERE name = $1)`,
			o.Name).Scan(&exists); err != nil {
			return 0, err
		}
		if !exists {
			return 0, &noSubscriber{name: o.Name}
		}
		return 0, nil // another replica applies
	}
	if err != nil {
		return 0, err
	}
	events, err := read(ctx, tx, cursor, o.Batch)
	if err != nil {
		return 0, err
	}
	last := cursor
	var stopped error
	var stoppedSeq *int64
	for _, e := range events {
		if err := insert(ctx, tx, e); err != nil {
			var bad *badPayload
			if !errors.As(err, &bad) {
				return 0, err
			}
			stopped = err
			stoppedSeq = &bad.seq
			break
		}
		last = e.seq
		applied++
	}
	if last != cursor || !sameSeq(wasStopped, stoppedSeq) {
		if _, err := tx.Exec(ctx, `UPDATE event_subscribers SET seq = $2, stopped_seq = $3,
			updated_at = clock_timestamp() WHERE name = $1`, o.Name, last, stoppedSeq); err != nil {
			return 0, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	return applied, stopped
}

func sameSeq(a, b *int64) bool {
	return (a == nil && b == nil) || (a != nil && b != nil && *a == *b)
}

// unmigrated is an Apply error that the API's migrations have not run yet: a table or
// column of the event log is missing, or the subscriber has no row.
func unmigrated(err error) bool {
	var pg *pgconn.PgError
	if errors.As(err, &pg) && (pg.Code == "42P01" || pg.Code == "42703") {
		return true
	}
	var none *noSubscriber
	return errors.As(err, &none)
}

// noSubscriber is an Apply of a name event_subscribers has no row for.
type noSubscriber struct{ name string }

func (n *noSubscriber) Error() string {
	return fmt.Sprintf("history: event_subscribers has no subscriber %q", n.name)
}

// Run applies until ctx ends: at once while a batch comes back full, else after Poll. An
// error is logged, and Apply runs again after Poll. Before the API's migrations have made
// the event log, it logs that once, at warn, until an Apply ends any other way: a success
// or another error (#8116, #8463). An event it cannot store is logged once, at error,
// until the subscriber passes it or stops at another one: the subscriber's stopped_seq and
// its alert say that it is still stuck, so a log every Poll adds nothing (#8256). Every
// replica runs it; the cursor's row lock lets one apply at a time.
func Run(ctx context.Context, pool *pgxpool.Pool, o Options) {
	o = o.withDefaults()
	waiting := false
	var stuck *int64
	for {
		applied, err := Apply(ctx, pool, o)
		if !unmigrated(err) {
			waiting = false
		}
		var bad *badPayload
		if !errors.As(err, &bad) {
			stuck = nil
		}
		if err != nil && ctx.Err() == nil && unmigrated(err) {
			if !waiting {
				slog.WarnContext(ctx, "the history subscriber waits for the API's migrations",
					"subscriber", o.Name, "error", err.Error())
				waiting = true
			}
		} else if err != nil && ctx.Err() == nil {
			if bad != nil {
				if stuck == nil || *stuck != bad.seq {
					slog.ErrorContext(ctx, "the history subscriber stopped at an event it cannot store",
						"subscriber", o.Name, "seq", bad.seq, "error", err.Error())
					stuck = &bad.seq
				}
			} else {
				slog.ErrorContext(ctx, "the history subscriber could not apply events",
					"subscriber", o.Name, "error", err.Error())
			}
		}
		if err == nil && applied >= o.Batch {
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(o.Poll):
		}
	}
}

type event struct {
	seq     int64
	kind    string
	payload []byte
}

func read(ctx context.Context, tx pgx.Tx, after int64, limit int) ([]event, error) {
	rows, err := tx.Query(ctx, `SELECT seq, kind, payload FROM events WHERE seq > $1 ORDER BY seq LIMIT $2`,
		after, limit)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (event, error) {
		var e event
		err := row.Scan(&e.seq, &e.kind, &e.payload)
		return e, err
	})
}

// badPayload is a history event whose payload does not fit its table.
type badPayload struct {
	seq    int64
	reason string
}

func (b *badPayload) Error() string {
	return fmt.Sprintf("history: event %d does not fit its table: %s", b.seq, b.reason)
}

// The fields of a history payload. A pointer or raw value is nil when the field is
// missing; json.RawMessage keeps a JSON null as the bytes "null".
type payload struct {
	At        *string         `json:"at"`
	ChangedAt *string         `json:"changed_at"`
	EntityID  *string         `json:"entity_id"`
	Kind      *string         `json:"entity_kind"`
	Ref       *string         `json:"ref"`
	Key       *string         `json:"key"`
	Op        *string         `json:"op"`
	Owner     *string         `json:"owner"`
	Step      *string         `json:"step"`
	Handler   json.RawMessage `json:"handler"`
	Old       json.RawMessage `json:"old"`
	New       json.RawMessage `json:"new"`
	Source    json.RawMessage `json:"source"`
	Actor     json.RawMessage `json:"actor"`
}

func insert(ctx context.Context, tx pgx.Tx, e event) error {
	if e.kind != propertyChanged && e.kind != schemaChanged && e.kind != configChanged {
		return nil
	}
	bad := func(reason string) error { return &badPayload{seq: e.seq, reason: reason} }
	var p payload
	if err := json.Unmarshal(e.payload, &p); err != nil {
		return bad(err.Error())
	}
	stamp := p.ChangedAt
	if stamp == nil {
		stamp = p.At
	}
	if stamp == nil {
		return bad("no at")
	}
	at, err := time.Parse(time.RFC3339Nano, *stamp)
	if err != nil {
		return bad("at is not a time")
	}
	if !object(p.Actor) {
		return bad("actor is not an object")
	}
	if e.kind == configChanged {
		if p.Owner == nil {
			return bad("no owner")
		}
		if p.Step == nil {
			return bad("no step")
		}
		var handler *string
		if h := value(p.Handler); h != nil {
			if err := json.Unmarshal(h, &handler); err != nil {
				return bad("handler is not a string")
			}
		}
		_, err := tx.Exec(ctx, `INSERT INTO handler_config_history (event_seq, owner, step, handler, old, new, at, actor)
			VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::timestamptz, $8::jsonb) ON CONFLICT (event_seq) DO NOTHING`,
			e.seq, *p.Owner, *p.Step, handler, value(p.Old), value(p.New), at, []byte(p.Actor))
		return stored(e.seq, err)
	}
	if e.kind == schemaChanged {
		if p.Owner == nil {
			return bad("no owner")
		}
		if value(p.New) == nil {
			return bad("no new")
		}
		_, err := tx.Exec(ctx, `INSERT INTO entity_property_schema_history (event_seq, owner, old, new, at, actor)
			VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::timestamptz, $6::jsonb) ON CONFLICT (event_seq) DO NOTHING`,
			e.seq, *p.Owner, value(p.Old), value(p.New), at, []byte(p.Actor))
		return stored(e.seq, err)
	}
	for _, f := range []struct {
		name  string
		value *string
	}{{"entity_id", p.EntityID}, {"entity_kind", p.Kind}, {"ref", p.Ref}, {"key", p.Key}, {"op", p.Op}} {
		if f.value == nil {
			return bad("no " + f.name)
		}
	}
	if !uuidPattern.MatchString(*p.EntityID) {
		return bad("entity_id is not a uuid")
	}
	if *p.Op != "set" && *p.Op != "remove" {
		return bad("op is " + *p.Op)
	}
	var source *string
	if s := value(p.Source); s != nil {
		if err := json.Unmarshal(s, &source); err != nil {
			return bad("source is not a string")
		}
	}
	_, err = tx.Exec(ctx, `INSERT INTO entity_property_history
		(event_seq, entity_id, kind, ref, key, op, old, new, source, at, actor)
		VALUES ($1, $2::uuid, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10::timestamptz, $11::jsonb)
		ON CONFLICT (event_seq) DO NOTHING`,
		e.seq, *p.EntityID, *p.Kind, *p.Ref, *p.Key, *p.Op, value(p.Old), value(p.New), source, at,
		[]byte(p.Actor))
	return stored(e.seq, err)
}

// stored names the event's seq in a failed insert's error. That error rolls the batch back,
// and the cursor stays.
func stored(seq int64, err error) error {
	if err != nil {
		return fmt.Errorf("history: could not store event %d: %w", seq, err)
	}
	return nil
}

var uuidPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// value is raw as a jsonb parameter: nil (SQL NULL) for a missing field or a JSON null.
func value(raw json.RawMessage) []byte {
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return nil
	}
	return raw
}

func object(raw json.RawMessage) bool {
	return len(raw) > 0 && raw[0] == '{'
}
