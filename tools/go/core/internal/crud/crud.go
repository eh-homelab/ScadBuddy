// Package crud guards and answers the routes under /api/v1/internal/crud/<caller>/, as
// backend/scadbuddy/crud/guard.py does: the caller's own live service credential in every
// auth mode, the CRUD headers on every answer of a matched route, and a database that is out
// of reach as a 503 a client can tell from a route's own 500. A shared area's routes, under
// /api/v1/internal/crud/<area>/ (AddShared, #7958), take any caller whose scopes hold
// crud.<area>.
package crud

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
)

// Prefix is the CRUD routes' path up to the caller.
const Prefix = "/api/v1/internal/crud/"

const problems = "https://scadbuddy.dev/problems/auth/"

// Handler answers a guarded request: a value to write as JSON, or a Problem, or another error.
type Handler func(r *http.Request, p Params) (any, error)

// Route is one method and path of an area. A {name} segment is a path parameter.
type Route struct {
	Method string
	// Path is what follows /api/v1/internal/crud/<caller>.
	Path   string
	Handle Handler
	// MaxBody is the route's body bound, 0 for a route with none.
	MaxBody int64
	// Status is the status of an answer that is no problem, 0 for 200.
	Status int
}

// Params are a matched path's parameters.
type Params map[string]string

// Resolver finds the service a request's Authorization header names, nil for none.
type Resolver interface {
	Resolve(ctx context.Context, authorization string) *auth.Service
}

// Router answers the routes added to it, and gives every other request to Next unchanged.
type Router struct {
	Resolver Resolver
	// Enforce is SCADBUDDY_AUTH_MODE=enforce, whose refusals auth/check.py words.
	Enforce bool
	Next    http.Handler

	routes []mounted
}

type mounted struct {
	Route
	caller   string
	shared   bool
	segments []string
	// template is the route as Starlette writes it ({name...} is {name:path}): its spans'
	// and metrics' http.route, as the Python route's.
	template string
}

// rest is a trailing {name...} segment.
var rest = regexp.MustCompile(`\{([A-Za-z_][A-Za-z0-9_]*)\.\.\.\}`)

// Add serves an area's routes for one caller.
func (rt *Router) Add(caller string, routes ...Route) {
	for _, r := range routes {
		segments := strings.Split(strings.TrimPrefix(Prefix, "/")+caller+r.Path, "/")
		template := rest.ReplaceAllString(Prefix+caller+r.Path, "{$1:path}")
		rt.routes = append(rt.routes, mounted{r, caller, false, segments, template})
	}
}

// AddShared serves a shared area's routes, under /api/v1/internal/crud/<area>: any caller
// whose credential's scopes hold crud.<area> may call them. An area's name is never a caller's.
func (rt *Router) AddShared(area string, routes ...Route) {
	for _, r := range routes {
		segments := strings.Split(strings.TrimPrefix(Prefix, "/")+area+r.Path, "/")
		template := rest.ReplaceAllString(Prefix+area+r.Path, "{$1:path}")
		rt.routes = append(rt.routes, mounted{r, area, true, segments, template})
	}
}

func (rt *Router) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	route, params := rt.match(r)
	if route == nil {
		rt.Next.ServeHTTP(w, r)
		return
	}
	telemetry.SetRoute(r.Context(), route.template)
	// The body gate stands outside the routes, so its refusal has no CRUD headers.
	if route.MaxBody > 0 && r.ContentLength > route.MaxBody {
		httpx.WriteProblem(w, r, &httpx.Problem{
			Status: http.StatusRequestEntityTooLarge,
			Detail: fmt.Sprintf("a body is at most %d bytes", route.MaxBody),
		})
		return
	}
	w.Header().Set("X-ScadBuddy-Crud", "1")
	w.Header().Set("Cache-Control", "no-store")
	answer, err := rt.answer(route, r, params)
	if err == nil {
		status := route.Status
		if status == 0 {
			status = http.StatusOK
		}
		err = writeAnswer(w, status, answer)
	}
	if err != nil {
		httpx.WriteProblem(w, r, problemOf(r, err))
	}
}

