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
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/events"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/turns"
)

const (
	// poolMax is this process's share of the database's connections (backend
	// tests/test_pg_pools.py counts the rest).
	poolMax         = 4
	shutdownTimeout = 20 * time.Second
)

func main() {
	if len(os.Args) > 1 {
		fmt.Println("scadbuddy-core takes no arguments; its settings are in tools/go/core/README.md")
		return
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, os.Getenv); err != nil {
		slog.Error("scadbuddy-core stopped", "error", err.Error())
		os.Exit(1)
	}
}

func run(ctx context.Context, env func(string) string) error {
	upstream, err := newUpstream(env("SCADBUDDY_CORE_UPSTREAM"))
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
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return err
	}
	defer pool.Close()

	router, err := newRouter(pool, upstream, env("SCADBUDDY_CORE_AREAS"), env("SCADBUDDY_AUTH_MODE"))
	if err != nil {
		return err
	}
	address := env("SCADBUDDY_CORE_LISTEN")
	if address == "" {
		address = ":8080"
	}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return err
	}
	server := &http.Server{Handler: router, ReadHeaderTimeout: 30 * time.Second}
	slog.Info("scadbuddy-core listens", "address", listener.Addr().String())
	return serve(ctx, server, listener)
}

func serve(ctx context.Context, server *http.Server, listener net.Listener) error {
	failed := make(chan error, 1)
	go func() { failed <- server.Serve(listener) }()
	select {
	case err := <-failed:
		return err
	case <-ctx.Done():
	}
	drain, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancel()
	return server.Shutdown(drain)
}

// newRouter answers the named areas: <area> for all its routes here, <area>:read for its GETs.
func newRouter(pool *pgxpool.Pool, next http.Handler, areas, authMode string) (*crud.Router, error) {
	router := &crud.Router{
		Resolver: &auth.Resolver{Pool: pool},
		Enforce:  authMode == "enforce",
		Next:     next,
	}
	log, turn := &events.Log{Pool: pool}, &turns.Turns{Pool: pool}
	for name := range strings.SplitSeq(areas, ",") {
		switch name = strings.TrimSpace(name); name {
		case "":
		case "session-events:read":
			router.Add("agent", log.Reads()...)
		case "session-events":
			router.Add("agent", log.Reads()...)
			router.Add("agent", log.Writes()...)
		case "session-turns:read":
			router.Add("agent", turn.Polled()...)
		default:
			return nil, fmt.Errorf("SCADBUDDY_CORE_AREAS names %q, which this build does not have", name)
		}
	}
	return router, nil
}

// peerHeader names the request's TCP peer to Python. It is always overwritten here.
const peerHeader = "X-ScadBuddy-Peer"

// newUpstream forwards to the Python API at an http:// URL or a unix:///path socket.
func newUpstream(raw string) (http.Handler, error) {
	target, err := url.Parse(raw)
	if err != nil || raw == "" {
		return nil, errors.New("SCADBUDDY_CORE_UPSTREAM is an http:// URL or a unix:// path")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.DisableCompression = true
	transport.MaxIdleConnsPerHost = 64
	switch target.Scheme {
	case "http":
	case "unix":
		path := target.Path
		transport.DialContext = func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", path)
		}
		target = &url.URL{Scheme: "http", Host: "scadbuddy"}
	default:
		return nil, errors.New("SCADBUDDY_CORE_UPSTREAM is an http:// URL or a unix:// path")
	}
	return &httputil.ReverseProxy{
		Transport:     transport,
		FlushInterval: -1, // SSE and progress answers reach the client as they are written.
		Rewrite: func(p *httputil.ProxyRequest) {
			p.Out.URL.Scheme, p.Out.URL.Host = target.Scheme, target.Host
			p.Out.Host = p.In.Host
			// Rewrite drops these; Python decides what to believe (core/proxies.py).
			for _, h := range []string{"Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"} {
				if v, ok := p.In.Header[h]; ok {
					p.Out.Header[h] = v
				}
			}
			host, _, err := net.SplitHostPort(p.In.RemoteAddr)
			if err != nil {
				host = p.In.RemoteAddr
			}
			p.Out.Header.Set(peerHeader, host)
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
