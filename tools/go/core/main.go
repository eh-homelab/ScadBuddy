// Command scadbuddy-core is ScadBuddy's API in Go (#7667). It answers the areas it has and
// gives every other request, unchanged, to the Python API.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"path"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel/metric"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/interceptor"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"

	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry/pgxtrace"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/access"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/agentreads"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/agentstart"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/approvals"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/assistantcost"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/audit"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobs"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobstore"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/changes"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/devices"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/edits"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/escalations"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/events"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/fetchgrants"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/filamentpresets"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/fleet"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/forget"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/front"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/gate"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/history"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/activities"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/labelprint"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/libraryrun"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/mcprelay"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/notify"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/ordercosts"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/packages"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/pending"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/plugins"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/publish"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/questions"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/realtime"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/scan"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/secrets"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/sessions"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/stores"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/sweeps"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/tagreads"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/tasks"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/temporalx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/transcripts"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/turns"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/unknowntrays"
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
	// untraced are the paths the API never traces either (core/tracing.py EXCLUDED_URLS).
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
	// Set but empty stops the start, as in Python; newRouter reads empty as unset.
	if _, set := lookupEnv("SCADBUDDY_AUTH_MODE"); set {
		if _, err := auth.ParseMode(env("SCADBUDDY_AUTH_MODE"), true); err != nil {
			return fmt.Errorf("SCADBUDDY_AUTH_MODE: %w", err)
		}
	}
	// A value Python refuses at its start stops core's too, not each request later.
	if err := assistantcost.CheckEnv(lookupEnv); err != nil {
		return err
	}
	if err := devices.CheckEnv(lookupEnv); err != nil {
		return err
	}
	trusted, err := front.ParseNetworks(env("SCADBUDDY_CORE_TRUSTED_PROXIES"))
	if err != nil {
		return fmt.Errorf("SCADBUDDY_CORE_TRUSTED_PROXIES: %w", err)
	}
	upstream, err := newUpstream(env("SCADBUDDY_CORE_UPSTREAM"), trusted, tel)
	if err != nil {
		return err
	}
	// The inventory's writes are activities on Temporal (#6949 K1): the area inventory runs
	// the worker, so it needs Temporal's address; inventory:read alone asks Temporal about a
	// stale operation when the address is given. The filament presets' writes and an unknown
	// tray's resolve run on Temporal too (#7204, #8489), but their areas do not need the
	// address: without it the reads serve and each write answers 503 writes_not_configured
	// (plan ruling P22).
	areas := env("SCADBUDDY_CORE_AREAS")
	libraryAreas := namesArea(areas, filamentpresets.Area) || namesArea(areas, unknowntrays.Area)
	var temporal *temporalx.Config
	if namesArea(areas, "inventory") ||
		(namesArea(areas, "inventory:read") || libraryAreas) && env("SCADBUDDY_TEMPORAL_ADDRESS") != "" {
		c, err := temporalx.FromEnv(env)
		if err != nil {
			return err
		}
		temporal = &c
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
	callers := callerPackages
	var temporalClient client.Client
	if temporal != nil {
		if temporalClient, err = temporalx.Client(*temporal, tel.Tracer()); err != nil {
			return err
		}
		defer temporalClient.Close()
		callers = map[string]areaRoutes{inventory.Caller: inventory.AreasWith(temporalx.Checker{Client: temporalClient})}
	}
	libraryRuns := libraryAreas && temporal != nil
	var options []routerOption
	if libraryRuns {
		options = append(options, withLibraryRuns(libraryrun.TemporalStarter{
			Client: temporalClient, Queue: temporal.FilamentPresetsQueue, LibraryQueue: temporal.LibraryQueue,
		}))
	}
	// The event stream's one LISTEN connection, which both listeners' streams share.
	hub := bus.NewHub(pool, tel.Meter())
	stream := &streams{hub: hub, env: env}
	options = append(options, withStreams(stream))
	go func() {
		// A stream never ends by itself: end them all first, so the servers can drain.
		<-ctx.Done()
		hub.Close()
	}()
	if env("SCADBUDDY_SERVICE_BOOTSTRAP_DIR") == "" {
		// Said once at start (#8175): with no directory, no machine has its proof file.
		slog.Warn("SCADBUDDY_SERVICE_BOOTSTRAP_DIR is unset: core refuses, and revokes, every " +
			"fleet-rotator credential that names a machine")
	}
	router, err := newRouter(pool, upstream, keys, areas, env("SCADBUDDY_AUTH_MODE"), callers, tel.Meter(), lookupEnv, tel, options...)
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
		crudRouter, err := newRouter(pool, crudUpstream, keys, areas, env("SCADBUDDY_AUTH_MODE"), callers, tel.Meter(), lookupEnv, tel, options...)
		if err != nil {
			return err
		}
		servers[crudAddress] = tel.Server(front.CRUDOnly(crudRouter), untraced...)
	}
	// The history subscriber (#6949): every replica runs it, and one at a time applies.
	go history.Run(ctx, pool, history.Options{})
	health := func(ctx context.Context) error {
		_, err := temporalClient.CheckHealth(ctx, &client.CheckHealthRequest{})
		return err
	}
	stopWorkers := []func(context.Context){}
	if namesArea(areas, "inventory") {
		acts := activities.New(pool, or(env("SCADBUDDY_ORDERS_CURRENCY"), "USD"))
		record := activities.Record(pool, tel.Meter())
		stopWorkers = append(stopWorkers, startWorker(ctx, "inventory", health, func() worker.Worker {
			w := worker.New(temporalClient, temporal.InventoryQueue, worker.Options{
				Interceptors: []interceptor.WorkerInterceptor{record},
			})
			activities.Register(w, acts)
			return w
		}))
	}
	if libraryRuns {
		// The workflow of each preset write and each unknown tray's resolve (plan ruling P21,
		// #8489 U2). Its one activity runs on the Python library queue, which the API process
		// serves.
		stopWorkers = append(stopWorkers, startWorker(ctx, "library-run", health, func() worker.Worker {
			w := worker.New(temporalClient, temporal.FilamentPresetsQueue, worker.Options{})
			w.RegisterWorkflowWithOptions(libraryrun.Workflow, workflow.RegisterOptions{Name: libraryrun.WorkflowName})
			return w
		}))
	}
	return serve(ctx, servers, func(drain context.Context) {
		for _, stop := range stopWorkers {
			stop(drain)
		}
		tel.Shutdown(drain)
	})
}