// ServiceOf is the verified service of a guarded request, nil outside a route's handler.
func ServiceOf(r *http.Request) *auth.Service {
	return auth.ServiceOf(r.Context())
}

func (rt *Router) answer(route *mounted, r *http.Request, params Params) (any, error) {
	service, p := rt.refusal(route, r)
	if p != nil {
		return nil, p
	}
	r = r.WithContext(auth.WithService(r.Context(), service))
	if route.MaxBody > 0 {
		if r.ContentLength < 0 {
			return nil, &httpx.Problem{
				Status: http.StatusLengthRequired, Detail: "a body names its Content-Length",
			}
		}
		r.Body = http.MaxBytesReader(nil, r.Body, route.MaxBody)
	}
	return route.Handle(r, params)
}

// match is the route of a request, nil when none. A path with an escape in it is left to
// Next, which decodes it as Starlette does.
func (rt *Router) match(r *http.Request) (*mounted, Params) {
	if strings.Contains(r.URL.EscapedPath(), "%") {
		return nil, nil
	}
	path := strings.Split(strings.TrimPrefix(r.URL.Path, "/"), "/")
next:
	for i := range rt.routes {
		route := &rt.routes[i]
		last := len(route.segments) - 1
		rest := strings.HasSuffix(route.segments[last], "...}")
		if route.Method != r.Method || len(route.segments) != len(path) &&
			!(rest && len(path) > len(route.segments)) {
			continue
		}
		params := Params{}
		for j, want := range route.segments {
			if j == last && rest {
				// A trailing {name...} takes the rest of the path, slashes and all.
				value := strings.Join(path[j:], "/")
				if value == "" {
					continue next
				}
				params[strings.Trim(want, "{.}")] = value
			} else if strings.HasPrefix(want, "{") {
				if path[j] == "" {
					continue next
				}
				params[strings.Trim(want, "{}")] = path[j]
			} else if want != path[j] {
				continue next
			}
		}
		return route, params
	}
	return nil, nil
}

func (rt *Router) refusal(route *mounted, r *http.Request) (*auth.Service, *httpx.Problem) {
	caller := route.caller
	service := rt.Resolver.Resolve(r.Context(), r.Header.Get("Authorization"))
	if service == nil {
		detail := "This route needs a service credential."
		if rt.Enforce {
			detail = "This route needs a service credential with its scope."
		}
		p := httpx.WithReason(http.StatusUnauthorized, detail, "no_credential")
		p.Type = problems + "signed-out"
		p.Headers = map[string]string{"WWW-Authenticate": `Bearer realm="scadbuddy"`}
		return nil, p
	}
	var p *httpx.Problem
	switch {
	case !route.shared && service.Caller != caller:
		p = httpx.WithReason(http.StatusForbidden,
			"These routes belong to another service.", "wrong_caller")
	case !service.Has("crud." + caller):
		detail := "This credential's scope does not cover this route."
		if rt.Enforce {
			detail = "This credential's scope does not cover this request."
		}
		p = httpx.WithReason(http.StatusForbidden, detail, "out_of_scope")
	default:
		return service, nil
	}
	p.Type = problems + "forbidden"
	return nil, p
}

// problemOf is the answer to a handler's error.
func problemOf(r *http.Request, err error) *httpx.Problem {
	var p *httpx.Problem
	var tooLarge *http.MaxBytesError
	switch {
	case errors.As(err, &p):
		return p
	case errors.As(err, &tooLarge):
		return &httpx.Problem{
			Status: http.StatusRequestEntityTooLarge,
			Detail: fmt.Sprintf("a body is at most %d bytes", tooLarge.Limit),
		}
	case databasePasses(err):
		slog.WarnContext(r.Context(), "database unavailable", "path", r.URL.Path)
		return httpx.WithReason(http.StatusServiceUnavailable,
			"The database is out of reach.", "database-unavailable")
	default:
		slog.ErrorContext(r.Context(), "unhandled error", "path", r.URL.Path, "error", err.Error())
		return &httpx.Problem{Status: http.StatusInternalServerError, Detail: "InternalError"}
	}
}

