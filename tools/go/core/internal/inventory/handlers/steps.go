package handlers

import (
	"context"
	"encoding/json"
	"math/big"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// The per-step interfaces (README §8's class diagram): a handler implements the interface
// of each step it fills. The base flow resolves a step's handler, finds its implementation
// in the Registry and calls it in the write's transaction. Each In carries the item (the
// fields the flow reads: id, kind, kind path, uom, steps, class and special type), the
// step's Choice (its parameters), who the write is by (By, a store.Actor), and the phase 2
// input of the route or activity that called the step.

// ReceiveIn is a receive: a lot (ReceiveLot, a lots route) or units (ReceiveUnits, a units
// route), and the shop's currency, which a lot's cost is in unless it names another.
type ReceiveIn struct {
	Item   wire.Item
	Choice Choice
	By     json.RawMessage
	Lot    *wire.ReceiveLot
	Units  *wire.ReceiveUnits
	Shop   string
}

// ReceiveOut is what a receive made: the lot, or the units.
type ReceiveOut struct {
	Lot   *wire.Lot   `json:"lot,omitempty"`
	Units []wire.Unit `json:"units,omitempty"`
}

// ConsumeIn is a use (UseInput, with the order and order item of an order's take).
type ConsumeIn struct {
	Item   wire.Item
	Choice Choice
	By     json.RawMessage
	Use    wire.UseInput
}

// CountIn is a count of the whole item (Count) or of one lot (LotID and Adjust).
type CountIn struct {
	Item   wire.Item
	Choice Choice
	By     json.RawMessage
	Count  *wire.CountInput
	LotID  string
	Adjust *wire.AdjustInput
}

// MoveIn is a new place (LotPatch.location_id or UnitPatch.location_id; nil clears it) for
// one lot or one unit.
type MoveIn struct {
	Item       wire.Item
	Choice     Choice
	By         json.RawMessage
	LotID      string
	UnitID     string
	LocationID *string
}

// RetireIn is a unit's new status (UnitStatusInput).
type RetireIn struct {
	Item   wire.Item
	Choice Choice
	By     json.RawMessage
	UnitID string
	Status wire.UnitStatusInput
}

// LifeIn, BillIn, LabelIn and SyncIn are the steps K1 builds no handler for (K2 to K6).
type (
	LifeIn struct {
		Item   wire.Item
		Choice Choice
		By     json.RawMessage
	}
	BillIn  LifeIn
	LabelIn LifeIn
	SyncIn  LifeIn
)

// Receiver fills the receive step.
type Receiver interface {
	Receive(ctx context.Context, tx pgx.Tx, in ReceiveIn) (ReceiveOut, error)
}

// Consumer fills the consume step.
type Consumer interface {
	Consume(ctx context.Context, tx pgx.Tx, in ConsumeIn) ([]wire.Move, error)
}

// Counter fills the count step.
type Counter interface {
	Count(ctx context.Context, tx pgx.Tx, in CountIn) ([]wire.Move, error)
}

// Mover fills the move step.
type Mover interface {
	Move(ctx context.Context, tx pgx.Tx, in MoveIn) error
}

// Retirer fills the retire step.
type Retirer interface {
	Retire(ctx context.Context, tx pgx.Tx, in RetireIn) (wire.Unit, error)
}

// Coster fills the cost step: what a take of qty would charge per unit now, and whether
// any of it is of unknown cost. It reads only.
type Coster interface {
	Estimate(ctx context.Context, q pgx.Tx, item string, qty *big.Rat) (unit *big.Rat, unknown bool, err error)
}

// Lifer fills the life step (K2).
type Lifer interface {
	Life(ctx context.Context, tx pgx.Tx, in LifeIn) error
}

// Biller fills the bill step (K5).
type Biller interface {
	Bill(ctx context.Context, tx pgx.Tx, in BillIn) error
}

// Labeler fills the label step.
type Labeler interface {
	Label(ctx context.Context, tx pgx.Tx, in LabelIn) error
}

// Syncer fills the sync step (K3, K6).
type Syncer interface {
	Sync(ctx context.Context, tx pgx.Tx, in SyncIn) error
}

// Stock is the stock store (store.Stock) the K1 defaults write through: phase 2
// inventory/stock.py, statement for statement, with its locks (R-K1-1). The store holds
// every SQL statement; it is an interface here because the store's saves import this
// package.
type Stock interface {
	// ReceiveLot is receive_in: a lot and its receive move.
	ReceiveLot(ctx context.Context, tx pgx.Tx, item wire.Item, in wire.ReceiveLot, shop string, by json.RawMessage) (wire.Lot, error)
	// ReceiveUnits is receive_units: units in_stock, each with a receive move.
	ReceiveUnits(ctx context.Context, tx pgx.Tx, item wire.Item, in wire.ReceiveUnits, by json.RawMessage) ([]wire.Unit, error)
	// Take is use and take: the named lot first, then FIFO, a shortfall on the newest lot.
	Take(ctx context.Context, tx pgx.Tx, item wire.Item, in wire.UseInput, by json.RawMessage) ([]wire.Move, error)
	// CountItem is count_item: adjust moves, a negative lot settled first.
	CountItem(ctx context.Context, tx pgx.Tx, item wire.Item, in wire.CountInput, by json.RawMessage) ([]wire.Move, error)
	// AdjustLot is adjust_lot: one adjust move of the difference, none when it matches.
	AdjustLot(ctx context.Context, tx pgx.Tx, item wire.Item, lot string, in wire.AdjustInput, by json.RawMessage) ([]wire.Move, error)
	// PlaceLot and PlaceUnit set the place of a lot or a unit (update_lot's and
	// update_unit's location_id): no move.
	PlaceLot(ctx context.Context, tx pgx.Tx, lot string, location *string) error
	PlaceUnit(ctx context.Context, tx pgx.Tx, unit string, location *string) error
	// SetUnitStatus is set_unit_status.
	SetUnitStatus(ctx context.Context, tx pgx.Tx, unit string, in wire.UnitStatusInput, by json.RawMessage) (wire.Unit, error)
	// EstimateLots is estimate_unit_cost; EstimateUnits is the same over a unit item's
	// units in stock, each at its own cost.
	EstimateLots(ctx context.Context, q pgx.Tx, item string, qty *big.Rat) (*big.Rat, bool, error)
	EstimateUnits(ctx context.Context, q pgx.Tx, item string, qty *big.Rat) (*big.Rat, bool, error)
}
