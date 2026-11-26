// Package wire is the JSON of the inventory's public models (backend/scadbuddy/inventory/
// models.py), field for field, as Go reads and writes it (#6949, K1). The phase 2 fields
// stay (R-K1-6): tracking, life_basis, life_hours, life_months and compatible_with are read
// from the class, the steps and the relations, and still taken as input for one release.
package wire

import (
	"bytes"
	"encoding/json"
	"errors"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
)

// Opt is a patch field that tells an absent field from a null one: Set is false when the
// field is absent; Value is nil when it is null.
type Opt[T any] struct {
	Set   bool
	Value *T
}

// Some is a field set to v.
func Some[T any](v T) Opt[T] { return Opt[T]{Set: true, Value: &v} }

// Null is a field set to null.
func Null[T any]() Opt[T] { return Opt[T]{Set: true} }

// UnmarshalJSON runs only for a field that is present.
func (o *Opt[T]) UnmarshalJSON(b []byte) error {
	o.Set = true
	if bytes.Equal(b, []byte("null")) {
		o.Value = nil
		return nil
	}
	var v T
	if err := json.Unmarshal(b, &v); err != nil {
		return err
	}
	o.Value = &v
	return nil
}

// MarshalJSON writes the value or null. Tag the field omitzero to leave an absent one out.
func (o Opt[T]) MarshalJSON() ([]byte, error) {
	if o.Value == nil {
		return []byte("null"), nil
	}
	return json.Marshal(*o.Value)
}

// Decimal is a decimal as pydantic writes it: a JSON string ("5.99"). A JSON number is taken
// too.
type Decimal string

// UnmarshalJSON takes a string or a number that is a plain decimal.
func (d *Decimal) UnmarshalJSON(b []byte) error {
	var s string
	if len(b) > 0 && b[0] == '"' {
		if err := json.Unmarshal(b, &s); err != nil {
			return err
		}
	} else {
		var n json.Number
		if err := json.Unmarshal(b, &n); err != nil {
			return err
		}
		s = n.String()
	}
	if _, err := json.Number(s).Float64(); err != nil {
		return errors.New("wire: a decimal is a plain decimal number")
	}
	*d = Decimal(s)
	return nil
}

// Class is an item's resolved class (§3); null is a key left open.
type Class struct {
	Nature   *string `json:"nature"`
	Role     *string `json:"role"`
	Counting *string `json:"counting"`
	Billing  *string `json:"billing"`
}

// ItemKind is models.py ItemKind. The class keys and special_type are the kind's own
// columns (C22): the keys it answers itself, not those its parents answer.
type ItemKind struct {
	ID              string          `json:"id"`
	Name            string          `json:"name"`
	ParentID        *string         `json:"parent_id"`
	Tracking        string          `json:"tracking"`
	UOM             string          `json:"uom"`
	AttributeSchema json.RawMessage `json:"attribute_schema"`
	LifeBasis       string          `json:"life_basis"`
	Saleable        bool            `json:"saleable"`
	Builtin         bool            `json:"builtin"`
	ArchivedAt      *time.Time      `json:"archived_at"`
	ItemCount       int             `json:"item_count"`
	Nature          *string         `json:"nature"`
	Role            *string         `json:"role"`
	Counting        *string         `json:"counting"`
	Billing         *string         `json:"billing"`
	SpecialType     *string         `json:"special_type"`
	Steps           model.Steps     `json:"steps"`
	// IncompleteSteps are the steps whose handler lacks a required parameter (R-K1-8).
	IncompleteSteps []string `json:"incomplete_steps"`
}

// KindInput is models.py KindInput. Tracking and LifeBasis are the phase 2 inputs (R-K1-6):
// a tracking sets the class keys its rule gives (§8) that the parents and the class keys
// given here leave open; a life basis names the steps.life handler.
type KindInput struct {
	ID              string          `json:"id"`
	Name            string          `json:"name"`
	ParentID        *string         `json:"parent_id,omitempty"`
	Tracking        *string         `json:"tracking,omitempty"`
	UOM             string          `json:"uom,omitempty"`
	AttributeSchema json.RawMessage `json:"attribute_schema,omitempty"`
	LifeBasis       *string         `json:"life_basis,omitempty"`
	Saleable        bool            `json:"saleable,omitempty"`
	Nature          *string         `json:"nature,omitempty"`
	Role            *string         `json:"role,omitempty"`
	Counting        *string         `json:"counting,omitempty"`
	Billing         *string         `json:"billing,omitempty"`
	SpecialType     *string         `json:"special_type,omitempty"`
	Steps           model.Steps     `json:"steps,omitempty"`
}

// KindPatch is models.py KindPatch: an absent field keeps its value. parent_id null makes
// the kind a root; a class key or special_type null leaves it open; any other null keeps
// the value. Steps replaces the kind's own steps.
type KindPatch struct {
	Name            *string         `json:"name,omitempty"`
	ParentID        Opt[string]     `json:"parent_id,omitzero"`
	Tracking        *string         `json:"tracking,omitempty"`
	UOM             *string         `json:"uom,omitempty"`
	AttributeSchema json.RawMessage `json:"attribute_schema,omitempty"`
	LifeBasis       *string         `json:"life_basis,omitempty"`
	Saleable        *bool           `json:"saleable,omitempty"`
	Nature          Opt[string]     `json:"nature,omitzero"`
	Role            Opt[string]     `json:"role,omitzero"`
	Counting        Opt[string]     `json:"counting,omitzero"`
	Billing         Opt[string]     `json:"billing,omitzero"`
	SpecialType     Opt[string]     `json:"special_type,omitzero"`
	Steps           *model.Steps    `json:"steps,omitempty"`
}