// databasePasses says whether err is a state of the database that passes: psycopg's
// OperationalError. An error with no SQLSTATE came from the connection or the pool.
func databasePasses(err error) bool {
	var pg *pgconn.PgError
	if errors.As(err, &pg) && len(pg.Code) == 5 {
		switch pg.Code[:2] {
		case "08", "40", "53", "54", "55", "57", "58":
			return true
		}
		return false
	}
	var connect *pgconn.ConnectError
	return errors.As(err, &connect) || errors.Is(err, context.DeadlineExceeded) ||
		pgconn.Timeout(err) || errors.Is(err, io.ErrUnexpectedEOF)
}

// uuidPattern is every CRUD route's UUID: the lower-case spelling only.
var uuidPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// IsUUID says whether s is a UUID as the CRUD routes spell one.
func IsUUID(s string) bool { return uuidPattern.MatchString(s) }

// UUID is the path parameter name, or the 422 of one that is no UUID.
func (p Params) UUID(name string) (string, error) {
	if id := p[name]; IsUUID(id) {
		return id, nil
	}
	return "", httpx.Invalid(httpx.Issue{
		Loc: []string{"path", name},
		Msg: fmt.Sprintf("String should match pattern '%s'", uuidPattern),
	})
}

// QueryInt is the query parameter name as an integer within [least, most]; given is false,
// and the value 0, when the request does not name it. As Starlette, the last one counts.
func QueryInt(r *http.Request, name string, least, most int64) (value int64, given bool, err error) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		return 0, false, nil
	}
	value, parse := strconv.ParseInt(strings.TrimSpace(values[len(values)-1]), 10, 64)
	msg := ""
	switch {
	case parse != nil:
		msg = "Input should be a valid integer, unable to parse string as an integer"
	case value < least:
		msg = fmt.Sprintf("Input should be greater than or equal to %d", least)
	case value > most:
		msg = fmt.Sprintf("Input should be less than or equal to %d", most)
	default:
		return value, true, nil
	}
	return 0, true, httpx.Invalid(httpx.Issue{Loc: []string{"query", name}, Msg: msg})
}

// QueryText is the required query parameter name, 1 to most characters.
func QueryText(r *http.Request, name string, most int) (string, error) {
	value, given := query(r, name)
	issue := httpx.Issue{Loc: []string{"query", name}, Msg: "Field required"}
	if given {
		issue.Msg = lengthIssue(value, 1, most)
		if issue.Msg == "" {
			return value, nil
		}
	}
	return "", httpx.Invalid(issue)
}

// Body is a request's JSON object, read whole.
type Body struct {
	fields map[string]json.RawMessage
	issues []httpx.Issue
	at     []string
	parent *Body
}

// ReadBody is the request's body as an object whose members are all among names.
func ReadBody(r *http.Request, names ...string) (*Body, error) {
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, err
	}
	return ParseBody(raw, names...)
}

// ParseBody is raw as an object whose members are all among names.
func ParseBody(raw []byte, names ...string) (*Body, error) {
	if len(raw) == 0 {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body"}, Msg: "Field required"})
	}
	if !json.Valid(raw) {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body", "0"}, Msg: "JSON decode error"})
	}
	b := &Body{at: []string{"body"}}
	b.object(raw, names)
	return b, nil
}

