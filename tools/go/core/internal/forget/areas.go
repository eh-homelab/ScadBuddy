package forget

import (
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// Areas are this package's areas and their routes.
func Areas(pool *pgxpool.Pool, keys *seal.Keys) map[string][]crud.Route {
	drop := &Forget{Pool: pool, Keys: keys}
	return map[string][]crud.Route{
		"forget:read": drop.Reads(),
		"forget":      append(drop.Reads(), drop.Writes()...),
	}
}
