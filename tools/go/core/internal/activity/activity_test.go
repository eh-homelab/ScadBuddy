package activity

import (
	"encoding/json"
	"os"
	"testing"
)

func TestTitleIsPythonsCapitalizedKindAndSubject(t *testing.T) {
	raw, err := os.ReadFile("../../../../../backend/tests/fixtures/activity-title-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct{ Kind, Subject, Title string }
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatal("no vectors")
	}
	for _, c := range cases {
		if got := Title(c.Kind, c.Subject); got != c.Title {
			t.Errorf("Title(%q, %q) = %q, want %q", c.Kind, c.Subject, got, c.Title)
		}
	}
}