// namesArea says whether the comma-separated areas name area.
func namesArea(areas, area string) bool {
	for name := range strings.SplitSeq(areas, ",") {
		if strings.TrimSpace(name) == area {
			return true
		}
	}
	return false
}

// workerRetry is how long the worker waits before it asks Temporal again.
var workerRetry = 5 * time.Second

// startWorker starts the named Temporal worker beside the HTTP servers. A Temporal out of reach
// does not stop core, which is the API pod's front. Until health (Temporal's CheckHealth)
// answers, the worker waits workerRetry and asks again. Then it builds a worker and starts it.
// The SDK keeps a worker's first Start result (sync.OnceValue), so a worker whose Start
// failed is never started again: it is stopped, so it lets go of what Start took, and the
// next try builds a new one. A worker that starts after ctx ends is stopped at once. The
// function it gives stops the started worker, and waits for it at most until drain ends. A
// worker that started before stop is called is always stopped, even when drain has already
// ended (#8256).
func startWorker(ctx context.Context, name string, health func(context.Context) error, build func() worker.Worker) (stop func(drain context.Context)) {
	started := make(chan worker.Worker, 1)
	go func() {
		for {
			ask, cancel := context.WithTimeout(ctx, 10*time.Second)
			err := health(ask)
			cancel()
			if err == nil {
				w := build()
				if err = w.Start(); err == nil {
					if ctx.Err() != nil {
						w.Stop()
						started <- nil
						return
					}
					slog.Info("the " + name + " worker polls Temporal")
					started <- w
					return
				}
				w.Stop()
			}
			if ctx.Err() == nil {
				slog.Warn("the "+name+" worker could not start", "error", err.Error())
			}
			select {
			case <-ctx.Done():
				started <- nil
				return
			case <-time.After(workerRetry):
			}
		}
	}()
	return func(drain context.Context) {
		// A worker already started is taken first: with drain already ended, a select on both
		// could pick drain and leave the worker running.
		var w worker.Worker
		select {
		case w = <-started:
		default:
			select {
			case w = <-started:
			case <-drain.Done():
				return
			}
		}
		if w == nil {
			return
		}
		stopped := make(chan struct{})
		go func() {
			w.Stop()
			close(stopped)
		}()
		select {
		case <-stopped:
		case <-drain.Done():
			slog.Warn("the " + name + " worker did not stop before the drain ended")
		}
	}
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
	assistantcost.Areas, fetchgrants.Areas,
}

