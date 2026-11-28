package update

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	oldSHA = "1111111111111111111111111111111111111111"
	newSHA = "2222222222222222222222222222222222222222"
)

// fakeGitHub serves the releases list, the compare API and the assets.
type fakeGitHub struct {
	t        *testing.T
	srv      *httptest.Server
	key      ed25519.PrivateKey
	sha      string
	created  time.Time
	status   string // compare status
	binary   []byte
	sums     []byte // nil: built from binary
	sig      []byte // nil: signed with key
	calls    atomic.Int32
	failList atomic.Int32 // answer 502 this many times first
	limited  bool
	extra    []map[string]any
}

func newFake(t *testing.T) *fakeGitHub {
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	f := &fakeGitHub{t: t, key: key, sha: newSHA, created: time.Date(2026, 11, 28, 0, 0, 0, 0, time.UTC), status: "ahead", binary: []byte("new fleetd binary")}
	f.srv = httptest.NewTLSServer(http.HandlerFunc(f.serve))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeGitHub) asset(name string) string { return f.srv.URL + "/dl/" + name }

func (f *fakeGitHub) sumsBody() []byte {
	if f.sums != nil {
		return f.sums
	}
	h := sha256.Sum256(f.binary)
	return []byte(fmt.Sprintf("%s  fleetd-linux-amd64\n%s  fleetd-linux-arm64\n", hex.EncodeToString(h[:]), strings.Repeat("0", 64)))
}

