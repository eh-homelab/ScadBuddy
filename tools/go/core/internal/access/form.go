package access

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// bodyMax is the API's JSON gate (backend api/limits.py MAX_TEXT_BODY_BYTES). A chunked body
// is counted as it is read, as there.
const bodyMax = 8 * 1024 * 1024

// form collects what a request's shape gets wrong, as pydantic does: every issue at once.
type form struct{ issues []httpx.Issue }

func (f *form) fail(loc []string, msg string) {
	f.issues = append(f.issues, httpx.Issue{Loc: append([]string{}, loc...), Msg: msg})
}

// err is the 422 of the issues, nil when none.
func (f *form) err() error {
	if len(f.issues) == 0 {
		return nil
	}
	return httpx.Invalid(f.issues...)
}

func at(loc []string, more ...string) []string { return append(append([]string{}, loc...), more...) }

// readBody is the request's JSON body, read whole.
func readBody(r *http.Request) (json.RawMessage, error) {
	raw, err := io.ReadAll(http.MaxBytesReader(nil, r.Body, bodyMax))
	if err != nil {
		return nil, err
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body"}, Msg: "Field required"})
	}
	if !json.Valid(raw) {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body", "0"}, Msg: "JSON decode error"})
	}
	return raw, nil
}

// member is one member of a JSON object, in the order it is written.
type member struct {
	key string
	raw json.RawMessage
}

// members are an object's members in order, the last of a repeated key kept in the place of
// the first, as a Python dict keeps them; ok is false for a value that is no object.
func members(raw json.RawMessage) (out []member, ok bool) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	if t, err := dec.Token(); err != nil || t != json.Delim('{') {
		return nil, false
	}
	at := map[string]int{}
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return nil, false
		}
		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return nil, false
		}
		key := t.(string)
		if i, seen := at[key]; seen {
			out[i].raw = value
			continue
		}
		at[key] = len(out)
		out = append(out, member{key, value})
	}
	return out, true
}

// object is the object at loc with only the names given; nil when it is no object.
func (f *form) object(loc []string, raw json.RawMessage, names ...string) map[string]json.RawMessage {
	list, ok := members(raw)
	if !ok {
		f.fail(loc, "Input should be a valid dictionary or object to extract fields from")
		return nil
	}
	fields := map[string]json.RawMessage{}
	for _, m := range list {
		known := false
		for _, n := range names {
			known = known || n == m.key
		}
		if !known {
			f.fail(at(loc, m.key), "Extra inputs are not permitted")
		}
		fields[m.key] = m.raw
	}
	return fields
}

// required is the member name, nil (with an issue) when it is absent.
func (f *form) required(loc []string, fields map[string]json.RawMessage, name string) json.RawMessage {
	raw, ok := fields[name]
	if !ok && fields != nil {
		f.fail(at(loc, name), "Field required")
	}
	return raw
}

func isNull(raw json.RawMessage) bool { return string(bytes.TrimSpace(raw)) == "null" }

// text is the string at loc, least to most characters, matching pattern when it is not nil.
func (f *form) text(loc []string, raw json.RawMessage, least, most int, pattern *regexp.Regexp) string {
	if raw == nil {
		return ""
	}
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		f.fail(loc, "Input should be a valid string")
		return ""
	}
	if msg := lengthIssue(*s, least, most); msg != "" {
		f.fail(loc, msg)
	} else if pattern != nil && !pattern.MatchString(*s) {
		f.fail(loc, fmt.Sprintf("String should match pattern '%s'", pattern))
	}
	return *s
}

