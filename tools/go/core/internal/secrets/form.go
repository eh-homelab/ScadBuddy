package secrets

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"unicode/utf8"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// form reads a JSON body as the pydantic models of crud_agent_secrets.py (removed in #7667) do (extra="forbid"):
// each read notes its issue at its loc, and err is the 422 of all of them.
type form struct {
	at     []string
	fields map[string]json.RawMessage
	issues *[]httpx.Issue
}

// readForm is the request's body as an object whose members are all among names; nil with
// the 422 when it is none.
func readForm(r *http.Request, names ...string) (*form, error) {
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, err
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body"}, Msg: "Field required"})
	}
	if !json.Valid(raw) {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body", "0"}, Msg: "JSON decode error"})
	}
	f := &form{at: []string{"body"}, issues: &[]httpx.Issue{}}
	f.object(raw, names)
	return f, nil
}

func (f *form) object(raw json.RawMessage, names []string) bool {
	if json.Unmarshal(raw, &f.fields) != nil || f.fields == nil {
		f.fail(nil, "Input should be a valid dictionary or object to extract fields from")
		return false
	}
	for name := range f.fields {
		if !slices.Contains(names, name) {
			f.fail([]string{name}, "Extra inputs are not permitted")
		}
	}
	return true
}

func (f *form) fail(path []string, msg string) {
	*f.issues = append(*f.issues, httpx.Issue{Loc: append(slices.Clone(f.at), path...), Msg: msg})
}

func (f *form) err() error {
	if len(*f.issues) == 0 {
		return nil
	}
	return httpx.Invalid(*f.issues...)
}

// raw is the member name, absent or null as nil.
func (f *form) raw(name string) json.RawMessage {
	raw, ok := f.fields[name]
	if !ok || string(raw) == "null" {
		return nil
	}
	return raw
}

func (f *form) required(name string) (json.RawMessage, bool) {
	raw, ok := f.fields[name]
	if !ok && f.fields != nil {
		f.fail([]string{name}, "Field required")
	}
	return raw, ok
}

// nested is the member name as an object, nil when absent or null (or not an object).
func (f *form) nested(name string, names ...string) *form {
	raw := f.raw(name)
	if raw == nil {
		return nil
	}
	n := &form{at: append(slices.Clone(f.at), name), issues: f.issues}
	if !n.object(raw, names) {
		return nil
	}
	return n
}

// boolean is the optional member name, def when absent.
func (f *form) boolean(name string, def bool) bool {
	raw, ok := f.fields[name]
	if !ok {
		return def
	}
	var b bool
	if json.Unmarshal(raw, &b) != nil {
		f.fail([]string{name}, "Input should be a valid boolean")
	}
	return b
}

// needBool is the required boolean name.
func (f *form) needBool(name string) bool {
	if _, ok := f.required(name); !ok {
		return false
	}
	return f.boolean(name, false)
}

// text is the optional string name (null allowed), least to most characters, matching
// pattern when one is given.
func (f *form) text(name string, least, most int, pattern *regexp.Regexp) *string {
	raw := f.raw(name)
	if raw == nil {
		return nil
	}
	return f.check([]string{name}, raw, least, most, pattern)
}

// needText is the required string name; nullable lets it be null.
func (f *form) needText(name string, least, most int, pattern *regexp.Regexp, nullable bool) *string {
	raw, ok := f.required(name)
	if !ok {
		return nil
	}
	if string(raw) == "null" {
		if !nullable {
			f.fail([]string{name}, "Input should be a valid string")
		}
		return nil
	}
	return f.check([]string{name}, raw, least, most, pattern)
}

func (f *form) check(path []string, raw json.RawMessage, least, most int, pattern *regexp.Regexp) *string {
	var s string
	if json.Unmarshal(raw, &s) != nil {
		f.fail(path, "Input should be a valid string")
		return nil
	}
	n := utf8.RuneCountInString(s)
	switch {
	case n < least:
		f.fail(path, fmt.Sprintf("String should have at least %d characters", least))
	case n > most:
		f.fail(path, fmt.Sprintf("String should have at most %d characters", most))
	case pattern != nil && !pattern.MatchString(s):
		f.fail(path, fmt.Sprintf("String should match pattern '%s'", pattern))
	}
	return &s
}

// choice is the string name, one of choices; required unless def is given.
func (f *form) choice(name string, def *string, choices ...string) string {
	raw, ok := f.fields[name]
	if !ok {
		if def != nil {
			return *def
		}
		f.required(name)
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) != nil || !slices.Contains(choices, s) {
		f.fail([]string{name}, "Input should be "+strconv.Quote(choices[0])+" or another choice")
	}
	return s
}

// integer is the required integer name within [least, most].
func (f *form) integer(name string, least, most int64) int64 {
	raw, ok := f.required(name)
	if !ok {
		return 0
	}
	var n int64
	if json.Unmarshal(raw, &n) != nil {
		// pydantic takes a whole float such as 3.0 too.
		var x float64
		if json.Unmarshal(raw, &x) != nil || x != float64(int64(x)) {
			f.fail([]string{name}, "Input should be a valid integer")
			return 0
		}
		n = int64(x)
	}
	if n < least || n > most {
		f.fail([]string{name}, fmt.Sprintf("Input should be between %d and %d", least, most))
	}
	return n
}
