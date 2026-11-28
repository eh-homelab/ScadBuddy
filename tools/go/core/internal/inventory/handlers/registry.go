package handlers

import (
	"fmt"
	"slices"
)

// ErrNotBuilt is a handler the catalog names that has no implementation yet (R-K1-7).
type ErrNotBuilt struct {
	Handler string
}

func (e ErrNotBuilt) Error() string {
	return fmt.Sprintf("handler %s is not built", e.Handler)
}

// Registry maps the catalog's handlers to their implementations. The per-step
// interfaces an implementation satisfies are in steps.go.
type Registry struct {
	Catalog *Catalog
	impls   map[string]any
}

// Implement registers impl for the handler id.
func (r *Registry) Implement(id string, impl any) {
	if r.impls == nil {
		r.impls = map[string]any{}
	}
	r.impls[id] = impl
}

// For is the implementation of handler id on step: an error when the catalog has no such
// handler or it does not fill the step, ErrNotBuilt when nothing implements it.
func (r *Registry) For(step Step, id string) (any, error) {
	sp, ok := r.Catalog.Specs[id]
	if !ok {
		return nil, fmt.Errorf("unknown handler %s", id)
	}
	if !slices.Contains(sp.Steps, step) {
		return nil, fmt.Errorf("handler %s does not fill step %s", id, step)
	}
	impl, ok := r.impls[id]
	if !ok {
		return nil, ErrNotBuilt{Handler: id}
	}
	return impl, nil
}

// Default is the full catalog (Builtin) with the handlers K1 builds, writing through st:
// the defaults of the counted classes and refuse. Every other handler is ErrNotBuilt until
// its phase builds it (R-K1-7).
func Default(st Stock) *Registry {
	r := &Registry{Catalog: Builtin()}
	for id, impl := range map[string]any{
		"refuse":           refuse{},
		"receive.lot":      receiveLot{st},
		"receive.units":    receiveUnits{st},
		"consume.lot_fifo": consumeLotFIFO{st},
		"count.lot":        countLot{st},
		"move.place":       movePlace{st},
		"retire.default":   retireDefault{st},
		"cost.lot_fifo":    costLotFIFO{st},
		"cost.unit":        costUnit{st},
	} {
		r.Implement(id, impl)
	}
	return r
}
