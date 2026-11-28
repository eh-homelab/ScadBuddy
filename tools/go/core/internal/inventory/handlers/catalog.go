// Package handlers is the inventory handler catalog and the resolver that picks, per
// step, the handler and parameters a kind, an item or a unit gets (design §4).
package handlers

import "github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"

// Step is one thing the base flow does to stock (design §4.1).
type Step string

// Steps are the base flow's steps, in its order.
var Steps = []Step{"receive", "consume", "count", "move", "retire", "cost", "life", "bill", "label", "sync"}

// Param is one handler parameter's schema (design §4.3).
type Param struct {
	Type     string // integer, number, string, boolean or list
	Required bool
	Enum     []string
}

// Spec is one handler: the steps it fills, the class (and special type) it applies to,
// and its parameters. Applies keys are nature, role, counting, billing and special.
type Spec struct {
	ID      string
	Steps   []Step
	Applies map[string]string
	Params  map[string]Param
}

// Column is the default handlers of the classes Match matches.
type Column struct {
	Name     string
	Match    model.Class
	Handlers map[Step]string
}

// SpecialType is a named set of handlers that replace the defaults (design §4.4).
type SpecialType struct {
	ID                                string
	Needs                             model.Class
	Handlers                          map[Step]string
	RequiresProperties, Panels, Tools []string
}

// Catalog is every handler, default column and special type.
type Catalog struct {
	Specs   map[string]Spec
	Columns []Column
	Special map[string]SpecialType
}

// SpecialType is the special type id, and whether the catalog has it.
func (c *Catalog) SpecialType(id string) (SpecialType, bool) {
	sp, ok := c.Special[id]
	return sp, ok
}

func spec(id string, steps []Step, applies map[string]string, params map[string]Param) Spec {
	return Spec{ID: id, Steps: steps, Applies: applies, Params: params}
}

func one(s Step) []Step { return []Step{s} }

func physical(kv ...string) map[string]string {
	m := map[string]string{"nature": "physical"}
	for i := 0; i+1 < len(kv); i += 2 {
		m[kv[i]] = kv[i+1]
	}
	return m
}

func service(kv ...string) map[string]string {
	m := physical(kv...)
	m["nature"] = "service"
	return m
}

func forSpecial(id string) map[string]string { return map[string]string{"special": id} }

func optInt() Param { return Param{Type: "integer"} }

