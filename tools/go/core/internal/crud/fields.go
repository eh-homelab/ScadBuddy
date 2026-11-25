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
	"unicode/utf8"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// Reply is an answer whose status the handler decides, or whose body is not JSON.
type Reply struct {
	Status int
	// Value is written as JSON when MediaType is empty.
	Value     any
	MediaType string
	Headers   map[string]string
	Body      []byte
}

// writeAnswer writes a handler's answer: a *Reply as it says, else status with the value.
func writeAnswer(w http.ResponseWriter, status int, answer any) error {
	reply, ok := answer.(*Reply)
	if !ok {
		return httpx.WriteJSONStatus(w, status, answer)
	}
	for k, v := range reply.Headers {
		w.Header().Set(k, v)
	}
	if reply.MediaType != "" {
		httpx.WriteBytes(w, reply.Status, reply.MediaType, reply.Body)
		return nil
	}
	return httpx.WriteJSONStatus(w, reply.Status, reply.Value)
}

// Present says whether the body names name, null included: pydantic's model_fields_set.
func (b *Body) Present(name string) bool {
	_, ok := b.fields[name]
	return ok
}

func (b *Body) optional(name string) (json.RawMessage, bool) {
	raw, ok := b.fields[name]
	if !ok || string(raw) == "null" {
		return nil, false
	}
	return raw, true
}

// Match is the required string name, least to most characters, that matches pattern.
func (b *Body) Match(name string, least, most int, pattern *regexp.Regexp) string {
	raw, ok := b.required(name)
	if !ok {
		return ""
	}
	return b.matched([]string{name}, raw, least, most, pattern)
}

// OptText is the string name, least to most characters; nil when it is absent or null.
// A pattern, when given, must match.
func (b *Body) OptText(name string, least, most int, pattern *regexp.Regexp) *string {
	raw, ok := b.optional(name)
	if !ok {
		return nil
	}
	s := b.matched([]string{name}, raw, least, most, pattern)
	return &s
}

func (b *Body) matched(path []string, raw json.RawMessage, least, most int, pattern *regexp.Regexp) string {
	before := len(b.issues)
	s := b.text(path, raw, least, most)
	if len(b.issues) == before && pattern != nil && !pattern.MatchString(s) {
		b.fail(path, fmt.Sprintf("String should match pattern '%s'", pattern))
	}
	return s
}

// Choice is the required string name, one of choices.
func (b *Body) Choice(name string, choices ...string) string {
	raw, ok := b.required(name)
	if !ok {
		return ""
	}
	return b.choice([]string{name}, raw, choices)
}

// ChoiceOr is the string name, one of choices; or, when it is absent.
func (b *Body) ChoiceOr(name, or string, choices ...string) string {
	raw, ok := b.fields[name]
	if !ok {
		return or
	}
	return b.choice([]string{name}, raw, choices)
}

// OptChoice is the string name, one of choices; nil when it is absent or null.
func (b *Body) OptChoice(name string, choices ...string) *string {
	raw, ok := b.optional(name)
	if !ok {
		return nil
	}
	s := b.choice([]string{name}, raw, choices)
	return &s
}

func (b *Body) choice(path []string, raw json.RawMessage, choices []string) string {
	var s *string
	if json.Unmarshal(raw, &s) == nil && s != nil {
		for _, c := range choices {
			if c == *s {
				return c
			}
		}
	}
	quoted := make([]string, len(choices))
	for i, c := range choices {
		quoted[i] = "'" + c + "'"
	}
	msg := "Input should be " + quoted[0]
	if n := len(quoted); n > 1 {
		msg = "Input should be " + strings.Join(quoted[:n-1], ", ") + " or " + quoted[n-1]
	}
	b.fail(path, msg)
	return ""
}

// Bool is the boolean name; or, when it is absent.
func (b *Body) Bool(name string, or bool) bool {
	raw, ok := b.fields[name]
	if !ok {
		return or
	}
	return b.boolean([]string{name}, raw)
}

