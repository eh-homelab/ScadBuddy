package filamentpresets

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/libraryrun"
)

// Write, Starter and Failure are libraryrun's: each write is one run of core's workflow
// LibraryRun, whose one step is the write's Python activity on the library queue.
type (
	Write   = libraryrun.Write
	Starter = libraryrun.Starter
	Failure = libraryrun.Failure
)

// timeoutFailure is a Failure's Type when Temporal timed the activity out: the write may
// have run, so the same key is sent again.
const timeoutFailure = libraryrun.TimeoutFailure

// The activities (filament/preset_writes.py PRESET_WRITE_ACTIVITIES) and their bounds on the
// mirror (R11): WRITE_MIRROR_TIMEOUT and preset_mirror.py RESEND_TIMEOUT.
const (
	copyActivity   = "filament_preset_copy"
	editActivity   = "filament_preset_edit"
	mirrorActivity = "filament_preset_mirror_again"

	writeMirrorBound = 30 * time.Second
	resendBound      = 60 * time.Second
	// An activity's start-to-close is its bound plus activityMargin, and the route waits for
	// it that plus waitMargin (R11).
	activityMargin = 30 * time.Second
	waitMargin     = libraryrun.WaitMargin
)

// R7's limits, as preset_writes.py has them, and the idempotency key's length
// (crud/idempotency.py MaybeIdempotencyKey).
const (
	maxEditKeys = 500
	maxName     = 200
	shownKey    = 64
	maxKeyChars = 200
)

