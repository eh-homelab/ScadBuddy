package crud

import (
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// Bytes is an answer of raw bytes, as Starlette's Response(content, media_type) is.
type Bytes struct {
	MediaType string
	Body      []byte
}

// Time is a timestamp written as pydantic writes one in UTC: microseconds only when it has
// some, and Z.
type Time time.Time

// MarshalJSON writes t in UTC.
func (t Time) MarshalJSON() ([]byte, error) {
	u := time.Time(t).UTC()
	layout := "2006-01-02T15:04:05Z"
	if u.Nanosecond()/1000 != 0 {
		layout = "2006-01-02T15:04:05.000000Z"
	}
	return []byte(`"` + u.Format(layout) + `"`), nil
}

// TimeOf is a nullable column's time, nil for NULL.
func TimeOf(t *time.Time) *Time {
	if t == nil {
		return nil
	}
	v := Time(*t)
	return &v
}

// ParseTime reads an ISO 8601 date and time as pydantic does; one with no offset is UTC.
func ParseTime(s string) (time.Time, bool) {
	for _, layout := range []string{
		"2006-01-02T15:04:05.999999999Z07:00", "2006-01-02 15:04:05.999999999Z07:00",
		"2006-01-02T15:04:05.999999999", "2006-01-02 15:04:05.999999999",
		"2006-01-02T15:04Z07:00", "2006-01-02T15:04",
	} {
		if t, err := time.Parse(layout, s); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

// PyUUID is s as pydantic reads a UUID (any case, hyphens or none, braces, urn:uuid:),
// in the lower-case hyphenated spelling; ok is false when it is none.
func PyUUID(s string) (string, bool) {
	s = strings.ToLower(s)
	s = strings.TrimPrefix(s, "urn:uuid:")
	if strings.HasPrefix(s, "{") && strings.HasSuffix(s, "}") {
		s = s[1 : len(s)-1]
	}
	if len(s) == 32 && !strings.Contains(s, "-") {
		s = s[:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:]
	}
	return s, IsUUID(s)
}

const uuidMsg = "Input should be a valid UUID"

// PyUUID is the path parameter name read as pydantic reads a UUID, or its 422.
func (p Params) PyUUID(name string) (string, error) {
	if id, ok := PyUUID(p[name]); ok {
		return id, nil
	}
	return "", httpx.Invalid(httpx.Issue{Loc: []string{"path", name}, Msg: uuidMsg})
}

// Match is the path parameter name when it matches pattern, or its 422.
func (p Params) Match(name string, pattern *regexp.Regexp) (string, error) {
	if v := p[name]; pattern.MatchString(v) {
		return v, nil
	}
	return "", httpx.Invalid(httpx.Issue{
		Loc: []string{"path", name}, Msg: fmt.Sprintf("String should match pattern '%s'", pattern),
	})
}

// Text is the path parameter name, least to most characters, or its 422.
func (p Params) Text(name string, least, most int) (string, error) {
	v := p[name]
	if msg := lengthIssue(v, least, most); msg != "" {
		return "", httpx.Invalid(httpx.Issue{Loc: []string{"path", name}, Msg: msg})
	}
	return v, nil
}

// QueryRequired is the 422 of the required query parameter name, absent.
func QueryRequired(name string) error { return queryIssue(name, "Field required") }

func query(r *http.Request, name string) (string, bool) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		return "", false
	}
	return values[len(values)-1], true
}

func queryIssue(name, msg string) error {
	return httpx.Invalid(httpx.Issue{Loc: []string{"query", name}, Msg: msg})
}

// QueryUUID is the required query parameter name, read as pydantic reads a UUID.
func QueryUUID(r *http.Request, name string) (string, error) {
	v, ok := query(r, name)
	if !ok {
		return "", queryIssue(name, "Field required")
	}
	if id, ok := PyUUID(v); ok {
		return id, nil
	}
	return "", queryIssue(name, uuidMsg)
}

// QueryMatch is the required query parameter name, matching pattern.
func QueryMatch(r *http.Request, name string, pattern *regexp.Regexp) (string, error) {
	v, ok := query(r, name)
	if !ok {
		return "", queryIssue(name, "Field required")
	}
	if !pattern.MatchString(v) {
		return "", queryIssue(name, fmt.Sprintf("String should match pattern '%s'", pattern))
	}
	return v, nil
}

// QueryOptMatch is the optional query parameter name, matching pattern; nil when absent.
func QueryOptMatch(r *http.Request, name string, pattern *regexp.Regexp) (*string, error) {
	if _, ok := query(r, name); !ok {
		return nil, nil
	}
	v, err := QueryMatch(r, name, pattern)
	return &v, err
}

// QueryOptText is the optional query parameter name, at most most characters.
func QueryOptText(r *http.Request, name string, most int) (*string, error) {
	v, ok := query(r, name)
	if !ok {
		return nil, nil
	}
	if msg := lengthIssue(v, 0, most); msg != "" {
		return nil, queryIssue(name, msg)
	}
	return &v, nil
}

// QueryEnum is the required query parameter name, one of values.
func QueryEnum(r *http.Request, name string, values ...string) (string, error) {
	v, ok := query(r, name)
	if !ok {
		return "", queryIssue(name, "Field required")
	}
	for _, want := range values {
		if v == want {
			return v, nil
		}
	}
	return "", queryIssue(name, "Input should be "+strings.Join(values, ", "))
}

// QueryBool is the optional query parameter name as pydantic reads a bool; false when absent.
func QueryBool(r *http.Request, name string) (bool, error) {
	v, ok := query(r, name)
	if !ok {
		return false, nil
	}
	switch strings.ToLower(v) {
	case "1", "on", "t", "true", "y", "yes":
		return true, nil
	case "0", "off", "f", "false", "n", "no":
		return false, nil
	}
	return false, queryIssue(name, "Input should be a valid boolean, unable to interpret input")
}

// Raw is the member name as it was sent; ok is false when it is absent.
func (b *Body) Raw(name string) (json.RawMessage, bool) {
	raw, ok := b.fields[name]
	return raw, ok
}

// Fail adds an issue at path, under the body.
func (b *Body) Fail(path []string, msg string) { b.fail(path, msg) }

// First is the message of the first issue, "" for none.
func (b *Body) First() string {
	if len(b.issues) == 0 {
		return ""
	}
	return b.issues[0].Msg
}

func present(raw json.RawMessage, ok bool) bool { return ok && string(raw) != "null" }

// OptText is the optional string name, least to most characters; nil when absent or null.
func (b *Body) OptText(name string, least, most int) *string {
	raw, ok := b.fields[name]
	if !present(raw, ok) {
		return nil
	}
	s := b.text([]string{name}, raw, least, most)
	return &s
}

// NullText is the required string name, at most most characters, which may be null.
func (b *Body) NullText(name string, most int) *string {
	raw, ok := b.required(name)
	if !present(raw, ok) {
		return nil
	}
	s := b.text([]string{name}, raw, 0, most)
	return &s
}

// Match is the required string name, matching pattern.
func (b *Body) Match(name string, pattern *regexp.Regexp) string {
	raw, ok := b.required(name)
	if !ok {
		return ""
	}
	return b.match([]string{name}, raw, pattern)
}

// OptMatch is the optional string name, matching pattern; nil when absent or null.
func (b *Body) OptMatch(name string, pattern *regexp.Regexp) *string {
	raw, ok := b.fields[name]
	if !present(raw, ok) {
		return nil
	}
	s := b.match([]string{name}, raw, pattern)
	return &s
}

func (b *Body) match(path []string, raw json.RawMessage, pattern *regexp.Regexp) string {
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		b.fail(path, "Input should be a valid string")
		return ""
	}
	if !pattern.MatchString(*s) {
		b.fail(path, fmt.Sprintf("String should match pattern '%s'", pattern))
	}
	return *s
}

