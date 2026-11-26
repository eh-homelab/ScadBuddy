package realtime

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
)

// The topics against the shared vectors (backend/tests/fixtures/event-stream-vectors.json),
// which Python's valid_topic wrote.
func TestATopicIsValidAsPythonSays(t *testing.T) {
	raw, err := os.ReadFile("../../../../../backend/tests/fixtures/event-stream-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		Topics []struct {
			Topic string
			Valid bool
		}
	}
	if err := json.Unmarshal(raw, &v); err != nil || len(v.Topics) == 0 {
		t.Fatalf("no vectors: %v", err)
	}
	for _, c := range v.Topics {
		if got := ValidTopic(bus.Value{Kind: 's', Str: c.Topic}); got != c.Valid {
			t.Errorf("%q: valid %v, want %v", c.Topic, got, c.Valid)
		}
	}
}

func TestAnOriginIsNormalisedAsPythonDoes(t *testing.T) {
	for in, want := range map[string]string{
		"https://ScadBuddy.Example:443": "https://scadbuddy.example",
		"http://localhost:5173":         "http://localhost:5173",
		"http://[::1]:80":               "http://[::1]",
		"ftp://x":                       "",
		"null":                          "",
	} {
		got, _ := normalisedOrigin(in)
		if got != want {
			t.Errorf("%s: %q, want %q", in, got, want)
		}
	}
}
