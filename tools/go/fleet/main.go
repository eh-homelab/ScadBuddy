// Command fleet is ScadBuddy's agent fleet chat server: fleetchat's MCP server over
// sbfleet's Store and Bus, served at /mcp, with /healthz (spec
// 2026-11-25-agent-fleet-messaging-design §6.5, §6.6). Its settings are in README.md.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"runtime/debug"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleet/sbfleet"
	"github.com/eh-homelab/ScadBuddy/tools/go/fleetchat"
)

const (
	serverName = "scadbuddy-fleet"
	// On SIGTERM the server drains for shutdownGrace, then ends the requests still open
	// (the MCP GET streams never end by themselves) and stops by shutdownTimeout.
	shutdownGrace   = 5 * time.Second
	shutdownTimeout = 10 * time.Second
)

type config struct {
	Listen         string
	CoreURL        string
	TokenFile      string
	BootstrapFile  string
	CredentialFile string
	MaxText        int
	PageSize       int
	MaxPageSize    int
	SessionTimeout time.Duration
}

func main() {
	os.Exit(realMain(os.Args[1:], os.Getenv, os.Stderr))
}

func realMain(args []string, getenv func(string) string, stderr io.Writer) int {
	cfg, err := parseConfigTo(args, getenv, stderr)
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	if err != nil {
		fmt.Fprintf(stderr, "fleet: %v\n", err)
		return 2
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, cfg); err != nil {
		slog.Error("fleet stopped", "err", err.Error())
		return 1
	}
	return 0
}

// setting is one flag and its environment fallback.
type setting struct {
	flag, env, usage string
	parse            func(string) error
	def              string
}

func parseConfig(args []string, getenv func(string) string) (config, error) {
	return parseConfigTo(args, getenv, io.Discard)
}

// parseConfigTo reads the flags, then FLEETCHAT_* for each flag not given, then the
// defaults. A bad value is an error, wherever it came from. --help is flag.ErrHelp,
// with the usage written to out.
func parseConfigTo(args []string, getenv func(string) string, out io.Writer) (config, error) {
	var c config
	str := func(p *string) func(string) error {
		return func(v string) error { *p = v; return nil }
	}
	num := func(p *int) func(string) error {
		return func(v string) error {
			n, err := strconv.Atoi(strings.TrimSpace(v))
			if err != nil {
				return errors.New("not a whole number")
			}
			*p = n
			return nil
		}
	}
	dur := func(p *time.Duration) func(string) error {
		return func(v string) error {
			d, err := time.ParseDuration(strings.TrimSpace(v))
			if err != nil {
				return errors.New("not a Go duration (such as 30m)")
			}
			*p = d
			return nil
		}
	}
	settings := []setting{
		{"listen", "FLEETCHAT_LISTEN", "the address to serve /mcp and /healthz on", str(&c.Listen), ":8080"},
		{"core-url", "FLEETCHAT_CORE_URL", "core's URL, such as http://scadbuddy-core:8080 (required)", str(&c.CoreURL), ""},
		{"service-token-file", "FLEETCHAT_SERVICE_TOKEN_FILE", "a projected ServiceAccount token (audience scadbuddy) to exchange for the fleet credential", str(&c.TokenFile), ""},
		{"service-bootstrap-file", "FLEETCHAT_SERVICE_BOOTSTRAP_FILE", "a bootstrap secret to exchange for the fleet credential", str(&c.BootstrapFile), ""},
		{"credential-file", "FLEETCHAT_CREDENTIAL_FILE", "a fixed fleet credential, read at every request and never renewed (tests, local runs)", str(&c.CredentialFile), ""},
		{"max-text", "FLEETCHAT_MAX_TEXT", "the largest message text, in bytes", num(&c.MaxText), "16777216"},
		{"page-size", "FLEETCHAT_PAGE_SIZE", "the inbox page when a call gives no limit", num(&c.PageSize), "50"},
		{"max-page-size", "FLEETCHAT_MAX_PAGE_SIZE", "the largest inbox page", num(&c.MaxPageSize), "200"},
		{"session-timeout", "FLEETCHAT_SESSION_TIMEOUT", "close an MCP session after this long without a POST (Claude Code never sends DELETE /mcp); 0 never does", dur(&c.SessionTimeout), "24h"},
	}
	fs := flag.NewFlagSet("fleet", flag.ContinueOnError)
	fs.SetOutput(out)
	version := fs.Bool("version", false, "print the version and exit")
	for _, s := range settings {
		fs.Func(s.flag, fmt.Sprintf("%s (env %s, default %q)", s.usage, s.env, s.def), s.parse)
	}
	if err := fs.Parse(args); err != nil {
		return c, err
	}
	if *version {
		fmt.Fprintln(out, buildVersion())
		return c, flag.ErrHelp
	}
	if fs.NArg() > 0 {
		return c, fmt.Errorf("fleet takes no arguments, got %q", fs.Args())
	}
	given := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { given[f.Name] = true })
	for _, s := range settings {
		if given[s.flag] {
			continue
		}
		if v := getenv(s.env); v != "" {
			if err := s.parse(v); err != nil {
				return c, fmt.Errorf("%s: %w", s.env, err)
			}
		} else if err := s.parse(s.def); err != nil {
			return c, err
		}
	}
	return c, c.check()
}

