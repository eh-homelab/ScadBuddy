package store

import (
	"bytes"
	"context"
	"encoding/json"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
)

// configChanged is the parameter history's event (design §4.7, R-K1-13): one per step whose
// resolved handler or parameters a save changed. The history subscriber writes each to
// handler_config_history.
const configChanged = "handler_config.changed"

// publishConfig publishes, in the save's transaction (bus.PublishIn), one configChanged for
// each step whose handler or parameters differ between before and after, the owner's
// resolved steps (owner is item_kind:<id>, item:<uuid> or unit:<uuid>). old and new are the
// resolved parameters (null for a step that did not resolve); handler is the new one. A
// save that rolls back takes its events with it.
func publishConfig(ctx context.Context, tx pgx.Tx, owner string, before, after map[handlers.Step]handlers.Choice, by Actor) error {
	actor, err := ReadActor(by)
	if err != nil {
		return err
	}
	for _, step := range handlers.Steps {
		b, had := before[step]
		a, has := after[step]
		oldParams, err := params(b, had)
		if err != nil {
			return err
		}
		newParams, err := params(a, has)
		if err != nil {
			return err
		}
		if had == has && b.Handler == a.Handler && bytes.Equal(oldParams, newParams) {
			continue
		}
		var handler any
		if has {
			handler = a.Handler
		}
		if _, err := bus.PublishIn(ctx, tx, configChanged,
			httpx.Field{Key: "owner", Value: owner},
			httpx.Field{Key: "step", Value: string(step)},
			httpx.Field{Key: "handler", Value: handler},
			httpx.Field{Key: "old", Value: oldParams},
			httpx.Field{Key: "new", Value: newParams},
			httpx.Field{Key: "actor", Value: json.RawMessage(actor)},
			httpx.Field{Key: "retention_seconds", Value: 0},
		); err != nil {
			return err
		}
	}
	return nil
}

// params is a choice's parameters as JSON with sorted keys: null for no choice, {} for none.
func params(c handlers.Choice, ok bool) (json.RawMessage, error) {
	if !ok {
		return json.RawMessage("null"), nil
	}
	if c.Params == nil {
		return json.RawMessage("{}"), nil
	}
	return json.Marshal(c.Params)
}
