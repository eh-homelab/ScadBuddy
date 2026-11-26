// Package flow is the inventory's base flow (design §4.1, #6949 K1): every stock write
// reads its item, resolves the step's handler and parameters (handlers.Catalog.Resolve),
// finds the handler's implementation in the Registry and calls it in the write's
// transaction. It names no kind and no special type (design §2): what a kind does is in
// its class and its steps.
//
// Each write method runs in the caller's transaction, one of store.Write (an activity) or
// store.Tx, which takes the event log's lock and sets the actor before any row lock. A
// refusal is a *store.Refused; any other error is a failure the caller retries.
package flow

import (
	"context"
	"errors"
	"fmt"
	"math/big"
	"slices"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// Flow is the base flow over a database and a registry of handlers.
type Flow struct {
	Pool *pgxpool.Pool
	Reg  *handlers.Registry
}

// New is the base flow with K1's handlers (handlers.Default over store.Stock).
func New(pool *pgxpool.Pool) *Flow {
	return &Flow{Pool: pool, Reg: handlers.Default(store.Stock{})}
}

// Moved is what a Repoint moved (store.Moved).
type Moved = store.Moved

// call is one step on its target: the item locked (FOR SHARE when lock) and read with its
// chain and the unit's steps, the step resolved, and its handler's implementation. A
// blocking problem of the step is 422 steps_invalid; a missing required parameter is 409
// step_incomplete (R-K1-8); a handler with no implementation is 409 handler_not_built
// (R-K1-7).
func (f *Flow) call(ctx context.Context, q pgx.Tx, step handlers.Step, at store.Target, lock bool) (wire.Item, handlers.Choice, any, error) {
	sub, err := store.Lock(ctx, q, at, lock)
	if err != nil {
		return wire.Item{}, handlers.Choice{}, nil, err
	}
	res, problems, err := f.Reg.Catalog.Resolve(sub.Chain, sub.Item.Steps, sub.Unit)
	if err != nil {
		return wire.Item{}, handlers.Choice{}, nil, store.ClassRefusal(err)
	}
	var blocking []handlers.Problem
	missing := []string{}
	for _, p := range problems {
		switch {
		case p.Step != step:
		case p.Reason == handlers.MissingParam:
			missing = append(missing, p.Missing...)
		default:
			blocking = append(blocking, p)
		}
	}
	if err := store.StepsRefusal(blocking); err != nil {
		return wire.Item{}, handlers.Choice{}, nil, err
	}
	choice, ok := res.Steps[step]
	if !ok {
		return wire.Item{}, handlers.Choice{}, nil, &store.Refused{Status: 409, Code: "step_not_applicable",
			Detail: fmt.Sprintf("the item's class has no %s step: %s", step, sub.Item.ID),
			Fields: map[string]any{"step": string(step), "class": sub.Item.Class}}
	}
	if len(missing) > 0 {
		slices.Sort(missing)
		return wire.Item{}, handlers.Choice{}, nil, &store.Refused{Status: 409, Code: "step_incomplete",
			Detail: fmt.Sprintf("the %s step's handler %s lacks a required parameter: %s", step, choice.Handler, sub.Item.ID),
			Fields: map[string]any{"step": string(step), "handler": choice.Handler, "missing": missing}}
	}
	impl, err := f.Reg.For(step, choice.Handler)
	var notBuilt handlers.ErrNotBuilt
	if errors.As(err, &notBuilt) {
		return wire.Item{}, handlers.Choice{}, nil, &store.Refused{Status: 409, Code: "handler_not_built",
			Detail: fmt.Sprintf("the %s step's handler %s is not built yet: %s", step, choice.Handler, sub.Item.ID),
			Fields: map[string]any{"step": string(step), "handler": choice.Handler}}
	}
	if err != nil {
		return wire.Item{}, handlers.Choice{}, nil, err
	}
	return sub.Item, choice, impl, nil
}

// as is impl as the step's interface; a handler registered for a step it does not
// implement is a programming error.
func as[T any](impl any, step handlers.Step) (T, error) {
	h, ok := impl.(T)
	if !ok {
		return h, fmt.Errorf("flow: the %s handler %T does not implement its step", step, impl)
	}
	return h, nil
}

// Receive is the receive step: in.Lot on a lots route, in.Units on a units route (the
// other is 409 wrong_counting). The flow sets in.Item and in.Choice.
func (f *Flow) Receive(ctx context.Context, tx pgx.Tx, itemID string, in handlers.ReceiveIn) (handlers.ReceiveOut, error) {
	const step = "receive"
	item, choice, impl, err := f.call(ctx, tx, step, store.Target{Item: itemID}, true)
	if err != nil {
		return handlers.ReceiveOut{}, err
	}
	h, err := as[handlers.Receiver](impl, step)
	if err != nil {
		return handlers.ReceiveOut{}, err
	}
	in.Item, in.Choice = item, choice
	return h.Receive(ctx, tx, in)
}

// Consume is the consume step: a use, or an order's take (in.Use names the order).
func (f *Flow) Consume(ctx context.Context, tx pgx.Tx, itemID string, in handlers.ConsumeIn) ([]wire.Move, error) {
	const step = "consume"
	item, choice, impl, err := f.call(ctx, tx, step, store.Target{Item: itemID}, true)
	if err != nil {
		return nil, err
	}
	h, err := as[handlers.Consumer](impl, step)
	if err != nil {
		return nil, err
	}
	in.Item, in.Choice = item, choice
	return h.Consume(ctx, tx, in)
}

// Count is the count step: of the whole item (at.Item, in.Count), or of one lot (at.Lot,
// in.Adjust).
func (f *Flow) Count(ctx context.Context, tx pgx.Tx, at store.Target, in handlers.CountIn) ([]wire.Move, error) {
	const step = "count"
	item, choice, impl, err := f.call(ctx, tx, step, at, true)
	if err != nil {
		return nil, err
	}
	h, err := as[handlers.Counter](impl, step)
	if err != nil {
		return nil, err
	}
	in.Item, in.Choice, in.LotID = item, choice, at.Lot
	return h.Count(ctx, tx, in)
}

// Move is the move step: a new place for the lot (at.Lot) or the unit (at.Unit).
func (f *Flow) Move(ctx context.Context, tx pgx.Tx, at store.Target, in handlers.MoveIn) error {
	const step = "move"
	item, choice, impl, err := f.call(ctx, tx, step, at, true)
	if err != nil {
		return err
	}
	h, err := as[handlers.Mover](impl, step)
	if err != nil {
		return err
	}
	in.Item, in.Choice, in.LotID, in.UnitID = item, choice, at.Lot, at.Unit
	return h.Move(ctx, tx, in)
}

// Retire is the retire step: the unit's new status.
func (f *Flow) Retire(ctx context.Context, tx pgx.Tx, unitID string, in handlers.RetireIn) (wire.Unit, error) {
	const step = "retire"
	item, choice, impl, err := f.call(ctx, tx, step, store.Target{Unit: unitID}, true)
	if err != nil {
		return wire.Unit{}, err
	}
	h, err := as[handlers.Retirer](impl, step)
	if err != nil {
		return wire.Unit{}, err
	}
	in.Item, in.Choice, in.UnitID = item, choice, unitID
	return h.Retire(ctx, tx, in)
}

// Estimate is the cost step's estimate: what a take of qty would charge per unit now, and
// whether any of it is of unknown cost. It reads only, in a read-only transaction of its
// own.
func (f *Flow) Estimate(ctx context.Context, itemID string, qty *big.Rat) (*big.Rat, bool, error) {
	const step = "cost"
	var unit *big.Rat
	var unknown bool
	err := pgx.BeginTxFunc(ctx, f.Pool, pgx.TxOptions{AccessMode: pgx.ReadOnly}, func(q pgx.Tx) error {
		item, _, impl, err := f.call(ctx, q, step, store.Target{Item: itemID}, false)
		if err != nil {
			return err
		}
		h, err := as[handlers.Coster](impl, step)
		if err != nil {
			return err
		}
		unit, unknown, err = h.Estimate(ctx, q, item.ID, qty)
		return err
	})
	return unit, unknown, err
}

// GiveBack is phase 2 give_back, not a step: one return per use of the order item and
// item not yet reversed, at the use's own cost.
func (f *Flow) GiveBack(ctx context.Context, tx pgx.Tx, itemID, orderItemID string, by store.Actor) ([]wire.Move, error) {
	return store.GiveBack(ctx, tx, itemID, orderItemID, by)
}

// Recost is phase 2 recost_lot, not a step: the lot's past uses, returns and adjusts take
// its cost now. orders are the orders the caller locked; a move of another is 409
// recost_orders_moved.
func (f *Flow) Recost(ctx context.Context, tx pgx.Tx, lotID string, orders []string) (wire.RecostResult, error) {
	return store.Recost(ctx, tx, lotID, orders)
}

// SetLotCost is phase 2 set_lot_cost, not a step: the lot's cost now, known; no move
// changes.
func (f *Flow) SetLotCost(ctx context.Context, tx pgx.Tx, lotID string, cost wire.Decimal) (wire.Lot, error) {
	return store.SetLotCost(ctx, tx, lotID, cost)
}

// Repoint moves the stock rows that name kind:gone to kind:kept (store.Repoint); with only,
// just those rows, and never a refusal.
func (f *Flow) Repoint(ctx context.Context, tx pgx.Tx, kind, gone, kept string, only *Moved) (Moved, error) {
	return store.Repoint(ctx, tx, kind, gone, kept, only)
}

// LotUpdate is phase 2 update_lot in one transaction (C30): the note (store.UpdateLot, which
// locks the lot as update_lot does), the cost (SetLotCost), and the place (the move step).
func (f *Flow) LotUpdate(ctx context.Context, tx pgx.Tx, lotID string, p wire.LotPatch, by store.Actor) (wire.Lot, error) {
	if err := store.UpdateLot(ctx, tx, lotID, p.Note); err != nil {
		return wire.Lot{}, err
	}
	if p.UnitCostMinor != nil {
		if _, err := store.SetLotCost(ctx, tx, lotID, *p.UnitCostMinor); err != nil {
			return wire.Lot{}, err
		}
	}
	if p.LocationID.Set {
		if err := f.Move(ctx, tx, store.Target{Lot: lotID}, handlers.MoveIn{LocationID: p.LocationID.Value, By: by}); err != nil {
			return wire.Lot{}, err
		}
	}
	l, _, err := store.Lot(ctx, tx, lotID)
	return l, err
}

// UnitUpdate is phase 2 update_unit in one transaction (C30): the name and the serial
// (store.UpdateUnit), and the place (the move step).
func (f *Flow) UnitUpdate(ctx context.Context, tx pgx.Tx, unitID string, p wire.UnitPatch, by store.Actor) (wire.Unit, error) {
	if err := store.UpdateUnit(ctx, tx, unitID, p.Name, p.Serial); err != nil {
		return wire.Unit{}, err
	}
	if p.LocationID.Set {
		if err := f.Move(ctx, tx, store.Target{Unit: unitID}, handlers.MoveIn{LocationID: p.LocationID.Value, By: by}); err != nil {
			return wire.Unit{}, err
		}
	}
	u, _, err := store.Unit(ctx, tx, unitID)
	return u, err
}
