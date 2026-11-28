package handlers

import (
	"context"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// consumeLotFIFO is consume.lot_fifo: the named lot first, then FIFO by (received_at, id);
// what no lot holds goes on the newest lot at its cost (phase 2 use and take). Each move
// takes its lot's cost now: the snapshot of cost.lot_fifo.
type consumeLotFIFO struct{ st Stock }

func (h consumeLotFIFO) Consume(ctx context.Context, tx pgx.Tx, in ConsumeIn) ([]wire.Move, error) {
	return h.st.Take(ctx, tx, in.Item, in.Use, in.By)
}