// RequiredBool is the required boolean name.
func (b *Body) RequiredBool(name string) bool {
	raw, ok := b.required(name)
	if !ok {
		return false
	}
	return b.boolean([]string{name}, raw)
}

// OptBool is the boolean name; nil when it is absent or null.
func (b *Body) OptBool(name string) *bool {
	raw, ok := b.optional(name)
	if !ok {
		return nil
	}
	v := b.boolean([]string{name}, raw)
	return &v
}

func (b *Body) boolean(path []string, raw json.RawMessage) bool {
	var v *bool
	if json.Unmarshal(raw, &v) != nil || v == nil {
		b.fail(path, "Input should be a valid boolean")
		return false
	}
	return *v
}

// OptInt is the integer name, at least least; nil when it is absent or null.
func (b *Body) OptInt(name string, least int64) *int64 {
	raw, ok := b.optional(name)
	if !ok {
		return nil
	}
	var n *int64
	if json.Unmarshal(raw, &n) != nil || n == nil {
		b.fail([]string{name}, "Input should be a valid integer")
		return nil
	}
	if *n < least {
		b.fail([]string{name}, fmt.Sprintf("Input should be greater than or equal to %d", least))
	}
	return n
}

// OptUUID is the string name, a UUID; nil when it is absent or null.
func (b *Body) OptUUID(name string) *string {
	if _, ok := b.optional(name); !ok {
		return nil
	}
	s := b.UUID(name)
	return &s
}

// RawObject is the required JSON object name, as it was sent.
func (b *Body) RawObject(name string) json.RawMessage {
	raw, ok := b.required(name)
	if !ok {
		return nil
	}
	var object map[string]json.RawMessage
	if json.Unmarshal(raw, &object) != nil || object == nil {
		b.fail([]string{name}, "Input should be a valid dictionary")
		return nil
	}
	return raw
}

// Strings is the list name of least to most strings, each itemLeast to itemMost characters.
// An absent list is empty when optional, else a missing field.
func (b *Body) Strings(name string, optional bool, least, most, itemLeast, itemMost int) []string {
	items, ok := b.list(name, optional, least, most)
	if !ok {
		return []string{}
	}
	texts := make([]string, len(items))
	for i, item := range items {
		texts[i] = b.text([]string{name, strconv.Itoa(i)}, item, itemLeast, itemMost)
	}
	return texts
}

// Objects is the list name of at most most objects, each with members among names. Each
// item's Done gives its issues to the body. An absent list is empty when optional.
func (b *Body) Objects(name string, optional bool, most int, names ...string) []*Nested {
	items, ok := b.list(name, optional, 0, most)
	if !ok {
		return nil
	}
	out := make([]*Nested, len(items))
	for i, item := range items {
		n := &Nested{Body: Body{at: append(append([]string{}, b.at...), name, strconv.Itoa(i))}, parent: b}
		n.object(item, names)
		out[i] = n
	}
	return out
}

func (b *Body) list(name string, optional bool, least, most int) ([]json.RawMessage, bool) {
	raw, ok := b.fields[name]
	if !ok {
		if !optional && b.fields != nil {
			b.fail([]string{name}, "Field required")
		}
		return nil, false
	}
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || items == nil {
		b.fail([]string{name}, "Input should be a valid list")
		return nil, false
	}
	if len(items) < least {
		b.fail([]string{name}, fmt.Sprintf("List should have at least %d item after validation", least))
	}
	if len(items) > most {
		b.fail([]string{name}, fmt.Sprintf("List should have at most %d items after validation", most))
		return nil, false
	}
	return items, true
}

// Fail adds a value error at the body's path, as a pydantic validator's.
func (b *Body) Fail(path []string, msg string) { b.fail(path, "Value error, "+msg) }