func lengthIssue(s string, least, most int) string {
	n := len([]rune(s))
	switch {
	case n < least:
		return fmt.Sprintf("String should have at least %d character%s", least, plural(least))
	case n > most:
		return fmt.Sprintf("String should have at most %d character%s", most, plural(most))
	}
	return ""
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

// boolean is the boolean at loc, fallback when it is absent.
func (f *form) boolean(loc []string, raw json.RawMessage, fallback bool) bool {
	if raw == nil {
		return fallback
	}
	var b *bool
	if json.Unmarshal(raw, &b) != nil || b == nil {
		f.fail(loc, "Input should be a valid boolean")
		return fallback
	}
	return *b
}

// number is the number at loc, fallback when it is absent; above least (or at it, when
// equal is true) and at most most.
func (f *form) number(loc []string, raw json.RawMessage, fallback, least float64, equal bool, most float64) float64 {
	if raw == nil {
		return fallback
	}
	var x *float64
	if json.Unmarshal(raw, &x) != nil || x == nil {
		f.fail(loc, "Input should be a valid number")
		return fallback
	}
	switch {
	case equal && *x < least:
		f.fail(loc, fmt.Sprintf("Input should be greater than or equal to %v", least))
	case !equal && *x <= least:
		f.fail(loc, fmt.Sprintf("Input should be greater than %v", least))
	case *x > most:
		f.fail(loc, fmt.Sprintf("Input should be less than or equal to %v", most))
	}
	return *x
}

// integer is the integer at loc, least to most. A number with no fraction counts.
func (f *form) integer(loc []string, raw json.RawMessage, least, most int64) int64 {
	if raw == nil {
		return 0
	}
	var x *float64
	if json.Unmarshal(raw, &x) != nil || x == nil || *x != float64(int64(*x)) {
		f.fail(loc, "Input should be a valid integer")
		return 0
	}
	n := int64(*x)
	switch {
	case n < least:
		f.fail(loc, fmt.Sprintf("Input should be greater than or equal to %d", least))
	case n > most:
		f.fail(loc, fmt.Sprintf("Input should be less than or equal to %d", most))
	}
	return n
}

// list is the list at loc, least to most items.
func (f *form) list(loc []string, raw json.RawMessage, least, most int) []json.RawMessage {
	if raw == nil {
		return nil
	}
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || items == nil {
		f.fail(loc, "Input should be a valid list")
		return nil
	}
	if len(items) < least {
		f.fail(loc, fmt.Sprintf("List should have at least %d item%s after validation", least, plural(least)))
	}
	if len(items) > most {
		f.fail(loc, fmt.Sprintf("List should have at most %d items after validation", most))
		return nil
	}
	return items
}

// texts is a list of strings, each least to most characters matching pattern.
func (f *form) texts(loc []string, raw json.RawMessage, least, most, chars int, pattern *regexp.Regexp) []string {
	items := f.list(loc, raw, least, most)
	out := make([]string, len(items))
	for i, item := range items {
		out[i] = f.text(at(loc, strconv.Itoa(i)), item, 1, chars, pattern)
	}
	return out
}

var uuidForm = regexp.MustCompile(
	`^(?i:urn:uuid:)?(\{)?([0-9A-Fa-f]{8})-?([0-9A-Fa-f]{4})-?([0-9A-Fa-f]{4})-?([0-9A-Fa-f]{4})-?([0-9A-Fa-f]{12})(\})?$`)

// canonicalUUID is s as Python's str(UUID(s)): lower case, hyphenated; ok false for no UUID.
func canonicalUUID(s string) (string, bool) {
	m := uuidForm.FindStringSubmatch(s)
	if m == nil || (m[1] == "") != (m[7] == "") {
		return "", false
	}
	return strings.ToLower(m[2] + "-" + m[3] + "-" + m[4] + "-" + m[5] + "-" + m[6]), true
}

// uuid is the UUID at loc, canonical.
func (f *form) uuid(loc []string, raw json.RawMessage) string {
	if raw == nil {
		return ""
	}
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		f.fail(loc, "UUID input should be a string, bytes or UUID object")
		return ""
	}
	id, ok := canonicalUUID(*s)
	if !ok {
		f.fail(loc, "Input should be a valid UUID")
	}
	return id
}

// query is the last value of the query parameter name; ok false (with an issue) when absent.
func (f *form) query(r *http.Request, name string) (string, bool) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		f.fail([]string{"query", name}, "Field required")
		return "", false
	}
	return values[len(values)-1], true
}

// isoTime is a timestamptz as pydantic writes a UTC datetime.
func isoTime(t time.Time) string {
	t = t.UTC()
	if t.Nanosecond()/1000 == 0 {
		return t.Format("2006-01-02T15:04:05Z")
	}
	return t.Format("2006-01-02T15:04:05.000000Z")
}
