// Package unknowntrays is the unknown-spool flow's Go routes (#7143, area unknown-trays):
// the list of trays a printer reports that no spool holds, with ranked candidates, the
// resolve and dismiss routes, and the print worker's CRUD read of the block. Go writes no
// spool here: the resolve write is Python's, the activity filament_resolve_unknown_tray on
// the library queue, which Go runs through internal/libraryrun (resolve.go, #8489).
package unknowntrays

import (
	"net/http"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/libraryrun"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	// Area names every route; Read names the GETs.
	Area = "unknown-trays"
	Read = Area + ":read"

	listPath    = "/api/v1/filament/unknown-trays"
	dismissPath = listPath + "/{id}/dismiss"
	resolvePath = listPath + "/{id}/resolve"
)

// Public are the area's public routes by area name. newRouter serves them behind the one
// person-auth gate (internal/auth). The resolve starts its activity through starter; with a
// nil starter (a core with no Temporal) it answers 503 writes_not_configured.
func Public(pool *pgxpool.Pool, _ *seal.Keys, starter libraryrun.Starter) map[string][]auth.Public {
	list := auth.Public{
		Pattern: "GET " + listPath,
		Route:   auth.Route{Class: "api", Template: listPath},
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			listing, err := List(r.Context(), pool)
			if err != nil {
				failed(w, r, err)
				return
			}
			w.Header().Set("Cache-Control", "no-store")
			if err := httpx.WriteJSON(w, listing); err != nil {
				failed(w, r, err)
			}
		}),
	}
	dismiss := auth.Public{
		Pattern: "POST " + dismissPath,
		Route:   auth.Route{Class: "api", Template: dismissPath},
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			id := r.PathValue("id")
			gone := &httpx.Problem{
				Status: http.StatusNotFound, Detail: "That unknown tray is gone.",
				Extensions: []httpx.Field{{Key: "code", Value: "tray_gone"}},
			}
			if !crud.IsUUID(id) {
				httpx.WriteProblem(w, r, gone)
				return
			}
			tag, err := pool.Exec(r.Context(), `DELETE FROM filament_unknown_trays WHERE id = $1`, id)
			if err != nil {
				failed(w, r, err)
				return
			}
			if tag.RowsAffected() == 0 {
				httpx.WriteProblem(w, r, gone)
				return
			}
			w.WriteHeader(http.StatusNoContent)
		}),
	}
	resolve := auth.Public{
		Pattern: "POST " + resolvePath,
		Route:   auth.Route{Class: "api", Template: resolvePath},
		Handler: http.HandlerFunc((&resolver{starter: starter}).resolve),
	}
	return map[string][]auth.Public{Read: {list}, Area: {list, dismiss, resolve}}
}

func failed(w http.ResponseWriter, r *http.Request, err error) {
	httpx.WriteProblem(w, r, crud.ProblemOf(r, err))
}
