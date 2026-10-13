// Command bambuddy-fs mounts Bambuddy's print library, through ScadBuddy's API, as a
// FUSE filesystem (#2686).
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"text/tabwriter"
	"time"

	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/fsys"
	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/telemetry"
)

type config struct {
	// Command is "mount" or "unsent".
	Command    string
	API        string
	ReadOnly   bool
	All        bool
	AllowOther bool
	TTL        time.Duration
	CacheDir   string
	CacheMax   byteSize
	StateDir   string
	Mountpoint string
	Telemetry  telemetry.Config
}

const usageText = `Usage: bambuddy-fs mount --api <url> [flags] <mountpoint>
       bambuddy-fs unsent [--state-dir <dir>]

mount mounts Bambuddy's print library, through ScadBuddy's API, as a filesystem.
unsent lists the writes whose upload failed, kept to be sent again, and exits.

Flags:
`

func newFlagSet(cfg *config) *flag.FlagSet {
	set := flag.NewFlagSet("mount", flag.ContinueOnError)
	set.SetOutput(io.Discard)
	set.StringVar(&cfg.API, "api", "", "ScadBuddy's API root, like http://host/api/v1 (required)")
	set.BoolVar(&cfg.ReadOnly, "ro", false, "refuse every write with EROFS")
	set.BoolVar(&cfg.All, "all", false, "list every file, not only unsliced 3MFs")
	set.BoolVar(&cfg.AllowOther, "allow-other", false, "let other users and processes see the mount (FUSE allow_other)")
	set.DurationVar(&cfg.TTL, "attr-ttl", 5*time.Second, "how long listings and kernel entries and attributes are kept")
	set.StringVar(&cfg.CacheDir, "cache-dir", defaultCacheDir(), "where downloaded contents and files being written are kept")
	cfg.CacheMax = 2 << 30
	set.Var(&cfg.CacheMax, "cache-max", "at most this much downloaded contents is kept, least recently used removed first, like 500M or 2G (0: no bound)")
	stateDirFlag(set, cfg)
	set.StringVar(&cfg.Telemetry.Endpoint, "otel-endpoint", "", "OTLP/HTTP collector base URL for traces and metrics (default $OTEL_EXPORTER_OTLP_ENDPOINT, else "+telemetry.DefaultEndpoint+")")
	set.BoolVar(&cfg.Telemetry.Disabled, "no-telemetry", false, "send no traces or metrics (by default they go to eh-homelab's collector, with the host's name)")
	set.BoolVar(&cfg.Telemetry.Disabled, "no-tracing", false, "the same as --no-telemetry")
	return set
}

func stateDirFlag(set *flag.FlagSet, cfg *config) {
	set.StringVar(&cfg.StateDir, "state-dir", defaultStateDir(), "where writes whose upload failed are kept until they are sent")
}

// byteSize is a flag's size in bytes: a number with an optional binary suffix, K, M,
// G or T, alone or followed by "iB" or "B" (2G = 2GiB = 2GB = 2 << 30).
type byteSize int64

func (b *byteSize) String() string {
	for _, u := range []struct {
		suffix string
		shift  uint
	}{{"T", 40}, {"G", 30}, {"M", 20}, {"K", 10}} {
		if *b != 0 && *b%(1<<u.shift) == 0 {
			return strconv.FormatInt(int64(*b)>>u.shift, 10) + u.suffix
		}
	}
	return strconv.FormatInt(int64(*b), 10)
}

func (b *byteSize) Set(s string) error {
	num := strings.TrimSuffix(strings.TrimSuffix(strings.ToUpper(strings.TrimSpace(s)), "B"), "I")
	shift := 0
	if i := strings.IndexAny(num, "KMGT"); i >= 0 && i == len(num)-1 {
		shift = 10 * (strings.IndexByte("KMGT", num[i]) + 1)
		num = num[:i]
	}
	n, err := strconv.ParseInt(num, 10, 64)
	if err != nil || n < 0 || n > math.MaxInt64>>shift {
		return fmt.Errorf("%q is not a size like 500M or 2G", s)
	}
	*b = byteSize(n << shift)
	return nil
}

func defaultCacheDir() string { return xdgDir("XDG_CACHE_HOME", ".cache") }

// defaultStateDir holds the unsent writes, the only copy of what was written, so it is
// not under the cache directory, which cache cleaners may empty.
func defaultStateDir() string { return xdgDir("XDG_STATE_HOME", filepath.Join(".local", "state")) }

func xdgDir(env, underHome string) string {
	if d := os.Getenv(env); d != "" {
		return filepath.Join(d, "bambuddy-fs")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(os.TempDir(), "bambuddy-fs")
	}
	return filepath.Join(home, underHome, "bambuddy-fs")
}

