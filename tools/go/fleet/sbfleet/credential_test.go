package sbfleet

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetchat"
)

const proofSecret = "proof-0123456789abcdef0123456789abcdef"

// fakeExchange is core: the exchange route, issuing a new credential each time and
// accepting only the newest on /agents and /changes, unless refuseAll.
type fakeExchange struct {
	mu         sync.Mutex
	exchanges  int
	forms      []map[string]string
	current    string
	issued     []string
	status     int // the exchange's answer; 0 is 200
	retryAfter string
	refuseAll  bool
	calls      []string // the bearer of each /agents or /changes request
}

func credentialN(n int) string {
	return fmt.Sprintf("sbsvc_%016x.%s%c", n, strings.Repeat("A", 42), 'a'+rune(n%26))
}

func (f *fakeExchange) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch r.URL.Path {
	case exchangePath:
		f.exchanges++
		_ = r.ParseForm()
		form := map[string]string{}
		for k := range r.PostForm {
			form[k] = r.PostForm.Get(k)
		}
		f.forms = append(f.forms, form)
		if f.status != 0 {
			if f.retryAfter != "" {
				w.Header().Set("Retry-After", f.retryAfter)
			}
			w.WriteHeader(f.status)
			return
		}
		f.current = credentialN(len(f.issued) + 1)
		f.issued = append(f.issued, f.current)
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(w, `{"access_token":%q,"token_type":"Bearer","expires_in":900}`, f.current)
	case base + "/agents", base + "/changes":
		bearer := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		f.calls = append(f.calls, bearer)
		if f.refuseAll || bearer != f.current {
			w.Header().Set("Content-Type", "application/problem+json")
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = io.WriteString(w, `{"detail":"refused"}`)
			return
		}
		if r.URL.Path == base+"/changes" {
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = io.WriteString(w, "event: ready\ndata: {\"resync\":false,\"reconnect\":false}\nid: 1\n\n")
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"agents":[]}`)
	default:
		http.NotFound(w, r)
	}
}

// revoke makes core refuse every credential issued so far.
func (f *fakeExchange) revoke() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.current = "revoked"
}

func (f *fakeExchange) snapshot() (int, []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.exchanges, append([]string(nil), f.calls...)
}

type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) now() time.Time { c.mu.Lock(); defer c.mu.Unlock(); return c.t }
func (c *clock) add(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

// captureLogs sends slog's default output to a buffer for the test.
func captureLogs(t *testing.T) *syncBuffer {
	t.Helper()
	buf := &syncBuffer{}
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return buf
}

type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string { s.mu.Lock(); defer s.mu.Unlock(); return s.b.String() }

func newExchange(t *testing.T, proof Proof) (*Exchange, *fakeExchange, *clock, string) {
	t.Helper()
	f := &fakeExchange{}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	file := filepath.Join(t.TempDir(), "proof")
	if err := os.WriteFile(file, []byte(proofSecret+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	c := &clock{t: time.Unix(1_800_000_000, 0)}
	e := NewExchange(srv.URL+"/", file, proof)
	e.now = c.now
	return e, f, c, srv.URL
}

func TestExchangeSendsTheProof(t *testing.T) {
	for _, proof := range []Proof{ProofKubernetes, ProofBootstrap} {
		e, f, _, _ := newExchange(t, proof)
		token, err := e.Token()
		if err != nil {
			t.Fatal(err)
		}
		if token != credentialN(1) {
			t.Errorf("token %q", token)
		}
		want := map[string]string{
			"grant_type": grantType, "subject_token": proofSecret,
			"subject_token_type": string(proof), "client_id": "fleet",
		}
		if fmt.Sprint(f.forms[0]) != fmt.Sprint(want) {
			t.Errorf("form %v, want %v", f.forms[0], want)
		}
	}
}

func TestExchangeRenewsAtHalfLife(t *testing.T) {
	e, f, c, _ := newExchange(t, ProofKubernetes)
	first, err := e.Token()
	if err != nil {
		t.Fatal(err)
	}
	c.add(449 * time.Second)
	if got, _ := e.Token(); got != first {
		t.Errorf("before half-life: %q, want %q", got, first)
	}
	if n, _ := f.snapshot(); n != 1 {
		t.Fatalf("before half-life: %d exchanges, want 1", n)
	}
	c.add(2 * time.Second) // 451 s of 900
	second, err := e.Token()
	if err != nil {
		t.Fatal(err)
	}
	if n, _ := f.snapshot(); n != 2 || second == first {
		t.Errorf("after half-life: %d exchanges and %q, want 2 and a new credential", n, second)
	}
}

func TestExchangeBacksOffAndKeepsAValidCredential(t *testing.T) {
	e, f, c, _ := newExchange(t, ProofKubernetes)
	first, _ := e.Token()
	f.mu.Lock()
	f.status = http.StatusInternalServerError
	f.mu.Unlock()
	c.add(500 * time.Second) // past half-life, before expiry
	if got, err := e.Token(); err != nil || got != first {
		t.Fatalf("a failed renewal: %q, %v; want the held credential", got, err)
	}
	if got, _ := e.Token(); got != first {
		t.Fatal("the held credential")
	}
	if n, _ := f.snapshot(); n != 2 {
		t.Errorf("%d exchanges, want 2: no retry inside the 5 s back-off", n)
	}
	c.add(5 * time.Second)
	_, _ = e.Token()
	if n, _ := f.snapshot(); n != 3 {
		t.Errorf("%d exchanges, want 3 after the back-off", n)
	}
	c.add(400 * time.Second) // past expiry
	if _, err := e.Token(); err == nil || !strings.Contains(err.Error(), "HTTP 500") {
		t.Errorf("after expiry: %v, want the exchange's error", err)
	}
}

func TestExchangeRateLimited(t *testing.T) {
	e, f, c, _ := newExchange(t, ProofKubernetes)
	f.status, f.retryAfter = http.StatusTooManyRequests, "30"
	if _, err := e.Token(); err == nil {
		t.Fatal("a credential from a 429")
	}
	c.add(29 * time.Second)
	_, _ = e.Token()
	if n, _ := f.snapshot(); n != 1 {
		t.Errorf("%d exchanges inside Retry-After, want 1", n)
	}
}

func TestUnauthorizedRenewsAndRetriesOnce(t *testing.T) {
	e, f, c, url := newExchange(t, ProofKubernetes)
	s := NewStore(url, e.Token, OnUnauthorized(e.Invalidate))
	if _, err := s.Agents(context.Background()); err != nil {
		t.Fatal(err)
	}
	c.add(time.Minute) // past reexchangeMin, before half-life
	f.revoke()
	if _, err := s.Agents(context.Background()); err != nil {
		t.Fatalf("after a 401: %v, want the retry to succeed", err)
	}
	n, calls := f.snapshot()
	want := []string{credentialN(1), credentialN(1), credentialN(2)}
	if n != 2 || fmt.Sprint(calls) != fmt.Sprint(want) {
		t.Errorf("%d exchanges, calls %v; want 2 and %v", n, calls, want)
	}

	// A 401 the new credential gets too is not retried again.
	f.mu.Lock()
	f.refuseAll = true
	f.mu.Unlock()
	c.add(time.Minute)
	_, err := s.Agents(context.Background())
	if err == nil || !strings.Contains(err.Error(), "401") {
		t.Errorf("a refused retry: %v, want core's 401", err)
	}
	_, calls = f.snapshot()
	if len(calls) != 5 {
		t.Errorf("%d calls, want 5: one retry only", len(calls))
	}
}

func TestUnauthorizedRenewsTheStream(t *testing.T) {
	e, f, c, url := newExchange(t, ProofKubernetes)
	if _, err := e.Token(); err != nil {
		t.Fatal(err)
	}
	c.add(time.Minute)
	f.revoke()
	b := newBus(url, e.Token, OnUnauthorized(e.Invalidate))
	last := ""
	var got []fleetchat.Event
	framed, err := b.follow(context.Background(), &last, func(ev fleetchat.Event) { got = append(got, ev) })
	if err != nil || !framed || len(got) != 1 || !got[0].Resync {
		t.Errorf("follow: %v, %v, %+v; want the renewed stream's resync", framed, err, got)
	}
	if n, _ := f.snapshot(); n != 2 {
		t.Errorf("%d exchanges, want 2", n)
	}
}

func TestExchangeNeverLogsSecrets(t *testing.T) {
	logs := captureLogs(t)
	e, f, c, url := newExchange(t, ProofBootstrap)
	s := NewStore(url, e.Token, OnUnauthorized(e.Invalidate))
	_, _ = s.Agents(context.Background())
	c.add(time.Minute)
	f.revoke()
	_, _ = s.Agents(context.Background())
	f.mu.Lock()
	f.status = http.StatusBadRequest
	f.mu.Unlock()
	c.add(time.Hour)
	_, err := e.Token()
	f.mu.Lock()
	f.status, f.retryAfter = http.StatusTooManyRequests, "1"
	f.mu.Unlock()
	c.add(time.Minute)
	_, err2 := e.Token()
	out := logs.String() + fmt.Sprint(err) + fmt.Sprint(err2)
	if !strings.Contains(out, "exchanged the fleet service credential") {
		t.Fatalf("the logs saw no exchange: %s", out)
	}
	for _, secret := range append([]string{proofSecret}, f.issued...) {
		if strings.Contains(out, secret) {
			t.Errorf("a secret reached the logs or an error: %q in %s", secret, out)
		}
	}
}

func TestExchangeProofFileMissing(t *testing.T) {
	e := NewExchange("http://127.0.0.1:1", filepath.Join(t.TempDir(), "none"), ProofKubernetes)
	if _, err := e.Token(); err == nil || !strings.Contains(err.Error(), "not found") {
		t.Errorf("%v, want the missing proof", err)
	}
}

func TestExchangeNotUpSetsNoBackOff(t *testing.T) {
	proof := filepath.Join(t.TempDir(), "proof")
	if err := os.WriteFile(proof, []byte("p"), 0o600); err != nil {
		t.Fatal(err)
	}
	// A port just closed refuses the connection, as an API that is not up yet does.
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	l.Close()
	e := NewExchange("http://"+addr, proof, ProofKubernetes)
	if _, err := e.Token(); err == nil || !strings.Contains(err.Error(), "not up yet") {
		t.Fatalf("%v, want the API not up yet", err)
	}
	if !e.nextTry.IsZero() {
		t.Errorf("nextTry %v, want no back-off", e.nextTry)
	}
}
