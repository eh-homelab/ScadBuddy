package sessions

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
)

// The browser options' shared vectors (#7027): the bounds and the values that
// workers/playwright-mcp/options.mjs and this package must both take or refuse.
const browserVectors = "../../../../../workers/playwright-mcp/browser-vectors.json"

type vector struct {
	Name  string          `json:"name"`
	Value json.RawMessage `json:"value"`
}

func TestTheBrowserOptionsMatchTheSharedVectors(t *testing.T) {
	raw, err := os.ReadFile(browserVectors)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Bounds struct {
			UserAgentPattern string   `json:"user_agent_pattern"`
			LocalePattern    string   `json:"locale_pattern"`
			LocaleMax        int      `json:"locale_max"`
			ViewportWidth    [2]int64 `json:"viewport_width"`
			ViewportHeight   [2]int64 `json:"viewport_height"`
		} `json:"bounds"`
		Valid   []vector `json:"valid"`
		Invalid []vector `json:"invalid"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	b := doc.Bounds
	if b.UserAgentPattern != uaPattern.String() || b.LocalePattern != localePattern.String() ||
		b.LocaleMax != browserLocaleMx || b.ViewportWidth != [2]int64{viewportWMin, viewportWMax} ||
		b.ViewportHeight != [2]int64{viewportHMin, viewportHMax} {
		t.Errorf("bounds %+v differ from this package's", b)
	}
	parse := func(v vector) error {
		body, err := crud.ParseBody(v.Value, "user_agent", "viewport", "locale")
		if err != nil {
			return err
		}
		browserOf(body)
		return body.Err()
	}
	for _, v := range doc.Valid {
		if err := parse(v); err != nil {
			t.Errorf("%s: refused: %v", v.Name, err)
		}
	}
	for _, v := range doc.Invalid {
		if err := parse(v); err == nil {
			t.Errorf("%s: taken", v.Name)
		}
	}
	if len(doc.Valid) == 0 || len(doc.Invalid) == 0 {
		t.Fatal("no vectors")
	}
}
