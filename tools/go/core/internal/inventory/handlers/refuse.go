package handlers

import (
	"context"
	"fmt"
	"math/big"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/problem"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// refuse is the handler of a step a class does not have (a service's receive, a durable's
// count): every call is 409 step_not_applicable with the step and the class. It replaces
// phase 2's item_not_counted (R-K1-23).
type refuse struct{}

func notApplicable(step Step, item wire.Item) error {
	return &problem.Refused{Status: 409, Code: "step_not_applicable",
		Detail: fmt.Sprintf("the item's class has no %s step: %s", step, item.ID),
		Fields: map[string]any{"step": string(step), "class": item.Class}}
}

func (refuse) Receive(_ context.Context, _ pgx.Tx, in ReceiveIn) (ReceiveOut, error) {
	return ReceiveOut{}, notApplicable("receive", in.Item)
}

func (refuse) Consume(_ context.Context, _ pgx.Tx, in ConsumeIn) ([]wire.Move, error) {
	return nil, notApplicable("consume", in.Item)
}

func (refuse) Count(_ context.Context, _ pgx.Tx, in CountIn) ([]wire.Move, error) {
	return nil, notApplicable("count", in.Item)
}

func (refuse) Move(_ context.Context, _ pgx.Tx, in MoveIn) error {
	return notApplicable("move", in.Item)
}

func (refuse) Retire(_ context.Context, _ pgx.Tx, in RetireIn) (wire.Unit, error) {
	return wire.Unit{}, notApplicable("retire", in.Item)
}

func (refuse) Estimate(_ context.Context, _ pgx.Tx, item string, _ *big.Rat) (*big.Rat, bool, error) {
	return nil, false, notApplicable("cost", wire.Item{ID: item})
}

func (refuse) Life(_ context.Context, _ pgx.Tx, in LifeIn) error {
	return notApplicable("life", in.Item)
}

func (refuse) Bill(_ context.Context, _ pgx.Tx, in BillIn) error {
	return notApplicable("bill", in.Item)
}

func (refuse) Label(_ context.Context, _ pgx.Tx, in LabelIn) error {
	return notApplicable("label", in.Item)
}

func (refuse) Sync(_ context.Context, _ pgx.Tx, in SyncIn) error {
	return notApplicable("sync", in.Item)
}

// wrongCounting is a lots call on an item counted in units, or the reverse (R-K1-23,
// phase 2's wrong_tracking).
func wrongCounting(item wire.Item) error {
	return &problem.Refused{Status: 409, Code: "wrong_counting",
		Detail: "a lot call on an item counted in units, or a unit call on one counted in lots: " + item.ID}
}