// callerPackages are the packages whose areas are mounted for a caller of their own (the
// key), not for the agent.
var callerPackages = map[string]areaRoutes{inventory.Caller: inventory.Areas}

// publicAreas is a package's public routes by area name, over the pool, with the KEKs.
// newRouter serves each behind the one person-auth gate (internal/auth); an area may have
// CRUD routes, public routes or both.
type publicAreas func(pool *pgxpool.Pool, keys *seal.Keys) map[string][]auth.Public

// publicPackages are the packages with public areas.
// Area unknown-trays is not among them: its resolve needs the router's Temporal, so newRouter
// adds its routes itself.
var publicPackages = []publicAreas{scan.PublicAreas, devices.Routes}

// mirrorBlobs is the blob store as area package-mirrors calls it (packages.Blobs): a
// publish to a key that names a blob is packages.ErrStale.
type mirrorBlobs struct{ *blobstore.Blobs }

func (m mirrorBlobs) PublishFile(ctx context.Context, key, name string, file io.ReadSeeker) error {
	err := m.Blobs.PublishFile(ctx, key, name, file)
	if errors.Is(err, blobstore.ErrStale) {
		return packages.ErrStale
	}
	return err
}

func (m mirrorBlobs) ReadFile(ctx context.Context, key, name string, max int64) ([]byte, bool, error) {
	data, found, err := m.Blobs.ReadFile(ctx, key, name, max)
	if errors.Is(err, blobstore.ErrTooLarge) {
		return nil, false, fmt.Errorf("%w: %w", packages.ErrTooLarge, err)
	}
	return data, found, err
}

// workspaceBlobs is the blob store as area workspace calls it (workspace.Store). Python
// names a store failure by its class in a write's refusal; the classes are its own.
type workspaceBlobs struct{ *blobstore.Blobs }

func (w workspaceBlobs) Put(ctx context.Context, kind, _ string, data []byte) (string, error) {
	key, err := w.Blobs.PutContent(ctx, kind, data)
	switch blobstore.Reason(err) {
	case "store_full":
		return "", storeClass{err, "StoreFullError"}
	case "store_busy":
		return "", storeClass{err, "StageLockBusyError"}
	}
	return key, err
}

func (w workspaceBlobs) Get(ctx context.Context, key string) (io.ReadCloser, bool, error) {
	return w.Blobs.Fetch(ctx, key)
}

// storeClass is a store failure with the name of Python's exception class for it.
type storeClass struct {
	error
	class string
}

func (e storeClass) Class() string { return e.class }
func (e storeClass) Unwrap() error { return e.error }

// sealed are the areas that seal or open with the KEK: without one they refuse to start.
var sealed = map[string]bool{
	"credentials": true, "payload-keys": true, "keys": true, "notify": true, "transcripts": true,
	"remote-plugins": true, "workspace": true,
}

// streams are what the event stream's areas (changes, ws) share: the process's one hub and
// the settings the socket reads.
type streams struct {
	hub *bus.Hub
	env func(string) string
	// used says whether an area named uses the hub.
	used bool
}

// routerOptions are newRouter's optional parts: the event stream's areas' hub, and the
// Temporal that the filament presets' writes and an unknown tray's resolve start on (none:
// they answer 503).
type routerOptions struct {
	stream      *streams
	libraryRuns libraryrun.Starter
}

type routerOption func(*routerOptions)

func withStreams(s *streams) routerOption { return func(o *routerOptions) { o.stream = s } }

func withLibraryRuns(s libraryrun.Starter) routerOption {
	return func(o *routerOptions) { o.libraryRuns = s }
}

