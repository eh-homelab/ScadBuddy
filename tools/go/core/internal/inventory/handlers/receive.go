package handlers

import (
	"context"

	"github.com/jackc/pgx/v5"
)

// receiveLot is receive.lot: a new lot and its receive move (phase 2 receive_in).
type receiveLot struct{ st Stock }

func (h receiveLot) Receive(ctx context.Context, tx pgx.Tx, in ReceiveIn) (ReceiveOut, error) {
	if in.Lot == nil {
		return ReceiveOut{}, wrongCounting(in.Item)
	}
	lot, err := h.st.ReceiveLot(ctx, tx, in.Item, *in.Lot, in.Shop, in.By)
	if err != nil {
		return ReceiveOut{}, err
	}
	return ReceiveOut{Lot: &lot}, nil
}

// receiveUnits is receive.units: units in stock, each with its receive move (phase 2
// receive_units).
type receiveUnits struct{ st Stock }

func (h receiveUnits) Receive(ctx context.Context, tx pgx.Tx, in ReceiveIn) (ReceiveOut, error) {
	if in.Units == nil {
		return ReceiveOut{}, wrongCounting(in.Item)
	}
	units, err := h.st.ReceiveUnits(ctx, tx, in.Item, *in.Units, in.By)
	if err != nil {
		return ReceiveOut{}, err
	}
	return ReceiveOut{Units: units}, nil
}
