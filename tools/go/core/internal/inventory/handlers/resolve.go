package handlers

import (
	"fmt"
	"math"
	"reflect"
	"slices"
	"sort"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
)

// Choice is the handler a step resolved to and its merged parameters.
type Choice struct {
	Handler string
	Params  map[string]any
}

// Resolution is a chain's (and an item's, and a unit's) class, special type and handlers.
type Resolution struct {
	Class   model.Class
	Special string
	Steps   map[Step]Choice
}

// Problem is one step that does not resolve to a usable handler. Missing names the
// parameters concerned (the class keys, for special_needs). unknown_special is a special
// type that is not in the catalog: it has no step and no Missing.
type Problem struct {
	Step    Step
	Handler string
	Reason  string
	Missing []string
}

// The reasons a Problem gives.
const (
	UnknownStep      = "unknown_step"
	UnknownHandler   = "unknown_handler"
	WrongStep        = "wrong_step"
	NotApplicable    = "not_applicable"
	UnknownParam     = "unknown_param"
	WrongType        = "wrong_type"
	NotInEnum        = "not_in_enum"
	MissingParam     = "missing_param"
	SpecialNeeds     = "special_needs"
	UnknownSpecial   = "unknown_special"
	UnitNamesHandler = "unit_names_handler"
)

// Blocking is every problem but a missing parameter: a save with one is refused.
func Blocking(ps []Problem) []Problem {
	return filter(ps, func(p Problem) bool { return p.Reason != MissingParam })
}

// Incomplete is only the missing parameters: a later layer may still set them.
func Incomplete(ps []Problem) []Problem {
	return filter(ps, func(p Problem) bool { return p.Reason == MissingParam })
}

func filter(ps []Problem, keep func(Problem) bool) []Problem {
	var out []Problem
	for _, p := range ps {
		if keep(p) {
			out = append(out, p)
		}
	}
	return out
}

// Resolve gives an item's every step its handler and parameters (design §4.2): the
// class default, then the special type, then the kinds root to leaf, then item, then
// unit (parameters only). A layer that names a new handler drops the old one's
// parameters; a layer that sets only params merges them. A nil item or unit sets nothing.
//
// The error is a class that does not resolve (*model.ClassChanged, model.ErrDurableLot,
// model.ErrSecondSpecial, a value out of range), or model.ErrClassOpen: an item's class
// must be complete (§3).
func (c *Catalog) Resolve(chain model.Chain, item, unit model.Steps) (Resolution, []Problem, error) {
	return c.resolve(chain, item, unit, false)
}

// ResolveKind is Resolve for a kind save: the kind may leave class keys open. A handler's
// applies and a special type's needs are checked only against the keys the kind sets. A
// step with no handler (an open class has no default column) is left out.
func (c *Catalog) ResolveKind(chain model.Chain) (Resolution, []Problem, error) {
	return c.resolve(chain, nil, nil, true)
}

func (c *Catalog) resolve(chain model.Chain, item, unit model.Steps, openOK bool) (Resolution, []Problem, error) {
	cls, special, err := chain.Class()
	if err != nil {
		return Resolution{}, nil, err
	}
	if !openOK && !cls.Complete() {
		return Resolution{}, nil, fmt.Errorf("%w: %+v", model.ErrClassOpen, cls)
	}
	res := Resolution{Class: cls, Special: special, Steps: map[Step]Choice{}}
	var problems []Problem

	if cls.Complete() {
		var cols []Column
		for _, col := range c.Columns {
			if cls.Matches(col.Match) {
				cols = append(cols, col)
			}
		}
		if len(cols) != 1 {
			return Resolution{}, nil, fmt.Errorf("%d default columns match %+v", len(cols), cls)
		}
		for step, id := range cols[0].Handlers {
			res.Steps[step] = Choice{Handler: id}
		}
	}

	if special != "" {
		sp, ok := c.Special[special]
		if !ok {
			problems = append(problems, Problem{Reason: UnknownSpecial})
		} else {
			if miss := unmatched(cls, sp.Needs, openOK); miss != nil {
				problems = append(problems, Problem{Reason: SpecialNeeds, Missing: miss})
			}
			for step, id := range sp.Handlers {
				res.Steps[step] = Choice{Handler: id}
			}
		}
	}

	for _, k := range chain {
		problems = apply(res.Steps, k.Steps, false, problems)
	}
	problems = apply(res.Steps, item, false, problems)
	problems = apply(res.Steps, unit, true, problems)

	for _, step := range Steps {
		ch, ok := res.Steps[step]
		if !ok {
			continue
		}
		if ch.Handler == "" {
			// Params set on an open class's step: a narrower kind or the item picks the
			// handler, and the params are checked then.
			delete(res.Steps, step)
			continue
		}
		problems = append(problems, c.check(step, ch, cls, special, openOK)...)
	}
	return res, problems, nil
}