func (f *fakeGitHub) serve(w http.ResponseWriter, r *http.Request) {
	f.calls.Add(1)
	if r.Header.Get("traceparent") != "" {
		f.t.Errorf("a trace header went to GitHub")
	}
	switch {
	case r.URL.Path == "/repos/o/r/releases":
		if f.limited {
			w.Header().Set("X-RateLimit-Remaining", "0")
			w.Header().Set("X-RateLimit-Reset", fmt.Sprint(time.Now().Add(time.Hour).Unix()))
			w.WriteHeader(http.StatusForbidden)
			return
		}
		if f.failList.Load() > 0 {
			f.failList.Add(-1)
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		rel := map[string]any{
			"tag_name": "fleetd-" + f.sha[:7], "target_commitish": f.sha, "created_at": f.created,
			"draft": false, "prerelease": false,
			"assets": []map[string]any{
				{"name": "fleetd-linux-amd64", "browser_download_url": f.asset("fleetd-linux-amd64")},
				{"name": "SHA256SUMS", "browser_download_url": f.asset("SHA256SUMS")},
				{"name": "SHA256SUMS.sig", "browser_download_url": f.asset("SHA256SUMS.sig")},
			},
		}
		list := append([]map[string]any{rel}, f.extra...)
		_ = json.NewEncoder(w).Encode(list)
	case strings.HasPrefix(r.URL.Path, "/repos/o/r/compare/"):
		_ = json.NewEncoder(w).Encode(map[string]any{"status": f.status})
	case r.URL.Path == "/dl/fleetd-linux-amd64":
		_, _ = w.Write(f.binary)
	case r.URL.Path == "/dl/SHA256SUMS":
		_, _ = w.Write(f.sumsBody())
	case r.URL.Path == "/dl/SHA256SUMS.sig":
		sig := f.sig
		if sig == nil {
			sig = ed25519.Sign(f.key, f.sumsBody())
		}
		_, _ = io.WriteString(w, base64.StdEncoding.EncodeToString(sig)+"\n")
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

type harness struct {
	f       *fakeGitHub
	exe     string
	ran     []string
	version string // what the fake `<new> version` prints; "" = the release's sha
	logs    bytes.Buffer
}

func newHarness(t *testing.T) *harness {
	h := &harness{f: newFake(t)}
	dir := t.TempDir()
	h.exe = filepath.Join(dir, "fleetd")
	if err := os.WriteFile(h.exe, []byte("old fleetd binary"), 0o755); err != nil {
		t.Fatal(err)
	}
	return h
}

func (h *harness) updater(t *testing.T, current string) (*Updater, error) {
	host, _ := url.Parse(h.f.srv.URL)
	return New(Config{
		Current:   current,
		Exe:       h.exe,
		API:       h.f.srv.URL,
		Repo:      "o/r",
		PublicKey: h.f.key.Public().(ed25519.PublicKey),
		HTTP:      h.f.srv.Client(),
		AllowHost: func(s string) bool { return s == host.Hostname() },
		RunVersion: func(_ context.Context, path string) (string, error) {
			h.ran = append(h.ran, path)
			b, err := os.ReadFile(path)
			if err != nil {
				return "", err
			}
			if !bytes.Equal(b, h.f.binary) {
				return "", errors.New("ran the wrong file")
			}
			if h.version != "" {
				return h.version + "\n", nil
			}
			return h.f.sha + "\n", nil
		},
		GOOS:    "linux",
		GOARCH:  "amd64",
		Backoff: time.Millisecond,
		Log:     slog.New(slog.NewTextHandler(&h.logs, nil)),
	})
}

func (h *harness) mustRead(t *testing.T, p string) string {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// noStrays fails when a temporary download is left next to the binary.
func (h *harness) noStrays(t *testing.T) {
	t.Helper()
	ents, _ := os.ReadDir(filepath.Dir(h.exe))
	for _, e := range ents {
		if e.Name() != "fleetd" && e.Name() != "fleetd.old" {
			t.Errorf("stray file %s", e.Name())
		}
	}
}

func TestHappyPathSwapsAndKeepsOld(t *testing.T) {
	h := newHarness(t)
	u, err := h.updater(t, oldSHA)
	if err != nil {
		t.Fatal(err)
	}
	done, err := u.Check(context.Background())
	if err != nil || !done {
		t.Fatalf("Check = %v, %v", done, err)
	}
	if got := h.mustRead(t, h.exe); got != string(h.f.binary) {
		t.Errorf("binary %q", got)
	}
	if got := h.mustRead(t, h.exe+".old"); got != "old fleetd binary" {
		t.Errorf(".old %q", got)
	}
	st, _ := os.Stat(h.exe)
	if st.Mode().Perm() != 0o755 {
		t.Errorf("mode %v", st.Mode())
	}
	if len(h.ran) != 1 {
		t.Errorf("smoke runs %v", h.ran)
	}
	h.noStrays(t)
}

func TestBadSignatureRefused(t *testing.T) {
	for name, mutate := range map[string]func(f *fakeGitHub){
		"tampered": func(f *fakeGitHub) {
			sig := ed25519.Sign(f.key, f.sumsBody())
			sig[0] ^= 1
			f.sig = sig
		},
		"wrong key": func(f *fakeGitHub) {
			_, other, _ := ed25519.GenerateKey(rand.Reader)
			f.sig = ed25519.Sign(other, f.sumsBody())
		},
		"short": func(f *fakeGitHub) { f.sig = []byte("short") },
	} {
		t.Run(name, func(t *testing.T) {
			h := newHarness(t)
			mutate(h.f)
			u, _ := h.updater(t, oldSHA)
			done, err := u.Check(context.Background())
			if done || !errors.Is(err, ErrSignature) {
				t.Fatalf("Check = %v, %v", done, err)
			}
			if got := h.mustRead(t, h.exe); got != "old fleetd binary" {
				t.Errorf("binary changed: %q", got)
			}
			if len(h.ran) != 0 {
				t.Errorf("ran an unverified binary")
			}
			h.noStrays(t)
		})
	}
}

func TestChecksumMismatchRefused(t *testing.T) {
	h := newHarness(t)
	h.f.sums = []byte(strings.Repeat("a", 64) + "  fleetd-linux-amd64\n")
	u, _ := h.updater(t, oldSHA)
	done, err := u.Check(context.Background())
	if done || !errors.Is(err, ErrChecksum) {
		t.Fatalf("Check = %v, %v", done, err)
	}
	if got := h.mustRead(t, h.exe); got != "old fleetd binary" {
		t.Errorf("binary changed: %q", got)
	}
	if len(h.ran) != 0 {
		t.Errorf("ran a binary that failed its checksum")
	}
	h.noStrays(t)
}

func TestSmokeTestFailureKeepsOld(t *testing.T) {
	h := newHarness(t)
	h.version = oldSHA // the new binary reports the wrong version
	u, _ := h.updater(t, oldSHA)
	done, err := u.Check(context.Background())
	if done || !errors.Is(err, ErrSmokeTest) {
		t.Fatalf("Check = %v, %v", done, err)
	}
	if got := h.mustRead(t, h.exe); got != "old fleetd binary" {
		t.Errorf("binary changed: %q", got)
	}
	if _, err := os.Stat(h.exe + ".old"); !os.IsNotExist(err) {
		t.Errorf(".old exists: %v", err)
	}
	h.noStrays(t)
}

func TestDevBuildNeverUpdates(t *testing.T) {
	for _, v := range []string{"", "dev", "dev+123456789abc", "v1.2.3", "ABCDEF" + oldSHA[6:], oldSHA[:39]} {
		h := newHarness(t)
		if _, err := h.updater(t, v); !errors.Is(err, ErrDevBuild) {
			t.Errorf("New(%q) = %v", v, err)
		}
		if h.f.calls.Load() != 0 {
			t.Errorf("%q: called GitHub", v)
		}
	}
}

func TestNewerOrOlder(t *testing.T) {
	for _, tc := range []struct {
		status  string
		current string
		want    bool
	}{
		{"ahead", oldSHA, true},
		{"behind", oldSHA, false},
		{"diverged", oldSHA, false},
		{"identical", oldSHA, false},
		{"ahead", newSHA, false}, // the running release is the newest
	} {
		h := newHarness(t)
		h.f.status = tc.status
		u, _ := h.updater(t, tc.current)
		done, err := u.Check(context.Background())
		if err != nil || done != tc.want {
			t.Errorf("%s from %s: Check = %v, %v", tc.status, tc.current[:4], done, err)
		}
		if !tc.want && h.mustRead(t, h.exe) != "old fleetd binary" {
			t.Errorf("%s: binary changed", tc.status)
		}
	}
}

func TestNewestByCreatedAt(t *testing.T) {
	h := newHarness(t)
	// An older fleetd release, a non-fleetd release and a draft come after it in the list.
	h.f.extra = []map[string]any{
		{"tag_name": "fleetd-3333333", "target_commitish": strings.Repeat("3", 40), "created_at": h.f.created.Add(-time.Hour)},
		{"tag_name": "v9.9.9", "target_commitish": strings.Repeat("4", 40), "created_at": h.f.created.Add(time.Hour)},
		{"tag_name": "fleetd-5555555", "target_commitish": strings.Repeat("5", 40), "created_at": h.f.created.Add(time.Hour), "draft": true},
	}
	u, _ := h.updater(t, oldSHA)
	if done, err := u.Check(context.Background()); err != nil || !done {
		t.Fatalf("Check = %v, %v", done, err)
	}
}

func TestTransientErrorsRetry(t *testing.T) {
	h := newHarness(t)
	h.f.failList.Store(2)
	u, _ := h.updater(t, oldSHA)
	if done, err := u.Check(context.Background()); err != nil || !done {
		t.Fatalf("Check = %v, %v", done, err)
	}
}

func TestRateLimitHonoured(t *testing.T) {
	h := newHarness(t)
	h.f.limited = true
	u, _ := h.updater(t, oldSHA)
	if _, err := u.Check(context.Background()); err == nil {
		t.Fatal("no error under a rate limit")
	}
	n := h.f.calls.Load()
	if n != 1 {
		t.Errorf("retried under a rate limit: %d calls", n)
	}
	if _, err := u.Check(context.Background()); err != nil {
		t.Fatalf("second check: %v", err)
	}
	if h.f.calls.Load() != n {
		t.Error("called GitHub again before the reset")
	}
}

func TestDownloadOffAllowedHostsRefused(t *testing.T) {
	h := newHarness(t)
	u, _ := h.updater(t, oldSHA)
	u.cfg.AllowHost = func(string) bool { return false }
	done, err := u.Check(context.Background())
	if done || err == nil || !strings.Contains(err.Error(), "not allowed") {
		t.Fatalf("Check = %v, %v", done, err)
	}
}

func TestDefaultAllowHost(t *testing.T) {
	for host, want := range map[string]bool{
		"github.com": true, "objects.githubusercontent.com": true, "release-assets.githubusercontent.com": true,
		"evil.com": false, "github.com.evil.com": false, "raw.githubusercontent.com": false,
	} {
		if got := DefaultAllowHost(host); got != want {
			t.Errorf("%s: %v", host, got)
		}
	}
}

func TestLoopLogsOncePerRelease(t *testing.T) {
	h := newHarness(t)
	h.f.sums = []byte(strings.Repeat("a", 64) + "  fleetd-linux-amd64\n")
	u, _ := h.updater(t, oldSHA)
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		for h.f.calls.Load() < 20 {
			time.Sleep(time.Millisecond)
		}
		cancel()
	}()
	if u.Loop(ctx, time.Millisecond) {
		t.Fatal("Loop reported an update")
	}
	if n := strings.Count(h.logs.String(), "checksum"); n != 1 {
		t.Errorf("logged %d times:\n%s", n, h.logs.String())
	}
}

func TestPublicKeyDecodes(t *testing.T) {
	if k := PublicKey(); len(k) != ed25519.PublicKeySize {
		t.Fatalf("key length %d", len(k))
	}
}