// QueryUUID is the query parameter name, a UUID; nil when the request does not name it and
// it is optional.
func QueryUUID(r *http.Request, name string, optional bool) (*string, error) {
	value, given := query(r, name)
	issue := httpx.Issue{Loc: []string{"query", name}, Msg: "Field required"}
	if !given {
		if optional {
			return nil, nil
		}
		return nil, httpx.Invalid(issue)
	}
	if IsUUID(value) {
		return &value, nil
	}
	issue.Msg = fmt.Sprintf("String should match pattern '%s'", uuidPattern)
	return nil, httpx.Invalid(issue)
}

// QueryOptText is the query parameter name, least to most characters; nil when the
// request does not name it.
func QueryOptText(r *http.Request, name string, least, most int) (*string, error) {
	value, given := query(r, name)
	if !given {
		return nil, nil
	}
	if msg := lengthIssue(value, least, most); msg != "" {
		return nil, queryIssue(name, msg)
	}
	return &value, nil
}

// QueryChoice is the required query parameter name, one of choices.
func QueryChoice(r *http.Request, name string, choices ...string) (string, error) {
	value, given := query(r, name)
	if !given {
		return "", queryIssue(name, "Field required")
	}
	for _, c := range choices {
		if c == value {
			return c, nil
		}
	}
	return "", queryIssue(name, "Input should be one of "+strings.Join(choices, ", "))
}

// QueryBool is the query parameter name as pydantic reads a boolean; false when the request
// does not name it.
func QueryBool(r *http.Request, name string) (bool, error) {
	value, given := query(r, name)
	if !given {
		return false, nil
	}
	switch strings.ToLower(value) {
	case "1", "on", "t", "true", "y", "yes":
		return true, nil
	case "0", "off", "f", "false", "n", "no":
		return false, nil
	}
	return false, queryIssue(name, "Input should be a valid boolean, unable to interpret input")
}

