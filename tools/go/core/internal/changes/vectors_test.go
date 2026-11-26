package changes

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
)

// The callers' filters against the shared vectors (backend/tests/fixtures/
// event-stream-vectors.json), which Python's keep_agent, keep_fleet and keep_camera wrote.
func TestEachCallerKeepsWhatPythonKeeps(t *testing.T) {
	raw, err := os.ReadFile("../../../../../backend/tests/fixtures/event-stream-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		Changes []struct {
			Name, Channel, Payload string
			Want                   struct {
				Live string
				Keep map[string]bool
			}
		}
	}
	if err := json.Unmarshal(raw, &v); err != nil || len(v.Changes) == 0 {
		t.Fatalf("no vectors: %v", err)
	}
	keeps := map[string]func(*bus.Notice) bool{"agent": KeepAgent, "fleet": KeepFleet, "camera": KeepCamera}
	for _, c := range v.Changes {
		channel := c.Channel
		if channel == "" {
			channel = bus.Channel
		}
		n := &bus.Notice{Channel: channel, Payload: c.Payload}
		if channel == bus.Channel {
			n.Event, _ = bus.Decode(c.Payload)
		}
		for caller, keep := range keeps {
			if got := keep(n); got != c.Want.Keep[caller] {
				t.Errorf("%s: %s keeps %v, want %v", c.Name, caller, got, c.Want.Keep[caller])
			}
		}
		if got := bus.ChangeFrame(channel, c.Payload, n.LoggedID()); got != c.Want.Live {
			t.Errorf("%s: live frame\n got %q\nwant %q", c.Name, got, c.Want.Live)
		}
	}
}
