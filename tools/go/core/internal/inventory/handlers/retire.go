package handlers

import (
	"context"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// retireDefault is retire.default: a unit's new status, every status, with phase 2's move
// when it crosses in_stock (set_unit_status).
type retireDefault struct{ st Stock }

func (h retireDefault) Retire(ctx context.Context, tx pgx.Tx, in RetireIn) (wire.Unit, error) {
	if in.UnitID == "" {
		return wire.Unit{}, wrongCounting(in.Item)
	}
	return h.st.SetUnitStatus(ctx, tx, in.UnitID, in.Status, in.By)
}
