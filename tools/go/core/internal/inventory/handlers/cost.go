package handlers

import (
	"context"
	"math/big"

	"github.com/jackc/pgx/v5"
)

// costLotFIFO is cost.lot_fifo: what a take would charge, FIFO over the lots at each lot's
// cost now (phase 2 estimate_unit_cost); a take writes the same costs as its snapshot.
type costLotFIFO struct{ st Stock }

func (h costLotFIFO) Estimate(ctx context.Context, q pgx.Tx, item string, qty *big.Rat) (*big.Rat, bool, error) {
	return h.st.EstimateLots(ctx, q, item, qty)
}

// costUnit is cost.unit: each unit carries its own cost (the snapshot its moves take); an
// estimate is the oldest units in stock, each at its cost.
type costUnit struct{ st Stock }

func (h costUnit) Estimate(ctx context.Context, q pgx.Tx, item string, qty *big.Rat) (*big.Rat, bool, error) {
	return h.st.EstimateUnits(ctx, q, item, qty)
}
