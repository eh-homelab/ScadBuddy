package bus

import (
	"strings"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

func topics(t ...string) []httpx.Field { return []httpx.Field{{Key: "topics", Value: t}} }

func TestValidEnvelope(t *testing.T) {
	long := "fleet:" + strings.Repeat("é", TopicMax-len("fleet:"))
	for _, c := range []struct {
		name   string
		kind   string
		fields []httpx.Field
		want   bool
	}{
		{"no topics", "fleet.message", nil, true},
		{"a topic in the namespace", "fleet.message", topics("fleet:agent:x"), true},
		{"a deeper kind", "fleet.message.sent_now", topics("fleet:x"), true},
		{"a topic of 200 characters", "fleet.message", topics(long), true},
		{"a topic of 201 characters", "fleet.message", topics(long + "x"), false},
		{"a topic outside", "fleet.message", topics("tasks"), false},
		{"topics not strings", "fleet.message", []httpx.Field{{Key: "topics", Value: "fleet:x"}}, false},
		{"a kind with no dot", "fleet", nil, false},
		{"a kind in upper case", "Fleet.message", nil, false},
		{"a kind with a dash", "fleet.new-message", nil, false},
		{"a kind with an empty part", "fleet..message", nil, false},
		{"a kind that starts with a digit", "1fleet.message", nil, false},
		{"an empty kind", "", nil, false},
	} {
		if got := validEnvelope(c.kind, c.fields); got != c.want {
			t.Errorf("%s: validEnvelope(%q) = %v, want %v", c.name, c.kind, got, c.want)
		}
	}
}
