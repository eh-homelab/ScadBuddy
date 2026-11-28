package store

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
)

// TestSpecialRequiresArePythons reads testdata/special_requires.json, which backend
// tests/inventory/test_kinds_required.py reads too, so Python's SPECIAL_REQUIRES and the
// catalog's requires_properties cannot drift (R-K1-33).
func TestSpecialRequiresArePythons(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "special_requires.json"))
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		SpecialTypes map[string][]string `json:"special_types"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	got := map[string][]string{}
	for id, sp := range handlers.Builtin().Special {
		got[id] = sp.RequiresProperties
	}
	if !reflect.DeepEqual(got, file.SpecialTypes) {
		t.Errorf("the catalog's requires_properties are %v, the vector's %v", got, file.SpecialTypes)
	}
}
