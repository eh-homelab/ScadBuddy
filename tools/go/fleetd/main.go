// Command fleetd is the fleet's machine service (#7987, spec
// 2026-11-26-fleet-session-registry-and-rotator-design §6.6): it finds the live Claude Code
// sessions on this machine and keeps core's session registry up to date. `fleetd scan`
// prints what it would send and sends nothing.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/discover"
	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/registry"
	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/report"
	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/update"
	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/watch"
)

// DefaultAPI is the deployed ScadBuddy's API root.
const DefaultAPI = "https://scadbuddy.internal.nullreference.io/api/v1"

// version is set at build time with -ldflags "-X main.version=…"; else the VCS revision.
var version string

const (
	defaultInterval = 10 * time.Second
	defaultRefresh  = 5 * time.Minute
	// defaultUpdateInterval is how often run looks for a new release (#8588).
	defaultUpdateInterval = 15 * time.Minute
	gitTimeout            = 5 * time.Second
	// settle lets a burst of file changes end before the scan they start.
	settle = 250 * time.Millisecond
)

// stringList is a flag that may be repeated.
type stringList []string

func (s *stringList) String() string     { return strings.Join(*s, ",") }
func (s *stringList) Set(v string) error { *s = append(*s, v); return nil }

type config struct {
	Command    string
	API        string
	TokenFile  string
	ConfigDirs []string
	Interval   time.Duration
	Refresh    time.Duration
	Telemetry  telemetryConfig
	// UpdateInterval and NoUpdate drive auto-update (#8588).
	UpdateInterval time.Duration
	NoUpdate       bool
}

func defaultConfigDir() string {
	if d := os.Getenv("CLAUDE_CONFIG_DIR"); d != "" {
		return d
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".claude"
	}
	return filepath.Join(home, ".claude")
}

func defaultAPI() string {
	if v := os.Getenv("FLEETD_API"); v != "" {
		return v
	}
	return DefaultAPI
}

const usage = `fleetd keeps ScadBuddy's fleet session registry up to date for this machine.

  fleetd run  [--api <url>] [--token-file <file>] [--config-dir <dir>]... [--interval 10s] [--refresh 5m] [--otel-endpoint <url>] [--no-telemetry] [--update-interval 15m] [--no-update]
  fleetd scan [--config-dir <dir>]...
  fleetd version

run sends the machine and its live sessions every --interval and on each change in
<config dir>/sessions: only the rows that changed, and all of them every --refresh.
run also replaces itself with the newest signed fleetd release, checked every
--update-interval, and restarts in place, unless --no-update is given or this is a dev build.
scan prints what run would send, as JSON, and sends nothing.
`

func newFlagSet(cmd string, cfg *config, dirs *stringList) *flag.FlagSet {
	set := flag.NewFlagSet("fleetd "+cmd, flag.ContinueOnError)
	set.Var(dirs, "config-dir", "a Claude Code config dir to watch; repeat for more (default $CLAUDE_CONFIG_DIR, else ~/.claude)")
	if cmd == "run" {
		set.StringVar(&cfg.API, "api", defaultAPI(), "ScadBuddy's API root, like https://host/api/v1 (default $FLEETD_API, else "+DefaultAPI+")")
		set.StringVar(&cfg.TokenFile, "token-file", os.Getenv("FLEETD_TOKEN_FILE"), "a file holding a personal access token of scope fleet from ScadBuddy's Settings → Access tokens, read again for each request (default $FLEETD_TOKEN_FILE)")
		set.DurationVar(&cfg.Interval, "interval", defaultInterval, "how often to scan")
		set.DurationVar(&cfg.Refresh, "refresh", defaultRefresh, "how often to send every row again, changed or not")
		set.StringVar(&cfg.Telemetry.Endpoint, "otel-endpoint", "", "OTLP/HTTP collector base URL for traces, metrics and logs (default $OTEL_EXPORTER_OTLP_ENDPOINT, else "+telemetryDefaultEndpoint+")")
		set.BoolVar(&cfg.Telemetry.Disabled, "no-telemetry", false, "send no traces, metrics or logs")
		set.DurationVar(&cfg.UpdateInterval, "update-interval", defaultUpdateIntervalFromEnv(), "how often to look for a new signed release (default $FLEETD_UPDATE_INTERVAL, else 15m)")
		set.BoolVar(&cfg.NoUpdate, "no-update", false, "never replace this binary with a new release")
	}
	return set
}

