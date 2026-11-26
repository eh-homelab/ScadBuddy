package sessions

import (
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// Areas are this package's areas and their routes.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	rows := &Sessions{Pool: pool}
	return map[string][]crud.Route{
		"sessions:read":          rows.Reads(),
		"sessions":               append(rows.Reads(), rows.Writes()...),
		"session-resources:read": rows.ResourceReads(),
		"session-resources":      append(rows.ResourceReads(), rows.ResourceWrites()...),
	}
}
