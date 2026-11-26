// Package problem is the inventory's refusal: an answer, not a failure (ruling C4). It is a
// package of its own so the handlers, which the store's saves import, can refuse too.
package problem

import "github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"

// Base is inventory/problems.py PROBLEMS: a refusal's type is it and its code.
const Base = "https://scadbuddy.dev/problems/inventory/"

// Refused is a write's refusal. store.Write records it as a problem whose type is Base plus
// Code, so a retry gets the same refusal and Temporal never retries a 4xx. What the write
// did before it refused does not commit.
type Refused struct {
	Status   int
	Code     string
	Detail   string
	Problems any
	// Type replaces the problem's type, for a code another area owns (bad_property_schema
	// keeps the entities' type, inventory/problems.py).
	Type string
	// Fields are the problem's other members (step, handler, missing, class), as
	// core/problems.py ApiError writes its extensions.
	Fields map[string]any
}

func (r *Refused) Error() string { return r.Code + ": " + r.Detail }

// Body is the RFC 9457 problem.
func (r *Refused) Body() map[string]any {
	body := map[string]any{}
	for k, v := range r.Fields {
		body[k] = v
	}
	body["type"] = Base + r.Code
	body["title"] = httpx.Title(r.Status)
	body["status"] = r.Status
	body["detail"] = r.Detail
	body["code"] = r.Code
	if r.Problems != nil {
		body["problems"] = r.Problems
	}
	if r.Type != "" {
		body["type"] = r.Type
	}
	return body
}