func parse(args []string, stderr io.Writer) (config, error) {
	var cfg config
	if len(args) == 0 {
		return cfg, errors.New("no command: run, scan or version")
	}
	cfg.Command = args[0]
	switch cfg.Command {
	case "-h", "-help", "--help", "help":
		fmt.Fprint(stderr, usage)
		return cfg, flag.ErrHelp
	case "version":
		return cfg, nil
	case "run", "scan":
	default:
		return cfg, fmt.Errorf("unknown command %q: run, scan or version", cfg.Command)
	}
	var dirs stringList
	set := newFlagSet(cfg.Command, &cfg, &dirs)
	set.SetOutput(stderr)
	if err := set.Parse(args[1:]); err != nil {
		return cfg, err
	}
	if set.NArg() > 0 {
		return cfg, fmt.Errorf("unexpected argument %q", set.Arg(0))
	}
	if len(dirs) == 0 {
		dirs = stringList{defaultConfigDir()}
	}
	for _, d := range dirs {
		abs, err := filepath.Abs(d)
		if err != nil {
			return cfg, err
		}
		cfg.ConfigDirs = append(cfg.ConfigDirs, abs)
	}
	if cfg.Command == "run" {
		if cfg.Interval < time.Second {
			return cfg, errors.New("--interval must be at least 1s")
		}
		if cfg.Refresh < cfg.Interval {
			return cfg, errors.New("--refresh must not be shorter than --interval")
		}
		if _, err := registry.CheckAPI(cfg.API); err != nil {
			return cfg, err
		}
		if !cfg.NoUpdate && cfg.UpdateInterval < time.Minute {
			return cfg, errors.New("--update-interval must be at least 1m")
		}
		if cfg.TokenFile == "" {
			return cfg, errors.New("no token file: pass --token-file or set $FLEETD_TOKEN_FILE")
		}
	}
	return cfg, nil
}

// defaultUpdateIntervalFromEnv is $FLEETD_UPDATE_INTERVAL when it parses, else 15m.
func defaultUpdateIntervalFromEnv() time.Duration {
	if d, err := time.ParseDuration(os.Getenv("FLEETD_UPDATE_INTERVAL")); err == nil {
		return d
	}
	return defaultUpdateInterval
}

// serviceVersion is version, else "dev+<12 hex of the commit>", else "dev".
func serviceVersion() string {
	if version != "" {
		return version
	}
	if info, ok := debug.ReadBuildInfo(); ok {
		for _, s := range info.Settings {
			if s.Key == "vcs.revision" && len(s.Value) >= 12 {
				return "dev+" + s.Value[:12]
			}
		}
	}
	return "dev"
}

func readMachine() (discover.Machine, error) {
	return discover.ReadMachine(discover.MachineOptions{
		MachineIDFile: "/etc/machine-id",
		OSReleaseFile: "/etc/os-release",
		Version:       serviceVersion(),
	})
}

func scanOptions(cfg config) discover.Options {
	return discover.Options{ConfigDirs: cfg.ConfigDirs, ProcRoot: "/proc", GitTimeout: gitTimeout}
}

// dryRun is what `fleetd scan` prints.
type dryRun struct {
	Machine  machineOut   `json:"machine"`
	Sessions []sessionOut `json:"sessions"`
}

type machineOut struct {
	ID string `json:"id"`
	discover.Machine
}

type sessionOut struct {
	SessionID string `json:"session_id"`
	discover.Row
}

// writeScan prints the machine and rows as JSON.
func writeScan(w io.Writer, m discover.Machine, rows []discover.Row) error {
	out := dryRun{Machine: machineOut{ID: m.ID, Machine: m}, Sessions: []sessionOut{}}
	for _, r := range rows {
		out.Sessions = append(out.Sessions, sessionOut{SessionID: r.SessionID, Row: r})
	}
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	enc.SetEscapeHTML(false)
	return enc.Encode(out)
}

// reexecPath is set by run when a new release is in place: main execs it once run's
// loops and telemetry have stopped (#8588).
var reexecPath string

func main() {
	code := realMain(os.Args[1:], os.Stdout, os.Stderr)
	if reexecPath != "" {
		// Same pid, args and environment, so systemd keeps the service.
		err := syscall.Exec(reexecPath, os.Args, os.Environ())
		fmt.Fprintf(os.Stderr, "fleetd: re-exec of the new release failed: %v\n", err)
		os.Exit(1)
	}
	os.Exit(code)
}

