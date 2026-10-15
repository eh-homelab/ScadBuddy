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

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/fsys"
)

func TestParseFlags(t *testing.T) {
	cfg, err := parse([]string{"mount", "--api", "http://h/api/v1", "--ro", "--attr-ttl", "2s", "/mnt/x"})
	if err != nil {
		t.Fatal(err)
	}
	if cfg.API != "http://h/api/v1" || !cfg.ReadOnly || cfg.TTL != 2*time.Second || cfg.Mountpoint != "/mnt/x" {
		t.Fatalf("%+v", cfg)
	}
}

func TestMountDefaultsItsAPIAndMountpoint(t *testing.T) {
	t.Setenv("HOME", "/home/u")
	t.Setenv("BAMBUDDY_FS_API", "")
	t.Setenv("BAMBUDDY_FS_DIR", "")
	cfg, err := parse([]string{"mount"})
	if err != nil || cfg.API != DefaultAPI || cfg.Mountpoint != "/home/u/bambuddy" {
		t.Fatalf("%+v %v", cfg, err)
	}
	t.Setenv("BAMBUDDY_FS_API", "http://env/api/v1")
	t.Setenv("BAMBUDDY_FS_DIR", "/env/mnt")
	cfg, err = parse([]string{"mount"})
	if err != nil || cfg.API != "http://env/api/v1" || cfg.Mountpoint != "/env/mnt" {
		t.Fatalf("env: %+v %v", cfg, err)
	}
	for _, cmd := range []string{"umount", "unmount", "status"} {
		cfg, err := parse([]string{cmd, "--state-dir", "/s"})
		if err != nil || cfg.Command != strings.Replace(cmd, "unmount", "umount", 1) || cfg.Mountpoint != "/env/mnt" || cfg.StateDir != "/s" {
			t.Errorf("%s: %+v %v", cmd, cfg, err)
		}
		if cfg, err := parse([]string{cmd, "/m"}); err != nil || cfg.Mountpoint != "/m" {
			t.Errorf("%s /m: %+v %v", cmd, cfg, err)
		}
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

func TestParseCacheMax(t *testing.T) {
	cfg, err := parse([]string{"mount", "--api", "http://h/api/v1", "/m"})
	if err != nil || cfg.CacheMax != 2<<30 || cfg.CacheMax.String() != "2G" {
		t.Fatalf("default: %d %v", cfg.CacheMax, err)
	}
	for arg, want := range map[string]byteSize{"500M": 500 << 20, "2GiB": 2 << 30, "1gb": 1 << 30, "4096": 4096, "0": 0, "3T": 3 << 40} {
		cfg, err := parse([]string{"mount", "--api", "http://h/api/v1", "--cache-max", arg, "/m"})
		if err != nil || cfg.CacheMax != want {
			t.Errorf("%s: %d %v, want %d", arg, cfg.CacheMax, err, want)
		}
	}
	for _, arg := range []string{"lots", "-1", "2X", "9999999T"} {
		if _, err := parse([]string{"mount", "--api", "http://h/api/v1", "--cache-max", arg, "/m"}); err == nil {
			t.Errorf("%s: accepted", arg)
		}
	}
}

func TestParseTelemetryFlags(t *testing.T) {
	cfg, err := parse([]string{"mount", "--api", "http://h/api/v1", "/m"})
	if err != nil || cfg.Telemetry.Endpoint != "" || cfg.Telemetry.Disabled {
		t.Fatalf("defaults: %+v %v", cfg.Telemetry, err)
	}
	cfg, err = parse([]string{"mount", "--api", "http://h/api/v1", "--otel-endpoint", "https://c.example", "--no-telemetry", "/m"})
	if err != nil || cfg.Telemetry.Endpoint != "https://c.example" || !cfg.Telemetry.Disabled {
		t.Fatalf("%+v %v", cfg.Telemetry, err)
	}
	if cfg, err := parse([]string{"mount", "--api", "http://h/api/v1", "--no-tracing", "/m"}); err != nil || !cfg.Telemetry.Disabled {
		t.Fatalf("--no-tracing: %+v %v", cfg.Telemetry, err)
	}
}

func TestParseHelpAndErrors(t *testing.T) {
	for _, args := range [][]string{nil, {"--help"}, {"mount", "--help"}, {"mount", "-h"}} {
		if _, err := parse(args); !errors.Is(err, flag.ErrHelp) {
			t.Errorf("%v: %v, want ErrHelp", args, err)
		}
	}
	for _, args := range [][]string{{"unsent", "x"}, {"unsent", "--api", "http://h/api/v1"}, {"frobnicate"}, {"mount", "--api", ""}, {"mount", "--api", "http://h/api/v1", "a", "b"}, {"mount", "--bogus"}, {"mount", "--allow-other", "--no-allow-other"}, {"umount", "a", "b"}, {"status", "--ro"}, {"allow-other", "x"}} {
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
	if err := listUnsent(&out, state); err != nil || !strings.Contains(out.String(), "no unsent writes in "+state) {
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
	refused := filepath.Join(state, "refused", "5")
	if err := os.MkdirAll(refused, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(refused, "contents"), []byte("big"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(refused, "record.json"), []byte(`{"folder_id": 0, "name": "big.3mf", "size": 3, "attempts": 1, "last_error": "upload: 413 Request Entity Too Large"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	out.Reset()
	if err := listUnsent(&out, state); err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 6 || !strings.HasPrefix(lines[0], "PATH") || lines[4] != "refused (kept, not sent again):" || !strings.HasPrefix(lines[5], refused) || !strings.Contains(lines[5], "big.3mf") {
		t.Fatalf("%q", out.String())
	}
	if f := strings.Fields(lines[1]); !slices.Equal(f, []string{entry, "4", "a.3mf", "3", "2", "upload:", "502", "Bad", "Gateway"}) {
		t.Fatalf("entry: %q", lines[1])
	}
	if !strings.HasPrefix(lines[2], bad) || !strings.Contains(lines[2], "unreadable record") {
		t.Fatalf("corrupt: %q", lines[2])
	}
}

func TestASecondMountOfACacheDirectoryIsRefused(t *testing.T) {
	cache := t.TempDir()
	lock, err := fsys.LockCache(cache)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if _, err := fsys.LockCache(cache); err == nil || !strings.Contains(err.Error(), "--cache-dir") {
		t.Fatalf("second: %v", err)
	}
}

func TestEachAPIHasItsOwnCache(t *testing.T) {
	for api, want := range map[string]string{
		"http://scadbuddy.lan/api/v1":       "scadbuddy.lan",
		"https://scadbuddy.lan:8443/api/v1": "scadbuddy.lan_8443",
		"http://[::1]:8080/api/v1":          "__1_8080",
		"https://other.example.com/api/v1":  "other.example.com",
	} {
		if got := cacheName(api); got != want {
			t.Errorf("%s: %q, want %q", api, got, want)
		}
	}
}

func TestASecondMountOfAStateDirectoryIsRefused(t *testing.T) {
	state := t.TempDir()
	lock, err := fsys.LockState(state)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fsys.LockState(state); err == nil || !strings.Contains(err.Error(), "another bambuddy-fs mount") {
		t.Fatalf("second: %v", err)
	}
	_ = lock.Close()
	again, err := fsys.LockState(state)
	if err != nil {
		t.Fatalf("after unlock: %v", err)
	}
	_ = again.Close()
}
