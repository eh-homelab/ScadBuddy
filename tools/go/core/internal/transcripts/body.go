package transcripts

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"unicode/utf8"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// form is a request's JSON object, read whole, as the routes' pydantic models read it (extra
// members forbidden). Its reads collect issues; err is the 422 of them all.
type form struct {
	fields map[string]json.RawMessage
	issues []httpx.Issue
	at     []string
}

func readForm(r *http.Request, names ...string) (*form, error) {
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
	f := &form{at: []string{"body"}}
	f.object(raw, names)
	return f, nil
}

func (f *form) object(raw json.RawMessage, names []string) {
	if json.Unmarshal(raw, &f.fields) != nil || f.fields == nil {
		f.fail("Input should be a valid dictionary or object to extract fields from")
		return
	}
	for name := range f.fields {
		known := false
		for _, n := range names {
			known = known || n == name
		}
		if !known {
			f.fail("Extra inputs are not permitted", name)
		}
	}
}

func (f *form) fail(msg string, path ...string) {
	loc := append(append([]string{}, f.at...), path...)
	f.issues = append(f.issues, httpx.Issue{Loc: loc, Msg: msg})
}

func (f *form) err() error {
	if len(f.issues) == 0 {
		return nil
	}
	return httpx.Invalid(f.issues...)
}

// field is the member name; present is false (and an issue noted) when it is required and
// absent.
func (f *form) field(name string) (json.RawMessage, bool) {
	if f.fields == nil {
		return nil, false
	}
	raw, ok := f.fields[name]
	if !ok {
		f.fail("Field required", name)
	}
	return raw, ok
}

// text is a required string of at most most characters; nullable takes null as nil.
func (f *form) text(name string, least, most int, nullable bool, pattern *regexp.Regexp) *string {
	raw, ok := f.field(name)
	if !ok {
		return nil
	}
	if nullable && string(raw) == "null" {
		return nil
	}
	return f.textAt([]string{name}, raw, least, most, pattern)
}

func (f *form) textAt(path []string, raw json.RawMessage, least, most int, pattern *regexp.Regexp) *string {
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		f.fail("Input should be a valid string", path...)
		return nil
	}
	n := utf8.RuneCountInString(*s)
	switch {
	case n < least:
		f.fail(fmt.Sprintf("String should have at least %d character%s", least, plural(least)), path...)
	case n > most:
		f.fail(fmt.Sprintf("String should have at most %d character%s", most, plural(most)), path...)
	case pattern != nil && !pattern.MatchString(*s):
		f.fail(fmt.Sprintf("String should match pattern '%s'", pattern), path...)
	}
	return s
}

// texts is a required list of least to most strings, each at most chars characters.
func (f *form) texts(name string, least, most, chars int) []string {
	raw, ok := f.field(name)
	if !ok {
		return nil
	}
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || items == nil {
		f.fail("Input should be a valid list", name)
		return nil
	}
	if len(items) < least {
		f.fail(fmt.Sprintf("List should have at least %d item after validation", least), name)
	}
	if len(items) > most {
		f.fail(fmt.Sprintf("List should have at most %d items after validation", most), name)
		return nil
	}
	out := make([]string, len(items))
	for i, item := range items {
		if s := f.textAt([]string{name, strconv.Itoa(i)}, item, 0, chars, nil); s != nil {
			out[i] = *s
		}
	}
	return out
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}