// Enum is the required string name, one of values.
func (b *Body) Enum(name string, values ...string) string {
	raw, ok := b.required(name)
	if !ok {
		return ""
	}
	return b.enum([]string{name}, raw, values)
}

func (b *Body) enum(path []string, raw json.RawMessage, values []string) string {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		for _, v := range values {
			if s == v {
				return s
			}
		}
	}
	b.fail(path, "Input should be "+strings.Join(values, ", "))
	return ""
}

// PyUUID is the required member name, read as pydantic reads a UUID.
func (b *Body) PyUUID(name string) string {
	raw, ok := b.required(name)
	if !ok {
		return ""
	}
	return b.pyUUID([]string{name}, raw)
}

func (b *Body) pyUUID(path []string, raw json.RawMessage) string {
	var s string
	if json.Unmarshal(raw, &s) != nil {
		b.fail(path, "UUID input should be a string, bytes or UUID object")
		return ""
	}
	id, ok := PyUUID(s)
	if !ok {
		b.fail(path, uuidMsg)
	}
	return id
}

func (b *Body) list(name string, least, most int, required bool) []json.RawMessage {
	raw, ok := b.fields[name]
	if !ok && required {
		b.fail([]string{name}, "Field required")
	}
	if !ok {
		return []json.RawMessage{}
	}
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || items == nil {
		b.fail([]string{name}, "Input should be a valid list")
		return nil
	}
	if len(items) < least {
		b.fail([]string{name}, fmt.Sprintf("List should have at least %d item after validation", least))
	}
	if len(items) > most {
		b.fail([]string{name}, fmt.Sprintf("List should have at most %d items after validation", most))
		return nil
	}
	return items
}

