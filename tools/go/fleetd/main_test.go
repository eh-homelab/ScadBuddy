package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/discover"
)

func TestParse(t *testing.T) {
	t.Setenv("FLEETD_API", "")
	t.Setenv("FLEETD_TOKEN_FILE", "")
	t.Setenv("CLAUDE_CONFIG_DIR", "/fake/claude")
	var stderr bytes.Buffer

	cfg, err := parse([]string{"scan"}, &stderr)
	if err != nil || len(cfg.ConfigDirs) != 1 || cfg.ConfigDirs[0] != "/fake/claude" {
		t.Fatalf("scan default: %+v %v", cfg, err)
	}
	cfg, err = parse([]string{"scan", "--config-dir", "/a", "--config-dir", "/b"}, &stderr)
	if err != nil || strings.Join(cfg.ConfigDirs, ",") != "/a,/b" {
		t.Fatalf("repeated: %+v %v", cfg, err)
	}
	if _, err := parse([]string{"run"}, &stderr); err == nil || !strings.Contains(err.Error(), "token file") {
		t.Fatalf("run without a token file: %v", err)
	}
	if _, err := parse([]string{"run", "--token-file", "/t", "--api", "http://scadbuddy.example.com/api/v1"}, &stderr); err == nil {
		t.Fatal("http to a remote host was accepted")
	}
	cfg, err = parse([]string{"run", "--token-file", "/t"}, &stderr)
	if err != nil || cfg.API != DefaultAPI || cfg.Interval != 10*time.Second || cfg.Refresh != 5*time.Minute {
		t.Fatalf("run defaults: %+v %v", cfg, err)
	}
	t.Setenv("FLEETD_API", "http://localhost:8000/api/v1")
	t.Setenv("FLEETD_TOKEN_FILE", "/env/token")
	cfg, err = parse([]string{"run"}, &stderr)
	if err != nil || cfg.API != "http://localhost:8000/api/v1" || cfg.TokenFile != "/env/token" {
		t.Fatalf("env defaults: %+v %v", cfg, err)
	}
	if _, err := parse([]string{"--help"}, &stderr); err != flag.ErrHelp {
		t.Fatalf("--help: %v", err)
	}
	if _, err := parse([]string{"mount"}, &stderr); err == nil {
		t.Fatal("an unknown command was accepted")
	}
}

func TestHelpExitsZero(t *testing.T) {
	var out, errb bytes.Buffer
	if code := realMain([]string{"--help"}, &out, &errb); code != 0 {
		t.Fatalf("exit %d", code)
	}
	if code := realMain([]string{"bogus"}, &out, &errb); code != 2 {
		t.Fatalf("exit %d", code)
	}
}

func TestWriteScan(t *testing.T) {
	var b bytes.Buffer
	m := discover.Machine{ID: strings.Repeat("a", 64), Hostname: "h", OS: "linux/amd64", OSUser: "u", ServiceVersion: "dev"}
	rows := []discover.Row{{SessionID: "aaaaaaaa-1111-4111-8111-111111111111", PID: 1, ProcStart: "2", Cwd: "/w", ConfigDir: "/c"}}
	if err := writeScan(&b, m, rows); err != nil {
		t.Fatal(err)
	}
	var got struct {
		Machine  map[string]any   `json:"machine"`
		Sessions []map[string]any `json:"sessions"`
	}
	if err := json.Unmarshal(b.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Machine["id"] != m.ID || got.Machine["hostname"] != "h" {
		t.Errorf("machine %v", got.Machine)
	}
	if len(got.Sessions) != 1 || got.Sessions[0]["session_id"] != rows[0].SessionID || got.Sessions[0]["cwd"] != "/w" {
		t.Errorf("sessions %v", got.Sessions)
	}
	b.Reset()
	_ = writeScan(&b, m, nil)
	if !strings.Contains(b.String(), `"sessions": []`) {
		t.Errorf("no sessions is not an empty list: %s", b.String())
	}
}

func TestLoopScansOnChange(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	changes := make(chan struct{}, 1)
	var n atomic.Int32
	scanned := make(chan struct{}, 10)
	done := make(chan struct{})
	go func() {
		loop(ctx, time.Hour, changes, func() { n.Add(1); scanned <- struct{}{} })
		close(done)
	}()
	<-scanned // the first scan, at once
	changes <- struct{}{}
	select {
	case <-scanned:
	case <-time.After(5 * time.Second):
		t.Fatal("no scan after a change")
	}
	cancel()
	<-done
	if n.Load() != 2 {
		t.Fatalf("%d scans", n.Load())
	}
}