// Item is models.py Item. Decimals are plain strings ("7", "2.5"). LifeHours, LifeMonths
// and CompatibleWith are read from the item's own steps.life and its compatible_with
// relations (R-K1-6, R-K1-11). MissingProperties are the keys the special type and the
// kinds' schemas require that the item has no value for, read on every read and never
// stored (R-K1-8).
type Item struct {
	ID                string      `json:"id"`
	KindID            string      `json:"kind_id"`
	KindPath          []string    `json:"kind_path"`
	Tracking          string      `json:"tracking"`
	UOM               string      `json:"uom"`
	Name              string      `json:"name"`
	Manufacturer      string      `json:"manufacturer"`
	CompatibleWith    []string    `json:"compatible_with"`
	LowStock          string      `json:"low_stock"`
	LifeHours         *string     `json:"life_hours"`
	LifeMonths        *int64      `json:"life_months"`
	Saleable          *bool       `json:"saleable"`
	EffectiveSaleable bool        `json:"effective_saleable"`
	Source            string      `json:"source"`
	SourceRef         *string     `json:"source_ref"`
	EditedFields      []string    `json:"edited_fields"`
	Note              string      `json:"note"`
	ArchivedAt        *time.Time  `json:"archived_at"`
	OnHand            string      `json:"on_hand"`
	StockValueMinor   string      `json:"stock_value_minor"`
	CostUnknown       bool        `json:"cost_unknown"`
	Low               bool        `json:"low"`
	OfferCount        int         `json:"offer_count"`
	Steps             model.Steps `json:"steps"`
	IncompleteSteps   []string    `json:"incomplete_steps"`
	Class             Class       `json:"class"`
	SpecialType       *string     `json:"special_type"`
	MissingProperties []string    `json:"missing_properties"`
}

// ItemInput is models.py ItemInput. LifeHours and LifeMonths go into steps.life (R-K1-6);
// each CompatibleWith code becomes a compatible_with relation to a model kind (R-K1-11).
type ItemInput struct {
	KindID         string      `json:"kind_id"`
	Name           string      `json:"name"`
	Manufacturer   string      `json:"manufacturer,omitempty"`
	CompatibleWith []string    `json:"compatible_with,omitempty"`
	LowStock       *Decimal    `json:"low_stock,omitempty"`
	LifeHours      *Decimal    `json:"life_hours,omitempty"`
	LifeMonths     *int64      `json:"life_months,omitempty"`
	Saleable       *bool       `json:"saleable,omitempty"`
	Source         string      `json:"source,omitempty"`
	SourceRef      *string     `json:"source_ref,omitempty"`
	Note           string      `json:"note,omitempty"`
	Steps          model.Steps `json:"steps,omitempty"`
}

// InventoryItemPatch is models.py InventoryItemPatch: an absent field keeps its value.
// life_hours, life_months and saleable null clear it; another null keeps the value. Steps
// replaces the item's own steps.
type InventoryItemPatch struct {
	Name           *string      `json:"name,omitempty"`
	Manufacturer   *string      `json:"manufacturer,omitempty"`
	CompatibleWith *[]string    `json:"compatible_with,omitempty"`
	LowStock       *Decimal     `json:"low_stock,omitempty"`
	LifeHours      Opt[Decimal] `json:"life_hours,omitzero"`
	LifeMonths     Opt[int64]   `json:"life_months,omitzero"`
	Saleable       Opt[bool]    `json:"saleable,omitzero"`
	Note           *string      `json:"note,omitempty"`
	Steps          *model.Steps `json:"steps,omitempty"`
}

// Unit is models.py Unit (C29), plus its steps (parameters only, §4.2 layer 5) and a
// service term's starts_at and ends_at. Status is in_stock, in_service, retired, scrapped,
// sold or ended.
type Unit struct {
	ID                string          `json:"id"`
	ItemID            string          `json:"item_id"`
	Serial            *string         `json:"serial"`
	Name              string          `json:"name"`
	LocationID        *string         `json:"location_id"`
	Status            string          `json:"status"`
	InstalledInUnitID *string         `json:"installed_in_unit_id"`
	CostMinor         string          `json:"cost_minor"`
	CostUnknown       bool            `json:"cost_unknown"`
	Origin            string          `json:"origin"`
	InvoiceLineID     *string         `json:"invoice_line_id"`
	ProductionRunID   *string         `json:"production_run_id"`
	InServiceAt       *time.Time      `json:"in_service_at"`
	RetiredAt         *time.Time      `json:"retired_at"`
	LifeHours         *string         `json:"life_hours"`
	LifeMonths        *int64          `json:"life_months"`
	PrinterID         *string         `json:"printer_id"`
	LocationRefID     *string         `json:"location_ref_id"`
	ReceivedAt        time.Time       `json:"received_at"`
	CreatedBy         json.RawMessage `json:"created_by"`
	Steps             model.Steps     `json:"steps"`
	StartsAt          *time.Time      `json:"starts_at"`
	EndsAt            *time.Time      `json:"ends_at"`
}
