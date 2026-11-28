package handlers

import (
	"context"

	"github.com/jackc/pgx/v5"
)

// movePlace is move.place: the place of a lot or a unit. It writes no stock move
// (handlers.yaml; R-K1-1).
type movePlace struct{ st Stock }

func (h movePlace) Move(ctx context.Context, tx pgx.Tx, in MoveIn) error {
	switch {
	case in.LotID != "":
		return h.st.PlaceLot(ctx, tx, in.LotID, in.LocationID)
	case in.UnitID != "":
		return h.st.PlaceUnit(ctx, tx, in.UnitID, in.LocationID)
	}
	return wrongCounting(in.Item)
}