func realMain(args []string, stdout, stderr io.Writer) int {
	cfg, err := parse(args, stderr)
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	if err != nil {
		fmt.Fprintf(stderr, "fleetd: %v\n\n%s", err, usage)
		return 2
	}
	switch cfg.Command {
	case "version":
		fmt.Fprintln(stdout, serviceVersion())
		return 0
	case "scan":
		m, err := readMachine()
		if err != nil {
			fmt.Fprintf(stderr, "fleetd: reading the machine's id: %v\n", err)
			return 1
		}
		if err := writeScan(stdout, m, discover.Scan(context.Background(), scanOptions(cfg))); err != nil {
			fmt.Fprintf(stderr, "fleetd: %v\n", err)
			return 1
		}
		return 0
	}
	return run(cfg, stderr)
}

func run(cfg config, stderr io.Writer) int {
	m, err := readMachine()
	if err != nil {
		fmt.Fprintf(stderr, "fleetd: reading the machine's id: %v\n", err)
		return 1
	}
	tel, stopTelemetry := startTelemetry(cfg.Telemetry, stderr)
	defer stopTelemetry(5 * time.Second)
	u, _ := url.Parse(cfg.API)
	client, err := registry.New(cfg.API, cfg.TokenFile, registry.WrapTransport(tel.transport(u.Hostname())))
	if err != nil {
		slog.Error("fleetd: " + err.Error())
		return 1
	}
	var dirs []string
	for _, d := range cfg.ConfigDirs {
		dirs = append(dirs, filepath.Join(d, "sessions"))
	}
	w, err := watch.New(dirs)
	if err != nil {
		// Without inotify the interval still scans.
		slog.Warn("fleetd: watching for changes failed; scanning on the interval only", "err", err)
	}
	var changes <-chan struct{}
	if w != nil {
		defer w.Close()
		changes = w.Changes()
	}
	sigCtx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	// ctx also ends when a new release is in place, so the loops stop before the re-exec.
	ctx, cancel := context.WithCancel(sigCtx)
	defer cancel()
	var updating sync.WaitGroup
	if up := newUpdater(cfg); up != nil {
		updating.Add(1)
		go func() {
			defer updating.Done()
			if up.Loop(ctx, cfg.UpdateInterval) {
				reexecPath = up.Exe
				cancel()
			}
		}()
	}
	rep := report.New(client, m, cfg.Refresh)
	slog.Info("fleetd: started", "config_dirs", len(cfg.ConfigDirs), "version", m.ServiceVersion)
	scan := func() {
		if w != nil {
			w.Ensure()
		}
		tel.scan(ctx, func(ctx context.Context) (report.Result, error) {
			err := rep.Report(ctx, discover.Scan(ctx, scanOptions(cfg)))
			return rep.Last, err
		})
	}
	loop(ctx, cfg.Interval, changes, scan)
	cancel()
	updating.Wait()
	slog.Info("fleetd: stopped")
	return 0
}

// loop calls scan at once, then every interval and shortly after each change, until ctx
// ends.
func loop(ctx context.Context, interval time.Duration, changes <-chan struct{}, scan func()) {
	tick := time.NewTicker(interval)
	defer tick.Stop()
	scan()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		case <-changes:
			select {
			case <-ctx.Done():
				return
			case <-time.After(settle):
			}
			// Drain what came while settling, and restart the interval.
			select {
			case <-changes:
			default:
			}
			tick.Reset(interval)
		}
		scan()
	}
}

// updaterRun is an update.Updater and the path it replaces.
type updaterRun struct {
	*update.Updater
	Exe string
}

// newUpdater is run's auto-updater, or nil when it is off (#8588).
func newUpdater(cfg config) *updaterRun {
	if cfg.NoUpdate {
		slog.Info("fleetd: auto-update is off (--no-update)")
		return nil
	}
	exe, err := os.Executable()
	if err == nil {
		// The real file, read before any swap: after one, /proc/self/exe names the .old.
		exe, err = filepath.EvalSymlinks(exe)
	}
	if err != nil {
		slog.Warn("fleetd: auto-update is off: cannot find this binary", "err", err)
		return nil
	}
	up, err := update.New(update.Config{Current: version, Exe: exe, GOOS: runtime.GOOS, GOARCH: runtime.GOARCH})
	if err != nil {
		slog.Info("fleetd: auto-update is off", "err", err)
		return nil
	}
	return &updaterRun{Updater: up, Exe: exe}
}
