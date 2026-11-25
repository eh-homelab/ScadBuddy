package mcprelay

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// body reads a request's JSON object as the routes' pydantic models do: every issue is
// collected, at its loc, and the request is answered 422 once with all of them.
type body struct {
	fields map[string]json.RawMessage
	issues []httpx.Issue
	at     []string
}

var hashPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// read is the request's body as an object whose members are all among names.
func read(r *http.Request, names ...string) (*body, error) {
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, err
	}
	if len(raw) == 0 {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body"}, Msg: "Field required"})
	}
	if !json.Valid(raw) {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body", "0"}, Msg: "JSON decode error"})
	}
	b := &body{at: []string{"body"}}
	if b.object(raw) {
		b.extra(names)
	}
	return b, nil
}

func (b *body) object(raw json.RawMessage) bool {
	if err := json.Unmarshal(raw, &b.fields); err != nil || b.fields == nil {
		b.fail(nil, "Input should be a valid dictionary or object to extract fields from")
		return false
	}
	return true
}

func (b *body) extra(names []string) {
	for name := range b.fields {
		known := false
		for _, n := range names {
			known = known || n == name
		}
		if !known {
			b.fail([]string{name}, "Extra inputs are not permitted")
		}
	}
}

func (b *body) fail(path []string, msg string) {
	loc := append(append([]string{}, b.at...), path...)
	b.issues = append(b.issues, httpx.Issue{Loc: loc, Msg: msg})
}

func (b *body) err() error {
	if len(b.issues) == 0 {
		return nil
	}
	return httpx.Invalid(b.issues...)
}

func (b *body) required(name string) (json.RawMessage, bool) {
	raw, ok := b.fields[name]
	if !ok && b.fields != nil {
		b.fail([]string{name}, "Field required")
	}
	return raw, ok && b.fields != nil
}

// given is an optional member: absent or null is not given.
func (b *body) given(name string) (json.RawMessage, bool) {
	raw, ok := b.fields[name]
	return raw, ok && string(raw) != "null"
}

func (b *body) str(name string, raw json.RawMessage) (string, bool) {
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		b.fail([]string{name}, "Input should be a valid string")
		return "", false
	}
	return *s, true
}

func (b *body) text(name string, least, most int) string {
	raw, ok := b.required(name)
	if !ok {
		return ""
	}
	s, ok := b.str(name, raw)
	if ok {
		b.length([]string{name}, s, least, most)
	}
	return s
}

func (b *body) optionalText(name string, most int) *string {
	raw, ok := b.given(name)
	if !ok {
		return nil
	}
	s, ok := b.str(name, raw)
	if !ok {
		return nil
	}
	b.length([]string{name}, s, 0, most)
	return &s
}

func (b *body) length(path []string, s string, least, most int) {
	switch n := utf8.RuneCountInString(s); {
	case n < least:
		b.fail(path, fmt.Sprintf("String should have at least %d character%s", least, plural(least)))
	case n > most:
		b.fail(path, fmt.Sprintf("String should have at most %d character%s", most, plural(most)))
	}
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

func (b *body) hash(name string) string {
	raw, ok := b.required(name)
	if !ok {
		return ""
	}
	s, ok := b.str(name, raw)
	if ok && !hashPattern.MatchString(s) {
		b.fail([]string{name}, fmt.Sprintf("String should match pattern '%s'", hashPattern))
	}
	return s
}

func (b *body) uuid(name string) string {
	raw, ok := b.required(name)
	if !ok {
		return ""
	}
	return b.uuidOf(name, raw)
}

func (b *body) optionalUUID(name string) *string {
	raw, ok := b.given(name)
	if !ok {
		return nil
	}
	id := b.uuidOf(name, raw)
	return &id
}

func (b *body) uuidOf(name string, raw json.RawMessage) string {
	s, ok := b.str(name, raw)
	if !ok {
		return ""
	}
	id, ok := canonicalUUID(s)
	if !ok {
		b.fail([]string{name}, "Input should be a valid UUID")
	}
	return id
}

// canonicalUUID is a UUID in the spellings pydantic takes (hyphens or none, braces, a urn
// prefix, either case), written lower-case with hyphens.
func canonicalUUID(s string) (string, bool) {
	if len(s) > 9 && strings.EqualFold(s[:9], "urn:uuid:") {
		s = s[9:]
	} else if strings.HasPrefix(s, "{") && strings.HasSuffix(s, "}") {
		s = s[1 : len(s)-1]
	}
	switch len(s) {
	case 36:
		for _, i := range []int{8, 13, 18, 23} {
			if s[i] != '-' {
				return "", false
			}
		}
		s = strings.ReplaceAll(s, "-", "")
	case 32:
	default:
		return "", false
	}
	raw, err := hex.DecodeString(s)
	if err != nil || len(raw) != 16 {
		return "", false
	}
	h := hex.EncodeToString(raw)
	return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:], true
}