// parse reads the command line, "mount" and its flags. It returns flag.ErrHelp for
// --help, wherever it stands.
func parse(args []string) (config, error) {
	var cfg config
	if len(args) == 0 {
		return cfg, flag.ErrHelp
	}
	switch args[0] {
	case "-h", "-help", "--help":
		return cfg, flag.ErrHelp
	case "mount":
	case "unsent":
		cfg.Command = "unsent"
		fl := flag.NewFlagSet("unsent", flag.ContinueOnError)
		fl.SetOutput(io.Discard)
		stateDirFlag(fl, &cfg)
		if err := fl.Parse(args[1:]); err != nil {
			return cfg, err
		}
		if fl.NArg() != 0 {
			return cfg, errors.New("unsent takes no arguments")
		}
		return cfg, nil
	default:
		return cfg, fmt.Errorf("unknown command %q", args[0])
	}
	cfg.Command = "mount"
	fl := newFlagSet(&cfg)
	if err := fl.Parse(args[1:]); err != nil {
		return cfg, err
	}
	if cfg.API == "" {
		return cfg, errors.New("--api is required")
	}
	if fl.NArg() != 1 {
		return cfg, errors.New("exactly one mountpoint is required")
	}
	cfg.Mountpoint = fl.Arg(0)
	return cfg, nil
}

func main() {
	cfg, err := parse(os.Args[1:])
	switch {
	case errors.Is(err, flag.ErrHelp):
		fmt.Print(usageText)
		printDefaults(os.Stdout)
		return
	case err != nil:
		fmt.Fprintf(os.Stderr, "bambuddy-fs: %v\n", err)
		fmt.Fprintln(os.Stderr, "run bambuddy-fs --help for usage")
		os.Exit(2)
	}
	if cfg.Command == "unsent" {
		if err := listUnsent(os.Stdout, cfg.StateDir); err != nil {
			fmt.Fprintf(os.Stderr, "bambuddy-fs: %v\n", err)
			os.Exit(1)
		}
		return
	}
	os.Exit(run(cfg))
}

// listUnsent prints the writes queued under stateDir, one per line, then those the
// library refused, which are kept but not sent again.
func listUnsent(w io.Writer, stateDir string) error {
	all, err := fsys.ListUnsent(stateDir)
	if err != nil {
		return err
	}
	if len(all) == 0 {
		_, err := fmt.Fprintf(w, "no unsent writes in %s\n", stateDir)
		return err
	}
	tw := tabwriter.NewWriter(w, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "PATH\tFOLDER\tNAME\tSIZE\tATTEMPTS\tLAST ERROR")
	refused := false
	for _, u := range all {
		if u.Refused && !refused {
			refused = true
			fmt.Fprintln(tw, "\nrefused (kept, not sent again):")
		}
		if u.Err != nil {
			fmt.Fprintf(tw, "%s\t\t\t\t\tunreadable record: %v\n", u.Path, u.Err)
			continue
		}
		fmt.Fprintf(tw, "%s\t%d\t%s\t%d\t%d\t%s\n", u.Path, u.FolderID, u.Name, u.Size, u.Attempts, u.LastError)
	}
	return tw.Flush()
}

func printDefaults(w io.Writer) {
	fl := newFlagSet(&config{})
	fl.SetOutput(w)
	fl.PrintDefaults()
}

func run(cfg config) int {
	lib, err := api.New(cfg.API, nil)
	if err != nil {
		log.Printf("bambuddy-fs: %v", err)
		return 2
	}
	for _, d := range []string{cfg.CacheDir, cfg.StateDir} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			log.Printf("bambuddy-fs: %v", err)
			return 1
		}
	}
	// The state directory's queue is sent by one mount only.
	lock, err := fsys.LockState(cfg.StateDir)
	if err != nil {
		log.Printf("bambuddy-fs: %v", err)
		return 1
	}
	defer lock.Close()
	stopTelemetry, err := telemetry.Start(cfg.Telemetry)
	if err != nil {
		log.Printf("bambuddy-fs: telemetry: %v", err)
		return 1
	}
	// Flushes once the mount is gone and before the lock is released: within 5 s, or
	// 1 s if the mount failed, when there is little to send and the user waits.
	flush := 5 * time.Second
	defer func() { stopTelemetry(flush) }()
	root := fsys.NewRoot(lib, fsys.Options{All: cfg.All, ReadOnly: cfg.ReadOnly, TTL: cfg.TTL, CacheDir: cfg.CacheDir, CacheMax: int64(cfg.CacheMax), StateDir: cfg.StateDir})
	ttl := cfg.TTL
	server, err := fs.Mount(cfg.Mountpoint, root, &fs.Options{
		EntryTimeout: &ttl,
		AttrTimeout:  &ttl,
		MountOptions: fuse.MountOptions{FsName: "bambuddy", Name: "bambuddy-fs", AllowOther: cfg.AllowOther},
	})
	if err != nil {
		log.Printf("bambuddy-fs: mount: %v", err)
		flush = time.Second
		return 1
	}

	// The retrier sends the unsent writes, read-only mount or not, and stops before
	// the unmount; their records keep them for the next mount.
	retryCtx, cancelRetry := context.WithCancel(context.Background())
	retried := make(chan struct{})
	go func() {
		fsys.RetryUnsent(retryCtx, root)
		close(retried)
	}()
	stopRetry := func() {
		cancelRetry()
		<-retried
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	exited := make(chan struct{})
	go func() {
		server.Wait()
		close(exited)
	}()
	select {
	case <-ctx.Done():
	case <-exited:
		// Unmounted from outside, with fusermount -u.
		stopRetry()
		return 0
	}
	stopRetry()
	if err := server.Unmount(); err != nil {
		log.Printf("bambuddy-fs: unmount %s: %v (is it busy? close what uses it, then fusermount3 -u)", cfg.Mountpoint, err)
		return 1
	}
	<-exited
	return 0
}
