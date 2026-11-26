// Package access is the agent's tool access, tool listings, leases and plugin sessions:
// backend/scadbuddy/api/crud_agent_access.py (removed in #7667) with agent_data/access.py and leases.py,
// statement for statement (areas access and plugin-sessions). Each route is one of the agent
// store's methods and runs its SQL, a transaction as one transaction (#5299 D2).
package access

import (
	"sync"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// Store is the routes' state: the pool, and the acquires that wait on a grant's row now.
type Store struct {
	Pool *pgxpool.Pool

	mu       sync.Mutex
	rowWaits int
}

// Areas are the package's routes by area name.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	s := &Store{Pool: pool}
	row := crud.Route{Method: "GET", Path: "/access/row", Handle: s.accessRow}
	mark := crud.Route{Method: "GET", Path: "/plugin-sessions/mark", Handle: s.mark}
	return map[string][]crud.Route{
		"access:read": {row},
		"access": {
			row,
			{Method: "POST", Path: "/access/rows", Handle: s.accessRows},
			{Method: "POST", Path: "/access/write", Handle: s.accessWrite},
			{Method: "POST", Path: "/tool-listings/read", Handle: s.toolListings},
			{Method: "POST", Path: "/tool-listings/plugin", Handle: s.pluginListing},
			{Method: "POST", Path: "/tool-listings/package-server", Handle: s.packageServerListing},
			{Method: "POST", Path: "/leases/{key...}", Handle: s.acquireLease},
			{Method: "PUT", Path: "/leases/{key...}", Handle: s.renewLease},
			{Method: "DELETE", Path: "/leases/{key...}", Handle: s.releaseLease, Status: 204},
		},
		"plugin-sessions:read": {mark},
		"plugin-sessions": {
			mark,
			{Method: "POST", Path: "/plugin-sessions/seen", Handle: s.sessionSeen},
			{Method: "POST", Path: "/plugin-sessions/ended", Handle: s.sessionEnded},
			{Method: "POST", Path: "/plugin-sessions/dead", Handle: s.sessionsDead},
			{Method: "POST", Path: "/plugin-sessions/own", Handle: s.sessionsOwn},
		},
	}
}