// idleSeconds is a number of seconds, more than 0 and at most a week.
func (b *body) idleSeconds(name string) float64 {
	raw, ok := b.required(name)
	if !ok {
		return 0
	}
	var x *float64
	if json.Unmarshal(raw, &x) != nil || x == nil || math.IsInf(*x, 0) {
		b.fail([]string{name}, "Input should be a valid number")
		return 0
	}
	switch {
	case *x <= 0:
		b.fail([]string{name}, "Input should be greater than 0")
	case *x > idleSecondsMax:
		b.fail([]string{name}, fmt.Sprintf("Input should be less than or equal to %d", idleSecondsMax))
	}
	return *x
}

func (b *body) intIn(name string, least, most int64) int64 {
	raw, ok := b.required(name)
	if !ok {
		return 0
	}
	n, err := strconv.ParseInt(string(raw), 10, 64)
	if err != nil {
		b.fail([]string{name}, "Input should be a valid integer")
		return 0
	}
	switch {
	case n < least:
		b.fail([]string{name}, fmt.Sprintf("Input should be greater than or equal to %d", least))
	case n > most:
		b.fail([]string{name}, fmt.Sprintf("Input should be less than or equal to %d", most))
	}
	return n
}

func (b *body) optionalBool(name string) *bool {
	raw, ok := b.given(name)
	if !ok {
		return nil
	}
	var v *bool
	if json.Unmarshal(raw, &v) != nil || v == nil {
		b.fail([]string{name}, "Input should be a valid boolean")
		return nil
	}
	return v
}

// pairs is the answer's headers: at most headersMax [name, value] pairs.
func (b *body) pairs(name string) [][2]string {
	raw, ok := b.required(name)
	if !ok {
		return nil
	}
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || items == nil {
		b.fail([]string{name}, "Input should be a valid list")
		return nil
	}
	if len(items) > headersMax {
		b.fail([]string{name}, fmt.Sprintf("List should have at most %d items after validation", headersMax))
		return nil
	}
	out := make([][2]string, len(items))
	for i, item := range items {
		path := []string{name, strconv.Itoa(i)}
		var pair []json.RawMessage
		if json.Unmarshal(item, &pair) != nil || pair == nil {
			b.fail(path, "Input should be a valid tuple")
			continue
		}
		if len(pair) != 2 {
			b.fail(path, "Tuple should have 2 items")
			continue
		}
		for j, most := range []int{headerNameMax, headerValueMax} {
			at := append(append([]string{}, path...), strconv.Itoa(j))
			var s *string
			if json.Unmarshal(pair[j], &s) != nil || s == nil {
				b.fail(at, "Input should be a valid string")
				continue
			}
			b.length(at, *s, 0, most)
			out[i][j] = *s
		}
	}
	return out
}

// optionalObject is the member name as a JSON object's text; nil when absent or null.
func (b *body) optionalObject(name string) json.RawMessage {
	raw, ok := b.given(name)
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
