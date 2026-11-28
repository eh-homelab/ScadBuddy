package unknowntrays

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/libraryrun"
)

// The resolve's activity (filament/unknown_resolve.py RESOLVE_UNKNOWN_TRAY_ACTIVITY) and its
// start-to-close timeout (plan Task 3B). The route waits that plus libraryrun.WaitMargin.
const (
	resolveActivity = "filament_resolve_unknown_tray"
	resolveTimeout  = 30 * time.Second

	maxNetWeightG = 100000
	// bodyLimit bounds the body: four short fields.
	bodyLimit = 64 << 10
)

// The resolve's problem codes: the activity's failure types, and Go's own.
const (
	trayChanged         = "tray_changed"
	codeUnknown         = "code_unknown"
	spoolArchived       = "spool_archived"
	noAmsKind           = "no_ams_kind"
	spoolMissing        = "spool_missing"
	tagClash            = "tag_clash"
	invalidInput        = "invalid_input"
	resolvePending      = "resolve_pending"
	writesNotConfigured = "writes_not_configured"
)

var resolveProblems = map[string]struct {
	status int
	detail string
}{
	trayChanged:         {http.StatusConflict, "The tray changed after the list was read, or it has a spool now. Read the list again."},
	codeUnknown:         {http.StatusNotFound, "No spool has that code."},
	spoolArchived:       {http.StatusConflict, "That spool is archived."},
	noAmsKind:           {http.StatusConflict, "No place kind is marked as an AMS slot, so the tray has no place."},
	spoolMissing:        {http.StatusNotFound, "That spool does not exist."},
	tagClash:            {http.StatusConflict, "The tray's tag is on another spool."},
	invalidInput:        {http.StatusUnprocessableEntity, "The request is not a valid resolve."},
	resolvePending:      {http.StatusServiceUnavailable, "The link did not answer in time, and it may still run. Read the list of unknown trays, then send it again if the tray is still there."},
	writesNotConfigured: {http.StatusServiceUnavailable, "Resolving an unknown tray needs Temporal, and this server has no address for it."},
}

// failureCodes are the activity's failure types that are a problem of their own. The others
// are a 500.
var failureCodes = map[string]bool{
	trayChanged: true, codeUnknown: true, spoolArchived: true, noAmsKind: true, spoolMissing: true, tagClash: true,
}

// Resolved is the resolve's answer (G6): the spool the tray holds now, and its place.
type Resolved struct {
	ID         string  `json:"id"`
	Number     int64   `json:"number"`
	Code       string  `json:"code"`
	LocationID *string `json:"location_id"`
}

// resolver serves the resolve over starter. A nil starter is a core with no Temporal.
type resolver struct {
	starter libraryrun.Starter
}

func (x *resolver) resolve(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	tray := strings.ToLower(r.PathValue("id"))
	if !crud.IsUUID(tray) {
		// No row has that id: the activity would answer tray_changed.
		writeResolveCode(w, r, trayChanged, "")
		return
	}
	if x.starter == nil {
		writeResolveCode(w, r, writesNotConfigured, "")
		return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, bodyLimit))
	if err != nil {
		writeResolveCode(w, r, invalidInput, "the body is not readable, or larger than 64 KiB")
		return
	}
	fields, bad := resolveFields(raw)
	if bad != "" {
		writeResolveCode(w, r, invalidInput, bad)
		return
	}
	actor := libraryrun.Actor(auth.CallerOf(r.Context()))
	input := append(jsonv.Object{{Key: "tray_id", Value: tray}}, fields...)
	input = append(input, jsonv.Member{Key: "actor", Value: actor})
	write := libraryrun.Write{WorkflowID: runID(actor, tray, input), Activity: resolveActivity, Input: input, Timeout: resolveTimeout}
	ctx, cancel := context.WithTimeout(r.Context(), resolveTimeout+libraryrun.WaitMargin)
	defer cancel()
	result, err := x.starter.Run(ctx, write)
	if err != nil {
		var f *libraryrun.Failure
		switch {
		case !errors.As(err, &f):
			slog.WarnContext(r.Context(), "an unknown tray's resolve did not answer", "workflow_id", write.WorkflowID, "error", err.Error())
			writeResolveCode(w, r, resolvePending, "")
		case f.Type == libraryrun.TimeoutFailure:
			writeResolveCode(w, r, resolvePending, "")
		case f.Type == tagClash:
			writeResolveCode(w, r, tagClash, f.Message)
		case failureCodes[f.Type]:
			writeResolveCode(w, r, f.Type, "")
		default:
			slog.ErrorContext(r.Context(), "an unknown tray's resolve failed", "workflow_id", write.WorkflowID,
				"type", f.Type, "message", f.Message)
			httpx.WriteProblem(w, r, &httpx.Problem{Status: http.StatusInternalServerError, Detail: "The link of the spool failed."})
		}
		return
	}
	var out Resolved
	if err := json.Unmarshal(result, &out); err != nil || out.ID == "" || out.Code == "" {
		slog.ErrorContext(r.Context(), "an unknown tray's resolve answered what Go cannot read", "bytes", len(result))
		httpx.WriteProblem(w, r, &httpx.Problem{Status: http.StatusInternalServerError, Detail: "The link of the spool failed."})
		return
	}
	if err := httpx.WriteJSON(w, out); err != nil {
		failed(w, r, err)
	}
}

