package modeldata

import (
	"errors"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
)

func load(t *testing.T) *Model {
	t.Helper()
	m, err := Load(Dir())
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func steps(in []handlers.Step) []string {
	var out []string
	for _, s := range in {
		out = append(out, string(s))
	}
	return out
}

func byName(m map[handlers.Step]string) map[string]string {
	out := map[string]string{}
	for k, v := range m {
		out[string(k)] = v
	}
	return out
}

func TestTheCatalogIsTheProof(t *testing.T) {
	m, cat := load(t), handlers.Builtin()
	if !reflect.DeepEqual(m.Steps, steps(handlers.Steps)) {
		t.Errorf("steps: yaml %v, go %v", m.Steps, handlers.Steps)
	}
	if len(cat.Specs) != len(m.Handlers) {
		t.Errorf("handlers: yaml %d, go %d", len(m.Handlers), len(cat.Specs))
	}
	for _, h := range m.Handlers {
		s, ok := cat.Specs[h.ID]
		if !ok {
			t.Errorf("handler %s is not in the catalog", h.ID)
			continue
		}
		if !reflect.DeepEqual(steps(s.Steps), h.Steps) {
			t.Errorf("%s steps: yaml %v, go %v", h.ID, h.Steps, s.Steps)
		}
		if !reflect.DeepEqual(s.Applies, orEmpty(h.Applies)) {
			t.Errorf("%s applies: yaml %v, go %v", h.ID, h.Applies, s.Applies)
		}
		if len(s.Params) != len(h.Params) {
			t.Errorf("%s params: yaml %v, go %v", h.ID, h.Params, s.Params)
		}
		for k, p := range h.Params {
			g, ok := s.Params[k]
			if !ok || g.Type != p.Type || g.Required != p.Required || !slices.Equal(g.Enum, p.Enum) {
				t.Errorf("%s param %s: yaml %+v, go %+v", h.ID, k, p, g)
			}
		}
	}

	if len(cat.Columns) != len(m.Defaults) {
		t.Errorf("columns: yaml %d, go %d", len(m.Defaults), len(cat.Columns))
	}
	for _, col := range cat.Columns {
		d, ok := m.Defaults[col.Name]
		if !ok {
			t.Errorf("column %s is not in the yaml", col.Name)
			continue
		}
		if !reflect.DeepEqual(classMap(col.Match), d.Match) {
			t.Errorf("column %s match: yaml %v, go %v", col.Name, d.Match, col.Match)
		}
		if !reflect.DeepEqual(byName(col.Handlers), d.Handlers) {
			t.Errorf("column %s handlers: yaml %v, go %v", col.Name, d.Handlers, col.Handlers)
		}
	}

	if len(cat.Special) != len(m.Special) {
		t.Errorf("special types: yaml %d, go %d", len(m.Special), len(cat.Special))
	}
	for id, sp := range m.Special {
		g, ok := cat.Special[id]
		if !ok {
			t.Errorf("special type %s is not in the catalog", id)
			continue
		}
		if !reflect.DeepEqual(classMap(g.Needs), orEmpty(sp.Needs)) {
			t.Errorf("%s needs: yaml %v, go %v", id, sp.Needs, g.Needs)
		}
		if !reflect.DeepEqual(byName(g.Handlers), sp.Handlers) {
			t.Errorf("%s handlers: yaml %v, go %v", id, sp.Handlers, g.Handlers)
		}
		if !slices.Equal(g.RequiresProperties, sp.RequiresProperties) ||
			!slices.Equal(g.Panels, sp.Panels) || !slices.Equal(g.Tools, sp.Tools) {
			t.Errorf("%s requires_properties, panels or tools differ: yaml %+v, go %+v", id, sp, g)
		}
	}
}

func classMap(c model.Class) map[string]string {
	out := map[string]string{}
	for _, k := range []string{"nature", "role", "counting", "billing"} {
		if v := c.Get(k); v != "" {
			out[k] = v
		}
	}
	return out
}

func orEmpty(m map[string]string) map[string]string {
	if m == nil {
		return map[string]string{}
	}
	return m
}

func TestEveryKindHasAClass(t *testing.T) {
	m := load(t)
	if len(m.Kinds) != 33 {
		t.Fatalf("%d kinds, want 33", len(m.Kinds))
	}
	for _, k := range m.Kinds {
		ch, err := Chain(m, k.ID)
		if err != nil {
			t.Errorf("kind %s: %v", k.ID, err)
			continue
		}
		if _, _, err := ch.Class(); err != nil {
			t.Errorf("kind %s: %v", k.ID, err)
		}
	}
}

func TestChainRefusesAnUnknownKind(t *testing.T) {
	if _, err := Chain(load(t), "no_such_kind"); err == nil {
		t.Error("an unknown kind resolved")
	}
}

func TestEveryExampleItemResolves(t *testing.T) {
	m, cat := load(t), handlers.Builtin()
	if len(m.Items) != 16 {
		t.Fatalf("%d items, want 16", len(m.Items))
	}
	for _, it := range m.Items {
		ch, err := Chain(m, it.Kind)
		if err != nil {
			t.Errorf("item %s: %v", it.ID, err)
			continue
		}
		res, problems, err := cat.Resolve(ch, ToSteps(it.Steps), nil)
		if err != nil {
			t.Errorf("item %s: %v", it.ID, err)
			continue
		}
		if len(problems) > 0 {
			t.Errorf("item %s: %+v", it.ID, problems)
		}
		if res.Special != "" {
			for _, p := range m.Special[res.Special].RequiresProperties {
				if _, ok := it.Properties[p]; !ok {
					t.Errorf("item %s: special type %s requires property %s", it.ID, res.Special, p)
				}
			}
		}
		for _, id := range kindIDs(t, m, it.Kind) {
			for prop, spec := range kindByID(m, id).Schema {
				if s, _ := spec.(map[string]any); s["required"] == true {
					if _, ok := it.Properties[prop]; !ok {
						t.Errorf("item %s: kind %s requires property %s", it.ID, id, prop)
					}
				}
			}
		}
	}
}

func kindByID(m *Model, id string) Kind {
	for _, k := range m.Kinds {
		if k.ID == id {
			return k
		}
	}
	return Kind{}
}

func kindIDs(t *testing.T, m *Model, id string) []string {
	t.Helper()
	ch, err := Chain(m, id)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, k := range ch {
		out = append(out, k.ID)
	}
	return out
}

func TestEveryInstanceFitsItsClass(t *testing.T) {
	m, cat := load(t), handlers.Builtin()
	items := map[string]Item{}
	for _, it := range m.Items {
		items[it.ID] = it
	}
	for _, in := range m.Instances {
		it, ok := items[in.Item]
		if !ok {
			t.Errorf("instance %s: unknown item %s", in.ID, in.Item)
			continue
		}
		ch, err := Chain(m, it.Kind)
		if err != nil {
			t.Fatal(err)
		}
		res, _, err := cat.Resolve(ch, ToSteps(it.Steps), nil)
		if err != nil {
			t.Fatal(err)
		}
		cls := res.Class
		want := "unit"
		if cls.Counting == "lot" {
			want = "lot"
		}
		if cls.Nature == "service" {
			if cls.Billing != "term" {
				t.Errorf("instance %s: a %s service has no instances", in.ID, cls.Billing)
			}
			if in.StartsAt == "" || in.EndsAt == "" {
				t.Errorf("instance %s: a term needs starts_at and ends_at", in.ID)
			}
		}
		if in.Type != want {
			t.Errorf("instance %s is a %s, its class counts by %s", in.ID, in.Type, want)
		}
	}
}

func TestTheReadmeTableIsTheResolvedOne(t *testing.T) {
	m := load(t)
	got, err := Matrix(m, handlers.Builtin())
	if err != nil {
		t.Fatal(err)
	}
	_, rest, ok := strings.Cut(m.Readme, "<!-- matrix:start -->\n")
	if !ok {
		t.Fatal("no matrix start marker")
	}
	want, _, ok := strings.Cut(rest, "\n<!-- matrix:end -->")
	if !ok {
		t.Fatal("no matrix end marker")
	}
	if got != want {
		t.Errorf("the README table is not the resolved one.\ngot:\n%s\nwant:\n%s", got, want)
	}
}

func TestMatrixRefusesAnItemWhoseKindLeavesItsClassOpen(t *testing.T) {
	m := load(t)
	m.Items = []Item{{ID: "x", Name: "X", Kind: "consumable"}}
	if _, err := Matrix(m, handlers.Builtin()); !errors.Is(err, model.ErrClassOpen) {
		t.Errorf("got %v, want ErrClassOpen", err)
	}
}
