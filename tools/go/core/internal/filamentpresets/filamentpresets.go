// Package filamentpresets is ScadBuddy's own filament presets in Go (#7204, area
// filament-presets): the list, one preset with its versions and current document, and the
// keys that changed between two versions, and the writes: copy, edit and send again. A
// preset's document is a blob, read through internal/blobstore's OpenKey. Go writes no row
// here: each write is a Python activity on the library queue (filament/preset_writes.py),
// which Go starts through a Starter and whose answer it reads back (writes.go).
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
	copyPath   = detailPath + "/copy"
	mirrorPath = detailPath + "/mirror"
)

// Public are the area's routes. newRouter serves them behind the one person-auth gate
// (internal/auth). POST …/bambuddy-import is no pattern here, so it still goes on to Python.
// The writes start their activity through starter. With a nil starter (a core with no
// Temporal) each write answers 503 writes_not_configured, and the reads serve as before.
func Public(pool *pgxpool.Pool, blobs Blobs, starter Starter) []auth.Public {
	route := func(method, path string, handler http.HandlerFunc) auth.Public {
		return auth.Public{
			Pattern: method + " " + path,
			Route:   auth.Route{Class: "api", Template: path},
			// Every answer of the handler, a problem too, is no-store. The gate's own refusals get it in main.go.
			Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Cache-Control", "no-store")
				handler(w, r)
			}),
		}
	}
	writes := &writer{pool: pool, blobs: blobs, starter: starter}
	return []auth.Public{
		route(http.MethodGet, listPath, func(w http.ResponseWriter, r *http.Request) { serveList(w, r, pool) }),
		route(http.MethodGet, detailPath, func(w http.ResponseWriter, r *http.Request) { serveDetail(w, r, pool, blobs) }),
		route(http.MethodGet, diffPath, func(w http.ResponseWriter, r *http.Request) { serveDiff(w, r, pool, blobs) }),
		route(http.MethodPost, copyPath, writes.copy),
		route(http.MethodPatch, detailPath, writes.edit),
		route(http.MethodPost, mirrorPath, writes.mirror),
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

	// The writes' codes: an activity failure's type (filament/preset_writes.py,
	// preset_mirror.py), or Go's own check before the start.
	notManual           = "not_manual"
	versionMoved        = "version_moved"
	keyRefused          = "key_refused"
	mirrorOff           = "mirror_off"
	notConfigured       = "not_configured"
	instanceChanged     = "instance_changed"
	notMirrored         = "not_mirrored"
	keyReused           = "idempotency_key_reused"
	invalidInput        = "invalid_input"
	writePending        = "preset_write_pending"
	writesNotConfigured = "writes_not_configured"
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

	notManual:           {http.StatusConflict, "Only a manual preset can be edited: copy it first."},
	versionMoved:        {http.StatusConflict, "The preset has a newer version than the one edited."},
	keyRefused:          {http.StatusUnprocessableEntity, "That key cannot be set or unset."},
	mirrorOff:           {http.StatusConflict, "The filament preset mirror is off."},
	notConfigured:       {http.StatusConflict, "No Bambuddy is configured."},
	instanceChanged:     {http.StatusConflict, "The Bambuddy URL changed: reset the links before a send."},
	notMirrored:         {http.StatusConflict, "A Bambuddy preset is Bambuddy's own: it is not sent."},
	keyReused:           {http.StatusUnprocessableEntity, "This Idempotency-Key was used for another request."},
	invalidInput:        {http.StatusUnprocessableEntity, "The request is not a valid write."},
	writePending:        {http.StatusServiceUnavailable, "The write has not answered yet, and it may still run. Send it again: a copy or an edit with the same Idempotency-Key."},
	writesNotConfigured: {http.StatusServiceUnavailable, "Preset writes need Temporal, and this server has no address for it."},
}

// mirrorPendingDetail is the 503 detail of a send again: it takes no key, so it names none.
const mirrorPendingDetail = "The send did not finish in time and may still run. Read the preset, then send it again."

func writeCode(w http.ResponseWriter, r *http.Request, code string) {
	writeCodeDetail(w, r, code, "")
}

// writeCodeDetail is code's problem with detail in place of its own ("" keeps its own), and
// the extra extensions after code.
func writeCodeDetail(w http.ResponseWriter, r *http.Request, code, detail string, extra ...httpx.Field) {
	p := problems[code]
	if detail == "" {
		detail = p.detail
	}
	problem := &httpx.Problem{
		Status: p.status, Detail: detail,
		Extensions: append([]httpx.Field{{Key: "code", Value: code}}, extra...),
	}
	if code == keyReused {
		problem.Type = keyReusedProblem
	}
	httpx.WriteProblem(w, r, problem)
}

func failed(w http.ResponseWriter, r *http.Request, err error) {
	httpx.WriteProblem(w, r, crud.ProblemOf(r, err))
}

func answer(w http.ResponseWriter, r *http.Request, v any) {
	answerStatus(w, r, http.StatusOK, v)
}

func answerStatus(w http.ResponseWriter, r *http.Request, status int, v any) {
	if err := httpx.WriteJSONStatus(w, status, v); err != nil {
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
