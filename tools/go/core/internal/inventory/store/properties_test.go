package store

import (
	"bytes"
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// propertyVectors is testdata/property_values.json, which backend/tests/entities/
// test_property_vectors.py also runs: Python's answers for the value rules that Go ports
// (entities/properties.py _shape_problem, has_value, best_match(...).validator and
// completeness).
type propertyVectors struct {
	Shape []struct {
		Value   any     `json:"value"`
		Problem *string `json:"problem"`
	} `json:"shape"`
	HasValue []struct {
		Value any  `json:"value"`
		Has   bool `json:"has"`
	} `json:"has_value"`
	Fit []struct {
		Schema map[string]any `json:"schema"`
		Value  any            `json:"value"`
		Fails  *string        `json:"fails"`
	} `json:"fit"`
	Completeness []struct {
		Schema struct {
			Properties map[string]json.RawMessage `json:"properties"`
			Required   []string                   `json:"required"`
		} `json:"schema"`
		Values  map[string]any `json:"values"`
		Missing []string       `json:"missing"`
		Invalid []string       `json:"invalid"`
	} `json:"completeness"`
}

func TestPropertyValuesMatchPython(t *testing.T) {
	text, err := os.ReadFile("testdata/property_values.json")
	if err != nil {
		t.Fatal(err)
	}
	dec := json.NewDecoder(bytes.NewReader(text))
	dec.UseNumber()
	var v propertyVectors
	if err := dec.Decode(&v); err != nil {
		t.Fatal(err)
	}
	if len(v.Shape) == 0 || len(v.HasValue) == 0 || len(v.Fit) == 0 || len(v.Completeness) == 0 {
		t.Fatal("a list of vectors is empty")
	}
	for _, c := range v.Shape {
		want := ""
		if c.Problem != nil {
			want = *c.Problem
		}
		if got := shapeProblem(c.Value); got != want {
			t.Errorf("shapeProblem(%.40v) = %q, want %q", c.Value, got, want)
		}
	}
	for _, c := range v.HasValue {
		if got := hasValue(c.Value); got != c.Has {
			t.Errorf("hasValue(%#v) = %v, want %v", c.Value, got, c.Has)
		}
	}
	for _, c := range v.Fit {
		want := ""
		if c.Fails != nil {
			want = *c.Fails
		}
		keyword, ok := fits(c.Value, c.Schema)
		if ok != (want == "") || keyword != want {
			t.Errorf("fits(%v, %v) = %q, %v; want %q", c.Value, c.Schema, keyword, ok, want)
		}
	}
	for _, c := range v.Completeness {
		missing, invalid := completeness(c.Schema.Properties, c.Schema.Required, c.Values)
		keys := []string{}
		for _, i := range invalid {
			keys = append(keys, i.Key)
			if i.Reason == "" {
				t.Errorf("completeness(%v): %s has no reason", c.Values, i.Key)
			}
		}
		if !reflect.DeepEqual(missing, c.Missing) || !reflect.DeepEqual(keys, c.Invalid) {
			t.Errorf("completeness(%v) = %v, %v; want %v, %v", c.Values, missing, keys, c.Missing, c.Invalid)
		}
	}
}