// newRouter answers the named areas: <area> for all its routes here, <area>:read for its GETs.
// A named area's public routes stand in front of the CRUD router, behind the gate in
// SCADBUDDY_AUTH_MODE's mode; meter, when not nil, counts what enforce would refuse.
// lookupEnv is the process's environment, which the blobs area reads the store's settings from,
// and tel the process's telemetry (nil for none), which traces its store requests.
// stream is the process's event stream; without one, an area that needs it gets a hub of
// its own.
// The areas of areaPackages are the agent's; those of callers are their caller's. An area
// name that two callers' packages give stops the start; the agent's packages may share one
// (keys: secrets and fetchgrants).
func newRouter(pool *pgxpool.Pool, next http.Handler, keys *seal.Keys, areas, authMode string,
	callers map[string]areaRoutes, meter metric.MeterProvider,
	lookupEnv func(string) (string, bool), tel *telemetry.Telemetry, options ...routerOption) (http.Handler, error) {
	mode, err := auth.ParseMode(authMode, authMode != "")
	if err != nil {
		return nil, fmt.Errorf("SCADBUDDY_AUTH_MODE: %w", err)
	}
	resolver := &auth.Resolver{Pool: pool}
	if lookupEnv != nil {
		// Python's own setting: a fleet-rotator credential passes only while its machine's
		// proof file is there.
		resolver.BootstrapDir, _ = lookupEnv("SCADBUDDY_SERVICE_BOOTSTRAP_DIR")
	}
	var opts routerOptions
	for _, option := range options {
		option(&opts)
	}
	st := opts.stream
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
		Tokens:   resolver,
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
	for name, routes := range unknowntrays.Public(pool, keys, opts.libraryRuns) {
		publics[name] = append(publics[name], routes...)
	}
	// mountPublic serves a named area's public routes behind the gate.
	mountPublic := func(name string) {
		for _, p := range publics[name] {
			public.Handle(p.Pattern, gate.Wrap(p.Route, p.Handler))
			served = true
		}
	}
	log, turn := &events.Log{Pool: pool}, &turns.Turns{Pool: pool}
	more := map[string][]crud.Route{}
	callerOf := map[string]string{}
	add := func(caller string, p areaRoutes) error {
		for name, routes := range p(pool, keys) {
			if prev, twice := callerOf[name]; twice && prev != caller {
				return fmt.Errorf("two packages give the area %q", name)
			}
			more[name], callerOf[name] = append(more[name], routes...), caller
		}
		return nil
	}
	for _, p := range areaPackages {
		if err := add("agent", p); err != nil {
			return nil, err
		}
	}
	for caller, p := range callers {
		if err := add(caller, p); err != nil {
			return nil, err
		}
	}
	// Naming filament-presets twice mounts it once: a pattern given twice panics.
	presetsMounted := false
	// Naming both `unknown-trays` and `unknown-trays:read` mounts the area once.
	unknownTraysMounted := false
	// Naming label-printing twice mounts it once.
	labelsMounted := false
	devicesNamed := false
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
			router.Add(fleet.Caller, fleet.RegistryReads(pool)...)
			// fleetd's registry writes take its `fleet` access token too (#8041). Virtual
			// senders stay service-only: a machine's token never posts as `rotator` or
			// `github`.
			router.AddForToken(fleet.RotatorCaller, auth.FleetScope, fleet.RegistryWrites(pool,
				func(machine string) bool { return resolver.HasProof(fleet.RotatorCaller, machine) })...)
			router.Add(fleet.VirtualCaller, fleet.VirtualRoutes(pool)...)
		case blobstore.Area:
			// A shared area (#7958): any caller whose scopes hold crud.blobs.
			if keys == nil {
				return nil, fmt.Errorf("SCADBUDDY_CORE_AREAS names %q, which needs %s", name, seal.Variable)
			}
			router.AddShared(blobstore.Area, blobstore.Routes(pool, keys, lookupEnv, tel)...)
		case tagreads.Area:
			// The store's secret key is sealed: the dumps need the KEK, as area blobs does.
			if keys == nil {
				return nil, fmt.Errorf("SCADBUDDY_CORE_AREAS names %q, which needs %s", name, seal.Variable)
			}
			reads := &tagreads.Reads{Pool: pool, Blobs: blobstore.New(pool, keys, lookupEnv, tel), Upstream: next,
				Devices: &devices.Store{Pool: pool}}
			for _, p := range reads.Public()[tagreads.Area] {
				public.Handle(p.Pattern, gate.Wrap(p.Route, p.Handler))
				served = true
			}
		case filamentpresets.Area:
			// Public routes that read documents from the blob store, whose S3 secret is
			// sealed under the KEK. publicPackages pass no lookupEnv or tel, so the case is
			// its own.
			if keys == nil {
				return nil, fmt.Errorf("SCADBUDDY_CORE_AREAS names %q, which needs %s", name, seal.Variable)
			}
			if presetsMounted {
				break
			}
			presetsMounted = true
			store := filamentpresets.Store(blobstore.New(pool, keys, lookupEnv, tel))
			for _, p := range filamentpresets.Public(pool, store, opts.libraryRuns) {
				// no-store is set outside the gate, so its own 401 and 403 carry it too.
				public.Handle(p.Pattern, noStore(gate.Wrap(p.Route, p.Handler)))
				served = true
			}
		case labelprint.Area:
			// Public routes that call the CUPS server through the process's telemetry, so
			// the case is its own (#7170).
			if labelsMounted {
				break
			}
			labelsMounted = true
			for _, p := range labelprint.New(pool, tel).Public() {
				public.Handle(p.Pattern, gate.Wrap(p.Route, p.Handler))
				served = true
			}
		case packages.MirrorArea:
			// The blob store in process (#8137); its secret key is sealed under the KEK.
			if keys == nil {
				return nil, fmt.Errorf("SCADBUDDY_CORE_AREAS names %q, which needs %s", name, seal.Variable)
			}
			store := mirrorBlobs{blobstore.New(pool, keys, lookupEnv, tel)}
			router.Add("agent", packages.MirrorAreas(pool, store)[packages.MirrorArea]...)
		case "workspace":
			// Every route of crud_agent_workspace.py over the blob store in process (#8131):
			// a file is sealed under the KEK's keys, and the store's secret key is too.
			if keys == nil {
				return nil, fmt.Errorf("SCADBUDDY_CORE_AREAS names %q, which needs %s", name, seal.Variable)
			}
			store := workspaceBlobs{blobstore.New(pool, keys, lookupEnv, tel)}
			router.Add("agent", workspace.Routes(pool, keys, store)...)
		case "changes":
			s := streamOf()
			for caller, routes := range changes.Routes(s.hub) {
				router.Add(caller, routes...)
			}
		case "ws":
			streamOf()
			socket = true
		case devices.Area:
			devicesNamed = true
			mountPublic(name)
		case unknowntrays.Area, unknowntrays.Read:
			// Its CRUD route is the print worker's, not the agent's (areaPackages mounts on
			// the agent), so it has a case of its own.
			// The full area holds every route of the read-only one.
			if name == unknowntrays.Read && strings.Contains(","+strings.ReplaceAll(areas, " ", "")+",", ","+unknowntrays.Area+",") {
				break
			}
			if !unknownTraysMounted {
				unknownTraysMounted = true
				router.Add(unknowntrays.Caller, unknowntrays.BlockRoutes(pool)...)
			}
			for _, p := range publics[name] {
				// no-store is set outside the gate, so its own 401 and 403 carry it too.
				public.Handle(p.Pattern, noStore(gate.Wrap(p.Route, p.Handler)))
				served = true
			}
		default:
			if keys == nil && sealed[strings.TrimSuffix(name, ":read")] {
				return nil, fmt.Errorf("SCADBUDDY_CORE_AREAS names %q, which needs %s", name, seal.Variable)
			}
			routes, ok := more[name]
			_, open := publics[name]
			if !ok && !open {
				return nil, fmt.Errorf("SCADBUDDY_CORE_AREAS names %q, which this build does not have", name)
			}
			router.Add(callerOf[name], routes...)
			mountPublic(name)
		}
	}
	var handler http.Handler = router
	if served {
		handler = &publicFront{mux: public, next: router}
	}
	if socket {
		// The browser's socket is no CRUD route: it stands in front of the router.
		sock, err := realtime.New(context.Background(), st.hub, pool, handler, st.env, gate)
		if err != nil {
			return nil, err
		}
		if devicesNamed {
			// A paired device holds the same socket with its device token (#8244).
			sock.Devices = devices.New(pool, keys, nil, lookupEnv)
		}
		return sock, nil
	}
	return handler, nil
}

// publicFront serves a request from the public routes only where its path is canonical and
// one of them matches it, method included; every other request, a path the mux would answer
// with its own redirect among them, goes on to next unchanged.
type publicFront struct {
	mux  *http.ServeMux
	next http.Handler
}

func (f *publicFront) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if p := r.URL.Path; p == canonical(p) {
		if _, pattern := f.mux.Handler(r); pattern != "" {
			f.mux.ServeHTTP(w, r)
			return
		}
	}
	f.next.ServeHTTP(w, r)
}

// canonical is p as net/http's mux cleans it: path.Clean, keeping a trailing slash.
func canonical(p string) string {
	if p == "" || p[0] != '/' {
		return "/" + p
	}
	c := path.Clean(p)
	if strings.HasSuffix(p, "/") && c != "/" {
		c += "/"
	}
	return c
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

// noStore sets Cache-Control: no-store before next answers, whatever it answers.
func noStore(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		next.ServeHTTP(w, r)
	})
}
