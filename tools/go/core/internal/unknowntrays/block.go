package unknowntrays

import (
	"net/http"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
)

// Caller is the CRUD caller of the block read: the print worker.
const Caller = "print"

// Ref is one blocking tray in the block (UnknownSpoolRef).
type Ref struct {
	ID   string `json:"id"`
	Slot string `json:"slot"`
}

// Block is the print worker's answer (UnknownSpoolBlock).
type Block struct {
	Count int   `json:"count"`
	Trays []Ref `json:"trays"`
}

// BlockRoutes are the CRUD routes for caller Caller. They only read.
func BlockRoutes(pool *pgxpool.Pool) []crud.Route {
	return []crud.Route{{
		Method: "GET",
		Path:   "/filament/printers/{printer_id}/unknown-trays",
		Handle: func(r *http.Request, p crud.Params) (any, error) {
			printer, err := p.UUID("printer_id")
			if err != nil {
				return nil, err
			}
			res, err := pool.Query(r.Context(), `
				SELECT id::text, ams_id, tray_id FROM filament_unknown_trays
				WHERE printer_id = $1 ORDER BY ams_id, tray_id`, printer)
			if err != nil {
				return nil, err
			}
			defer res.Close()
			block := Block{Trays: []Ref{}}
			for res.Next() {
				var id string
				var ams, tray int
				if err := res.Scan(&id, &ams, &tray); err != nil {
					return nil, err
				}
				block.Trays = append(block.Trays, Ref{ID: id, Slot: TrayLabel(ams, tray)})
			}
			if err := res.Err(); err != nil {
				return nil, err
			}
			block.Count = len(block.Trays)
			return block, nil
		},
	}}
}
