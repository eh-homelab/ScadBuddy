package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
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
	t.Run("a proof file", func(t *testing.T) {
		c, err := parseConfig(nil, env(map[string]string{
			"FLEETCHAT_CORE_URL": "http://core", "FLEETCHAT_SERVICE_TOKEN_FILE": "/var/run/secrets/scadbuddy/token",
		}))
		if err != nil || c.TokenFile != "/var/run/secrets/scadbuddy/token" || c.CredentialFile != "" {
			t.Errorf("got %+v, %v", c, err)
		}
		c, err = parseConfig([]string{"-core-url", "http://core", "-service-bootstrap-file", "/run/fleet"}, env(nil))
		if err != nil || c.BootstrapFile != "/run/fleet" {
			t.Errorf("got %+v, %v", c, err)
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
		"two proofs":              {[]string{"-core-url", "http://core", "-service-token-file", "/a"}, map[string]string{"FLEETCHAT_SERVICE_BOOTSTRAP_FILE": "/b"}},
		"a proof and a fixed one": {[]string{"-core-url", "http://core", "-service-token-file", "/a", "-credential-file", "/t"}, nil},
		"core URL not http":       {[]string{"-core-url", "core:8080", "-credential-file", "/t"}, nil},
		"bad env number":          {[]string{"-core-url", "http://core", "-credential-file", "/t"}, map[string]string{"FLEETCHAT_MAX_TEXT": "lots"}},
		"bad env duration":        {[]string{"-core-url", "http://core", "-credential-file", "/t"}, map[string]string{"FLEETCHAT_SESSION_TIMEOUT": "5"}},
		"zero max text":           {[]string{"-core-url", "http://core", "-credential-file", "/t", "-max-text", "0"}, nil},
		"page over max":           {[]string{"-core-url", "http://core", "-credential-file", "/t", "-page-size", "300"}, nil},
		"negative timeout":        {[]string{"-core-url", "http://core", "-credential-file", "/t", "-session-timeout", "-1s"}, nil},
		"bad flag":                {[]string{"-core-url", "http://core", "-credential-file", "/t", "-page-size", "x"}, nil},
		"github key without ids":  {[]string{"-core-url", "http://core", "-credential-file", "/t", "-github-app-key-file", "/k"}, nil},
		"github ids without key":  {[]string{"-core-url", "http://core", "-credential-file", "/t", "-github-app-id", "1", "-github-installation-id", "2"}, nil},
		"github bad app id":       {[]string{"-core-url", "http://core", "-credential-file", "/t"}, map[string]string{"FLEETCHAT_GITHUB_APP_ID": "-3"}},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := parseConfig(tc.args, env(tc.env)); err == nil {
				t.Error("no error")
			}
		})
	}
	t.Run("the github app", func(t *testing.T) {
		c, err := parseConfig([]string{"-core-url", "http://core", "-credential-file", "/t", "-github-app-id", "5941183"}, env(map[string]string{
			"FLEETCHAT_GITHUB_INSTALLATION_ID": "186236249", "FLEETCHAT_GITHUB_APP_KEY_FILE": "/k",
			"FLEETCHAT_GITHUB_WEBHOOK_SECRET_FILE": "/w",
		}))
		if err != nil {
			t.Fatal(err)
		}
		if c.GitHubAppID != 5941183 || c.GitHubInstallationID != 186236249 || c.GitHubAppKeyFile != "/k" || c.GitHubWebhookSecretFile != "/w" {
			t.Errorf("got %+v", c)
		}
		// The webhook alone needs no App key.
		if _, err := parseConfig(nil, env(map[string]string{"FLEETCHAT_CORE_URL": "http://core", "FLEETCHAT_CREDENTIAL_FILE": "/t",
			"FLEETCHAT_GITHUB_WEBHOOK_SECRET_FILE": "/w"})); err != nil {
			t.Error(err)
		}
	})
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
	same := func(rt http.RoundTripper) http.RoundTripper { return rt }
	read, opts := credential(config{CredentialFile: path}, same)
	if len(opts) != 1 {
		t.Error("a fixed credential is renewed, or its requests are not wrapped")
	}
	for _, c := range []config{{CoreURL: "http://core", TokenFile: "/a"}, {CoreURL: "http://core", BootstrapFile: "/b"}} {
		if _, opts := credential(c, same); len(opts) != 2 {
			t.Errorf("%+v: an exchanged credential with no renewal on a 401, or not wrapped", c)
		}
	}
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
	srv := httptest.NewServer(newMux(ctx, svc, nil))
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

// TestGitHubWebhookRoute: /github/webhook is served only with a webhook secret file, and
// then refuses an unsigned delivery.
func TestGitHubWebhookRoute(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	svc := fleetchat.NewService(nil, idleBus{}, fleetchat.DefaultConfig())
	post := func(h http.Handler) int {
		srv := httptest.NewServer(h)
		defer srv.Close()
		res, err := http.Post(srv.URL+"/github/webhook", "application/json", strings.NewReader("{}"))
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		return res.StatusCode
	}
	off, err := githubWebhook(config{}, nil)
	if err != nil || off != nil {
		t.Fatalf("no secret file: %v %v", off, err)
	}
	if code := post(newMux(ctx, svc, off)); code != http.StatusNotFound {
		t.Errorf("no secret file: %d, want 404", code)
	}
	path := filepath.Join(t.TempDir(), "webhook")
	const secret = "generated-webhook-secret-for-the-test"
	if err := os.WriteFile(path, []byte(secret+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	on, err := githubWebhook(config{GitHubWebhookSecretFile: path}, nil)
	if err != nil || on == nil {
		t.Fatalf("secret file: %v %v", on, err)
	}
	if code := post(newMux(ctx, svc, on)); code != http.StatusUnauthorized {
		t.Errorf("unsigned delivery: %d, want 401", code)
	}
	if err := os.WriteFile(path, []byte(" \n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := githubWebhook(config{GitHubWebhookSecretFile: path}, nil); err == nil {
		t.Error("an empty secret file: no error")
	}
}

func TestGitHubWriter(t *testing.T) {
	if w, err := githubWriter(config{}, nil); w != nil || err != nil {
		t.Errorf("no key file: %v %v", w, err)
	}
	dir := t.TempDir()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	good := filepath.Join(dir, "key.pem")
	if err := os.WriteFile(good, pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)}), 0o600); err != nil {
		t.Fatal(err)
	}
	c := config{GitHubAppID: 1, GitHubInstallationID: 2, GitHubAppKeyFile: good}
	if w, err := githubWriter(c, nil); w == nil || err != nil {
		t.Errorf("a key: %v %v", w, err)
	}
	const notAKey = "not-a-key-but-secret-text"
	bad := filepath.Join(dir, "bad.pem")
	if err := os.WriteFile(bad, []byte(notAKey), 0o600); err != nil {
		t.Fatal(err)
	}
	c.GitHubAppKeyFile = bad
	_, err = githubWriter(c, nil)
	if err == nil || strings.Contains(err.Error(), notAKey) {
		t.Errorf("a bad key: %v", err)
	}
	c.GitHubAppKeyFile = filepath.Join(dir, "missing")
	if _, err := githubWriter(c, nil); err == nil {
		t.Error("a missing key: no error")
	}
}

// TestNoCoreImports: sbfleet and the binary reach core only over HTTP. The one other module
// of the repo they use is telemetry, the OpenTelemetry setup every Go module shares.
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
		if mod != "fleet" && mod != "fleetchat" && mod != "telemetry" {
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
