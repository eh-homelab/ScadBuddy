package turns

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// turnIDPattern is a turn id: a UUID (durable_turn_id), any case, as Postgres reads one.
var turnIDPattern = regexp.MustCompile(
	`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// form reads a request body as the Python route's pydantic models do: members by name, extras
// refused, each issue at its loc.
type form struct{ issues []httpx.Issue }

// fields is an object's members, read by name.
type fields struct {
	f   *form
	loc []string
	m   map[string]json.RawMessage
}

func readForm(r *http.Request, names ...string) (*form, *fields, error) {
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, nil, err
	}
	if len(raw) == 0 {
		return nil, nil, httpx.Invalid(httpx.Issue{Loc: []string{"body"}, Msg: "Field required"})
	}
	if !json.Valid(raw) {
		return nil, nil, httpx.Invalid(httpx.Issue{Loc: []string{"body", "0"}, Msg: "JSON decode error"})
	}
	f := &form{}
	return f, f.object([]string{"body"}, raw, names), nil
}

func (f *form) fail(loc []string, msg string) {
	f.issues = append(f.issues, httpx.Issue{Loc: append([]string{}, loc...), Msg: msg})
}

func (f *form) err() error {
	if len(f.issues) == 0 {
		return nil
	}
	return httpx.Invalid(f.issues...)
}

func at(loc []string, more ...string) []string { return append(append([]string{}, loc...), more...) }

// object is raw's members when it is an object whose members are all among names, else nil.
func (f *form) object(loc []string, raw json.RawMessage, names []string) *fields {
	var m map[string]json.RawMessage
	if json.Unmarshal(raw, &m) != nil || m == nil {
		f.fail(loc, "Input should be a valid dictionary or object to extract fields from")
		return nil
	}
	for name := range m {
		known := false
		for _, n := range names {
			known = known || n == name
		}
		if !known {
			f.fail(at(loc, name), "Extra inputs are not permitted")
		}
	}
	return &fields{f, loc, m}
}

// get is the member name; ok is false, with an issue, when a required one is absent. A nil
// fields (an object that failed) has no members and no further issues.
func (o *fields) get(name string) (json.RawMessage, bool) {
	if o == nil {
		return nil, false
	}
	raw, ok := o.m[name]
	if !ok {
		o.f.fail(at(o.loc, name), "Field required")
	}
	return raw, ok
}

// optional is the member name, nil when it is absent or null.
func (o *fields) optional(name string) json.RawMessage {
	if o == nil {
		return nil
	}
	raw, ok := o.m[name]
	if !ok || string(raw) == "null" {
		return nil
	}
	return raw
}

func (f *form) text(loc []string, raw json.RawMessage, least, most int) string {
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		f.fail(loc, "Input should be a valid string")
		return ""
	}
	n := len([]rune(*s))
	switch {
	case n < least:
		f.fail(loc, fmt.Sprintf("String should have at least %d character%s", least, plural(least)))
	case n > most:
		f.fail(loc, fmt.Sprintf("String should have at most %d character%s", most, plural(most)))
	}
	return *s
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

func (o *fields) text(name string, least, most int) string {
	raw, ok := o.get(name)
	if !ok {
		return ""
	}
	return o.f.text(at(o.loc, name), raw, least, most)
}

// literal is the string name, one of values.
func (o *fields) literal(name string, values ...string) string {
	raw, ok := o.get(name)
	if !ok {
		return ""
	}
	return o.f.literal(at(o.loc, name), raw, values)
}

func (f *form) literal(loc []string, raw json.RawMessage, values []string) string {
	var s *string
	if json.Unmarshal(raw, &s) == nil && s != nil {
		for _, v := range values {
			if v == *s {
				return v
			}
		}
	}
	f.fail(loc, "Input should be "+strings.Join(quoted(values), ", "))
	return ""
}

func quoted(values []string) []string {
	out := make([]string, len(values))
	for i, v := range values {
		out[i] = "'" + v + "'"
	}
	return out
}

func (o *fields) boolean(name string) bool {
	raw, ok := o.get(name)
	if !ok {
		return false
	}
	var b *bool
	if json.Unmarshal(raw, &b) != nil || b == nil {
		o.f.fail(at(o.loc, name), "Input should be a valid boolean")
		return false
	}
	return *b
}

// turnID is a turn id, lowercase as Postgres writes a uuid.
func (f *form) turnID(loc []string, raw json.RawMessage) string {
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		f.fail(loc, "Input should be a valid string")
		return ""
	}
	if !turnIDPattern.MatchString(*s) {
		f.fail(loc, fmt.Sprintf("String should match pattern '%s'", turnIDPattern))
		return ""
	}
	return strings.ToLower(*s)
}

func (o *fields) turnID(name string) string {
	raw, ok := o.get(name)
	if !ok {
		return ""
	}
	return o.f.turnID(at(o.loc, name), raw)
}

func (o *fields) number(name string, least, most float64) float64 {
	raw, ok := o.get(name)
	if !ok {
		return 0
	}
	var n *float64
	if json.Unmarshal(raw, &n) != nil || n == nil {
		o.f.fail(at(o.loc, name), "Input should be a valid number")
		return 0
	}
	switch {
	case *n < least:
		o.f.fail(at(o.loc, name), fmt.Sprintf("Input should be greater than or equal to %v", least))
	case *n > most:
		o.f.fail(at(o.loc, name), fmt.Sprintf("Input should be less than or equal to %v", most))
	}
	return *n
}

func (o *fields) integer(name string, least, most int64) int64 {
	raw, ok := o.get(name)
	if !ok {
		return 0
	}
	var n *int64
	if json.Unmarshal(raw, &n) != nil || n == nil {
		o.f.fail(at(o.loc, name), "Input should be a valid integer")
		return 0
	}
	switch {
	case *n < least:
		o.f.fail(at(o.loc, name), fmt.Sprintf("Input should be greater than or equal to %d", least))
	case *n > most:
		o.f.fail(at(o.loc, name), fmt.Sprintf("Input should be less than or equal to %d", most))
	}
	return *n
}

// list is the list at loc, least to most items; nil when it is no list or too long.
func (f *form) list(loc []string, raw json.RawMessage, least, most int) []json.RawMessage {
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || items == nil {
		f.fail(loc, "Input should be a valid list")
		return nil
	}
	if len(items) < least {
		f.fail(loc, fmt.Sprintf("List should have at least %d item%s after validation", least, plural(least)))
	}
	if len(items) > most {
		f.fail(loc, fmt.Sprintf("List should have at most %d item%s after validation", most, plural(most)))
		return nil
	}
	return items
}

// event is an event's JSON text: a JSON object with a string type, which the log's reads
// parse as jsonb.
func (f *form) event(loc []string, raw json.RawMessage) (string, bool) {
	before := len(f.issues)
	text := f.text(loc, raw, 2, eventCharsMax)
	if len(f.issues) > before {
		return "", false
	}
	if !json.Valid([]byte(text)) {
		f.fail(loc, "Value error, an event is not JSON")
		return "", false
	}
	var m map[string]json.RawMessage
	var kind *string
	if json.Unmarshal([]byte(text), &m) != nil || m == nil ||
		json.Unmarshal(m["type"], &kind) != nil || kind == nil {
		f.fail(loc, "Value error, an event is an object with a type")
		return "", false
	}
	return text, true
}

// events is a list of least to most event texts; ok is false when any failed.
func (f *form) events(loc []string, raw json.RawMessage, least, most int) ([]string, bool) {
	before := len(f.issues)
	items := f.list(loc, raw, least, most)
	texts := make([]string, len(items))
	for i, item := range items {
		texts[i], _ = f.event(at(loc, strconv.Itoa(i)), item)
	}
	return texts, len(f.issues) == before
}

// closing is a turn's closing events, the last one its session.status, idle or failed.
func (o *fields) closing(name string) []string {
	raw, ok := o.get(name)
	if !ok {
		return nil
	}
	loc := at(o.loc, name)
	texts, ok := o.f.events(loc, raw, 1, tailMax)
	if !ok {
		return nil
	}
	last, _ := parse(texts[len(texts)-1])
	kind, _ := last.(*object).get("type").(string)
	status, _ := last.(*object).get("status").(string)
	if kind != "session.status" || (status != "idle" && status != "failed") {
		o.f.fail(loc, "Value error, a turn's closing events end with its session.status, idle or failed")
	}
	return texts
}

// tiers is a turn's tiers, absent when the member is absent or null.
func (o *fields) tiers(name string) (tiers []string, given bool) {
	raw := o.optional(name)
	if raw == nil {
		return nil, false
	}
	loc := at(o.loc, name)
	items := o.f.list(loc, raw, 0, tiersMax)
	tiers = make([]string, len(items))
	for i, item := range items {
		tiers[i] = o.f.text(at(loc, strconv.Itoa(i)), item, 1, tierMax)
	}
	return tiers, true
}

type author struct{ kind, id string }

var ownerKinds = []string{"browser", "bearer", "oidc", "anonymous", "flow"}

func (o *fields) author(name string) author {
	raw, ok := o.get(name)
	if !ok {
		return author{}
	}
	a := o.f.object(at(o.loc, name), raw, []string{"kind", "id"})
	if a == nil {
		return author{}
	}
	return author{a.literal("kind", ownerKinds...), a.text("id", 1, nameMax)}
}

// next is the queue's head: whose it is, how it runs, and its user.turn and status events.
type next struct {
	turnID  string
	author  author
	tiers   []string
	tiered  bool
	bare    bool
	started []string
}

func (f *form) next(loc []string, raw json.RawMessage) *next {
	o := f.object(loc, raw, []string{"turn_id", "author", "tiers", "bare", "started"})
	if o == nil {
		return nil
	}
	n := &next{turnID: o.turnID("turn_id"), author: o.author("author")}
	n.tiers, n.tiered = o.tiers("tiers")
	n.bare = o.boolean("bare")
	if raw, ok := o.get("started"); ok {
		n.started, _ = f.events(at(loc, "started"), raw, 1, tailMax)
	}
	return n
}
