package main

import (
	"context"
	"errors"
	"flag"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetchat"
)

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func TestConfig(t *testing.T) {
	t.Run("defaults", func(t *testing.T) {
		c, err := parseConfig([]string{"-core-url", "http://core:8080", "-credential-file", "/run/token"}, env(nil))
		if err != nil {
			t.Fatal(err)
		}
		want := config{
			Listen: ":8080", CoreURL: "http://core:8080", CredentialFile: "/run/token",
			MaxText: 16777216, PageSize: 50, MaxPageSize: 200, SessionTimeout: 24 * time.Hour,
		}
		if c != want {
			t.Errorf("got %+v, want %+v", c, want)
		}
	})
	t.Run("env", func(t *testing.T) {
		c, err := parseConfig(nil, env(map[string]string{
			"FLEETCHAT_LISTEN": ":9000", "FLEETCHAT_CORE_URL": "http://core", "FLEETCHAT_CREDENTIAL_FILE": "/t",
			"FLEETCHAT_MAX_TEXT": "1024", "FLEETCHAT_PAGE_SIZE": "10", "FLEETCHAT_MAX_PAGE_SIZE": "20",
			"FLEETCHAT_SESSION_TIMEOUT": "30m",
		}))
		if err != nil {
			t.Fatal(err)
		}
		want := config{Listen: ":9000", CoreURL: "http://core", CredentialFile: "/t",
			MaxText: 1024, PageSize: 10, MaxPageSize: 20, SessionTimeout: 30 * time.Minute}
		if c != want {
			t.Errorf("got %+v, want %+v", c, want)
		}
	})
	t.Run("a flag wins over the env", func(t *testing.T) {
		c, err := parseConfig([]string{"-page-size", "5", "-core-url", "http://flag"}, env(map[string]string{
			"FLEETCHAT_CORE_URL": "http://env", "FLEETCHAT_CREDENTIAL_FILE": "/t", "FLEETCHAT_PAGE_SIZE": "10",
		}))
		if err != nil {
			t.Fatal(err)
		}
		if c.PageSize != 5 || c.CoreURL != "http://flag" {
			t.Errorf("got %+v", c)
		}
	})
	for name, tc := range map[string]struct {
		args []string
		env  map[string]string
	}{
		"missing core URL":        {[]string{"-credential-file", "/t"}, nil},
		"missing credential file": {[]string{"-core-url", "http://core"}, nil},
		"core URL not http":       {[]string{"-core-url", "core:8080", "-credential-file", "/t"}, nil},
		"bad env number":          {[]string{"-core-url", "http://core", "-credential-file", "/t"}, map[string]string{"FLEETCHAT_MAX_TEXT": "lots"}},
		"bad env duration":        {[]string{"-core-url", "http://core", "-credential-file", "/t"}, map[string]string{"FLEETCHAT_SESSION_TIMEOUT": "5"}},
		"zero max text":           {[]string{"-core-url", "http://core", "-credential-file", "/t", "-max-text", "0"}, nil},
		"page over max":           {[]string{"-core-url", "http://core", "-credential-file", "/t", "-page-size", "300"}, nil},
		"negative timeout":        {[]string{"-core-url", "http://core", "-credential-file", "/t", "-session-timeout", "-1s"}, nil},
		"bad flag":                {[]string{"-core-url", "http://core", "-credential-file", "/t", "-page-size", "x"}, nil},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := parseConfig(tc.args, env(tc.env)); err == nil {
				t.Error("no error")
			}
		})
	}
	t.Run("help needs no core URL", func(t *testing.T) {
		if _, err := parseConfig([]string{"--help"}, env(nil)); !errors.Is(err, flag.ErrHelp) {
			t.Errorf("err %v, want flag.ErrHelp", err)
		}
	})
}

func TestHelpExitsZero(t *testing.T) {
	var out strings.Builder
	if code := realMain([]string{"--help"}, env(nil), &out); code != 0 {
		t.Errorf("exit %d", code)
	}
	if !strings.Contains(out.String(), "-core-url") {
		t.Errorf("usage %q names no flag", out.String())
	}
}

