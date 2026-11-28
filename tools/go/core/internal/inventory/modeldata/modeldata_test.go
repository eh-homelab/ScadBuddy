package modeldata

import (
	"strings"
	"testing"
)

func TestTheProofLoads(t *testing.T) {
	dir, err := Dir()
	if err != nil {
		t.Fatal(err)
	}
	m, err := Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	for name, c := range map[string][2]int{
		"steps":     {len(m.Steps), 10},
		"handlers":  {len(m.Handlers), 38},
		"defaults":  {len(m.Defaults), 6},
		"special":   {len(m.Special), 2},
		"kinds":     {len(m.Kinds), 33},
		"items":     {len(m.Items), 16},
		"instances": {len(m.Instances), 17},
		"relations": {len(m.Relations), 16},
		"history":   {len(m.History), 16},
	} {
		if c[0] != c[1] {
			t.Errorf("%s: got %d, want %d", name, c[0], c[1])
		}
	}
	if !strings.Contains(m.Readme, "<!-- matrix:start -->") {
		t.Error("readme has no matrix marker")
	}
}
