package crud

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// Value is one member of a body, read as a pydantic field would be. Its issues are the body's.
type Value struct {
	b     *Body
	path  []string
	raw   json.RawMessage
	given bool
}

// Get is the member name of the body; Given is false when it is absent.
func (b *Body) Get(name string) Value {
	raw, ok := b.fields[name]
	return Value{b: b, path: []string{name}, raw: raw, given: ok}
}

// Given says whether the member is in the body.
func (v Value) Given() bool { return v.given }

// Null says whether the member is absent or null: an optional field's None.
func (v Value) Null() bool { return !v.given || string(v.raw) == "null" }

func (v Value) fail(msg string) { v.b.fail(v.path, msg) }

// present is false, with the issue, for an absent member; a null one is the type's issue.
func (v Value) present() bool {
	if !v.given && v.b.fields != nil {
		v.fail("Field required")
	}
	return v.given
}

// String is a required string of least to most characters that matches pattern, when one.
func (v Value) String(least, most int, pattern *regexp.Regexp) string {
	if !v.present() {
		return ""
	}
	var s *string
	if json.Unmarshal(v.raw, &s) != nil || s == nil {
		v.fail("Input should be a valid string")
		return ""
	}
	if msg := lengthIssue(*s, least, most); msg != "" {
		v.fail(msg)
	} else if pattern != nil && !pattern.MatchString(*s) {
		v.fail(fmt.Sprintf("String should match pattern '%s'", pattern))
	}
	return *s
}

// OptString is String, or nil when the member is absent or null.
func (v Value) OptString(least, most int, pattern *regexp.Regexp) *string {
	if v.Null() {
		return nil
	}
	s := v.String(least, most, pattern)
	return &s
}

// Enum is a required string among allowed.
func (v Value) Enum(allowed ...string) string {
	if !v.present() {
		return ""
	}
	var s *string
	if json.Unmarshal(v.raw, &s) == nil && s != nil {
		for _, a := range allowed {
			if a == *s {
				return *s
			}
		}
	}
	quoted := make([]string, len(allowed))
	for i, a := range allowed {
		quoted[i] = "'" + a + "'"
	}
	v.fail("Input should be " + strings.Join(quoted, ", "))
	return ""
}

// OptEnum is Enum, or nil when the member is absent or null.
func (v Value) OptEnum(allowed ...string) *string {
	if v.Null() {
		return nil
	}
	s := v.Enum(allowed...)
	return &s
}

// Int is a required integer within [least, most]. Unless strict, a float with no fraction is
// one, as pydantic's lax mode takes it.
func (v Value) Int(least, most int64, strict bool) int64 {
	if !v.present() {
		return 0
	}
	if string(v.raw) == "null" {
		v.fail("Input should be a valid integer")
		return 0
	}
	var n int64
	if err := json.Unmarshal(v.raw, &n); err != nil {
		var f float64
		if strict || json.Unmarshal(v.raw, &f) != nil || f != math.Trunc(f) || math.Abs(f) > 1<<53 {
			v.fail("Input should be a valid integer")
			return 0
		}
		n = int64(f)
	}
	switch {
	case n < least:
		v.fail(fmt.Sprintf("Input should be greater than or equal to %d", least))
	case n > most:
		v.fail(fmt.Sprintf("Input should be less than or equal to %d", most))
	}
	return n
}

// Float is a required number: greater than least (or equal to it, with orEqual), at most
// most. A NaN or an infinity is no number here.
func (v Value) Float(least float64, orEqual bool, most float64) float64 {
	if !v.present() {
		return 0
	}
	var f *float64
	if json.Unmarshal(v.raw, &f) != nil || f == nil {
		v.fail("Input should be a valid number")
		return 0
	}
	switch {
	case orEqual && *f < least:
		v.fail("Input should be greater than or equal to " + number(least))
	case !orEqual && *f <= least:
		v.fail("Input should be greater than " + number(least))
	case *f > most:
		v.fail("Input should be less than or equal to " + number(most))
	}
	return *f
}

func number(f float64) string { return strconv.FormatFloat(f, 'f', -1, 64) }

// Bool is a required boolean.
func (v Value) Bool() bool {
	if !v.present() {
		return false
	}
	var b *bool
	if json.Unmarshal(v.raw, &b) != nil || b == nil {
		v.fail("Input should be a valid boolean")
		return false
	}
	return *b
}

// BoolOr is the boolean, or def when the member is absent; a null is an issue.
func (v Value) BoolOr(def bool) bool {
	if !v.given {
		return def
	}
	return v.Bool()
}

// UUID is a required UUID in any spelling Python's uuid.UUID takes, in its canonical one.
func (v Value) UUID() string {
	if !v.present() {
		return ""
	}
	var s *string
	if json.Unmarshal(v.raw, &s) != nil || s == nil {
		v.fail("UUID input should be a string, bytes or UUID object")
		return ""
	}
	id, ok := CanonicalUUID(*s)
	if !ok {
		v.fail("Input should be a valid UUID")
	}
	return id
}

// CanonicalUUID is s as str(uuid.UUID(s)) spells it.
func CanonicalUUID(s string) (string, bool) {
	s = strings.TrimPrefix(strings.TrimPrefix(s, "urn:"), "uuid:")
	s = strings.Trim(s, "{}")
	s = strings.ToLower(strings.ReplaceAll(s, "-", ""))
	if len(s) != 32 {
		return "", false
	}
	for _, c := range s {
		if !strings.ContainsRune("0123456789abcdef", c) {
			return "", false
		}
	}
	return s[:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:], true
}

