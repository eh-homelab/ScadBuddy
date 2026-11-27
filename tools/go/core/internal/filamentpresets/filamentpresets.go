// Package filamentpresets is ScadBuddy's own filament presets in Go (#7204, area
// filament-presets): the list, one preset with its versions and current document, and the
// keys that changed between two versions. A preset's document is a blob, read through
// internal/blobstore's OpenKey. Go writes no row here: the writes stay Python's
// (filament/sb_presets.py).
package filamentpresets

import (
	"log/slog"
	"net/http"
	"regexp"
	"strconv"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	// Area names every route.
	Area = "filament-presets"

	listPath   = "/api/v1/filament/presets"
	detailPath = listPath + "/{preset_id}"
	diffPath   = detailPath + "/diff"
)

// Public are the area's routes. newRouter serves them behind the one person-auth gate
// (internal/auth). POST …/bambuddy-import is no pattern here, so it still goes on to Python.
func Public(pool *pgxpool.Pool, blobs Blobs) []auth.Public {
	route := func(path string, handler http.HandlerFunc) auth.Public {
		return auth.Public{
			Pattern: "GET " + path,
			Route:   auth.Route{Class: "api", Template: path},
			// Every answer of the area, a problem too, is no-store.
			Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Cache-Control", "no-store")
				handler(w, r)
			}),
		}
	}
	return []auth.Public{
		route(listPath, func(w http.ResponseWriter, r *http.Request) { serveList(w, r, pool) }),
		route(detailPath, func(w http.ResponseWriter, r *http.Request) { serveDetail(w, r, pool, blobs) }),
		route(diffPath, func(w http.ResponseWriter, r *http.Request) { serveDiff(w, r, pool, blobs) }),
	}
}

// The problem codes, in the extension `code` as the Python filament routes write it.
const (
	presetNotFound     = "preset_not_found"
	versionNotFound    = "version_not_found"
	documentMissing    = "document_missing"
	documentTooLarge   = "document_too_large"
	documentUnreadable = "document_unreadable"
	storeNotConfigured = "store_not_configured"
	storeUnavailable   = "store_unavailable"
)

var problems = map[string]struct {
	status int
	detail string
}{
	presetNotFound:     {http.StatusNotFound, "No filament preset has that id."},
	versionNotFound:    {http.StatusNotFound, "The preset has no such version."},
	documentMissing:    {http.StatusConflict, "The store has lost this version's document."},
	documentTooLarge:   {http.StatusConflict, "This version's document is larger than 1 MiB."},
	documentUnreadable: {http.StatusConflict, "This version's document cannot be read."},
	storeNotConfigured: {http.StatusServiceUnavailable, "The blob store is not an S3 store."},
	storeUnavailable:   {http.StatusServiceUnavailable, "The blob store is out of reach."},
}

func writeCode(w http.ResponseWriter, r *http.Request, code string) {
	p := problems[code]
	httpx.WriteProblem(w, r, &httpx.Problem{
		Status: p.status, Detail: p.detail,
		Extensions: []httpx.Field{{Key: "code", Value: code}},
	})
}

func failed(w http.ResponseWriter, r *http.Request, err error) {
	httpx.WriteProblem(w, r, crud.ProblemOf(r, err))
}

func answer(w http.ResponseWriter, r *http.Request, v any) {
	if err := httpx.WriteJSON(w, v); err != nil {
		failed(w, r, err)
	}
}

// logRead logs why a document was not read: the store's errors never name a URL.
func logRead(r *http.Request, code string, err error) {
	if err != nil && (code == storeUnavailable || code == documentUnreadable) {
		slog.WarnContext(r.Context(), "a filament preset's document was not read",
			"path", r.URL.Path, "code", code, "error", err.Error())
	}
}

// positive is a positive integer in its plain spelling: no sign, no leading zero.
var positive = regexp.MustCompile(`^[1-9][0-9]*$`)

// presetID is the path's preset_id, or false when it is no positive int64.
func presetID(r *http.Request) (int64, bool) {
	s := r.PathValue("preset_id")
	if !positive.MatchString(s) {
		return 0, false
	}
	id, err := strconv.ParseInt(s, 10, 64)
	return id, err == nil
}
