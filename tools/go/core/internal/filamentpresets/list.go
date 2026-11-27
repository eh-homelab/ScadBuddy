package filamentpresets

import (
	"context"
	"net/http"
	"strconv"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

// Row is one preset in the list's JSON. It has no blob_key: the document is the detail's.
type Row struct {
	ID                 int64       `json:"id"`
	Ref                string      `json:"ref"`
	Name               string      `json:"name"`
	Source             string      `json:"source"`
	Version            int32       `json:"version"`
	TypeID             *string     `json:"type_id"`
	PrinterModel       *string     `json:"printer_model"`
	NozzleDiameter     *string     `json:"nozzle_diameter"`
	FilamentType       *string     `json:"filament_type"`
	FilamentColour     *string     `json:"filament_colour"`
	CompatiblePrinters []string    `json:"compatible_printers"`
	SHA256             string      `json:"sha256"`
	BBPresetID         *int64      `json:"bb_preset_id"`
	BBInstance         *string     `json:"bb_instance"`
	Mirror             string      `json:"mirror"`
	MirrorError        *string     `json:"mirror_error"`
	MirrorErrorAt      *jsonv.Time `json:"mirror_error_at"`
	UsedByTypes        int64       `json:"used_by_types"`
	UsedBySpools       int64       `json:"used_by_spools"`
	CreatedAt          jsonv.Time  `json:"created_at"`
	UpdatedAt          jsonv.Time  `json:"updated_at"`

	blobKey string
}

// selectRows reads presets with their binding counts: the distinct types and spools whose
// bindings name each by the generated preset_id column (sb_presets.py BINDINGS).
const selectRows = `
	SELECT p.id, p.name, p.source, p.version, p.type_id::text, p.printer_model,
	       p.nozzle_diameter, p.filament_type, p.filament_colour, p.compatible_printers,
	       p.sha256, p.bb_preset_id, p.bb_instance, p.mirrored_sha256, p.mirror_error,
	       p.mirror_error_at, t.n, s.n, p.created_at, p.updated_at, p.blob_key
	FROM filament_presets p
	LEFT JOIN LATERAL (SELECT count(DISTINCT b.type_id) AS n FROM filament_spool_type_presets b
	                   WHERE b.preset_id = p.id) t ON true
	LEFT JOIN LATERAL (SELECT count(DISTINCT b.spool_id) AS n FROM filament_spool_presets b
	                   WHERE b.preset_id = p.id) s ON true`

func scanRows(rows pgx.Rows) ([]Row, error) {
	defer rows.Close()
	out := []Row{}
	for rows.Next() {
		var r Row
		var mirrored *string
		if err := rows.Scan(&r.ID, &r.Name, &r.Source, &r.Version, &r.TypeID, &r.PrinterModel,
			&r.NozzleDiameter, &r.FilamentType, &r.FilamentColour, &r.CompatiblePrinters,
			&r.SHA256, &r.BBPresetID, &r.BBInstance, &mirrored, &r.MirrorError,
			&r.MirrorErrorAt, &r.UsedByTypes, &r.UsedBySpools, &r.CreatedAt, &r.UpdatedAt,
			&r.blobKey); err != nil {
			return nil, err
		}
		r.Ref = "sb-" + strconv.FormatInt(r.ID, 10)
		r.Mirror = MirrorState(r.Source, r.SHA256, mirrored, r.MirrorError)
		if r.CompatiblePrinters == nil {
			r.CompatiblePrinters = []string{}
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// sources are filament_presets.source's values.
var sources = map[string]bool{"3dfp": true, "bambuddy": true, "manual": true}

// List is every preset, by source and type when given ("" for any), in the order
// lower(name), then id. It has no paging (plan ruling R14).
func List(ctx context.Context, pool *pgxpool.Pool, source, typeID string) ([]Row, error) {
	rows, err := pool.Query(ctx, selectRows+`
		WHERE ($1::text IS NULL OR p.source = $1::text) AND ($2::uuid IS NULL OR p.type_id = $2::uuid)
		ORDER BY lower(p.name), p.id`, given(source), given(typeID))
	if err != nil {
		return nil, err
	}
	return scanRows(rows)
}

func serveList(w http.ResponseWriter, r *http.Request, pool *pgxpool.Pool) {
	query := r.URL.Query()
	source, typeID := query.Get("source"), query.Get("type_id")
	var issues []httpx.Issue
	if query.Has("source") && !sources[source] {
		issues = append(issues, httpx.Issue{Loc: []string{"query", "source"},
			Msg: "source is one of 3dfp, bambuddy and manual"})
	}
	if query.Has("type_id") && !crud.IsUUID(typeID) {
		issues = append(issues, httpx.Issue{Loc: []string{"query", "type_id"}, Msg: "type_id is a UUID"})
	}
	if len(issues) > 0 {
		httpx.WriteProblem(w, r, httpx.Invalid(issues...))
		return
	}
	rows, err := List(r.Context(), pool, source, typeID)
	if err != nil {
		failed(w, r, err)
		return
	}
	answer(w, r, rows)
}

// given is s, or nil (SQL NULL) for "".
func given(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}
