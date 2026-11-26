// Package httpx writes the answers ScadBuddy's routes give: compact JSON, and RFC 9457
// problems as backend/scadbuddy/core/problems.py writes them.
package httpx

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
)

// Field is one extension member of a problem, in the order it is written.
type Field struct {
	Key   string
	Value any
}

// Issue is one entry of a 422's errors. Clients read Loc; Msg is for people.
type Issue struct {
	Loc []string `json:"loc"`
	Msg string   `json:"msg"`
}

// Problem is a failure the client sees as a problem document.
type Problem struct {
	Status     int
	Detail     string
	Type       string
	Headers    map[string]string
	Extensions []Field
}

func (p *Problem) Error() string { return p.Detail }

// WithReason is a problem with the machine-readable reason a client branches on.
func WithReason(status int, detail, reason string) *Problem {
	return &Problem{Status: status, Detail: detail, Extensions: []Field{{"reason", reason}}}
}

// Invalid is the 422 of a request that does not match a route's shape.
func Invalid(issues ...Issue) *Problem {
	return &Problem{
		Status:     http.StatusUnprocessableEntity,
		Detail:     "the request did not match the expected shape",
		Extensions: []Field{{"errors", issues}},
	}
}

// titles are the statuses core/problems.py names; every other one is "Error".
var titles = map[int]string{
	400: "Bad Request",
	404: "Not Found",
	405: "Method Not Allowed",
	409: "Conflict",
	413: "Content Too Large",
	415: "Unsupported Media Type",
	422: "Unprocessable Content",
	499: "Client Closed Request",
	500: "Internal Server Error",
	503: "Service Unavailable",
}

// Title is the title problems.py gives status: its name, or "Error".
func Title(status int) string {
	if title := titles[status]; title != "" {
		return title
	}
	return "Error"
}

// Marshal is v as compact JSON: no HTML escapes, no trailing newline.
func Marshal(v any) ([]byte, error) {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(b.Bytes(), []byte("\n")), nil
}

// WriteJSON answers 200 with v.
func WriteJSON(w http.ResponseWriter, v any) error {
	return WriteJSONStatus(w, http.StatusOK, v)
}

// WriteJSONStatus answers status with v; a 204 has no body.
func WriteJSONStatus(w http.ResponseWriter, status int, v any) error {
	if status == http.StatusNoContent {
		w.WriteHeader(status)
		return nil
	}
	body, err := Marshal(v)
	if err != nil {
		return err
	}
	write(w, status, "application/json", body)
	return nil
}

// WriteBytes answers status with body as mediaType.
func WriteBytes(w http.ResponseWriter, status int, mediaType string, body []byte) {
	write(w, status, mediaType, body)
}

// WriteProblem answers p, its members in problems.py's order.
func WriteProblem(w http.ResponseWriter, r *http.Request, p *Problem) {
	kind := p.Type
	if kind == "" {
		kind = "about:blank"
	}
	title := Title(p.Status)
	fields := append([]Field{
		{"type", kind},
		{"title", title},
		{"status", p.Status},
		{"detail", p.Detail},
		{"instance", r.URL.Path},
	}, p.Extensions...)
	var b bytes.Buffer
	b.WriteByte('{')
	for i, f := range fields {
		if i > 0 {
			b.WriteByte(',')
		}
		key, err := Marshal(f.Key)
		if err == nil {
			var value []byte
			if value, err = Marshal(f.Value); err == nil {
				b.Write(key)
				b.WriteByte(':')
				b.Write(value)
				continue
			}
		}
		panic(errors.Join(errors.New("a problem that is not JSON"), err))
	}
	b.WriteByte('}')
	for k, v := range p.Headers {
		w.Header().Set(k, v)
	}
	write(w, p.Status, "application/problem+json", b.Bytes())
}

func write(w http.ResponseWriter, status int, mediaType string, body []byte) {
	w.Header().Set("Content-Type", mediaType)
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(status)
	_, _ = w.Write(body)
}
