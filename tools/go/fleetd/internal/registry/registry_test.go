package registry

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/discover"
)

const (
	machineID = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	sessionID = "aaaaaaaa-1111-4111-8111-111111111111"
)

type seen struct {
	method, path, auth, contentType string
	body                            map[string]any
}

type fake struct {
	mu   sync.Mutex
	got  []seen
	next func(w http.ResponseWriter)
}

func (f *fake) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b, _ := io.ReadAll(r.Body)
	var body map[string]any
	_ = json.Unmarshal(b, &body)
	f.mu.Lock()
	f.got = append(f.got, seen{r.Method, r.URL.EscapedPath(), r.Header.Get("Authorization"), r.Header.Get("Content-Type"), body})
	next := f.next
	f.mu.Unlock()
	if next != nil {
		next(w)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{}`))
}

func tokenFile(t *testing.T, token string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(p, []byte(token+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

// tlsClient is a client of an https fake.
func tlsClient(t *testing.T, f *fake, token string) (*Client, string) {
	t.Helper()
	srv := httptest.NewTLSServer(f)
	t.Cleanup(srv.Close)
	file := tokenFile(t, token)
	c, err := New(srv.URL+"/api/v1", file, WithBaseTransport(srv.Client().Transport))
	if err != nil {
		t.Fatal(err)
	}
	return c, file
}

func TestPutMachineAndSession(t *testing.T) {
	f := &fake{}
	c, file := tlsClient(t, f, "sbpat_DUMMY1")
	ctx := context.Background()
	m := discover.Machine{ID: machineID, Hostname: "spark", OS: "linux/amd64", OSUser: "elan", ServiceVersion: "dev"}
	if err := c.PutMachine(ctx, m); err != nil {
		t.Fatal(err)
	}
	// The token file is read again on each request.
	if err := os.WriteFile(file, []byte("sbpat_DUMMY2"), 0o600); err != nil {
		t.Fatal(err)
	}
	row := discover.Row{SessionID: sessionID, PID: 100, ProcStart: "5000", Cwd: "/w", ConfigDir: "/h/.claude",
		GitRemote: "https://github.com/o/r.git", Kind: "interactive"}
	if err := c.PutSession(ctx, machineID, row); err != nil {
		t.Fatal(err)
	}
	if len(f.got) != 2 {
		t.Fatalf("%d requests", len(f.got))
	}
	mreq, sreq := f.got[0], f.got[1]
	if mreq.method != "PUT" || mreq.path != "/api/v1/internal/crud/fleet-rotator/machines/"+machineID {
		t.Errorf("machine: %s %s", mreq.method, mreq.path)
	}
	if mreq.auth != "Bearer sbpat_DUMMY1" || sreq.auth != "Bearer sbpat_DUMMY2" {
		t.Errorf("auth %q then %q", mreq.auth, sreq.auth)
	}
	if mreq.contentType != "application/json" {
		t.Errorf("content type %q", mreq.contentType)
	}
	wantMachine := map[string]any{"hostname": "spark", "os": "linux/amd64", "os_user": "elan", "service_version": "dev"}
	if !equal(mreq.body, wantMachine) {
		t.Errorf("machine body %v", mreq.body)
	}
	if sreq.method != "PUT" || sreq.path != "/api/v1/internal/crud/fleet-rotator/machines/"+machineID+"/sessions/"+sessionID {
		t.Errorf("session: %s %s", sreq.method, sreq.path)
	}
	wantSession := map[string]any{"pid": float64(100), "proc_start": "5000", "cwd": "/w", "config_dir": "/h/.claude",
		"git_remote": "https://github.com/o/r.git", "kind": "interactive"}
	if !equal(sreq.body, wantSession) {
		t.Errorf("session body %v", sreq.body)
	}
}

func equal(a, b map[string]any) bool {
	x, _ := json.Marshal(a)
	y, _ := json.Marshal(b)
	return string(x) == string(y)
}

func TestHTTPSOnly(t *testing.T) {
	file := tokenFile(t, "sbpat_DUMMY")
	for _, api := range []string{"http://scadbuddy.example.com/api/v1", "http://10.0.0.5/api/v1", "ftp://x/api/v1", "not a url"} {
		if _, err := New(api, file); err == nil {
			t.Errorf("New(%q) was accepted", api)
		}
	}
	for _, api := range []string{"https://scadbuddy.example.com/api/v1", "http://localhost:8000/api/v1", "http://127.0.0.1/api/v1", "http://[::1]:8/api/v1"} {
		if _, err := New(api, file); err != nil {
			t.Errorf("New(%q): %v", api, err)
		}
	}
	if _, err := New("https://x/api/v1", ""); err == nil {
		t.Error("no token file was accepted")
	}
}

func TestLocalhostHTTP(t *testing.T) {
	f := &fake{}
	srv := httptest.NewServer(f)
	defer srv.Close()
	c, err := New(srv.URL+"/api/v1", tokenFile(t, "sbpat_LOCAL"))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.PutMachine(context.Background(), discover.Machine{ID: machineID}); err != nil {
		t.Fatal(err)
	}
	if f.got[0].auth != "Bearer sbpat_LOCAL" {
		t.Fatal(f.got[0].auth)
	}
}

func TestEmptyTokenFileSendsNothing(t *testing.T) {
	f := &fake{}
	c, file := tlsClient(t, f, "x")
	_ = os.WriteFile(file, []byte("  \n"), 0o600)
	if err := c.PutMachine(context.Background(), discover.Machine{ID: machineID}); err == nil {
		t.Fatal("an empty token was sent")
	}
	if len(f.got) != 0 {
		t.Fatal("a request went out")
	}
}

func TestNoRedirect(t *testing.T) {
	f := &fake{next: func(w http.ResponseWriter) {
		w.Header().Set("Location", "http://127.0.0.1:1/steal")
		w.WriteHeader(http.StatusTemporaryRedirect)
	}}
	c, _ := tlsClient(t, f, "sbpat_DUMMY")
	if err := c.PutMachine(context.Background(), discover.Machine{ID: machineID}); err == nil {
		t.Fatal("a redirect was followed or taken as success")
	}
	if len(f.got) != 1 {
		t.Fatalf("%d requests", len(f.got))
	}
}

func problem(status int, body string) func(http.ResponseWriter) {
	return func(w http.ResponseWriter) {
		w.Header().Set("Content-Type", "application/problem+json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}
}

// An older core pod in a rollout answers 404 for a route it does not have yet: no reason,
// or Go's plain not-found text. That is transient.
func TestRouteMissing404(t *testing.T) {
	for _, body := range []string{
		`{"type":"about:blank","title":"Not Found","status":404,"detail":"Not Found","instance":"/x"}`,
		"404 page not found\n",
		"",
	} {
		f := &fake{next: problem(404, body)}
		c, _ := tlsClient(t, f, "sbpat_DUMMY")
		err := c.PutSession(context.Background(), machineID, discover.Row{SessionID: sessionID})
		if !errors.Is(err, ErrRouteMissing) {
			t.Errorf("body %q: %v, want ErrRouteMissing", body, err)
		}
		if errors.Is(err, ErrUnknownMachine) {
			t.Errorf("body %q: taken for unknown-machine", body)
		}
	}
}

// A 404 with a reason is a real answer to act on.
func TestReasoned404(t *testing.T) {
	f := &fake{next: problem(404, `{"type":"about:blank","title":"Not Found","status":404,"detail":"no fleet machine has that id: register it first","instance":"/x","reason":"unknown-machine"}`)}
	c, _ := tlsClient(t, f, "sbpat_DUMMY")
	err := c.PutSession(context.Background(), machineID, discover.Row{SessionID: sessionID})
	if !errors.Is(err, ErrUnknownMachine) || errors.Is(err, ErrRouteMissing) {
		t.Fatalf("%v, want ErrUnknownMachine", err)
	}

	f.next = problem(404, `{"status":404,"detail":"no fleet session has that id","reason":"unknown-session"}`)
	err = c.PutSession(context.Background(), machineID, discover.Row{SessionID: sessionID})
	var p *ProblemError
	if !errors.As(err, &p) || p.Reason != "unknown-session" || errors.Is(err, ErrRouteMissing) {
		t.Fatalf("%v", err)
	}
}

func TestProblemNamesReason(t *testing.T) {
	f := &fake{next: problem(403, `{"status":403,"detail":"another machine's proof registered that machine","reason":"machine-of-another-proof"}`)}
	c, _ := tlsClient(t, f, "sbpat_DUMMY")
	err := c.PutMachine(context.Background(), discover.Machine{ID: machineID})
	var p *ProblemError
	if !errors.As(err, &p) || p.Status != 403 || p.Reason != "machine-of-another-proof" {
		t.Fatalf("%v", err)
	}
	if strings.Contains(err.Error(), "sbpat_") {
		t.Fatal("the error names the token")
	}
}
