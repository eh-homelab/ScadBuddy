package notify

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// bodyMax is the API's JSON gate (api/limits.py MAX_TEXT_BODY_BYTES).
const bodyMax = 8 * 1024 * 1024

// form is a request's JSON object as pydantic reads a model with extra="forbid": each read
// notes what is wrong, and err is the 422 of all of it.
type form struct {
	fields map[string]json.RawMessage
	issues *[]httpx.Issue
	at     []string
}

func readForm(r *http.Request, names ...string) (*form, error) {
	raw, err := io.ReadAll(http.MaxBytesReader(nil, r.Body, bodyMax))
	if err != nil {
		return nil, err
	}
	if len(raw) == 0 {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body"}, Msg: "Field required"})
	}
	if !json.Valid(raw) {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body", "0"}, Msg: "JSON decode error"})
	}
	f := &form{issues: &[]httpx.Issue{}, at: []string{"body"}}
	f.object(raw, names)
	return f, nil
}

func (f *form) object(raw json.RawMessage, names []string) {
	if json.Unmarshal(raw, &f.fields) != nil || f.fields == nil {
		f.fields = nil
		f.fail(nil, "Input should be a valid dictionary or object to extract fields from")
		return
	}
	for name := range f.fields {
		if !slices.Contains(names, name) {
			f.fail([]string{name}, "Extra inputs are not permitted")
		}
	}
}

func (f *form) fail(path []string, msg string) {
	loc := append(append([]string{}, f.at...), path...)
	*f.issues = append(*f.issues, httpx.Issue{Loc: loc, Msg: msg})
}

func (f *form) err() error {
	if len(*f.issues) == 0 {
		return nil
	}
	return httpx.Invalid(*f.issues...)
}

// member is the field name: ok false when it is absent (a missing required one noted).
func (f *form) member(name string, required bool) (json.RawMessage, bool) {
	if f.fields == nil {
		return nil, false
	}
	raw, ok := f.fields[name]
	if !ok && required {
		f.fail([]string{name}, "Field required")
	}
	return raw, ok
}

// text is the string name, least to most characters, matching pattern when one is given;
// nil when it is absent, null where nullable, or wrong.
func (f *form) text(name string, required, nullable bool, least, most int, pattern *regexp.Regexp) *string {
	raw, ok := f.member(name, required)
	if !ok {
		return nil
	}
	if nullable && string(raw) == "null" {
		return nil
	}
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		f.fail([]string{name}, "Input should be a valid string")
		return nil
	}
	n := utf8.RuneCountInString(*s)
	switch {
	case n < least:
		f.fail([]string{name}, fmt.Sprintf("String should have at least %d characters", least))
	case n > most:
		f.fail([]string{name}, fmt.Sprintf("String should have at most %d characters", most))
	case pattern != nil && !pattern.MatchString(*s):
		f.fail([]string{name}, fmt.Sprintf("String should match pattern '%s'", pattern))
	default:
		return s
	}
	return nil
}

// choice is the required string name, one of values.
func (f *form) choice(name string, values ...string) string {
	raw, ok := f.member(name, true)
	if !ok {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) != nil || !slices.Contains(values, s) {
		f.fail([]string{name}, "Input should be "+strings.Join(values, " or "))
		return ""
	}
	return s
}

// flag is the boolean name, otherwise when it is absent.
func (f *form) flag(name string, otherwise bool) bool {
	raw, ok := f.member(name, false)
	if !ok {
		return otherwise
	}
	var b bool
	if json.Unmarshal(raw, &b) != nil {
		f.fail([]string{name}, "Input should be a valid boolean")
	}
	return b
}

// integer is the required whole number name, least to most.
func (f *form) integer(name string, least, most int64) int64 {
	raw, ok := f.member(name, true)
	if !ok {
		return 0
	}
	var x float64
	if json.Unmarshal(raw, &x) != nil || x != math.Trunc(x) {
		f.fail([]string{name}, "Input should be a valid integer")
		return 0
	}
	switch n := int64(x); {
	case n < least:
		f.fail([]string{name}, fmt.Sprintf("Input should be greater than or equal to %d", least))
	case n > most:
		f.fail([]string{name}, fmt.Sprintf("Input should be less than or equal to %d", most))
	default:
		return n
	}
	return 0
}

// nested is the object name, nil when it is absent, or null where nullable.
func (f *form) nested(name string, required, nullable bool, names ...string) *form {
	raw, ok := f.member(name, required)
	if !ok || (nullable && string(raw) == "null") {
		return nil
	}
	n := &form{issues: f.issues, at: append(append([]string{}, f.at...), name)}
	n.object(raw, names)
	return n
}

// newUUID is a random (version 4) UUID.
func newUUID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	h := hex.EncodeToString(b)
	return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}

// stamp is a time as pydantic writes a datetime: UTC as Z, microseconds only when not 0.
type stamp time.Time

func (s stamp) MarshalJSON() ([]byte, error) {
	t := time.Time(s).UTC()
	layout := "2006-01-02T15:04:05Z"
	if t.Nanosecond()/1000 != 0 {
		layout = "2006-01-02T15:04:05.000000Z"
	}
	return json.Marshal(t.Format(layout))
}
