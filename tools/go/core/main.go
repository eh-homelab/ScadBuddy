// Command scadbuddy-core is ScadBuddy's API in Go (#7667). It answers the areas it has and
// gives every other request, unchanged, to the Python API.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel/metric"

	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry/pgxtrace"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/access"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/agentreads"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/agentstart"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/approvals"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/audit"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobs"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobstore"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/changes"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/edits"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/escalations"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/events"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/fleet"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/forget"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/front"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/gate"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/history"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/mcprelay"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/notify"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/ordercosts"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/packages"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/pending"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/plugins"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/publish"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/questions"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/realtime"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/secrets"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/sessions"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/stores"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/sweeps"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/tasks"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/transcripts"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/turns"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/workspace"
)

const (
	// poolMax is this process's share of the database's connections (backend
	// tests/test_pg_pools.py counts the rest).
	poolMax         = 4
	shutdownTimeout = 20 * time.Second
	// serviceName is the resource's service.name (OTEL_SERVICE_NAME overrides it).
	serviceName = "scadbuddy-core"
)

var (
	// dialWait is how long a forward waits for the Python API to listen: at a pod's start,
	// core is up some 25 s before Python is.
	dialWait = 30 * time.Second
	// untraced are the paths the API never traces either (main.py EXCLUDED_URLS).
	untraced = []string{"/healthz", "/metrics", "/telemetry/v1/traces"}
)

func main() {
	if len(os.Args) > 1 {
		fmt.Println("scadbuddy-core takes no arguments; its settings are in tools/go/core/README.md")
		return
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	tel, err := telemetry.Start(telemetry.Options{ServiceName: serviceName, DropParentlessClients: true})
	if err != nil {
		// Core is the API pod's front: a bad OTEL_* variable turns telemetry off, as a
		// collector outage does, and never takes the API down.
		slog.Error("telemetry is off", "reason", err.Error())
		tel = &telemetry.Telemetry{}
	}
	// The JSON on stderr stays, for kubectl logs; with logs on, each record also goes over
	// OTLP, and a record logged inside a request carries its trace_id and span_id. The log
	// package's lines (net/http's and the forward's) go to stderr only.
	tel.SetDefaultLogger(slog.NewJSONHandler(os.Stderr, nil), serviceName, nil)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, os.LookupEnv, tel); err != nil {
		slog.Error("scadbuddy-core stopped", "error", err.Error())
		flush, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		tel.Shutdown(flush)
		cancel()
		os.Exit(1)
	}
}

