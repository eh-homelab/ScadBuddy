package plugins

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// form is a JSON object as pydantic reads a model with extra="forbid": the declared fields
// in their order, each read noting what is wrong, then the extra members; err is the 422.
type form struct {
	issues *[]httpx.Issue
	at     []string
	fields map[string]json.RawMessage
	order  []string
	names  []string
}

// mode is how a field is declared: required, required but nullable (`T | None` with no
// default), or optional (`T | None = None`).
type mode int

const (
	required mode = iota
	nullable
	optional
)

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
	f := &form{issues: &[]httpx.Issue{}, at: []string{"body"}, names: names}
	if !f.parse(raw) {
		f.fail(nil, "Input should be a valid dictionary or object to extract fields from")
	}
	return f, nil
}

// parse reads raw as an object, its members in order; false when it is none.
func (f *form) parse(raw json.RawMessage) bool {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if t, err := dec.Token(); err != nil || t != json.Delim('{') {
		return false
	}
	f.fields = map[string]json.RawMessage{}
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return false
		}
		key, _ := t.(string)
		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return false
		}
		if _, seen := f.fields[key]; !seen {
			f.order = append(f.order, key)
		}
		f.fields[key] = value
	}
	return true
}

func (f *form) fail(path []string, msg string) {
	loc := append(append([]string{}, f.at...), path...)
	*f.issues = append(*f.issues, httpx.Issue{Loc: loc, Msg: msg})
}

// done notes the members the model does not declare, after its own fields' issues.
func (f *form) done() {
	for _, key := range f.order {
		if !slices.Contains(f.names, key) {
			f.fail([]string{key}, "Extra inputs are not permitted")
		}
	}
}

// err is the 422 of everything the reads noted, nil when nothing.
func (f *form) err() error {
	f.done()
	if len(*f.issues) == 0 {
		return nil
	}
	return httpx.Invalid(*f.issues...)
}

// given says whether the body names name, null included: pydantic's model_fields_set.
func (f *form) given(name string) bool {
	_, ok := f.fields[name]
	return ok
}

// member is the raw value of name: ok false when there is nothing to read, a missing
// required one noted, a null nullable or optional one left nil.
func (f *form) member(name string, m mode) (json.RawMessage, bool) {
	if f.fields == nil {
		return nil, false
	}
	raw, ok := f.fields[name]
	if !ok {
		if m != optional {
			f.fail([]string{name}, "Field required")
		}
		return nil, false
	}
	if m != required && string(raw) == "null" {
		return nil, false
	}
	return raw, true
}

func lengthIssue(s string, least, most int) string {
	n := utf8.RuneCountInString(s)
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

// str checks raw as a string of least to most characters matching pattern, when one.
func (f *form) str(path []string, raw json.RawMessage, least, most int, pattern *regexp.Regexp) string {
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		f.fail(path, "Input should be a valid string")
		return ""
	}
	if msg := lengthIssue(*s, least, most); msg != "" {
		f.fail(path, msg)
	} else if pattern != nil && !pattern.MatchString(*s) {
		f.fail(path, fmt.Sprintf("String should match pattern '%s'", pattern))
	}
	return *s
}

// text is the string field name; nil when it is absent or null and may be.
func (f *form) text(name string, m mode, least, most int, pattern *regexp.Regexp) *string {
	raw, ok := f.member(name, m)
	if !ok {
		return nil
	}
	s := f.str([]string{name}, raw, least, most, pattern)
	return &s
}

// textOr is the string field name, or fallback when it is absent.
func (f *form) textOr(name, fallback string, least, most int) string {
	if !f.given(name) {
		return fallback
	}
	return *f.text(name, required, least, most, nil)
}

func literalIssue(choices []string) string {
	quoted := make([]string, len(choices))
	for i, c := range choices {
		quoted[i] = "'" + c + "'"
	}
	if n := len(quoted); n > 1 {
		return "Input should be " + strings.Join(quoted[:n-1], ", ") + " or " + quoted[n-1]
	}
	return "Input should be " + quoted[0]
}

func (f *form) literalAt(path []string, raw json.RawMessage, choices []string) string {
	var s *string
	if json.Unmarshal(raw, &s) == nil && s != nil && slices.Contains(choices, *s) {
		return *s
	}
	f.fail(path, literalIssue(choices))
	return ""
}

// literal is the field name, one of choices; nil when it is absent or null and may be.
func (f *form) literal(name string, m mode, choices ...string) *string {
	raw, ok := f.member(name, m)
	if !ok {
		return nil
	}
	s := f.literalAt([]string{name}, raw, choices)
	return &s
}

// boolean is the field name as pydantic's lax bool reads it.
func (f *form) boolean(name string, m mode) *bool {
	raw, ok := f.member(name, m)
	if !ok {
		return nil
	}
	var v any
	_ = json.Unmarshal(raw, &v)
	switch t := v.(type) {
	case bool:
		return &t
	case float64:
		if t == 0 || t == 1 {
			b := t == 1
			return &b
		}
		f.fail([]string{name}, "Input should be a valid boolean, unable to interpret input")
		return nil
	case string:
		switch strings.ToLower(t) {
		case "0", "off", "f", "false", "n", "no":
			b := false
			return &b
		case "1", "on", "t", "true", "y", "yes":
			b := true
			return &b
		}
		f.fail([]string{name}, "Input should be a valid boolean, unable to interpret input")
		return nil
	}
	f.fail([]string{name}, "Input should be a valid boolean")
	return nil
}