// unmatched is the class keys of want that cls does not have. With openOK (a kind save)
// a key cls leaves open is no mismatch: a child kind may still answer it.
func unmatched(cls, want model.Class, openOK bool) []string {
	var out []string
	for _, key := range []string{"nature", "role", "counting", "billing"} {
		if v := want.Get(key); v != "" && !fits(cls.Get(key), v, openOK) {
			out = append(out, key)
		}
	}
	return out
}

func fits(got, want string, openOK bool) bool {
	return got == want || (openOK && got == "")
}

// apply lays one layer's settings over the steps resolved so far (check.py apply_steps).
func apply(steps map[Step]Choice, layer model.Steps, paramsOnly bool, problems []Problem) []Problem {
	names := make([]string, 0, len(layer))
	for name := range layer {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		conf, step := layer[name], Step(name)
		if !slices.Contains(Steps, step) {
			problems = append(problems, Problem{Step: step, Handler: conf.Handler, Reason: UnknownStep})
			continue
		}
		if paramsOnly && conf.Handler != "" {
			problems = append(problems, Problem{Step: step, Handler: conf.Handler, Reason: UnitNamesHandler})
			continue
		}
		cur := steps[step]
		params := map[string]any{}
		if conf.Handler != "" && conf.Handler != cur.Handler {
			cur.Handler = conf.Handler
		} else {
			for k, v := range cur.Params {
				params[k] = v
			}
		}
		for k, v := range conf.Params {
			params[k] = v
		}
		if len(params) == 0 {
			params = nil
		}
		steps[step] = Choice{Handler: cur.Handler, Params: params}
	}
	return problems
}

// check is one step's handler against the catalog: it exists, fills the step, applies to
// the class and special type, and its parameters fit its schema. With openOK (a kind
// save) a class key the kind leaves open is no mismatch.
func (c *Catalog) check(step Step, ch Choice, cls model.Class, special string, openOK bool) []Problem {
	at := func(reason string, keys ...string) Problem {
		return Problem{Step: step, Handler: ch.Handler, Reason: reason, Missing: keys}
	}
	sp, ok := c.Specs[ch.Handler]
	if !ok {
		return []Problem{at(UnknownHandler)}
	}
	if !slices.Contains(sp.Steps, step) {
		return []Problem{at(WrongStep)}
	}
	for key, want := range sp.Applies {
		if key == "special" && special != want || key != "special" && !fits(cls.Get(key), want, openOK) {
			return []Problem{at(NotApplicable)}
		}
	}

	var out []Problem
	keys := make([]string, 0, len(ch.Params))
	for k := range ch.Params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		p, ok := sp.Params[k]
		if !ok {
			out = append(out, at(UnknownParam, k))
			continue
		}
		v := ch.Params[k]
		if !hasType(v, p.Type) {
			out = append(out, at(WrongType, k))
			continue
		}
		if p.Enum != nil {
			if s, _ := v.(string); !slices.Contains(p.Enum, s) {
				out = append(out, at(NotInEnum, k))
			}
		}
	}
	var missing []string
	for k, p := range sp.Params {
		if _, set := ch.Params[k]; p.Required && !set {
			missing = append(missing, k)
		}
	}
	if missing != nil {
		sort.Strings(missing)
		out = append(out, at(MissingParam, missing...))
	}
	return out
}

// hasType is check.py's TYPES: a boolean is never a number, and an integer is a whole
// number (a JSON 1500 decodes as float64).
func hasType(v any, typ string) bool {
	if v == nil {
		return false
	}
	rv := reflect.ValueOf(v)
	switch typ {
	case "integer":
		switch rv.Kind() {
		case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
			reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
			return true
		case reflect.Float32, reflect.Float64:
			f := rv.Float()
			return f == math.Trunc(f) && !math.IsInf(f, 0)
		}
	case "number":
		switch rv.Kind() {
		case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
			reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
			return true
		case reflect.Float32, reflect.Float64:
			f := rv.Float()
			return !math.IsNaN(f) && !math.IsInf(f, 0)
		}
	case "string":
		return rv.Kind() == reflect.String
	case "boolean":
		return rv.Kind() == reflect.Bool
	case "list":
		return rv.Kind() == reflect.Slice
	}
	return false
}