// resolveFields are the activity's fields from the body, in ResolveInput's order: spool_id
// and code (exactly one of them, the other null), net_weight_g and seen_at as sent. bad is
// why the body is refused.
func resolveFields(raw []byte) (fields jsonv.Object, bad string) {
	v, err := jsonv.Parse(raw)
	body, isObject := v.(jsonv.Object)
	if err != nil || !isObject {
		return nil, "the body is not a JSON object"
	}
	for _, m := range body {
		switch m.Key {
		case "spool_id", "code", "net_weight_g", "seen_at":
		default:
			return nil, "the field " + strconv.Quote(m.Key) + " is not known"
		}
	}
	var spool, code any
	if s, _ := body.Get("spool_id"); s != nil {
		text, isText := s.(string)
		if !isText || !crud.IsUUID(strings.ToLower(text)) {
			return nil, "spool_id: not a UUID"
		}
		spool = strings.ToLower(text)
	}
	if c, _ := body.Get("code"); c != nil {
		text, isText := c.(string)
		if !isText || strings.TrimSpace(text) == "" {
			return nil, "code: not a spool code"
		}
		code = text
	}
	if (spool == nil) == (code == nil) {
		return nil, "give exactly one of spool_id and code"
	}
	weight, _ := body.Get("net_weight_g")
	n, isNumber := weight.(json.Number)
	if !isNumber {
		return nil, "net_weight_g: the field is required, as a number"
	}
	grams, err := n.Float64()
	if err != nil || math.IsNaN(grams) || grams <= 0 || grams > maxNetWeightG {
		return nil, fmt.Sprintf("net_weight_g: more than 0 and at most %d", maxNetWeightG)
	}
	seen, _ := body.Get("seen_at")
	text, isText := seen.(string)
	if !isText {
		return nil, "seen_at: the field is required, as an RFC 3339 time"
	}
	if _, err := time.Parse(time.RFC3339Nano, text); err != nil {
		return nil, "seen_at: not an RFC 3339 time with a zone"
	}
	return jsonv.Object{
		{Key: "spool_id", Value: spool}, {Key: "code", Value: code},
		{Key: "net_weight_g", Value: n}, {Key: "seen_at", Value: text},
	}, ""
}

// runID is the resolve's workflow id (#8489 U4): the SHA-256 of the actor, the tray and the
// activity's input, so two equal sends join one run and another body runs apart (the
// activity's seen_at check and tray lock then refuse the second with tray_changed).
func runID(actor, tray string, input jsonv.Object) string {
	sum := sha256.Sum256(fmt.Appendf(nil, "%s\x1f%s\x1f%s", actor, tray, jsonv.Dumps(input)))
	return "unknown-tray-resolve-" + hex.EncodeToString(sum[:])
}

// writeResolveCode is code's problem with detail in place of its own ("" keeps its own).
func writeResolveCode(w http.ResponseWriter, r *http.Request, code, detail string) {
	p := resolveProblems[code]
	if detail == "" {
		detail = p.detail
	}
	httpx.WriteProblem(w, r, &httpx.Problem{
		Status: p.status, Detail: detail,
		Extensions: []httpx.Field{{Key: "code", Value: code}},
	})
}