// Builtin is docs/inventory-model/handlers.yaml as Go values.
func Builtin() *Catalog {
	specs := []Spec{
		spec("refuse", append([]Step(nil), Steps...), map[string]string{}, nil),

		spec("receive.lot", one("receive"), physical("counting", "lot"), nil),
		spec("receive.units", one("receive"), physical("counting", "unit"), nil),
		spec("receive.service_term", one("receive"), service("billing", "term"), map[string]Param{
			"term_months": {Type: "integer", Required: true},
		}),

		spec("consume.lot_fifo", one("consume"), physical("counting", "lot"), nil),
		spec("consume.remaining", one("consume"), physical("role", "consumable", "counting", "unit"), nil),
		spec("consume.use", one("consume"), physical("role", "durable"), nil),
		spec("consume.service", one("consume"), service(), nil),

		spec("count.lot", one("count"), physical("counting", "lot"), nil),
		spec("count.remaining", one("count"), physical("role", "consumable", "counting", "unit"), map[string]Param{
			"capacity": {Type: "number", Required: true},
		}),

		spec("move.place", one("move"), physical(), nil),

		spec("retire.default", one("retire"), physical(), nil),
		spec("retire.term_end", one("retire"), service("billing", "term"), nil),

		spec("cost.lot_fifo", one("cost"), physical("counting", "lot"), nil),
		spec("cost.unit", one("cost"), map[string]string{"counting": "unit"}, nil),
		spec("cost.bill", one("cost"), service(), nil),

		spec("life.none", one("life"), map[string]string{}, nil),
		spec("life.months", one("life"), physical("role", "durable"), map[string]Param{
			"months":      {Type: "integer", Required: true},
			"salvage_pct": {Type: "number"},
		}),
		spec("life.hours", one("life"), physical("role", "durable"), map[string]Param{
			"hours":    {Type: "number", Required: true},
			"source":   {Type: "string", Required: true, Enum: []string{"container_run_hours", "own_run_hours", "manual"}},
			"warn_pct": {Type: "number"},
		}),
		spec("life.uses", one("life"), physical("role", "durable"), map[string]Param{
			"uses":     {Type: "integer", Required: true},
			"warn_pct": {Type: "number"},
		}),
		spec("life.term", one("life"), service("billing", "term"), map[string]Param{
			"warn_days": optInt(),
		}),

		spec("bill.hourly", one("bill"), service("billing", "hourly"), map[string]Param{
			"cost_rate_minor":   optInt(),
			"sell_rate_minor":   optInt(),
			"increment_minutes": optInt(),
			"minimum_minutes":   optInt(),
		}),
		spec("bill.flat", one("bill"), service("billing", "flat"), map[string]Param{
			"cost_minor": optInt(),
			"sell_minor": optInt(),
		}),
		spec("bill.term", one("bill"), service("billing", "term"), map[string]Param{
			"sell_minor": optInt(),
		}),

		spec("label.default", one("label"), map[string]string{}, map[string]Param{
			"fields":  {Type: "list"},
			"enabled": {Type: "boolean"},
		}),

		spec("sync.none", one("sync"), map[string]string{}, nil),

		spec("filament.receive", one("receive"), forSpecial("filament_spool"), nil),
		spec("filament.consume_grams", one("consume"), forSpecial("filament_spool"), nil),
		spec("filament.weigh", one("count"), forSpecial("filament_spool"), nil),
		spec("filament.move", one("move"), forSpecial("filament_spool"), nil),
		spec("filament.close", one("retire"), forSpecial("filament_spool"), nil),
		spec("filament.cost_per_kg", one("cost"), forSpecial("filament_spool"), nil),
		spec("filament.label", one("label"), forSpecial("filament_spool"), nil),
		spec("filament.bambuddy_sync", one("sync"), forSpecial("filament_spool"), nil),

		spec("printer.receive", one("receive"), forSpecial("printer"), nil),
		spec("printer.consume_run", one("consume"), forSpecial("printer"), nil),
		spec("printer.machine_rate", one("cost"), forSpecial("printer"), nil),
		spec("printer.bambuddy_link", one("sync"), forSpecial("printer"), nil),
	}
	c := &Catalog{Specs: make(map[string]Spec, len(specs))}
	for _, s := range specs {
		c.Specs[s.ID] = s
	}

	c.Columns = []Column{
		{Name: "consumable_lot", Match: model.Class{Nature: "physical", Role: "consumable", Counting: "lot"}, Handlers: map[Step]string{
			"receive": "receive.lot", "consume": "consume.lot_fifo", "count": "count.lot", "move": "move.place",
			"retire": "retire.default", "cost": "cost.lot_fifo", "life": "life.none", "bill": "refuse",
			"label": "label.default", "sync": "sync.none",
		}},
		{Name: "consumable_unit", Match: model.Class{Nature: "physical", Role: "consumable", Counting: "unit"}, Handlers: map[Step]string{
			"receive": "receive.units", "consume": "consume.remaining", "count": "count.remaining", "move": "move.place",
			"retire": "retire.default", "cost": "cost.unit", "life": "life.none", "bill": "refuse",
			"label": "label.default", "sync": "sync.none",
		}},
		{Name: "durable_unit", Match: model.Class{Nature: "physical", Role: "durable", Counting: "unit"}, Handlers: map[Step]string{
			"receive": "receive.units", "consume": "consume.use", "count": "refuse", "move": "move.place",
			"retire": "retire.default", "cost": "cost.unit", "life": "life.none", "bill": "refuse",
			"label": "label.default", "sync": "sync.none",
		}},
		{Name: "service_term", Match: model.Class{Nature: "service", Billing: "term"}, Handlers: map[Step]string{
			"receive": "receive.service_term", "consume": "consume.service", "count": "refuse", "move": "refuse",
			"retire": "retire.term_end", "cost": "cost.bill", "life": "life.term", "bill": "bill.term",
			"label": "label.default", "sync": "sync.none",
		}},
		{Name: "service_hourly", Match: model.Class{Nature: "service", Billing: "hourly"}, Handlers: map[Step]string{
			"receive": "refuse", "consume": "consume.service", "count": "refuse", "move": "refuse",
			"retire": "refuse", "cost": "cost.bill", "life": "refuse", "bill": "bill.hourly",
			"label": "refuse", "sync": "sync.none",
		}},
		{Name: "service_flat", Match: model.Class{Nature: "service", Billing: "flat"}, Handlers: map[Step]string{
			"receive": "refuse", "consume": "consume.service", "count": "refuse", "move": "refuse",
			"retire": "refuse", "cost": "cost.bill", "life": "refuse", "bill": "bill.flat",
			"label": "refuse", "sync": "sync.none",
		}},
	}

	c.Special = map[string]SpecialType{
		"filament_spool": {
			ID:    "filament_spool",
			Needs: model.Class{Nature: "physical", Role: "consumable", Counting: "unit"},
			Handlers: map[Step]string{
				"receive": "filament.receive", "consume": "filament.consume_grams", "count": "filament.weigh",
				"move": "filament.move", "retire": "filament.close", "cost": "filament.cost_per_kg",
				"label": "filament.label", "sync": "filament.bambuddy_sync",
			},
			RequiresProperties: []string{"rgba", "diameter_mm"},
			Panels:             []string{"spool_grams", "spool_trays", "spool_tags", "spool_presets", "spool_k_values", "spool_weighing"},
			Tools:              []string{"filament_spool_*", "filament_type_*"},
		},
		"printer": {
			ID:    "printer",
			Needs: model.Class{Nature: "physical", Role: "durable", Counting: "unit"},
			Handlers: map[Step]string{
				"receive": "printer.receive", "consume": "printer.consume_run", "cost": "printer.machine_rate",
				"sync": "printer.bambuddy_link",
			},
			RequiresProperties: []string{"model_code"},
			Panels:             []string{"printer_status", "printer_camera", "printer_queue", "printer_installed"},
			Tools:              []string{"get_printer", "list_printers"},
		},
	}
	return c
}
