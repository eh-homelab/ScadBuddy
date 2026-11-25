package crud

import (
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// Reply is an answer whose status the handler decides, or whose body is not JSON.
type Reply struct {
	Status int
	// Value is written as JSON when MediaType is empty.
	Value     any
	MediaType string
	Body      []byte
}

// writeAnswer writes a handler's answer: a *Reply as it says, else status with the value.
func writeAnswer(w http.ResponseWriter, status int, answer any) error {
	reply, ok := answer.(*Reply)
	if !ok {
		return httpx.WriteJSONStatus(w, status, answer)
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
	values := r.URL.Query()[name]
	issue := httpx.Issue{Loc: []string{"query", name}, Msg: "Field required"}
	if len(values) == 0 {
		if optional {
			return nil, nil
		}
		return nil, httpx.Invalid(issue)
	}
	value := values[len(values)-1]
	if IsUUID(value) {
		return &value, nil
	}
	issue.Msg = fmt.Sprintf("String should match pattern '%s'", uuidPattern)
	return nil, httpx.Invalid(issue)
}

// QueryOptText is the query parameter name, least to most characters; nil when the
// request does not name it.
func QueryOptText(r *http.Request, name string, least, most int) (*string, error) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		return nil, nil
	}
	value := values[len(values)-1]
	if msg := lengthIssue(value, least, most); msg != "" {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", name}, Msg: msg})
	}
	return &value, nil
}

// QueryChoice is the required query parameter name, one of choices.
func QueryChoice(r *http.Request, name string, choices ...string) (string, error) {
	values := r.URL.Query()[name]
	issue := httpx.Issue{Loc: []string{"query", name}, Msg: "Field required"}
	if len(values) > 0 {
		value := values[len(values)-1]
		for _, c := range choices {
			if c == value {
				return c, nil
			}
		}
		issue.Msg = "Input should be one of " + strings.Join(choices, ", ")
	}
	return "", httpx.Invalid(issue)
}

// QueryBool is the query parameter name as pydantic reads a boolean; false when the request
// does not name it.
func QueryBool(r *http.Request, name string) (bool, error) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		return false, nil
	}
	switch strings.ToLower(values[len(values)-1]) {
	case "1", "on", "t", "true", "y", "yes":
		return true, nil
	case "0", "off", "f", "false", "n", "no":
		return false, nil
	}
	return false, httpx.Invalid(httpx.Issue{
		Loc: []string{"query", name}, Msg: "Input should be a valid boolean, unable to interpret input",
	})
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
