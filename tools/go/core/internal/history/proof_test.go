package history

import (
	"encoding/json"
	"maps"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"go.yaml.in/yaml/v3"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/modeldata"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// proof is the part of docs/inventory-model/examples.yaml this test reads (design §7).
type proof struct {
	Items   []proofItem `yaml:"items"`
	History []struct {
		Do     string `yaml:"do"`
		Target string `yaml:"target"`
		Key    string `yaml:"key"`
		Value  any    `yaml:"value"`
	} `yaml:"history"`
	Expect map[string][]struct {
		Key   string `yaml:"key"`
		Op    string `yaml:"op"`
		Old   any    `yaml:"old"`
		New   any    `yaml:"new"`
		Actor string `yaml:"actor"`
	} `yaml:"expect_property_history"`
}

type proofItem struct {
	ID         string         `yaml:"id"`
	Properties map[string]any `yaml:"properties"`
}

// The proof's two actors, by the names expect_property_history gives them.
var actors = map[string]string{
	"import": `{"kind":"provider","principal":"import"}`,
	"person": `{"kind":"browser"}`,
}

func readProof(t *testing.T) proof {
	t.Helper()
	dir, err := modeldata.Dir()
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(dir, "examples.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var p proof
	if err := yaml.Unmarshal(raw, &p); err != nil {
		t.Fatal(err)
	}
	return p
}

func jsonOf(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// TestPropertyHistoryReplaysTheProof makes the proof's items, applies its property events,
// and reads back the property history the proof expects (design §4.8).
func TestPropertyHistoryReplaysTheProof(t *testing.T) {
	p := readProof(t)
	pool := testdb.New(t)
	ids := map[string]string{}
	for ref := range p.Expect {
		itemID := strings.TrimPrefix(ref, "item:")
		i := slices.IndexFunc(p.Items, func(it proofItem) bool { return it.ID == itemID })
		if i < 0 {
			t.Fatalf("expect_property_history names %s, which items does not have", ref)
		}
		var id string
		if err := pool.QueryRow(ctx, `INSERT INTO entities (kind, ref) VALUES ('item', gen_random_uuid()::text)
			RETURNING id::text`).Scan(&id); err != nil {
			t.Fatal(err)
		}
		ids[ref] = id
		for _, k := range slices.Sorted(maps.Keys(p.Items[i].Properties)) {
			set(t, pool, id, k, jsonOf(t, p.Items[i].Properties[k]), actors["import"])
		}
	}
	for _, e := range p.History {
		id, ok := ids[e.Target]
		switch {
		case e.Do == "set_property" && ok:
			set(t, pool, id, e.Key, jsonOf(t, e.Value), actors["person"])
		case e.Do == "remove_property" && ok:
			remove(t, pool, id, e.Key, actors["person"])
		case e.Do == "set_property" || e.Do == "remove_property":
			t.Fatalf("history changes %s, which expect_property_history does not name", e.Target)
		}
	}
	apply(t, pool)

	names := map[string]string{}
	for name, actor := range actors {
		names[string(normalJSON(t, []byte(actor)))] = name
	}
	type row struct {
		Key, Op  string
		Old, New any
		Actor    string
	}
	for ref, want := range p.Expect {
		var got []row
		history, err := pool.Query(ctx, `SELECT key, op, old, new, actor FROM entity_property_history
			WHERE entity_id = $1 ORDER BY event_seq`, ids[ref])
		if err != nil {
			t.Fatal(err)
		}
		for history.Next() {
			var r row
			var old, new, actor []byte
			if err := history.Scan(&r.Key, &r.Op, &old, &new, &actor); err != nil {
				t.Fatal(err)
			}
			r.Old, r.New, r.Actor = decoded(t, old), decoded(t, new), names[string(normalJSON(t, actor))]
			got = append(got, r)
		}
		if err := history.Err(); err != nil {
			t.Fatal(err)
		}
		var expected []row
		for _, w := range want {
			expected = append(expected, row{w.Key, w.Op, normal(t, w.Old), normal(t, w.New), w.Actor})
		}
		if !reflect.DeepEqual(got, expected) {
			t.Errorf("%s: history\n%v\nwant\n%v", ref, got, expected)
		}

		// The last row per key is the current properties.
		replayed := map[string]any{}
		for _, r := range got {
			if r.Op == "remove" {
				delete(replayed, r.Key)
			} else {
				replayed[r.Key] = r.New
			}
		}
		current := scalar[map[string]any](t, pool, `SELECT coalesce(jsonb_object_agg(key, value), '{}')
			FROM entity_properties WHERE entity_id = $1`, ids[ref])
		if !reflect.DeepEqual(replayed, normal(t, current)) {
			t.Errorf("%s: history replays to %v, entity_properties holds %v", ref, replayed, current)
		}
	}
}

func decoded(t *testing.T, raw []byte) any {
	t.Helper()
	if raw == nil {
		return nil
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func normalJSON(t *testing.T, raw []byte) []byte {
	t.Helper()
	b, err := json.Marshal(decoded(t, raw))
	if err != nil {
		t.Fatal(err)
	}
	return b
}
