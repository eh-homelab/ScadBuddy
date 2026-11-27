package filamentpresets

import (
	"context"
	"errors"
	"net/http"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

// Version is one of a preset's versions.
type Version struct {
	Version     int32      `json:"version"`
	SHA256      string     `json:"sha256"`
	MadeBy      *string    `json:"made_by"`
	MadeAt      jsonv.Time `json:"made_at"`
	FetchRunID  *string    `json:"fetch_run_id"`
	BBUpdatedAt *string    `json:"bb_updated_at"`
}

// Detail is one preset: its row, its versions newest first, and the current version's
// document without credential keys (plan ruling R15). Document is nil and DocumentError
// names why when the document was not read.
type Detail struct {
	Row
	Versions      []Version `json:"versions"`
	Document      any       `json:"document"`
	DocumentError *string   `json:"document_error"`
}

var errNoPreset = errors.New("no such filament preset")

// read is the preset's row and versions, in one read-only snapshot.
func read(ctx context.Context, pool *pgxpool.Pool, id int64) (*Row, []Version, error) {
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
	if err != nil {
		return nil, nil, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	res, err := tx.Query(ctx, selectRows+` WHERE p.id = $1`, id)
	if err != nil {
		return nil, nil, err
	}
	rows, err := scanRows(res)
	if err != nil {
		return nil, nil, err
	}
	if len(rows) == 0 {
		return nil, nil, errNoPreset
	}
	res, err = tx.Query(ctx, `
		SELECT version, sha256, made_by, made_at, fetch_run_id::text, bb_updated_at
		FROM filament_preset_versions WHERE preset_id = $1 ORDER BY version DESC`, id)
	if err != nil {
		return nil, nil, err
	}
	defer res.Close()
	versions := []Version{}
	for res.Next() {
		var v Version
		if err := res.Scan(&v.Version, &v.SHA256, &v.MadeBy, &v.MadeAt, &v.FetchRunID, &v.BBUpdatedAt); err != nil {
			return nil, nil, err
		}
		versions = append(versions, v)
	}
	if err := res.Err(); err != nil {
		return nil, nil, err
	}
	return &rows[0], versions, nil
}

func serveDetail(w http.ResponseWriter, r *http.Request, pool *pgxpool.Pool, blobs Blobs) {
	id, ok := presetID(r)
	if !ok {
		writeCode(w, r, presetNotFound)
		return
	}
	row, versions, err := read(r.Context(), pool, id)
	if errors.Is(err, errNoPreset) {
		writeCode(w, r, presetNotFound)
		return
	}
	if err != nil {
		failed(w, r, err)
		return
	}
	detail := Detail{Row: *row, Versions: versions}
	doc, code, err := readDocument(r.Context(), blobs, row.blobKey)
	if code == "" && err != nil {
		failed(w, r, err)
		return
	}
	if code != "" {
		logRead(r, code, err)
		detail.DocumentError = &code
	} else {
		detail.Document = StripCredentials(doc)
	}
	answer(w, r, detail)
}