func TestCredential(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	read := credential(path)
	if _, err := read(); err == nil {
		t.Error("no error for a missing file")
	}
	if err := os.WriteFile(path, []byte("one\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if tok, err := read(); err != nil || tok != "one" {
		t.Errorf("got %q %v", tok, err)
	}
	// The file rotates: every call reads it again.
	if err := os.WriteFile(path, []byte("two"), 0o600); err != nil {
		t.Fatal(err)
	}
	if tok, _ := read(); tok != "two" {
		t.Errorf("got %q after rotation", tok)
	}
	if err := os.WriteFile(path, []byte("  \n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := read(); err == nil {
		t.Error("no error for an empty file")
	}
}

type idleBus struct{}

func (idleBus) Publish(context.Context, []string) error { return nil }
func (idleBus) Subscribe(ctx context.Context, _ func(fleetchat.Event)) error {
	<-ctx.Done()
	return ctx.Err()
}

func TestHealthz(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	svc := fleetchat.NewService(nil, idleBus{}, fleetchat.DefaultConfig())
	srv := httptest.NewServer(newMux(ctx, svc))
	defer srv.Close()
	res, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(res.Body)
	res.Body.Close()
	if res.StatusCode != 200 || strings.TrimSpace(string(body)) != "ok" {
		t.Errorf("healthz %d %q", res.StatusCode, body)
	}
	// /mcp is the MCP handler: a GET without a session is refused, not a 404.
	res, err = http.Get(srv.URL + "/mcp")
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode == http.StatusNotFound {
		t.Errorf("/mcp is not served")
	}
}

// TestNoCoreImports: sbfleet and the binary reach core only over HTTP.
func TestNoCoreImports(t *testing.T) {
	out, err := exec.Command("go", "list", "-deps", "./...").CombinedOutput()
	if err != nil {
		t.Fatalf("go list -deps: %v\n%s", err, out)
	}
	const repo = "github.com/eh-homelab/ScadBuddy/tools/go/"
	for _, pkg := range strings.Fields(string(out)) {
		rest, ok := strings.CutPrefix(pkg, repo)
		if !ok {
			continue
		}
		mod, _, _ := strings.Cut(rest, "/")
		if mod != "fleet" && mod != "fleetchat" {
			t.Errorf("fleet depends on %s", pkg)
		}
	}
}

// TestShutdown: on shutdown a request in flight finishes, a stream that never ends by itself
// is ended after the grace, and serve returns within its timeout.
func TestShutdown(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	started := make(chan struct{}, 2)
	mux := http.NewServeMux()
	mux.HandleFunc("/slow", func(w http.ResponseWriter, _ *http.Request) {
		started <- struct{}{}
		time.Sleep(300 * time.Millisecond)
		_, _ = io.WriteString(w, "done")
	})
	mux.HandleFunc("/stream", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		started <- struct{}{}
		<-r.Context().Done()
	})
	ctx, stop := context.WithCancel(context.Background())
	served := make(chan error, 1)
	const grace, timeout = 500 * time.Millisecond, 2 * time.Second
	go func() { served <- serve(ctx, listener, mux, grace, timeout) }()

	base := "http://" + listener.Addr().String()
	stream, err := http.Get(base + "/stream")
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Body.Close()
	slow := make(chan string, 1)
	go func() {
		res, err := http.Get(base + "/slow")
		if err != nil {
			slow <- err.Error()
			return
		}
		b, _ := io.ReadAll(res.Body)
		res.Body.Close()
		slow <- string(b)
	}()
	<-started
	<-started
	begun := time.Now()
	stop()
	if got := <-slow; got != "done" {
		t.Errorf("the request in flight got %q, want done", got)
	}
	select {
	case err := <-served:
		if err != nil {
			t.Errorf("serve: %v", err)
		}
	case <-time.After(timeout + time.Second):
		t.Fatal("serve did not return within its timeout")
	}
	if took := time.Since(begun); took < grace || took > timeout {
		t.Errorf("shutdown took %s, want between the grace %s and the timeout %s", took, grace, timeout)
	}
}
