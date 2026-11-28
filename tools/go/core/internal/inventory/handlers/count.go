package handlers

import (
	"context"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// countLot is count.lot: a count of the whole item (phase 2 count_item), or of one lot
// (adjust_lot), as adjust moves.
type countLot struct{ st Stock }

func (h countLot) Count(ctx context.Context, tx pgx.Tx, in CountIn) ([]wire.Move, error) {
	switch {
	case in.Count != nil:
		return h.st.CountItem(ctx, tx, in.Item, *in.Count, in.By)
	case in.Adjust != nil && in.LotID != "":
		return h.st.AdjustLot(ctx, tx, in.Item, in.LotID, *in.Adjust, in.By)
	}
	return nil, wrongCounting(in.Item)
}
