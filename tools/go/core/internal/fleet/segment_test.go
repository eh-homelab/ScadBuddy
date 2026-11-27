package fleet

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
)

// channelOf reads a {name} segment as fleetchat's ChannelSegment writes it (#8149), held to
// fleetchat's vectors. The routes test (core's fleet_channels_test.go) runs them over HTTP too,
// save the bad segments that need escapes, which the router leaves to Python.
func TestChannelOfHoldsToTheVectors(t *testing.T) {
	raw, err := os.ReadFile("../../../fleetchat/testdata/channel-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		Valid, Legacy, Invalid []string
		Segments               map[string]string
		BadSegments            []string
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if len(v.Segments) == 0 || len(v.BadSegments) == 0 {
		t.Fatal("channel-vectors.json lacks segments")
	}
	for _, ch := range append(append([]string{}, v.Valid...), v.Legacy...) {
		segment, ok := v.Segments[ch]
		if !ok {
			segment = ch
		}
		if got, err := channelOf(crud.Params{"name": segment}); err != nil || got != ch {
			t.Errorf("channelOf(%q) = %q, %v; want %q", segment, got, err, ch)
		}
	}
	for _, seg := range append(append([]string{}, v.BadSegments...), v.Invalid...) {
		if got, err := channelOf(crud.Params{"name": seg}); err == nil {
			t.Errorf("channelOf(%q) = %q; want a 422", seg, got)
		}
	}
}