// PyUUIDs is the required list name of least to most UUIDs, each read as pydantic does.
func (b *Body) PyUUIDs(name string, least, most int) []string {
	items := b.list(name, least, most, true)
	out := make([]string, len(items))
	for i, item := range items {
		out[i] = b.pyUUID([]string{name, strconv.Itoa(i)}, item)
	}
	return out
}

// Matches is the required list name of least to most strings, each matching pattern.
func (b *Body) Matches(name string, least, most int, pattern *regexp.Regexp) []string {
	items := b.list(name, least, most, true)
	out := make([]string, len(items))
	for i, item := range items {
		out[i] = b.match([]string{name, strconv.Itoa(i)}, item, pattern)
	}
	return out
}

// OptTexts is the optional list name of at most most strings of at most chars characters;
// empty when absent.
func (b *Body) OptTexts(name string, most, chars int) []string {
	items := b.list(name, 0, most, false)
	out := make([]string, len(items))
	for i, item := range items {
		out[i] = b.text([]string{name, strconv.Itoa(i)}, item, 0, chars)
	}
	return out
}

// Bool is the required bool name.
func (b *Body) Bool(name string) bool {
	raw, ok := b.required(name)
	if !ok {
		return false
	}
	return b.boolean(name, raw)
}

// OptBool is the optional bool name, fallback when absent.
func (b *Body) OptBool(name string, fallback bool) bool {
	raw, ok := b.fields[name]
	if !ok {
		return fallback
	}
	return b.boolean(name, raw)
}

func (b *Body) boolean(name string, raw json.RawMessage) bool {
	var v *bool
	if json.Unmarshal(raw, &v) != nil || v == nil {
		b.fail([]string{name}, "Input should be a valid boolean")
		return false
	}
	return *v
}

// IntIn is the integer name within [least, most]; fallback when it is absent, or required
// when fallback is nil.
func (b *Body) IntIn(name string, least, most int64, fallback *int64) int64 {
	raw, ok := b.fields[name]
	if !ok {
		if fallback != nil {
			return *fallback
		}
		b.fail([]string{name}, "Field required")
		return 0
	}
	var n *int64
	if json.Unmarshal(raw, &n) != nil || n == nil {
		b.fail([]string{name}, "Input should be a valid integer")
		return 0
	}
	switch {
	case *n < least:
		b.fail([]string{name}, fmt.Sprintf("Input should be greater than or equal to %d", least))
	case *n > most:
		b.fail([]string{name}, fmt.Sprintf("Input should be less than or equal to %d", most))
	}
	return *n
}

// Time is the required date and time name.
func (b *Body) Time(name string) time.Time {
	raw, ok := b.required(name)
	if !ok {
		return time.Time{}
	}
	return b.time(name, raw)
}

// OptTime is the optional date and time name; nil when absent or null.
func (b *Body) OptTime(name string) *time.Time {
	raw, ok := b.fields[name]
	if !present(raw, ok) {
		return nil
	}
	t := b.time(name, raw)
	return &t
}

func (b *Body) time(name string, raw json.RawMessage) time.Time {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		if t, ok := ParseTime(s); ok {
			return t
		}
	}
	b.fail([]string{name}, "Input should be a valid datetime")
	return time.Time{}
}

// Dict is the required object name as its JSON text: pydantic's dict[str, Any].
func (b *Body) Dict(name string) string {
	raw, ok := b.required(name)
	if !ok {
		return ""
	}
	var m map[string]json.RawMessage
	if json.Unmarshal(raw, &m) != nil || m == nil {
		b.fail([]string{name}, "Input should be a valid dictionary")
		return ""
	}
	return string(raw)
}

// Any is the member name's JSON text, null when it is absent.
func (b *Body) Any(name string) string {
	raw, ok := b.fields[name]
	if !ok {
		return "null"
	}
	return string(raw)
}
