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
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/fsys"
)

type config struct {
	API        string
	ReadOnly   bool
	All        bool
	AllowOther bool
	TTL        time.Duration
	CacheDir   string
	Mountpoint string
}

const usageText = `Usage: bambuddy-fs mount --api <url> [flags] <mountpoint>

Mounts Bambuddy's print library, through ScadBuddy's API, as a filesystem.

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
	return set
}

func defaultCacheDir() string {
	if d := os.Getenv("XDG_CACHE_HOME"); d != "" {
		return filepath.Join(d, "bambuddy-fs")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(os.TempDir(), "bambuddy-fs")
	}
	return filepath.Join(home, ".cache", "bambuddy-fs")
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
	default:
		return cfg, fmt.Errorf("unknown command %q", args[0])
	}
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
	os.Exit(run(cfg))
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
	if err := os.MkdirAll(cfg.CacheDir, 0o700); err != nil {
		log.Printf("bambuddy-fs: cache directory: %v", err)
		return 1
	}
	root := fsys.NewRoot(lib, fsys.Options{All: cfg.All, ReadOnly: cfg.ReadOnly, TTL: cfg.TTL, CacheDir: cfg.CacheDir})
	ttl := cfg.TTL
	server, err := fs.Mount(cfg.Mountpoint, root, &fs.Options{
		EntryTimeout: &ttl,
		AttrTimeout:  &ttl,
		MountOptions: fuse.MountOptions{FsName: "bambuddy", Name: "bambuddy-fs", AllowOther: cfg.AllowOther},
	})
	if err != nil {
		log.Printf("bambuddy-fs: mount: %v", err)
		return 1
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
		return 0
	}
	if err := server.Unmount(); err != nil {
		log.Printf("bambuddy-fs: unmount %s: %v (is it busy? close what uses it, then fusermount3 -u)", cfg.Mountpoint, err)
		return 1
	}
	<-exited
	return 0
}
