package main

import (
	"errors"
	"flag"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestParseFlags(t *testing.T) {
	cfg, err := parse([]string{"mount", "--api", "http://h/api/v1", "--ro", "--attr-ttl", "2s", "/mnt/x"})
	if err != nil {
		t.Fatal(err)
	}
	if cfg.API != "http://h/api/v1" || !cfg.ReadOnly || cfg.TTL != 2*time.Second || cfg.Mountpoint != "/mnt/x" {
		t.Fatalf("%+v", cfg)
	}
	if _, err := parse([]string{"mount", "/mnt/x"}); err == nil {
		t.Fatal("--api is required")
	}
}

func TestParseDefaultsAndAllowOther(t *testing.T) {
	t.Setenv("XDG_CACHE_HOME", "/xdg")
	t.Setenv("XDG_STATE_HOME", "/state")
	cfg, err := parse([]string{"mount", "--api", "http://h/api/v1", "--all", "--allow-other", "/m"})
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.All || !cfg.AllowOther || cfg.ReadOnly || cfg.TTL != 5*time.Second || cfg.CacheDir != "/xdg/bambuddy-fs" || cfg.StateDir != "/state/bambuddy-fs" {
		t.Fatalf("%+v", cfg)
	}
}

func TestParseHelpAndErrors(t *testing.T) {
	for _, args := range [][]string{nil, {"--help"}, {"mount", "--help"}, {"mount", "-h"}} {
		if _, err := parse(args); !errors.Is(err, flag.ErrHelp) {
			t.Errorf("%v: %v, want ErrHelp", args, err)
		}
	}
	for _, args := range [][]string{{"unsent", "x"}, {"unsent", "--api", "http://h/api/v1"}, {"frobnicate"}, {"mount", "--api", "http://h/api/v1"}, {"mount", "--api", "http://h/api/v1", "a", "b"}, {"mount", "--bogus"}} {
		if _, err := parse(args); err == nil || errors.Is(err, flag.ErrHelp) {
			t.Errorf("%v: %v, want an error", args, err)
		}
	}
}

func TestTheUnsentCommand(t *testing.T) {
	t.Setenv("XDG_STATE_HOME", "/state")
	if cfg, err := parse([]string{"unsent"}); err != nil || cfg.Command != "unsent" || cfg.StateDir != "/state/bambuddy-fs" {
		t.Fatalf("%+v %v", cfg, err)
	}
	state := t.TempDir()
	cfg, err := parse([]string{"unsent", "--state-dir", state})
	if err != nil || cfg.StateDir != state {
		t.Fatalf("%+v %v", cfg, err)
	}

	var out strings.Builder
	if err := listUnsent(&out, state); err != nil || !strings.Contains(out.String(), "no unsent writes in "+filepath.Join(state, "unsent")) {
		t.Fatalf("empty: %q %v", out.String(), err)
	}

	entry := filepath.Join(state, "unsent", "3")
	bad := filepath.Join(state, "unsent", "4")
	for _, d := range []string{entry, bad} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(d, "contents"), []byte("abc"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	record := `{"folder_id": 4, "name": "a.3mf", "size": 3, "attempts": 2, "last_error": "upload: 502 Bad Gateway", "base": null}`
	if err := os.WriteFile(filepath.Join(entry, "record.json"), []byte(record), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(bad, "record.json"), []byte("{"), 0o600); err != nil {
		t.Fatal(err)
	}
	out.Reset()
	if err := listUnsent(&out, state); err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 3 || !strings.HasPrefix(lines[0], "PATH") {
		t.Fatalf("%q", out.String())
	}
	if f := strings.Fields(lines[1]); !slices.Equal(f, []string{entry, "4", "a.3mf", "3", "2", "upload:", "502", "Bad", "Gateway"}) {
		t.Fatalf("entry: %q", lines[1])
	}
	if !strings.HasPrefix(lines[2], bad) || !strings.Contains(lines[2], "unreadable record") {
		t.Fatalf("corrupt: %q", lines[2])
	}
}