// Dict is a required JSON object, as it was sent.
func (v Value) Dict() json.RawMessage {
	if !v.present() {
		return nil
	}
	var m map[string]json.RawMessage
	if json.Unmarshal(v.raw, &m) != nil || m == nil {
		v.fail("Input should be a valid dictionary")
		return nil
	}
	return v.raw
}

// List is a required list of least to most items, each a Value of its own.
func (v Value) List(least, most int) []Value {
	if !v.present() {
		return nil
	}
	var items []json.RawMessage
	if json.Unmarshal(v.raw, &items) != nil || items == nil {
		v.fail("Input should be a valid list")
		return nil
	}
	if len(items) < least {
		v.fail(fmt.Sprintf("List should have at least %d item%s after validation", least, plural(least)))
	}
	if len(items) > most {
		v.fail(fmt.Sprintf("List should have at most %d item%s after validation", most, plural(most)))
		return nil
	}
	out := make([]Value, len(items))
	for i, raw := range items {
		path := append(append([]string{}, v.path...), strconv.Itoa(i))
		out[i] = Value{b: v.b, path: path, raw: raw, given: true}
	}
	return out
}

// Object is a required object whose members are all among names; nil when it is not one.
// Its issues are the body's.
func (v Value) Object(names ...string) *Body {
	if !v.present() {
		return nil
	}
	n := &Body{at: append(append([]string{}, v.b.at...), v.path...)}
	n.object(v.raw, names)
	if n.fields == nil {
		v.b.issues = append(v.b.issues, n.issues...)
		return nil
	}
	n.parent = v.b
	return n
}

// OptObject is Object, or nil when the member is absent or null.
func (v Value) OptObject(names ...string) *Body {
	if v.Null() {
		return nil
	}
	return v.Object(names...)
}

// Close gives a nested object's issues to the body it is in. A nil one has none.
func (b *Body) Close() {
	if b != nil && b.parent != nil {
		b.parent.issues = append(b.parent.issues, b.issues...)
		b.issues = nil
	}
}

// Invalid is the 422 of a body that is valid by its fields but not as a whole.
func Invalid(detail string) error {
	return &httpx.Problem{Status: 422, Detail: detail}
}

// Stamp is a time as pydantic writes a datetime in UTC: microseconds only when any.
type Stamp time.Time

// Scan reads a timestamptz.
func (s *Stamp) Scan(src any) error {
	t, ok := src.(time.Time)
	if !ok {
		return fmt.Errorf("a %T is no time", src)
	}
	*s = Stamp(t)
	return nil
}

func (s Stamp) MarshalJSON() ([]byte, error) {
	t := time.Time(s).UTC()
	out := t.Format("2006-01-02T15:04:05")
	if us := t.Nanosecond() / 1000; us != 0 {
		out += fmt.Sprintf(".%06d", us)
	}
	return []byte(`"` + out + `Z"`), nil
}

// QueryEnum is the optional query parameter name, one of allowed; nil when not given.
func QueryEnum(r *http.Request, name string, allowed ...string) (*string, error) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		return nil, nil
	}
	value := values[len(values)-1]
	for _, a := range allowed {
		if a == value {
			return &value, nil
		}
	}
	return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", name}, Msg: "Input should be one of the allowed values"})
}

// Raw is the member's JSON as it was sent.
func (v Value) Raw() json.RawMessage { return v.raw }

// Fail adds an issue at the member, as a field validator's ValueError.
func Fail(v Value, msg string) { v.fail(msg) }

// Issues are a 422's issues; none for another error.
func Issues(err error) []httpx.Issue {
	var p *httpx.Problem
	if !errors.As(err, &p) {
		return nil
	}
	for _, f := range p.Extensions {
		if issues, ok := f.Value.([]httpx.Issue); ok && f.Key == "errors" {
			return issues
		}
	}
	return nil
}

// QueryString is the query parameter name of least to most characters; nil when it is not
// given and not required.
func QueryString(r *http.Request, name string, least, most int, required bool) (*string, error) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		if required {
			return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", name}, Msg: "Field required"})
		}
		return nil, nil
	}
	value := values[len(values)-1]
	if msg := lengthIssue(value, least, most); msg != "" {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", name}, Msg: msg})
	}
	return &value, nil
}

// Bytes is an answer that is not JSON: its media type, headers and body.
type Bytes struct {
	MediaType string
	Headers   map[string]string
	Body      []byte
	noContent bool
}

// NoContent is a 204's answer.
var NoContent = &Bytes{noContent: true}

func (b *Bytes) write(w http.ResponseWriter) {
	if b.noContent {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	for k, v := range b.Headers {
		w.Header().Set(k, v)
	}
	w.Header().Set("Content-Type", b.MediaType)
	w.Header().Set("Content-Length", strconv.Itoa(len(b.Body)))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(b.Body)
}

// Match is the path parameter name when it matches pattern, or its 422.
func (p Params) Match(name string, pattern *regexp.Regexp) (string, error) {
	if v := p[name]; pattern.MatchString(v) {
		return v, nil
	}
	return "", httpx.Invalid(httpx.Issue{
		Loc: []string{"path", name},
		Msg: fmt.Sprintf("String should match pattern '%s'", pattern),
	})
}
