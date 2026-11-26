package handlers

import (
	"errors"
	"reflect"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
)

func durableChain() model.Chain {
	return model.Chain{
		{ID: "durable", Class: model.Class{Nature: "physical", Role: "durable", Counting: "unit"}},
		{ID: "part", ParentID: "durable"},
		{ID: "nozzle", ParentID: "part", Steps: model.Steps{
			"life": {Handler: "life.hours", Params: map[string]any{"hours": 1000, "source": "container_run_hours", "warn_pct": 90}},
		}},
	}
}

func TestResolveFollowsTheLayers(t *testing.T) {
	res, problems, err := Builtin().Resolve(durableChain(), model.Steps{"life": {Params: map[string]any{"hours": 1500}}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 0 {
		t.Fatalf("problems: %+v", problems)
	}
	want := Choice{Handler: "life.hours", Params: map[string]any{"hours": 1500, "source": "container_run_hours", "warn_pct": 90}}
	if !reflect.DeepEqual(res.Steps["life"], want) {
		t.Fatalf("life = %+v, want %+v", res.Steps["life"], want)
	}
	if res.Steps["consume"].Handler != "consume.use" || res.Steps["count"].Handler != "refuse" {
		t.Fatalf("defaults not taken: %+v", res.Steps)
	}
}

func TestANewHandlerDropsTheOldParams(t *testing.T) {
	ch := model.Chain{
		{ID: "durable", Class: model.Class{Nature: "physical", Role: "durable", Counting: "unit"}, Steps: model.Steps{
			"life": {Handler: "life.months", Params: map[string]any{"months": 60}},
		}},
	}
	item := model.Steps{"life": {Handler: "life.hours", Params: map[string]any{"hours": 10, "source": "manual"}}}
	res, problems, err := Builtin().Resolve(ch, item, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 0 {
		t.Fatalf("problems: %+v", problems)
	}
	want := Choice{Handler: "life.hours", Params: map[string]any{"hours": 10, "source": "manual"}}
	if !reflect.DeepEqual(res.Steps["life"], want) {
		t.Fatalf("life = %+v, want %+v", res.Steps["life"], want)
	}
}

func TestResolveProblems(t *testing.T) {
	service := model.Chain{{ID: "service", Class: model.Class{Nature: "service", Billing: "hourly"}}}
	lifeHours := func(params map[string]any) model.Steps {
		return model.Steps{"life": {Handler: "life.hours", Params: params}}
	}
	for _, tc := range []struct {
		name   string
		chain  model.Chain
		item   model.Steps
		unit   model.Steps
		step   Step
		reason string
	}{
		{"unknown handler", durableChain(), model.Steps{"life": {Handler: "life.forever"}}, nil, "life", "unknown_handler"},
		{"wrong step", durableChain(), model.Steps{"life": {Handler: "count.lot"}}, nil, "life", "wrong_step"},
		{"not applicable", durableChain(), model.Steps{"bill": {Handler: "bill.hourly"}}, nil, "bill", "not_applicable"},
		{"unknown param", durableChain(), model.Steps{"life": {Params: map[string]any{"hourz": 5}}}, nil, "life", "unknown_param"},
		{"a string is no number", durableChain(), model.Steps{"life": {Params: map[string]any{"hours": "ten"}}}, nil, "life", "wrong_type"},
		{"a boolean is no number", durableChain(), model.Steps{"life": {Params: map[string]any{"hours": true}}}, nil, "life", "wrong_type"},
		{"not in enum", durableChain(), model.Steps{"life": {Params: map[string]any{"source": "solar"}}}, nil, "life", "not_in_enum"},
		{"missing param", durableChain()[:1], lifeHours(map[string]any{"source": "manual"}), nil, "life", "missing_param"},
		{"unit names a handler", durableChain(), nil, model.Steps{"life": {Handler: "life.uses"}}, "life", "unit_names_handler"},
		{"hourly service", service, model.Steps{"bill": {Params: map[string]any{"cost_rate_minor": 1.5}}}, nil, "bill", "wrong_type"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, problems, err := Builtin().Resolve(tc.chain, tc.item, tc.unit)
			if err != nil {
				t.Fatal(err)
			}
			if len(problems) != 1 || problems[0].Step != tc.step || problems[0].Reason != tc.reason {
				t.Fatalf("problems = %+v, want one %s on %s", problems, tc.reason, tc.step)
			}
			if tc.reason == "missing_param" {
				if len(Blocking(problems)) != 0 || len(Incomplete(problems)) != 1 {
					t.Fatalf("Blocking %+v, Incomplete %+v", Blocking(problems), Incomplete(problems))
				}
				if !reflect.DeepEqual(problems[0].Missing, []string{"hours"}) {
					t.Fatalf("Missing = %v", problems[0].Missing)
				}
			} else if len(Blocking(problems)) != 1 {
				t.Fatalf("Blocking = %+v", Blocking(problems))
			}
		})
	}
}

func TestAKindMayLeaveItsClassOpen(t *testing.T) {
	ch := model.Chain{{ID: "consumable", Class: model.Class{Nature: "physical", Role: "consumable"}}}
	if _, _, err := Builtin().ResolveKind(ch); err != nil {
		t.Fatalf("kind: %v", err)
	}
	if _, _, err := Builtin().Resolve(ch, model.Steps{}, nil); !errors.Is(err, model.ErrClassOpen) {
		t.Fatalf("item: got %v, want ErrClassOpen", err)
	}
}

func TestAnItemWithNoStepsNeedsAClosedClass(t *testing.T) {
	ch := model.Chain{{ID: "consumable", Class: model.Class{Nature: "physical", Role: "consumable"}}}
	if _, _, err := Builtin().Resolve(ch, nil, nil); !errors.Is(err, model.ErrClassOpen) {
		t.Fatalf("got %v, want ErrClassOpen", err)
	}
}

func TestAClassThatDoesNotResolveIsAnError(t *testing.T) {
	changed := model.Chain{
		{ID: "consumable", Class: model.Class{Nature: "physical", Role: "consumable"}},
		{ID: "odd", Class: model.Class{Role: "durable"}},
	}
	var cc *model.ClassChanged
	if _, _, err := Builtin().ResolveKind(changed); !errors.As(err, &cc) {
		t.Fatalf("got %v, want *ClassChanged", err)
	}
	lot := model.Chain{{ID: "d", Class: model.Class{Nature: "physical", Role: "durable", Counting: "lot"}}}
	if _, _, err := Builtin().ResolveKind(lot); !errors.Is(err, model.ErrDurableLot) {
		t.Fatalf("got %v, want ErrDurableLot", err)
	}
}

func TestASpecialTypeNeedsItsClass(t *testing.T) {
	ch := model.Chain{{ID: "odd", Class: model.Class{Nature: "physical", Role: "durable", Counting: "unit"}, Special: "filament_spool"}}
	res, problems, err := Builtin().Resolve(ch, model.Steps{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.Special != "filament_spool" {
		t.Fatalf("Special = %q", res.Special)
	}
	found := false
	for _, p := range problems {
		if p.Reason == "special_needs" {
			found = true
		}
	}
	if !found {
		t.Fatalf("problems = %+v, want special_needs", problems)
	}
}

func TestASpecialTypeReplacesTheDefaults(t *testing.T) {
	ch := model.Chain{
		{ID: "consumable", Class: model.Class{Nature: "physical", Role: "consumable"}},
		{ID: "filament", Class: model.Class{Counting: "unit"}, Special: "filament_spool"},
	}
	res, problems, err := Builtin().Resolve(ch, model.Steps{}, nil)
	if err != nil || len(problems) != 0 {
		t.Fatalf("err %v, problems %+v", err, problems)
	}
	if res.Steps["count"].Handler != "filament.weigh" || res.Steps["life"].Handler != "life.none" {
		t.Fatalf("steps = %+v", res.Steps)
	}
}

func TestRegistryNamesAHandlerNotBuilt(t *testing.T) {
	r := &Registry{Catalog: Builtin()}
	if _, err := r.For("life", "life.hours"); !errors.As(err, new(ErrNotBuilt)) {
		t.Fatalf("got %v, want ErrNotBuilt", err)
	}
	r.Implement("life.hours", "impl")
	if got, err := r.For("life", "life.hours"); err != nil || got != "impl" {
		t.Fatalf("got %v, %v", got, err)
	}
	if _, err := r.For("count", "life.hours"); err == nil {
		t.Fatal("For took a handler on a step it does not fill")
	}
}

func TestResolveKindChecksWhatItNames(t *testing.T) {
	consumable := model.Kind{ID: "consumable", Class: model.Class{Nature: "physical", Role: "consumable"}}
	ch := model.Chain{consumable, {ID: "odd", ParentID: "consumable", Steps: model.Steps{
		"life": {Handler: "life.hours", Params: map[string]any{"hours": 10, "source": "manual"}},
	}}}
	_, problems, err := Builtin().ResolveKind(ch)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 1 || problems[0].Step != "life" || problems[0].Reason != NotApplicable {
		t.Fatalf("problems = %+v, want one not_applicable on life", problems)
	}

	// cost.unit applies to counting = unit; the kind leaves counting open, so a child may
	// still answer it.
	ch = model.Chain{consumable, {ID: "odd", ParentID: "consumable", Steps: model.Steps{"cost": {Handler: "cost.unit"}}}}
	res, problems, err := Builtin().ResolveKind(ch)
	if err != nil || len(problems) != 0 {
		t.Fatalf("err %v, problems %+v", err, problems)
	}
	if res.Steps["cost"].Handler != "cost.unit" {
		t.Fatalf("steps = %+v", res.Steps)
	}
}

func TestResolveKindChecksTheSpecialTypesSetKeys(t *testing.T) {
	open := model.Chain{{ID: "k", Class: model.Class{Nature: "physical", Role: "consumable"}, Special: "filament_spool"}}
	if _, problems, err := Builtin().ResolveKind(open); err != nil || len(problems) != 0 {
		t.Fatalf("open counting: err %v, problems %+v", err, problems)
	}
	wrong := model.Chain{{ID: "k", Class: model.Class{Nature: "physical", Role: "durable"}, Special: "filament_spool"}}
	_, problems, err := Builtin().ResolveKind(wrong)
	if err != nil || len(problems) != 1 || problems[0].Reason != SpecialNeeds {
		t.Fatalf("durable: err %v, problems %+v", err, problems)
	}
	if _, _, err := Builtin().Resolve(open, nil, nil); !errors.Is(err, model.ErrClassOpen) {
		t.Fatalf("item: got %v", err)
	}
}