func (c config) check() error {
	if c.CoreURL == "" {
		return errors.New("the core URL is required (-core-url or FLEETCHAT_CORE_URL)")
	}
	if u, err := url.Parse(c.CoreURL); err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return errors.New("the core URL must be an http or https URL with a host")
	}
	proofs := 0
	for _, f := range []string{c.TokenFile, c.BootstrapFile, c.CredentialFile} {
		if f != "" {
			proofs++
		}
	}
	if proofs != 1 {
		return errors.New("give exactly one of -service-token-file, -service-bootstrap-file and -credential-file (or FLEETCHAT_SERVICE_TOKEN_FILE, FLEETCHAT_SERVICE_BOOTSTRAP_FILE, FLEETCHAT_CREDENTIAL_FILE)")
	}
	if c.MaxText <= 0 || c.PageSize <= 0 || c.MaxPageSize <= 0 {
		return errors.New("max text, page size and max page size must be more than 0")
	}
	if c.PageSize > c.MaxPageSize {
		return fmt.Errorf("the page size (%d) is more than the max page size (%d)", c.PageSize, c.MaxPageSize)
	}
	if c.SessionTimeout < 0 {
		return errors.New("the session timeout must be 0 or more")
	}
	return nil
}

// buildVersion is the module's version from the build info, or "dev".
func buildVersion() string {
	if info, ok := debug.ReadBuildInfo(); ok && info.Main.Version != "" && info.Main.Version != "(devel)" {
		return info.Main.Version
	}
	return "dev"
}

// credential is the fleet credential and, for an exchanged one, the function told of a
// credential core refused. A proof is exchanged at core (sbfleet.Exchange); a fixed
// credential file is read at every call.
func credential(c config) (func() (string, error), []sbfleet.Option) {
	var e *sbfleet.Exchange
	switch {
	case c.TokenFile != "":
		e = sbfleet.NewExchange(c.CoreURL, c.TokenFile, sbfleet.ProofKubernetes)
	case c.BootstrapFile != "":
		e = sbfleet.NewExchange(c.CoreURL, c.BootstrapFile, sbfleet.ProofBootstrap)
	default:
		return credentialFile(c.CredentialFile), nil
	}
	return e.Token, []sbfleet.Option{sbfleet.OnUnauthorized(e.Invalidate)}
}

// credentialFile reads the file at every call: the credential may be rotated.
func credentialFile(path string) func() (string, error) {
	return func() (string, error) {
		data, err := os.ReadFile(path)
		if err != nil {
			return "", err
		}
		token := strings.TrimSpace(string(data))
		if token == "" {
			return "", fmt.Errorf("the credential file %s is empty", path)
		}
		return token, nil
	}
}

func newMux(ctx context.Context, svc *fleetchat.Service) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("/mcp", fleetchat.NewHandler(ctx, svc, serverName, buildVersion()))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, "ok\n")
	})
	return mux
}

func run(ctx context.Context, c config) error {
	token, opts := credential(c)
	svc := fleetchat.NewService(sbfleet.NewStore(c.CoreURL, token, opts...), sbfleet.NewBus(c.CoreURL, token, opts...), fleetchat.Config{
		MaxText: c.MaxText, PageSize: c.PageSize, MaxPageSize: c.MaxPageSize, SessionTimeout: c.SessionTimeout,
	})
	listener, err := net.Listen("tcp", c.Listen)
	if err != nil {
		return err
	}
	slog.Info("fleet listens", "address", listener.Addr().String(), "version", buildVersion())
	return serve(ctx, listener, newMux(ctx, svc), shutdownGrace, shutdownTimeout)
}

// serve serves handler on listener until ctx ends. Then it stops taking requests and lets
// the open ones finish for grace (a tool call in flight completes), ends the contexts of
// those still open (the MCP GET streams), and stops by timeout, closing what is left.
func serve(ctx context.Context, listener net.Listener, handler http.Handler, grace, timeout time.Duration) error {
	requests, endRequests := context.WithCancel(context.Background())
	defer endRequests()
	server := &http.Server{
		Handler:           handler,
		ReadHeaderTimeout: 30 * time.Second,
		BaseContext:       func(net.Listener) context.Context { return requests },
	}
	failed := make(chan error, 1)
	go func() { failed <- server.Serve(listener) }()
	select {
	case err := <-failed:
		return err
	case <-ctx.Done():
	}
	stop, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	drain, cancelDrain := context.WithTimeout(stop, grace)
	err := server.Shutdown(drain)
	cancelDrain()
	if err == nil {
		return nil
	}
	endRequests()
	if err := server.Shutdown(stop); err != nil {
		_ = server.Close()
		return err
	}
	return nil
}
