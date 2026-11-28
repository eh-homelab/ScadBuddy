// Package store holds every SQL statement of Go's inventory (#6949, K1): its transactions,
// the actor they write as, and the idempotency ledger that makes a retried write answer once.
package store

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"slices"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// Actor is who a write is by: a JSON object of Python's Actor (orders/models.py), as
// ReadActor writes it.
type Actor = json.RawMessage

// actorFields and actorKinds are Python's Actor: its fields in order, and its kinds. The
// history reads an actor with that strict model, so an actor with another field would
// make the history unreadable (testdata/actors.json, which a Python test checks too).
var (
	actorFields = []string{"kind", "principal", "session", "run_id"}
	actorKinds  = []string{"browser", "agent", "provider", "visitor", "customer"}
)

// ErrBadActor is an actor that Python's Actor does not read.
var ErrBadActor = errors.New("store: the actor is not an Actor object")

// ReadActor is raw as Python's Actor model_dump_json writes it: every field, in order, null
// where it is not given. A field Actor does not have, a field given twice, a kind it does
// not name, or a value that is not a string or null is ErrBadActor.
func ReadActor(raw []byte) (Actor, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	if t, err := dec.Token(); err != nil || t != json.Delim('{') {
		return nil, ErrBadActor
	}
	values := map[string]any{}
	for dec.More() {
		t, err := dec.Token()
		key, ok := t.(string)
		if err != nil || !ok || !slices.Contains(actorFields, key) {
			return nil, ErrBadActor
		}
		if _, twice := values[key]; twice {
			return nil, ErrBadActor
		}
		t, err = dec.Token()
		if _, isString := t.(string); err != nil || (t != nil && !isString) {
			return nil, ErrBadActor
		}
		values[key] = t
	}
	if _, err := dec.Token(); err != nil {
		return nil, ErrBadActor
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, ErrBadActor
	}
	if kind, _ := values["kind"].(string); !slices.Contains(actorKinds, kind) {
		return nil, ErrBadActor
	}
	var b bytes.Buffer
	b.WriteByte('{')
	for i, field := range actorFields {
		if i > 0 {
			b.WriteByte(',')
		}
		value, err := httpx.Marshal(values[field])
		if err != nil {
			return nil, err
		}
		b.WriteString(`"` + field + `":`)
		b.Write(value)
	}
	b.WriteByte('}')
	return b.Bytes(), nil
}

// Tx runs fn in one transaction written as actor. Before fn, and so before any row lock, it
// takes the event log's lock (bus.HoldLog) and sets scadbuddy.actor for the transaction, as
// Python's entities/actor.py set_actor does: a row whose trigger publishes, or a direct
// publish, then keeps the order of core/pg_events.py ("Lock order"), and the history
// triggers name actor. A bad actor is ErrBadActor, and nothing runs.
func Tx(ctx context.Context, pool *pgxpool.Pool, actor Actor, fn func(pgx.Tx) error) error {
	actor, err := ReadActor(actor)
	if err != nil {
		return err
	}
	return pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		if err := bus.HoldLog(ctx, tx); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `SELECT set_config('scadbuddy.actor', $1, true)`, string(actor)); err != nil {
			return err
		}
		return fn(tx)
	})
}