func (b *Body) object(raw json.RawMessage, names []string) {
	if err := json.Unmarshal(raw, &b.fields); err != nil || b.fields == nil {
		b.fail(nil, "Input should be a valid dictionary or object to extract fields from")
		return
	}
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

func (b *Body) fail(path []string, msg string) {
	loc := append(append([]string{}, b.at...), path...)
	b.issues = append(b.issues, httpx.Issue{Loc: loc, Msg: msg})
}

// Err is the 422 of what the body's reads found wrong, nil when nothing.
func (b *Body) Err() error {
	if len(b.issues) == 0 {
		return nil
	}
	return httpx.Invalid(b.issues...)
}

func (b *Body) required(name string) (json.RawMessage, bool) {
	raw, ok := b.fields[name]
	if !ok && b.fields != nil {
		b.fail([]string{name}, "Field required")
	}
	return raw, ok
}

// Text is the required string name, least to most characters.
func (b *Body) Text(name string, least, most int) string {
	raw, ok := b.required(name)
	if !ok {
		return ""
	}
	return b.text([]string{name}, raw, least, most)
}

// UUID is the required string name, a UUID.
func (b *Body) UUID(name string) string {
	raw, ok := b.required(name)
	if !ok {
		return ""
	}
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		b.fail([]string{name}, "Input should be a valid string")
		return ""
	}
	if !IsUUID(*s) {
		b.fail([]string{name}, fmt.Sprintf("String should match pattern '%s'", uuidPattern))
	}
	return *s
}

// Int is the required integer name, at least least.
func (b *Body) Int(name string, least int64) int64 {
	raw, ok := b.required(name)
	if !ok {
		return 0
	}
	var n *int64
	if json.Unmarshal(raw, &n) != nil || n == nil {
		b.fail([]string{name}, "Input should be a valid integer")
		return 0
	}
	if *n < least {
		b.fail([]string{name}, fmt.Sprintf("Input should be greater than or equal to %d", least))
	}
	return *n
}

