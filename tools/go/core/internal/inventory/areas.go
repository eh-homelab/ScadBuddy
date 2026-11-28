// Package inventory mounts the inventory's read routes for its service caller (#6949 K1,
// design §6.2, R-K1-3). Its writes are Temporal activities (package activities), so it has
// no write route.
package inventory

import (
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/routes"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// Caller is the service caller the routes belong to: /api/v1/internal/crud/inventory/, scope
// crud.inventory.
const Caller = store.Caller

// Areas are the areas inventory and inventory:read, each with every read route, and an
// operation read that does not ask Temporal.
func Areas(pool *pgxpool.Pool, keys *seal.Keys) map[string][]crud.Route {
	return AreasWith(nil)(pool, keys)
}

// AreasWith is Areas whose operation read asks temporal about a row that has run for more
// than a minute.
func AreasWith(temporal store.Temporal) func(*pgxpool.Pool, *seal.Keys) map[string][]crud.Route {
	return func(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
		return map[string][]crud.Route{
			"inventory":      routes.Reads(pool, temporal),
			"inventory:read": routes.Reads(pool, temporal),
		}
	}
}
