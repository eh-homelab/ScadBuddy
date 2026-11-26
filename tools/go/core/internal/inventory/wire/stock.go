package wire

import (
	"encoding/json"
	"time"
)

// Lots, units and moves: models.py Lot, Move and the stock inputs, field for field. Decimals
// are plain strings ("10", "-5", "333.333333").

// Lot is models.py Lot.
type Lot struct {
	ID                   string          `json:"id"`
	ItemID               string          `json:"item_id"`
	LocationID           *string         `json:"location_id"`
	ReceivedQty          string          `json:"received_qty"`
	OnHand               string          `json:"on_hand"`
	UnitCostMinor        string          `json:"unit_cost_minor"`
	CostUnknown          bool            `json:"cost_unknown"`
	CostCurrency         *string         `json:"cost_currency"`
	ForeignUnitCostMinor *string         `json:"foreign_unit_cost_minor"`
	Origin               string          `json:"origin"`
	InvoiceLineID        *string         `json:"invoice_line_id"`
	ProductionRunID      *string         `json:"production_run_id"`
	Note                 string          `json:"note"`
	ReceivedAt           time.Time       `json:"received_at"`
	CreatedBy            json.RawMessage `json:"created_by"`
}

// Move is models.py Move.
type Move struct {
	ID              string          `json:"id"`
	At              time.Time       `json:"at"`
	ItemID          string          `json:"item_id"`
	LotID           *string         `json:"lot_id"`
	UnitID          *string         `json:"unit_id"`
	Kind            string          `json:"kind"`
	Qty             string          `json:"qty"`
	UnitCostMinor   string          `json:"unit_cost_minor"`
	AmountMinor     string          `json:"amount_minor"`
	CostUnknown     bool            `json:"cost_unknown"`
	NeedsReview     bool            `json:"needs_review"`
	ReversesMoveID  *string         `json:"reverses_move_id"`
	Reason          string          `json:"reason"`
	OrderID         *string         `json:"order_id"`
	OrderItemID     *string         `json:"order_item_id"`
	ProductionRunID *string         `json:"production_run_id"`
	PrintRunID      *string         `json:"print_run_id"`
	EntityID        *string         `json:"entity_id"`
	Actor           json.RawMessage `json:"actor"`
}

// ReceiveLot is models.py ReceiveLot: the cost is unit_cost_minor or total_cost_minor (for
// the whole quantity), in currency (the shop's when absent).
type ReceiveLot struct {
	Qty            Decimal    `json:"qty"`
	UnitCostMinor  *Decimal   `json:"unit_cost_minor,omitempty"`
	TotalCostMinor *Decimal   `json:"total_cost_minor,omitempty"`
	Currency       *string    `json:"currency,omitempty"`
	LocationID     *string    `json:"location_id,omitempty"`
	Note           string     `json:"note,omitempty"`
	ReceivedAt     *time.Time `json:"received_at,omitempty"`
}

// ReceiveUnits is models.py ReceiveUnits.
type ReceiveUnits struct {
	Count      int      `json:"count"`
	CostMinor  *Decimal `json:"cost_minor,omitempty"`
	Serials    []string `json:"serials,omitempty"`
	LocationID *string  `json:"location_id,omitempty"`
	Name       string   `json:"name,omitempty"`
}

// UseInput is models.py UseInput, plus the order and order item an order's take names.
type UseInput struct {
	Qty         Decimal `json:"qty"`
	LotID       *string `json:"lot_id,omitempty"`
	Entity      *string `json:"entity,omitempty"`
	Reason      string  `json:"reason,omitempty"`
	OrderID     *string `json:"order_id,omitempty"`
	OrderItemID *string `json:"order_item_id,omitempty"`
}

// AdjustInput is models.py AdjustInput: a count of one lot.
type AdjustInput struct {
	OnHand Decimal `json:"on_hand"`
	Note   string  `json:"note"`
}

// CountInput is models.py CountInput: a count of the whole item.
type CountInput struct {
	OnHand Decimal `json:"on_hand"`
	Note   string  `json:"note"`
}

// LotPatch is models.py LotPatch: an absent field keeps its value; location_id null
// clears it.
type LotPatch struct {
	UnitCostMinor *Decimal    `json:"unit_cost_minor,omitempty"`
	LocationID    Opt[string] `json:"location_id,omitzero"`
	Note          *string     `json:"note,omitempty"`
}

// UnitPatch is models.py UnitPatch: serial null or blank clears it, as location_id null
// does.
type UnitPatch struct {
	Name       *string     `json:"name,omitempty"`
	Serial     Opt[string] `json:"serial,omitzero"`
	LocationID Opt[string] `json:"location_id,omitzero"`
}

// UnitStatusInput is models.py UnitStatusInput.
type UnitStatusInput struct {
	Status string  `json:"status"`
	Entity *string `json:"entity,omitempty"`
	Note   string  `json:"note,omitempty"`
}

// RecostedItem is models.py RecostedItem.
type RecostedItem struct {
	OrderID       *string `json:"order_id"`
	OrderItemID   string  `json:"order_item_id"`
	BeforeMinor   string  `json:"before_minor"`
	AfterMinor    string  `json:"after_minor"`
	BeforeUnknown bool    `json:"before_unknown"`
	AfterUnknown  bool    `json:"after_unknown"`
}

// RecostResult is models.py RecostResult.
type RecostResult struct {
	ItemID               string         `json:"item_id"`
	UnitCostMinor        string         `json:"unit_cost_minor"`
	CostUnknown          bool           `json:"cost_unknown"`
	BeforeUnitCostsMinor []string       `json:"before_unit_costs_minor"`
	Moves                int            `json:"moves"`
	Orders               []RecostedItem `json:"orders"`
}