// integer is the required field name as pydantic's lax int reads it, greater than above and
// at most most.
func (f *form) integer(name string, above, most int64) int64 {
	raw, ok := f.member(name, required)
	if !ok {
		return 0
	}
	path := []string{name}
	var v any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	_ = dec.Decode(&v)
	var n int64
	switch t := v.(type) {
	case json.Number:
		if i, err := t.Int64(); err == nil {
			n = i
		} else if x, err := t.Float64(); err == nil && x == math.Trunc(x) && math.Abs(x) < 1<<63 {
			n = int64(x)
		} else {
			f.fail(path, "Input should be a valid integer, got a number with a fractional part")
			return 0
		}
	case string:
		i, err := strconv.ParseInt(strings.TrimSpace(t), 10, 64)
		if err != nil {
			f.fail(path, "Input should be a valid integer, unable to parse string as an integer")
			return 0
		}
		n = i
	default:
		f.fail(path, "Input should be a valid integer")
		return 0
	}
	switch {
	case n <= above:
		f.fail(path, fmt.Sprintf("Input should be greater than %d", above))
	case n > most:
		f.fail(path, fmt.Sprintf("Input should be less than or equal to %d", most))
	}
	return n
}

// uuid is the field name as pydantic reads a UUID, in its canonical spelling.
func (f *form) uuid(name string) string {
	raw, ok := f.member(name, required)
	if !ok {
		return ""
	}
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		f.fail([]string{name}, "UUID input should be a string, bytes or UUID object")
		return ""
	}
	id, valid := crud.PyUUID(*s)
	if !valid {
		f.fail([]string{name}, "Input should be a valid UUID")
	}
	return id
}

// datetime is the field name as pydantic reads a datetime: ISO 8601 (a date alone is its
// midnight; no offset is UTC) or a Unix time (milliseconds past 2e10).
func (f *form) datetime(name string, m mode) *time.Time {
	raw, ok := f.member(name, m)
	if !ok {
		return nil
	}
	var v any
	_ = json.Unmarshal(raw, &v)
	switch t := v.(type) {
	case string:
		if at, ok := crud.ParseTime(t); ok {
			return &at
		}
		if at, err := time.Parse("2006-01-02", t); err == nil {
			return &at
		}
	case float64:
		if math.Abs(t) > 2e10 {
			t /= 1000
		}
		sec := math.Floor(t)
		at := time.Unix(int64(sec), int64(math.Round((t-sec)*1e6))*1000).UTC()
		return &at
	}
	f.fail([]string{name}, "Input should be a valid datetime")
	return nil
}

// model is the nested model name; nil when there is nothing to read (an issue noted for a
// value that is no object). Its issues are f's; call done on it.
func (f *form) model(name string, m mode, class string, names ...string) *form {
	raw, ok := f.member(name, m)
	if !ok {
		return nil
	}
	n := &form{issues: f.issues, at: append(append([]string{}, f.at...), name), names: names}
	if !n.parse(raw) {
		f.fail([]string{name}, "Input should be a valid dictionary or instance of "+class)
		return nil
	}
	return n
}

// entry is a member of a dict field, in the body's order.
type entry struct{ key, value string }

// dict is the field name as dict[str, Literal]: keys least to most characters, at most
// most entries; nil when it is absent or null and may be.
func (f *form) dict(name string, m mode, most, keyLeast, keyMost int, choices []string) []entry {
	raw, ok := f.member(name, m)
	if !ok {
		return nil
	}
	d := &form{issues: f.issues}
	if !d.parse(raw) {
		f.fail([]string{name}, "Input should be a valid dictionary")
		return nil
	}
	if len(d.order) > most {
		f.fail([]string{name}, fmt.Sprintf(
			"Dictionary should have at most %d items after validation, not more", most))
		return nil
	}
	out := make([]entry, 0, len(d.order))
	for _, key := range d.order {
		if msg := lengthIssue(key, keyLeast, keyMost); msg != "" {
			f.fail([]string{name, key, "[key]"}, msg)
		}
		value := f.literalAt([]string{name, key}, d.fields[key], choices)
		out = append(out, entry{key, value})
	}
	return out
}

// strings is the field name as list[str] of at most most items, each least to itemMost
// characters; nil when it is absent or null and may be.
func (f *form) strings(name string, m mode, most, least, itemMost int) []string {
	raw, ok := f.member(name, m)
	if !ok {
		return nil
	}
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || items == nil {
		f.fail([]string{name}, "Input should be a valid list")
		return nil
	}
	if len(items) > most {
		f.fail([]string{name}, fmt.Sprintf(
			"List should have at most %d item%s after validation, not %d", most, plural(most), len(items)))
		return nil
	}
	out := make([]string, len(items))
	for i, item := range items {
		out[i] = f.str([]string{name, strconv.Itoa(i)}, item, least, itemMost, nil)
	}
	return out
}
