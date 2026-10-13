package main

import (
	"errors"
	"flag"
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
	cfg, err := parse([]string{"mount", "--api", "http://h/api/v1", "--all", "--allow-other", "/m"})
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.All || !cfg.AllowOther || cfg.ReadOnly || cfg.TTL != 5*time.Second || cfg.CacheDir != "/xdg/bambuddy-fs" {
		t.Fatalf("%+v", cfg)
	}
}

func TestParseHelpAndErrors(t *testing.T) {
	for _, args := range [][]string{nil, {"--help"}, {"mount", "--help"}, {"mount", "-h"}} {
		if _, err := parse(args); !errors.Is(err, flag.ErrHelp) {
			t.Errorf("%v: %v, want ErrHelp", args, err)
		}
	}
	for _, args := range [][]string{{"frobnicate"}, {"mount", "--api", "http://h/api/v1"}, {"mount", "--api", "http://h/api/v1", "a", "b"}, {"mount", "--bogus"}} {
		if _, err := parse(args); err == nil || errors.Is(err, flag.ErrHelp) {
			t.Errorf("%v: %v, want an error", args, err)
		}
	}
}