// Texts is the required list name of least to most strings, each 2 to chars characters.
func (b *Body) Texts(name string, least, most, chars int) []string {
	raw, ok := b.required(name)
	if !ok {
		return nil
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
	texts := make([]string, len(items))
	for i, item := range items {
		texts[i] = b.text([]string{name, strconv.Itoa(i)}, item, 2, chars)
	}
	return texts
}

// Object is the optional object name, nil when it is absent or null. Its issues are b's.
func (b *Body) Object(name string, names ...string) *Nested {
	raw, ok := b.fields[name]
	if !ok || string(raw) == "null" {
		return nil
	}
	n := &Nested{Body: Body{at: append(append([]string{}, b.at...), name)}, parent: b}
	n.object(raw, names)
	return n
}

// Need is the required object name. A null or absent one is an issue, and nil.
func (b *Body) Need(name string, names ...string) *Nested {
	raw, ok := b.required(name)
	if !ok {
		return nil
	}
	if string(raw) == "null" {
		b.fail([]string{name}, "Input should be a valid dictionary or object to extract fields from")
		return nil
	}
	return b.Object(name, names...)
}

// Null says whether name is absent or null.
func (b *Body) Null(name string) bool {
	raw, ok := b.fields[name]
	return !ok || string(raw) == "null"
}

// TextOr is the string name, least to most characters, or fallback when it is absent. A null
// one is an issue.
func (b *Body) TextOr(name, fallback string, least, most int) string {
	raw, ok := b.fields[name]
	if !ok {
		return fallback
	}
	return b.text([]string{name}, raw, least, most)
}

// Pattern is the issue of s that does not match pattern, written as pydantic does.
func (b *Body) Pattern(name string, s string, pattern *regexp.Regexp) {
	if !pattern.MatchString(s) {
		b.fail([]string{name}, fmt.Sprintf("String should match pattern '%s'", pattern))
	}
}

// IntRange is the required integer name within [least, most]; a float with no fraction counts.
func (b *Body) IntRange(name string, least, most int64) int64 {
	raw, ok := b.required(name)
	if !ok {
		return 0
	}
	return b.intIn([]string{name}, raw, least, most)
}

// IntOr is the integer name within [least, most], or fallback when it is absent.
func (b *Body) IntOr(name string, fallback, least, most int64) int64 {
	raw, ok := b.fields[name]
	if !ok {
		return fallback
	}
	return b.intIn([]string{name}, raw, least, most)
}

// OptIntRange is the optional integer name within [least, most]; nil when absent or null.
func (b *Body) OptIntRange(name string, least, most int64) *int64 {
	if b.Null(name) {
		return nil
	}
	v := b.intIn([]string{name}, b.fields[name], least, most)
	return &v
}

func (b *Body) intIn(path []string, raw json.RawMessage, least, most int64) int64 {
	var n *int64
	if json.Unmarshal(raw, &n) != nil || n == nil {
		var f *float64
		// pydantic takes a float with no fraction as an integer.
		if json.Unmarshal(raw, &f) != nil || f == nil || *f != float64(int64(*f)) {
			b.fail(path, "Input should be a valid integer")
			return 0
		}
		v := int64(*f)
		n = &v
	}
	switch {
	case *n < least:
		b.fail(path, fmt.Sprintf("Input should be greater than or equal to %d", least))
	case *n > most:
		b.fail(path, fmt.Sprintf("Input should be less than or equal to %d", most))
	}
	return *n
}

// OptFloat is the optional finite number name within [least, most]; nil when absent or null.
func (b *Body) OptFloat(name string, least, most float64) *float64 {
	if b.Null(name) {
		return nil
	}
	var f *float64
	if json.Unmarshal(b.fields[name], &f) != nil || f == nil {
		b.fail([]string{name}, "Input should be a valid number")
		return nil
	}
	if *f < least || *f > most {
		b.fail([]string{name}, fmt.Sprintf("Input should be between %g and %g", least, most))
	}
	return f
}

// List is the list name's items: required unless optional, nil when absent or null and
// optional. At most most items.
func (b *Body) List(name string, optional bool, most int) []json.RawMessage {
	raw, ok := b.fields[name]
	if !ok || (optional && string(raw) == "null") {
		if !optional {
			b.required(name)
		}
		return nil
	}
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || items == nil {
		b.fail([]string{name}, "Input should be a valid list")
		return nil
	}
	if len(items) > most {
		b.fail([]string{name}, fmt.Sprintf("List should have at most %d items after validation", most))
		return nil
	}
	return items
}

// TextItems is List's items as strings of least to most characters.
func (b *Body) TextItems(name string, items []json.RawMessage, least, most int) []string {
	texts := make([]string, len(items))
	for i, item := range items {
		texts[i] = b.text([]string{name, strconv.Itoa(i)}, item, least, most)
	}
	return texts
}

// ChoiceItems is List's items as strings, each one of values.
func (b *Body) ChoiceItems(name string, items []json.RawMessage, values ...string) []string {
	texts := make([]string, len(items))
	for i, item := range items {
		texts[i] = b.choice([]string{name, strconv.Itoa(i)}, item, values)
	}
	return texts
}

// Datetime is the date and time name as Postgres reads it (`::timestamptz`); required
// unless optional, nil when absent or null and optional. pydantic's forms: an ISO 8601 text,
// or a Unix time in seconds (milliseconds past 2e10).
func (b *Body) Datetime(name string, optional bool) *string {
	raw, ok := b.fields[name]
	if !ok || string(raw) == "null" {
		if !optional {
			if !ok {
				b.required(name)
			} else {
				b.fail([]string{name}, "Input should be a valid datetime")
			}
		}
		return nil
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		if datetimePattern.MatchString(s) {
			return &s
		}
	} else {
		var f float64
		if json.Unmarshal(raw, &f) == nil {
			if f > 2e10 || f < -2e10 {
				f /= 1000
			}
			sec := int64(f)
			t := time.Unix(sec, int64((f-float64(sec))*1e9)).UTC().Format(time.RFC3339Nano)
			return &t
		}
	}
	b.fail([]string{name}, "Input should be a valid datetime")
	return nil
}

var datetimePattern = regexp.MustCompile(
	`^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|z|[+-]\d{2}(:?\d{2})?)?$`)

// Nested is an object within a body. Done gives its issues to the body.
type Nested struct {
	Body
	parent *Body
}

// Done gives the object's issues to the body it is in.
func (n *Nested) Done() { n.parent.issues = append(n.parent.issues, n.issues...) }

func (b *Body) text(path []string, raw json.RawMessage, least, most int) string {
	var s *string
	if json.Unmarshal(raw, &s) != nil || s == nil {
		b.fail(path, "Input should be a valid string")
		return ""
	}
	if msg := lengthIssue(*s, least, most); msg != "" {
		b.fail(path, msg)
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
