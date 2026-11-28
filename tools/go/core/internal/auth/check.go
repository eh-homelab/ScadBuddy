package auth

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"slices"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/metric/noop"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// Mode is SCADBUDDY_AUTH_MODE (core/settings.py auth_mode). Never Enforce on prod: the
// owner's decision of 2026-11-17.
type Mode string

// The modes. Off looks at nothing; Observe serves what Enforce would refuse, and counts it.
const (
	Off     Mode = "off"
	Observe Mode = "observe"
	Enforce Mode = "enforce"
)

// ParseMode is SCADBUDDY_AUTH_MODE as Python takes it: unset is Off, and any other value
// than the three names (an empty one too) stops the start.
func ParseMode(value string, set bool) (Mode, error) {
	if !set {
		return Off, nil
	}
	switch m := Mode(value); m {
	case Off, Observe, Enforce:
		return m, nil
	}
	return "", fmt.Errorf("SCADBUDDY_AUTH_MODE must be off, observe or enforce, not %q", value)
}

// The reasons enforce refuses a request (auth/routes.py Reason).
const (
	SignedOut       = "signed_out"
	NotAPerson      = "not_a_person"
	NoCredential    = "no_credential"
	OutOfScope      = "out_of_scope"
	WrongCaller     = "wrong_caller"
	NoRequestHeader = "no_request_header"
)

// RequestHeader is the header every cookie request from the page carries (auth/routes.py).
const RequestHeader = "X-ScadBuddy-Request"

// WouldRefuseMetric is the counter of what enforce would refuse, served in observe.
// Prometheus names it scadbuddy_auth_would_refuse_total{class, reason}, as Python's.
const WouldRefuseMetric = "scadbuddy.auth.would_refuse"

const problems = "https://scadbuddy.dev/problems/auth/"

// details are what each refusal says (auth/check.py _DETAILS): never the credential or who
// it belongs to.
var details = map[string]string{
	SignedOut:       "Sign in to ScadBuddy: this request carries no live session.",
	NoCredential:    "This route needs a service credential with its scope.",
	NotAPerson:      "Only a person signed in themselves may do this.",
	OutOfScope:      "This credential's scope does not cover this request.",
	WrongCaller:     "These routes belong to another service.",
	NoRequestHeader: "A signed-in request from the page carries X-ScadBuddy-Request: 1.",
	"author_header": "This caller may not name an author.",
	"wrong_author":  "This caller may not name that author.",
}

// WouldRefuse is auth/routes.py would_refuse: why enforce refuses a request of class from c,
// or "" when it serves it. headerNeeded is false for a socket and a scriptless GET.
func WouldRefuse(class string, c Caller, requestHeader, headerNeeded bool, method string) string {
	service := c.Kind == KindService
	switch {
	case class == "public" || class == "transfer" || class == "exchange":
		return ""
	case c.IsFleetToken():
		// fleetd's token (#8041, #8179): core's rotator routes and nothing else.
		if class == FleetRotatorClass {
			return ""
		}
		return OutOfScope
	case strings.HasPrefix(class, "internal."):
		if !service {
			return NoCredential
		}
		return scoped(c, class)
	case c.Kind == KindToken && class == FleetRotatorClass:
		// Only a fleet token, above, may call them; a read or write token may not.
		return OutOfScope
	case strings.HasPrefix(class, "crud."):
		if !service {
			return NoCredential
		}
		if class != "crud."+c.Service.Caller {
			return WrongCaller
		}
		return scoped(c, class)
	case class == "flow.host" && service && c.Service.Has("flow.host"):
		return ""
	case c.Kind == KindAnonymous:
		return SignedOut
	case (class == "api" || class == "flow.host") && service && !c.Service.Has("api"):
		return OutOfScope
	}
	if c.Kind == KindToken {
		if class == "person" {
			return NotAPerson
		}
		if c.TokenScope != "write" && !slices.Contains([]string{"GET", "HEAD", "OPTIONS"}, strings.ToUpper(method)) {
			return OutOfScope
		}
		return ""
	}
	if class == "person" && !(c.Kind == KindPerson && (c.SessionKind == "login" || c.SessionKind == "break_glass")) {
		return NotAPerson
	}
	if c.Kind == KindPerson && headerNeeded && !requestHeader {
		return NoRequestHeader
	}
	return ""
}

// FleetRotatorClass is the class of core's routes for fleetd, the only ones a fleet token
// reaches (auth/routes.py FLEET_ROTATOR_CLASS).
const FleetRotatorClass = "crud.fleet-rotator"

func scoped(c Caller, class string) string {
	if c.Service.Has(class) {
		return ""
	}
	return OutOfScope
}

// Refusal is auth/check.py refusal: enforce's answer to a request WouldRefuse refuses. No
// live credential is a 401, with WWW-Authenticate where a bearer is what is wanted; a live
// one without the class is a 403.
func Refusal(class, reason string, bearer bool) *httpx.Problem {
	if reason == SignedOut || reason == NoCredential {
		p := httpx.WithReason(http.StatusUnauthorized, details[reason], reason)
		p.Type = problems + "signed-out"
		if bearer || strings.HasPrefix(class, "internal.") || strings.HasPrefix(class, "crud.") {
			p.Headers = map[string]string{"WWW-Authenticate": `Bearer realm="scadbuddy"`}
		}
		return p
	}
	p := httpx.WithReason(http.StatusForbidden, details[reason], reason)
	p.Type = problems + "forbidden"
	return p
}

