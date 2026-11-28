package filamentpresets

import "github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"

// actor is api/filament.py actor: who a write names. A person by their principal, the same
// through a session or a token of theirs; a service by its name; else anonymous.
func actor(c auth.Caller) string {
	switch c.Kind {
	case auth.KindPerson, auth.KindToken:
		return c.Principal
	case auth.KindService:
		if c.Service != nil {
			return "service:" + c.Service.Caller
		}
	}
	return "anonymous"
}