// run serves until ctx ends, over the process's environment, os.LookupEnv's shape: the
// blobs area tells a variable set to "" from one not set, as Python's settings do. tel is
// the process's telemetry, nil for none; serve flushes it.
func run(ctx context.Context, lookupEnv func(string) (string, bool), tel *telemetry.Telemetry) error {
	env := func(name string) string {
		v, _ := lookupEnv(name)
		return v
	}
	trusted, err := front.ParseNetworks(env("SCADBUDDY_CORE_TRUSTED_PROXIES"))
	if err != nil {
		return fmt.Errorf("SCADBUDDY_CORE_TRUSTED_PROXIES: %w", err)
	}
	upstream, err := newUpstream(env("SCADBUDDY_CORE_UPSTREAM"), trusted, tel)
	if err != nil {
		return err
	}
	dsn := env("SCADBUDDY_DATABASE_URL")
	if dsn == "" {
		return errors.New("SCADBUDDY_DATABASE_URL is required")
	}
	config, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		// pgx's parse errors can quote the DSN: say which variable, not what it holds.
		return errors.New("SCADBUDDY_DATABASE_URL is not a Postgres URL")
	}
	config.MaxConns = poolMax
	config.MaxConnIdleTime = time.Minute
	config.ConnConfig.ConnectTimeout = 5 * time.Second
	config.ConnConfig.RuntimeParams["application_name"] = "scadbuddy-core"
	if tel.Tracer() != nil || tel.Meter() != nil {
		config.ConnConfig.Tracer = pgxtrace.New(tel.Tracer(), tel.Meter())
	}
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return err
	}
	defer pool.Close()
	if err := pgxtrace.ObservePool(tel.Meter(), pool, serviceName); err != nil {
		return err
	}

	keys, err := seal.LoadKeys(env)
	if err != nil {
		return err
	}
	// The event stream's one LISTEN connection, which both listeners' streams share.
	hub := bus.NewHub(pool, tel.Meter())
	stream := &streams{hub: hub, env: env}
	go func() {
		// A stream never ends by itself: end them all first, so the servers can drain.
		<-ctx.Done()
		hub.Close()
	}()
	router, err := newRouter(pool, upstream, keys, env("SCADBUDDY_CORE_AREAS"), env("SCADBUDDY_AUTH_MODE"), tel.Meter(), lookupEnv, tel, stream)
	if err != nil {
		return err
	}
	if stream.used {
		hub.Start(5 * time.Second)
	}
	address := or(env("SCADBUDDY_CORE_LISTEN"), ":8080")
	servers := map[string]http.Handler{address: tel.Server(router, untraced...)}
	// The CRUD port, which pods that may reach nothing else are admitted to (crud/listener.py).
	if crudAddress := env("SCADBUDDY_CORE_CRUD_LISTEN"); crudAddress != "" {
		if crudAddress == address {
			return errors.New("SCADBUDDY_CORE_CRUD_LISTEN is the main address; the CRUD port needs its own")
		}
		// What Go does not answer goes to Python's own CRUD listener, never to the full API, so
		// Python's path filter still stands behind Go's.
		crudUpstream, err := newUpstream(env("SCADBUDDY_CORE_CRUD_UPSTREAM"), trusted, tel)
		if err != nil {
			return fmt.Errorf("SCADBUDDY_CORE_CRUD_UPSTREAM: %w", err)
		}
		crudRouter, err := newRouter(pool, crudUpstream, keys, env("SCADBUDDY_CORE_AREAS"), env("SCADBUDDY_AUTH_MODE"), tel.Meter(), lookupEnv, tel, stream)
		if err != nil {
			return err
		}
		servers[crudAddress] = tel.Server(front.CRUDOnly(crudRouter), untraced...)
	}
	// The history subscriber (#6949): every replica runs it, and one at a time applies.
	go history.Run(ctx, pool, history.Options{})
	return serve(ctx, servers, tel.Shutdown)
}