var editKey = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,128}$`)

// The problem types of crud/idempotency.py: KEY_REQUIRED_PROBLEM and KEY_PROBLEM.
const (
	keyRequiredProblem = "https://scadbuddy.dev/problems/crud/idempotency-key-required"
	keyReusedProblem   = "https://scadbuddy.dev/problems/crud/idempotency-key-reused"
)

// activityCodes are the failure types an activity raises that are a problem of their own.
var activityCodes = map[string]bool{
	presetNotFound: true, documentMissing: true, notManual: true, versionMoved: true,
	keyRefused: true, keyReused: true, invalidInput: true, mirrorOff: true,
	notConfigured: true, instanceChanged: true, notMirrored: true,
}

// Edited is the answer of an edit: whether it made a version, and the preset now.
type Edited struct {
	Changed bool    `json:"changed"`
	Preset  *Detail `json:"preset"`
}

// MirrorAgainOutcome is the answer of a send again: the outcome (preset_mirror.py
// MirrorAgain) and the preset's row now.
type MirrorAgainOutcome struct {
	Outcome string `json:"outcome"`
	Preset  Row    `json:"preset"`
}

// writer serves the writes over starter. A nil starter is a core with no Temporal.
type writer struct {
	pool    *pgxpool.Pool
	blobs   Blobs
	starter Starter
}

// refusal is a body that Go refuses before the start: invalid_input, or key_refused with
// the key as a failure shows it.
type refusal struct {
	code, detail, key string
}

func (x *writer) copy(w http.ResponseWriter, r *http.Request) {
	id, key, body, ok := x.begin(w, r, true)
	if !ok {
		return
	}
	name, bad := nameOf(body, true)
	if bad != nil {
		refuse(w, r, bad)
		return
	}
	caller := libraryrun.Actor(auth.CallerOf(r.Context()))
	input := jsonv.Object{{Key: "preset_id", Value: id}, {Key: "name", Value: name}, {Key: "actor", Value: caller}, {Key: "idempotency_key", Value: key}}
	result, ok := x.run(w, r, Write{WorkflowID: keyedID("copy", caller, key, id, input), Activity: copyActivity, Input: input}, writeMirrorBound, "")
	if !ok {
		return
	}
	var out struct {
		PresetID int64 `json:"preset_id"`
	}
	if err := json.Unmarshal(result, &out); err != nil || out.PresetID <= 0 {
		unreadable(w, r, result)
		return
	}
	answerDetail(w, r, x.pool, x.blobs, out.PresetID, http.StatusCreated)
}

func (x *writer) edit(w http.ResponseWriter, r *http.Request) {
	id, key, body, ok := x.begin(w, r, true)
	if !ok {
		return
	}
	fields, bad := editFields(body)
	if bad != nil {
		refuse(w, r, bad)
		return
	}
	caller := libraryrun.Actor(auth.CallerOf(r.Context()))
	input := append(jsonv.Object{{Key: "preset_id", Value: id}}, fields...)
	input = append(input, jsonv.Member{Key: "actor", Value: caller}, jsonv.Member{Key: "idempotency_key", Value: key})
	result, ok := x.run(w, r, Write{WorkflowID: keyedID("edit", caller, key, id, input), Activity: editActivity, Input: input}, writeMirrorBound, "")
	if !ok {
		return
	}
	var out struct {
		Changed *bool `json:"changed"`
	}
	if err := json.Unmarshal(result, &out); err != nil || out.Changed == nil {
		unreadable(w, r, result)
		return
	}
	detail, err := detailOf(r, x.pool, x.blobs, id)
	if errors.Is(err, errNoPreset) {
		writeCode(w, r, presetNotFound)
		return
	}
	if err != nil {
		failed(w, r, err)
		return
	}
	answer(w, r, Edited{Changed: *out.Changed, Preset: detail})
}

func (x *writer) mirror(w http.ResponseWriter, r *http.Request) {
	id, _, _, ok := x.begin(w, r, false)
	if !ok {
		return
	}
	write := Write{WorkflowID: fmt.Sprintf("filament-preset-mirror-%d", id), Activity: mirrorActivity,
		Input: jsonv.Object{{Key: "preset_id", Value: id}}}
	result, ok := x.run(w, r, write, resendBound, mirrorPendingDetail)
	if !ok {
		return
	}
	var outcome string
	if err := json.Unmarshal(result, &outcome); err != nil || outcome == "" {
		unreadable(w, r, result)
		return
	}
	row, _, err := read(r.Context(), x.pool, id)
	if errors.Is(err, errNoPreset) {
		writeCode(w, r, presetNotFound)
		return
	}
	if err != nil {
		failed(w, r, err)
		return
	}
	answer(w, r, MirrorAgainOutcome{Outcome: outcome, Preset: *row})
}

// begin checks what every write checks before its body: the path's preset id, a Temporal,
// and (keyed) the Idempotency-Key, 428 when it is missing (R12). Then it reads the body as
// a JSON object, when keyed. ok is false when it has answered.
func (x *writer) begin(w http.ResponseWriter, r *http.Request, keyed bool) (id int64, key string, body jsonv.Object, ok bool) {
	id, ok = presetID(r)
	if !ok {
		writeCode(w, r, presetNotFound)
		return 0, "", nil, false
	}
	if x.starter == nil {
		writeCode(w, r, writesNotConfigured)
		return 0, "", nil, false
	}
	if !keyed {
		return id, "", nil, true
	}
	values := r.Header.Values("Idempotency-Key")
	if len(values) == 0 {
		httpx.WriteProblem(w, r, &httpx.Problem{Status: http.StatusPreconditionRequired, Type: keyRequiredProblem,
			Detail: "This write needs an Idempotency-Key header. Nothing was done."})
		return 0, "", nil, false
	}
	key = values[0]
	if n := utf8.RuneCountInString(key); n < 1 || n > maxKeyChars {
		refuse(w, r, &refusal{code: invalidInput, detail: fmt.Sprintf("the Idempotency-Key is 1 to %d characters", maxKeyChars)})
		return 0, "", nil, false
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, DocumentLimit))
	if err != nil {
		refuse(w, r, &refusal{code: invalidInput, detail: "the body is not readable, or larger than 1 MiB"})
		return 0, "", nil, false
	}
	v, err := jsonv.Parse(raw)
	body, isObject := v.(jsonv.Object)
	if err != nil || !isObject {
		refuse(w, r, &refusal{code: invalidInput, detail: "the body is not a JSON object"})
		return 0, "", nil, false
	}
	return id, key, body, true
}

// run starts write with its timeout, its bound plus activityMargin, and waits for it at most
// that plus waitMargin. ok is false when it has answered the failure.
func (x *writer) run(w http.ResponseWriter, r *http.Request, write Write, bound time.Duration, pendingDetail string) (json.RawMessage, bool) {
	write.Timeout = bound + activityMargin
	ctx, cancel := context.WithTimeout(r.Context(), write.Timeout+waitMargin)
	defer cancel()
	result, err := x.starter.Run(ctx, write)
	if err == nil {
		return result, true
	}
	var f *Failure
	switch {
	case !errors.As(err, &f):
		slog.WarnContext(r.Context(), "a filament preset write did not answer", "workflow_id", write.WorkflowID, "error", err.Error())
		writeCodeDetail(w, r, writePending, pendingDetail)
	case f.Type == timeoutFailure:
		writeCodeDetail(w, r, writePending, pendingDetail)
	case activityCodes[f.Type]:
		var extra []httpx.Field
		switch f.Type {
		case versionMoved:
			var current int64
			if json.Unmarshal(f.Detail, &current) == nil {
				extra = append(extra, httpx.Field{Key: "current_version", Value: current})
			}
		case keyRefused:
			var key string
			if json.Unmarshal(f.Detail, &key) == nil {
				extra = append(extra, httpx.Field{Key: "key", Value: key})
			}
		}
		writeCodeDetail(w, r, f.Type, f.Message, extra...)
	default:
		slog.ErrorContext(r.Context(), "a filament preset write failed", "workflow_id", write.WorkflowID,
			"type", f.Type, "message", f.Message)
		httpx.WriteProblem(w, r, &httpx.Problem{Status: http.StatusInternalServerError, Detail: "The preset write failed."})
	}
	return nil, false
}

func unreadable(w http.ResponseWriter, r *http.Request, result json.RawMessage) {
	slog.ErrorContext(r.Context(), "a filament preset write answered what Go cannot read", "bytes", len(result))
	httpx.WriteProblem(w, r, &httpx.Problem{Status: http.StatusInternalServerError, Detail: "The preset write failed."})
}

func refuse(w http.ResponseWriter, r *http.Request, bad *refusal) {
	if bad.code == keyRefused {
		detail := bad.detail
		if detail == "" {
			detail = "the key " + bad.key + " cannot be set or unset"
		}
		writeCodeDetail(w, r, keyRefused, detail, httpx.Field{Key: "key", Value: bad.key})
		return
	}
	writeCodeDetail(w, r, bad.code, bad.detail)
}

// keyedID is R12's workflow id: the op, then the SHA-256 of the actor, the key and the
// preset id and the activity's input, so two sends of one key and one body join one run, and
// one key with another body runs apart (plan ruling P24).
func keyedID(op, caller, key string, id int64, input jsonv.Object) string {
	sum := sha256.Sum256(fmt.Appendf(nil, "%s\x1f%s\x1f%d\x1f%s", caller, key, id, jsonv.Dumps(input)))
	return "filament-preset-" + op + "-" + hex.EncodeToString(sum[:])
}

// nameOf is the body's name as preset_writes.py PresetName checks it: a string of 1 to
// maxName characters once stripped. It gives the string as sent; the activity strips it.
func nameOf(body jsonv.Object, required bool) (any, *refusal) {
	v, ok := body.Get("name")
	if !ok || v == nil {
		if required {
			return nil, &refusal{code: invalidInput, detail: "name: the field is required"}
		}
		return nil, nil
	}
	s, isText := v.(string)
	if !isText {
		return nil, &refusal{code: invalidInput, detail: "name: not a string"}
	}
	if n := utf8.RuneCountInString(strings.TrimSpace(s)); n < 1 || n > maxName {
		return nil, &refusal{code: invalidInput, detail: fmt.Sprintf("name: 1 to %d characters", maxName)}
	}
	return s, nil
}

// editFields are the edit's fields in PresetEditInput's order, checked as its validators
// check them (R7): a refused key of set, then of unset, first; then each field's shape;
// then the edit as a whole.
func editFields(body jsonv.Object) (jsonv.Object, *refusal) {
	set, hasSet := body.Get("set")
	unset, hasUnset := body.Get("unset")
	for _, v := range []any{set, unset} {
		for _, key := range keysOf(v) {
			if !editKey.MatchString(key) || key == "name" || IsCredential(key) {
				return nil, &refusal{code: keyRefused, key: shown(key)}
			}
		}
	}
	version, bad := expectedVersion(body)
	if bad != nil {
		return nil, bad
	}
	fields := jsonv.Object{{Key: "expected_version", Value: version}}
	name, bad := nameOf(body, false)
	if bad != nil {
		return nil, bad
	}
	if name != nil {
		fields = append(fields, jsonv.Member{Key: "name", Value: name})
	}
	setKeys := 0
	if hasSet {
		o, isObject := set.(jsonv.Object)
		if !isObject {
			return nil, &refusal{code: invalidInput, detail: "set: not an object"}
		}
		for _, m := range o {
			if !textOrTexts(m.Value) {
				return nil, &refusal{code: invalidInput, detail: "set." + shown(m.Key) + ": not a string or a list of strings"}
			}
		}
		setKeys = len(o)
		fields = append(fields, jsonv.Member{Key: "set", Value: o})
	}
	var unsetKeys []string
	if hasUnset {
		list, isList := unset.([]any)
		if !isList {
			return nil, &refusal{code: invalidInput, detail: "unset: not a list"}
		}
		for _, item := range list {
			s, isText := item.(string)
			if !isText {
				return nil, &refusal{code: invalidInput, detail: "unset: not a list of strings"}
			}
			unsetKeys = append(unsetKeys, s)
		}
		fields = append(fields, jsonv.Member{Key: "unset", Value: list})
	}
	if setKeys+len(unsetKeys) > maxEditKeys {
		return nil, &refusal{code: invalidInput, detail: fmt.Sprintf("an edit sets and unsets at most %d keys", maxEditKeys)}
	}
	var both []string
	if o, isObject := set.(jsonv.Object); isObject {
		for _, key := range unsetKeys {
			if o.Has(key) {
				both = append(both, key)
			}
		}
	}
	if len(both) > 0 {
		slices.Sort(both)
		return nil, &refusal{code: keyRefused, key: shown(both[0]), detail: "the key " + shown(both[0]) + " is both set and unset"}
	}
	if name == nil && setKeys == 0 && len(unsetKeys) == 0 {
		return nil, &refusal{code: invalidInput, detail: "an edit needs a name, a key to set or a key to unset"}
	}
	return fields, nil
}

// expectedVersion is the body's expected_version: a JSON integer above 0.
func expectedVersion(body jsonv.Object) (json.Number, *refusal) {
	v, _ := body.Get("expected_version")
	n, isNumber := v.(json.Number)
	if !isNumber {
		return "", &refusal{code: invalidInput, detail: "expected_version: the field is required, as an integer"}
	}
	i, err := strconv.ParseInt(string(n), 10, 64)
	if err != nil || i < 1 {
		return "", &refusal{code: invalidInput, detail: "expected_version: an integer above 0"}
	}
	return json.Number(strconv.FormatInt(i, 10)), nil
}

// keysOf are the keys _keys_allowed looks at: an object's keys, or a list's strings.
func keysOf(v any) []string {
	var keys []string
	switch v := v.(type) {
	case jsonv.Object:
		for _, m := range v {
			keys = append(keys, m.Key)
		}
	case []any:
		for _, item := range v {
			if s, ok := item.(string); ok {
				keys = append(keys, s)
			}
		}
	}
	return keys
}

func textOrTexts(v any) bool {
	switch v := v.(type) {
	case string:
		return true
	case []any:
		for _, item := range v {
			if _, ok := item.(string); !ok {
				return false
			}
		}
		return true
	}
	return false
}

// shown is preset_writes.py _shown: the key's first shownKey characters, then an ellipsis.
func shown(key string) string {
	if utf8.RuneCountInString(key) <= shownKey {
		return key
	}
	return string([]rune(key)[:shownKey]) + "…"
}
