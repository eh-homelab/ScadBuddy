package agentreads

import (
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// Areas are this package's areas and their routes.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	reads := &Reads{Pool: pool}
	return map[string][]crud.Route{
		"workspace-blob-refs:read": reads.HoldReads(),
		"workspace-blob-refs":      append(reads.HoldReads(), reads.HoldWrites()...),
		"events":                   reads.EventLog(),
		"flows":                    reads.Flows(),
	}
}
