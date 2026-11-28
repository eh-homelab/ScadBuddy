package modeldata

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

// normal is v after a JSON round trip, so a YAML int and a JSON number compare equal.
func normal(t *testing.T, v any) any {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// TestSeededKindsMatchTheProof: the K1 migration (design §8) seeds every root, every
// `seed: true` kind and every phase 2 built-in kind (`was.tracking`) as kinds.yaml has
// it. A kind's class columns hold only its own keys (C22). `printer` has no special type
// until K6 builds its handlers (R-K1-7).
func TestSeededKindsMatchTheProof(t *testing.T) {
	m := load(t)
	pool := testdb.New(t)
	ctx := context.Background()
	for _, k := range m.Kinds {
		if k.Parent != "" && !k.Seed && k.Was["tracking"] == nil {
			continue
		}
		var name, uom string
		var parent, nature, role, counting, billing, special *string
		var steps []byte
		var builtin bool
		err := pool.QueryRow(ctx, "SELECT name, parent_id, nature, role, counting, billing,"+
			" special_type, uom, steps, builtin FROM item_kinds WHERE id = $1", k.ID).
			Scan(&name, &parent, &nature, &role, &counting, &billing, &special, &uom, &steps, &builtin)
		if err != nil {
			t.Errorf("kind %s: %v", k.ID, err)
			continue
		}
		str := func(p *string) string {
			if p == nil {
				return ""
			}
			return *p
		}
		wantUOM := k.UOM
		if wantUOM == "" {
			wantUOM = "each"
		}
		wantSpecial := k.Special
		if k.ID == "printer" {
			wantSpecial = ""
		}
		got := map[string]string{
			"name": name, "parent": str(parent), "uom": uom, "special": str(special),
			"nature": str(nature), "role": str(role), "counting": str(counting), "billing": str(billing),
		}
		want := map[string]string{
			"name": k.Name, "parent": k.Parent, "uom": wantUOM, "special": wantSpecial,
			"nature": k.Class["nature"], "role": k.Class["role"],
			"counting": k.Class["counting"], "billing": k.Class["billing"],
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("kind %s: got %v, want %v", k.ID, got, want)
		}
		chain, err := Chain(m, k.ID)
		if err != nil {
			t.Fatal(err)
		}
		if leaf := chain[len(chain)-1]; leaf.Builtin != builtin {
			t.Errorf("kind %s: builtin %v, Chain says %v", k.ID, builtin, leaf.Builtin)
		}
		var gotSteps any
		if err := json.Unmarshal(steps, &gotSteps); err != nil {
			t.Fatal(err)
		}
		wantSteps := normal(t, ToSteps(k.Steps))
		if wantSteps == nil {
			wantSteps = map[string]any{}
		}
		if !reflect.DeepEqual(gotSteps, wantSteps) {
			t.Errorf("kind %s steps: got %v, want %v", k.ID, gotSteps, wantSteps)
		}
	}
}