func or(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

// serve serves each address's handler until ctx ends or one server fails. Then it drains
// the servers and flushes the telemetry, both within shutdownTimeout.
func serve(ctx context.Context, handlers map[string]http.Handler, flush func(context.Context)) error {
	// Every address is bound before any is served, so a failed bind leaves nothing running.
	listeners := map[net.Listener]http.Handler{}
	for address, handler := range handlers {
		listener, err := net.Listen("tcp", address)
		if err != nil {
			for l := range listeners {
				l.Close()
			}
			return err
		}
		listeners[listener] = handler
	}
	failed := make(chan error, len(listeners))
	var servers []*http.Server
	for listener, handler := range listeners {
		server := &http.Server{Handler: handler, ReadHeaderTimeout: 30 * time.Second}
		servers = append(servers, server)
		slog.Info("scadbuddy-core listens", "address", listener.Addr().String())
		go func() { failed <- server.Serve(listener) }()
	}
	var err error
	select {
	case err = <-failed:
	case <-ctx.Done():
	}
	drain, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancel()
	for _, server := range servers {
		err = errors.Join(err, ignoreClosed(server.Shutdown(drain)))
	}
	flush(drain)
	return ignoreClosed(err)
}

func ignoreClosed(err error) error {
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

// areaRoutes is a package's routes by area name, over the pool, with the KEKs (nil without
// SCADBUDDY_SECRET_KEY_FILE).
type areaRoutes func(pool *pgxpool.Pool, keys *seal.Keys) map[string][]crud.Route

// areaPackages are the packages whose areas the switch below does not name.
var areaPackages = []areaRoutes{
	access.Areas, agentreads.Areas, agentstart.Areas, approvals.Areas, audit.Areas, blobs.Areas,
	edits.Areas, escalations.Areas, forget.Areas, gate.Areas, mcprelay.Areas, notify.Areas,
	ordercosts.Areas, packages.Areas, pending.Areas, plugins.Areas, publish.Areas,
	questions.Areas, secrets.Areas, sessions.Areas, stores.Areas, sweeps.Areas, tasks.Areas,
	transcripts.Areas, workspace.Areas,
}

// publicAreas is a package's public routes by area name, over the pool, with the KEKs.
// newRouter serves each behind the one person-auth gate (internal/auth); an area may have
// CRUD routes, public routes or both.
type publicAreas func(pool *pgxpool.Pool, keys *seal.Keys) map[string][]auth.Public

// publicPackages are the packages with public areas.
var publicPackages = []publicAreas{}

// sealed are the areas that seal or open with the KEK: without one they refuse to start.
var sealed = map[string]bool{
	"credentials": true, "payload-keys": true, "keys": true, "notify": true, "transcripts": true,
	"remote-plugins": true,
}

// streams are what the event stream's areas (changes, ws) share: the process's one hub and
// the settings the socket reads.
type streams struct {
	hub *bus.Hub
	env func(string) string
	// used says whether an area named uses the hub.
	used bool
}

// newRouter answers the named areas: <area> for all its routes here, <area>:read for its GETs.
// A named area's public routes stand in front of the CRUD router, behind the gate in
// SCADBUDDY_AUTH_MODE's mode; meter, when not nil, counts what enforce would refuse.
// lookupEnv is the process's environment, which the blobs area reads the store's settings from,
// and tel the process's telemetry (nil for none), which traces its store requests.
// stream is the process's event stream; without one, an area that needs it gets a hub of
// its own.
func newRouter(pool *pgxpool.Pool, next http.Handler, keys *seal.Keys, areas, authMode string, meter metric.MeterProvider,
	lookupEnv func(string) (string, bool), tel *telemetry.Telemetry, stream ...*streams) (http.Handler, error) {
	mode, err := auth.ParseMode(authMode, authMode != "")
	if err != nil {
		return nil, fmt.Errorf("SCADBUDDY_AUTH_MODE: %w", err)
	}
	resolver := &auth.Resolver{Pool: pool}
	var st *streams
	if len(stream) > 0 {
		st = stream[0]
	}
	streamOf := func() *streams {
		if st == nil {
			st = &streams{hub: bus.NewHub(pool, nil), env: os.Getenv}
		}
		st.used = true
		return st
	}
	socket := false
	router := &crud.Router{
		Resolver: resolver,
		Enforce:  mode == auth.Enforce,
		Next:     next,
	}
	gate := auth.NewGate(resolver, mode, meter)
	public := http.NewServeMux()
	served := false
	publics := map[string][]auth.Public{}
	for _, p := range publicPackages {
		for name, routes := range p(pool, keys) {
			publics[name] = append(publics[name], routes...)
		}
	}
	log, turn := &events.Log{Pool: pool}, &turns.Turns{Pool: pool}
	more := map[string][]crud.Route{}
	for _, p := range areaPackages {
		for name, routes := range p(pool, keys) {
			more[name] = routes
		}
	}
	for name := range strings.SplitSeq(areas, ",") {
		switch name = strings.TrimSpace(name); name {
		case "":
		case "session-events:read":
			router.Add("agent", log.Reads()...)
		case "session-events":
			router.Add("agent", log.Reads()...)
			router.Add("agent", log.Writes()...)
		case "session-turns:read":
			router.Add("agent", turn.Reads()...)
		case "session-turns":
			router.Add("agent", turn.Reads()...)
			router.Add("agent", turn.Writes()...)
		case "fleet":
			router.Add(fleet.Caller, fleet.Routes(pool)...)
			router.Add(fleet.VirtualCaller, fleet.VirtualRoutes(pool)...)
		case blobstore.Area:
			// A shared area (#7958): any caller whose scopes hold crud.blobs.
			if keys == nil {
				return nil, fmt.Errorf("SCADBUDDY_CORE_AREAS names %q, which needs %s", name, seal.Variable)
			}
			router.AddShared(blobstore.Area, blobstore.Routes(pool, keys, lookupEnv, tel)...)
		case "changes":
			s := streamOf()
			for caller, routes := range changes.Routes(s.hub) {
				router.Add(caller, routes...)
			}
		case "ws":
			streamOf()
			socket = true
		default:
			if keys == nil && sealed[strings.TrimSuffix(name, ":read")] {
				return nil, fmt.Errorf("SCADBUDDY_CORE_AREAS names %q, which needs %s", name, seal.Variable)
			}
			routes, ok := more[name]
			outside, open := publics[name]
			if !ok && !open {
				return nil, fmt.Errorf("SCADBUDDY_CORE_AREAS names %q, which this build does not have", name)
			}
			router.Add("agent", routes...)
			for _, p := range outside {
				public.Handle(p.Pattern, gate.Wrap(p.Route, p.Handler))
				served = true
			}
		}
	}
	var handler http.Handler = router
	if served {
		public.Handle("/", router)
		handler = public
	}
	if socket {
		// The browser's socket is no CRUD route: it stands in front of the router.
		return realtime.New(context.Background(), st.hub, pool, handler, st.env, gate)
	}
	return handler, nil
}

// newUpstream forwards to the Python API at an http:// URL or a unix:///path socket. The
// request's client, as the trusted proxies name it, is the one X-Forwarded-For value Python
// reads; Python trusts the front alone. Each forward is a client span whose traceparent
// Python continues. A dial that Python refuses is tried again (upstreamWait).
func newUpstream(raw string, trusted front.Networks, tel *telemetry.Telemetry) (http.Handler, error) {
	target, err := url.Parse(raw)
	if err != nil || raw == "" {
		return nil, errors.New("SCADBUDDY_CORE_UPSTREAM is an http:// URL or a unix:// path")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.DisableCompression = true
	transport.MaxIdleConnsPerHost = 64
	dialer := &net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second}
	wait := &upstreamWait{}
	switch target.Scheme {
	case "http":
		transport.DialContext = wait.dial(dialer.DialContext)
	case "unix":
		path := target.Path
		transport.DialContext = wait.dial(func(ctx context.Context, _, _ string) (net.Conn, error) {
			return dialer.DialContext(ctx, "unix", path)
		})
		target = &url.URL{Scheme: "http", Host: "scadbuddy"}
	default:
		return nil, errors.New("SCADBUDDY_CORE_UPSTREAM is an http:// URL or a unix:// path")
	}
	return &httputil.ReverseProxy{
		Transport:     tel.Transport(transport, target.Hostname()),
		FlushInterval: -1, // SSE and progress answers reach the client as they are written.
		Rewrite: func(p *httputil.ProxyRequest) {
			p.Out = p.Out.WithContext(context.WithValue(p.Out.Context(), requestDone{}, p.In.Context().Done()))
			p.Out.URL.Scheme, p.Out.URL.Host = target.Scheme, target.Host
			p.Out.Host = p.In.Host
			// Rewrite drops these; Python reads them from its trusted peer, the front.
			for _, h := range []string{"Forwarded", "X-Forwarded-Host", "X-Forwarded-Proto"} {
				if v, ok := p.In.Header[h]; ok {
					p.Out.Header[h] = v
				}
			}
			p.Out.Header.Set("X-Forwarded-For", trusted.Client(p.In))
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			if !errors.Is(err, context.Canceled) {
				slog.WarnContext(r.Context(), "the Python API did not answer",
					"path", r.URL.Path, "error", err.Error())
			}
			w.WriteHeader(http.StatusBadGateway)
		},
	}, nil
}

// requestDone carries the forwarded request's Done channel to the dial. The transport dials on
// a context that its request's cancellation does not end, but that keeps its values.
type requestDone struct{}

// upstreamWait dials again, every 250 ms, while the upstream refuses the connection (or its
// socket is not there yet), as it does while Python starts beside core, until the request
// ends or dialWait passes. Only the dial is tried again, so no request is sent twice. Once
// Python has taken one connection it is up: a later refusal (Python restarted) fails at once.
type upstreamWait struct{ answered atomic.Bool }

func (u *upstreamWait) dial(dial func(ctx context.Context, network, address string) (net.Conn, error)) func(ctx context.Context, network, address string) (net.Conn, error) {
	return func(ctx context.Context, network, address string) (net.Conn, error) {
		done, _ := ctx.Value(requestDone{}).(<-chan struct{})
		deadline := time.Now().Add(dialWait)
		for {
			conn, err := dial(ctx, network, address)
			if err == nil {
				u.answered.Store(true)
				return conn, nil
			}
			if u.answered.Load() || !refused(err) || !time.Now().Before(deadline) {
				return nil, err
			}
			select {
			case <-ctx.Done():
				return nil, err
			case <-done:
				return nil, err
			case <-time.After(250 * time.Millisecond):
			}
		}
	}
}

// refused is a dial that found nothing listening: a refused TCP connection, or a Unix
// socket that is not there yet.
func refused(err error) bool {
	return errors.Is(err, syscall.ECONNREFUSED) || errors.Is(err, syscall.ENOENT)
}
