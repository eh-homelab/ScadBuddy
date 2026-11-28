package store

import (
	"context"
	"math/big"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// The read routes' reads that check their item first, as phase 2's lots and units did
// (_item(conn, item_id, counting, lock=False)): each in a read-only transaction of its own.

// ItemLots is the item's lots in FIFO order: item_not_found, or step_not_applicable or
// wrong_counting for an item that is not counted in lots.
func ItemLots(ctx context.Context, pool *pgxpool.Pool, itemID string) ([]wire.Lot, error) {
	var out []wire.Lot
	err := readOnly(ctx, pool, func(tx pgx.Tx) error {
		if _, err := countedItem(ctx, tx, itemID, "lot", false); err != nil {
			return err
		}
		var err error
		out, err = Lots(ctx, tx, itemID)
		return err
	})
	return out, err
}

// ItemUnits is the item's units, oldest first, only those of status when it is not empty:
// item_not_found, or step_not_applicable or wrong_counting for an item that is not counted
// in units.
func ItemUnits(ctx context.Context, pool *pgxpool.Pool, itemID, status string) ([]wire.Unit, error) {
	var out []wire.Unit
	err := readOnly(ctx, pool, func(tx pgx.Tx) error {
		if _, err := countedItem(ctx, tx, itemID, "unit", false); err != nil {
			return err
		}
		var err error
		out, err = Units(ctx, tx, itemID, status)
		return err
	})
	return out, err
}

func readOnly(ctx context.Context, pool *pgxpool.Pool, fn func(pgx.Tx) error) error {
	return pgx.BeginTxFunc(ctx, pool, pgx.TxOptions{AccessMode: pgx.ReadOnly}, fn)
}

// Quantity is a quantity given as text: bad_quantity when it is not a plain decimal, or is
// past what numeric(20,6) holds.
func Quantity(s string) (*big.Rat, error) { return decimal(wire.Decimal(s)) }

// Text is a decimal as the wire writes it: plain, six places at most, no trailing zero.
func Text(r *big.Rat) string { return text(r) }