// Match is the path parameter name, or the 422 of one that does not match pattern.
func (p Params) Match(name string, pattern *regexp.Regexp) (string, error) {
	if v := p[name]; pattern.MatchString(v) {
		return v, nil
	}
	return "", httpx.Invalid(httpx.Issue{
		Loc: []string{"path", name},
		Msg: fmt.Sprintf("String should match pattern '%s'", pattern),
	})
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

// Scan reads a timestamptz.
func (t *Time) Scan(src any) error {
	v, ok := src.(time.Time)
	if !ok {
		return fmt.Errorf("a %T is no time", src)
	}
	*t = Time(v)
	return nil
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
	// As the uuid crate's parse_str: braces or the urn: prefix only around the hyphenated form.
	hyphenated := len(s) == 36
	if len(s) == 38 && s[0] == '{' && s[37] == '}' {
		s, hyphenated = s[1:37], true
	} else if rest, ok := strings.CutPrefix(s, "urn:uuid:"); ok && len(rest) == 36 {
		s, hyphenated = rest, true
	}
	s = strings.ToLower(s)
	if len(s) == 32 && !hyphenated && !strings.Contains(s, "-") {
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
	return replaceInvalidUTF8(values[len(values)-1]), true
}

// replaceInvalidUTF8 is s with each maximal invalid UTF-8 subpart as U+FFFD, as Python's
// decode with errors="replace" (and so Starlette's query) reads it.
func replaceInvalidUTF8(s string) string {
	if utf8.ValidString(s) {
		return s
	}
	var b strings.Builder
	for i := 0; i < len(s); {
		r, n := utf8.DecodeRuneInString(s[i:])
		if r != utf8.RuneError || n > 1 {
			b.WriteString(s[i : i+n])
			i += n
			continue
		}
		b.WriteRune(utf8.RuneError)
		i += invalidSubpart(s[i:])
	}
	return b.String()
}

// invalidSubpart is the length of the invalid sequence at the start of s: a lead byte and
// the continuation bytes that still fit it, at least 1.
func invalidSubpart(s string) int {
	lead := s[0]
	need, low, high := 0, byte(0x80), byte(0xbf)
	switch {
	case lead >= 0xc2 && lead <= 0xdf:
		need = 1
	case lead >= 0xe0 && lead <= 0xef:
		need = 2
		if lead == 0xe0 {
			low = 0xa0
		} else if lead == 0xed {
			high = 0x9f
		}
	case lead >= 0xf0 && lead <= 0xf4:
		need = 3
		if lead == 0xf0 {
			low = 0x90
		} else if lead == 0xf4 {
			high = 0x8f
		}
	}
	n := 1
	for n <= need && n < len(s) && s[n] >= low && s[n] <= high {
		n++
		low, high = 0x80, 0xbf
	}
	return n
}

func queryIssue(name, msg string) error {
	return httpx.Invalid(httpx.Issue{Loc: []string{"query", name}, Msg: msg})
}

// QueryPyUUID is the required query parameter name, read as pydantic reads a UUID.
func QueryPyUUID(r *http.Request, name string) (string, error) {
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

// Required is the member name as it was sent, with "Field required" when an object lacks
// it; ok is false when it is absent.
func (b *Body) Required(name string) (json.RawMessage, bool) { return b.required(name) }

// Raw is the member name as it was sent; ok is false when it is absent.
func (b *Body) Raw(name string) (json.RawMessage, bool) {
	raw, ok := b.fields[name]
	return raw, ok
}

// FailAt adds an issue at path, under the body, with msg as it is.
func (b *Body) FailAt(path []string, msg string) { b.fail(path, msg) }

// First is the message of the first issue, "" for none.
func (b *Body) First() string {
	if len(b.issues) == 0 {
		return ""
	}
	return b.issues[0].Msg
}

func present(raw json.RawMessage, ok bool) bool { return ok && string(raw) != "null" }

// NullText is the required string name, at most most characters, which may be null.
func (b *Body) NullText(name string, most int) *string {
	raw, ok := b.required(name)
	if !present(raw, ok) {
		return nil
	}
	s := b.text([]string{name}, raw, 0, most)
	return &s
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

// PyUUIDs is the required list name of least to most UUIDs, each read as pydantic does.
func (b *Body) PyUUIDs(name string, least, most int) []string {
	items, _ := b.list(name, false, least, most)
	out := make([]string, len(items))
	for i, item := range items {
		out[i] = b.pyUUID([]string{name, strconv.Itoa(i)}, item)
	}
	return out
}

// Matches is the required list name of least to most strings, each matching pattern.
func (b *Body) Matches(name string, least, most int, pattern *regexp.Regexp) []string {
	items, _ := b.list(name, false, least, most)
	out := make([]string, len(items))
	for i, item := range items {
		out[i] = b.matched([]string{name, strconv.Itoa(i)}, item, 0, math.MaxInt, pattern)
	}
	return out
}

// IntIn is the integer name within [least, most]; fallback when it is absent, or required
// when fallback is nil.
func (b *Body) IntIn(name string, least, most int64, fallback *int64) int64 {
	raw, ok := b.fields[name]
	if !ok {
		if fallback != nil {
			return *fallback
		}
		b.required(name)
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

// QueryOptChoice is the optional query parameter name, one of allowed; nil when not given.
func QueryOptChoice(r *http.Request, name string, allowed ...string) (*string, error) {
	value, given := query(r, name)
	if !given {
		return nil, nil
	}
	for _, a := range allowed {
		if a == value {
			return &value, nil
		}
	}
	return nil, queryIssue(name, "Input should be one of the allowed values")
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
	value, given := query(r, name)
	if !given {
		if required {
			return nil, queryIssue(name, "Field required")
		}
		return nil, nil
	}
	if msg := lengthIssue(value, least, most); msg != "" {
		return nil, queryIssue(name, msg)
	}
	return &value, nil
}
