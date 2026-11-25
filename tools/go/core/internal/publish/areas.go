package publish

import (
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// Areas are this package's areas and their routes.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	sends := &Publish{Pool: pool}
	return map[string][]crud.Route{
		"publish":       sends.Sends(),
		"session-reads": sends.Reads(),
	}
}