// Route is what Gate needs of one route.
type Route struct {
	// Class is the route's class in auth/routes.py: "api" for a public area's routes,
	// "person" for what only a person signed in themselves may do.
	Class string
	// Template is the route as Starlette writes it: the log names it, never the path.
	Template string
	// Scriptless is a GET the browser makes with no script (an <img>, a download), which
	// carries no X-ScadBuddy-Request (auth/routes.py SCRIPTLESS_GETS).
	Scriptless bool
}

// Public is one public route an area serves (a path outside /internal/crud/): Pattern is
// net/http's ("POST /api/v1/filament/presets"), Route what the gate checks, Handler the
// answer, which reads its caller with CallerOf.
type Public struct {
	Pattern string
	Route   Route
	Handler http.Handler
}

// Gate is auth/gate.py AuthGate and auth/check.py auth_check for the routes it wraps.
type Gate struct {
	Resolver *Resolver
	Mode     Mode

	wouldRefuse metric.Int64Counter
	mu          sync.Mutex
	logged      map[[2]string]time.Time
}

// NewGate is a gate over resolver in mode. meter, when not nil, gets the would-refuse count.
func NewGate(resolver *Resolver, mode Mode, meter metric.MeterProvider) *Gate {
	if meter == nil {
		meter = noop.NewMeterProvider()
	}
	// The error is only for an invalid name, fixed here; a failed counter is a no-op.
	counter, _ := meter.Meter("github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth").Int64Counter(
		WouldRefuseMetric,
		metric.WithDescription("Requests enforce would refuse, served in observe, by route class and reason."))
	return &Gate{Resolver: resolver, Mode: mode, wouldRefuse: counter, logged: map[[2]string]time.Time{}}
}

type callerKey struct{}

// WithCaller is ctx carrying c, for a handler's test.
func WithCaller(ctx context.Context, c Caller) context.Context {
	return context.WithValue(ctx, callerKey{}, c)
}

// CallerOf is the caller Gate put on ctx; anonymous where no gate ran.
func CallerOf(ctx context.Context) Caller {
	if c, ok := ctx.Value(callerKey{}).(Caller); ok {
		return c
	}
	return Caller{Kind: KindAnonymous}
}

// HasScope says whether the caller on ctx holds scope: a service credential's scope, or an
// access token's "read" or "write".
func HasScope(ctx context.Context, scope string) bool {
	return slices.Contains(CallerOf(ctx).Scopes(), scope)
}

// Wrap serves next behind the check: the caller is resolved in every mode and put on the
// request's context (CallerOf). In Observe a request enforce would refuse is served,
// counted and logged; in Enforce it is refused as Python refuses it: a problem, or a 403
// with no body for a WebSocket handshake, which is closed before it is accepted.
func (g *Gate) Wrap(route Route, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c := g.Resolver.Caller(r.Context(), r.Header)
		r = r.WithContext(WithCaller(r.Context(), c))
		if c.IsFleetToken() {
			// Refused in every mode where enforce would refuse it (auth/check.py auth_check,
			// #8041): it must never act as its person. Not counted, as Python counts none.
			if reason := WouldRefuse(route.Class, c, false, false, r.Method); reason != "" {
				if strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
					w.WriteHeader(http.StatusForbidden)
					return
				}
				httpx.WriteProblem(w, r, Refusal(route.Class, reason, true))
				return
			}
		}
		if g.Mode != Observe && g.Mode != Enforce {
			next.ServeHTTP(w, r)
			return
		}
		socket := strings.EqualFold(r.Header.Get("Upgrade"), "websocket")
		reason := WouldRefuse(route.Class, c, r.Header.Get(RequestHeader) == "1",
			!socket && !route.Scriptless, r.Method)
		if reason != "" {
			if g.Mode == Enforce {
				if socket {
					w.WriteHeader(http.StatusForbidden)
					return
				}
				_, bearer := r.Header["Authorization"]
				httpx.WriteProblem(w, r, Refusal(route.Class, reason, bearer))
				return
			}
			g.note(r.Context(), route, reason)
		}
		next.ServeHTTP(w, r)
	})
}

// logEvery is auth/observe.py LOG_INTERVAL: a class and reason are logged once a minute.
const logEvery = time.Minute

// note is auth/observe.py WouldRefuse.note: counted, and logged at most once a minute.
func (g *Gate) note(ctx context.Context, route Route, reason string) {
	g.wouldRefuse.Add(ctx, 1, metric.WithAttributes(
		attribute.String("class", route.Class), attribute.String("reason", reason)))
	key, now := [2]string{route.Class, reason}, time.Now()
	g.mu.Lock()
	if last, seen := g.logged[key]; seen && now.Sub(last) < logEvery {
		g.mu.Unlock()
		return
	}
	g.logged[key] = now
	g.mu.Unlock()
	// The route's template, never the request's path: it can carry ids and names.
	slog.WarnContext(ctx, "enforce would refuse a "+route.Class+" request ("+reason+
		"); served, as SCADBUDDY_AUTH_MODE=observe", "route", route.Template)
}
