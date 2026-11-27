package jsonv

import (
	"encoding/json"
	"os"
	"testing"
)

func TestLoadsWritesWhatPythonsJSONWrites(t *testing.T) {
	raw, err := os.ReadFile("../../../../../backend/tests/fixtures/python-json-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct{ Input, Output string }
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatal("no vectors")
	}
	for _, c := range cases {
		v, err := Loads([]byte(c.Input))
		if err != nil {
			t.Fatalf("%s: %v", c.Input, err)
		}
		if got := Dumps(v); got != c.Output {
			t.Errorf("%s:\n got %s\nwant %s", c.Input, got, c.Output)
		}
	}
}
