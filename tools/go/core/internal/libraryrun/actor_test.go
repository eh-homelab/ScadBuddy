package libraryrun

import (
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
)

func TestActorIsPythons(t *testing.T) {
	for _, c := range []struct {
		caller auth.Caller
		want   string
	}{
		{auth.Caller{Kind: auth.KindPerson, Principal: "user:test#ann"}, "user:test#ann"},
		{auth.Caller{Kind: auth.KindToken, Principal: "user:test#bob", TokenScope: "write"}, "user:test#bob"},
		{auth.Caller{Kind: auth.KindService, Service: &auth.Service{Caller: "agent"}}, "service:agent"},
		{auth.Caller{Kind: auth.KindAnonymous}, "anonymous"},
		{auth.Caller{}, "anonymous"},
	} {
		if got := Actor(c.caller); got != c.want {
			t.Errorf("%+v: %q", c.caller, got)
		}
	}
}
