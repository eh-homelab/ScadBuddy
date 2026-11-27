package ghsecrets

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/crypto/nacl/box"
)

const (
	appID          = 5
	installationID = 77
	ghToken        = "ghs_fake_installation_token_0123456789"
)

var (
	appKeyOnce sync.Once
	appKey     *rsa.PrivateKey
)

// testKey is an RSA key made for the tests; no real key is ever read.
func testKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	appKeyOnce.Do(func() {
		k, err := rsa.GenerateKey(rand.Reader, 2048)
		if err != nil {
			panic(err)
		}
		appKey = k
	})
	return appKey
}

// fakeGitHub is an httptest GitHub: it checks the App's JWT, mints tokens, serves a
// sealed-box public key and keeps what was put.
type fakeGitHub struct {
	t       *testing.T
	appPub  *rsa.PublicKey
	boxPub  *[32]byte
	boxPriv *[32]byte
	now     time.Time

	mu          sync.Mutex
	tokenAsks   []map[string]any
	puts        map[string]map[string]any
	paths       []string
	grantExtra  bool
	updatedAt   string
	missingName string
}

func newFake(t *testing.T) (*fakeGitHub, *httptest.Server) {
	pub, priv, err := box.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	f := &fakeGitHub{t: t, appPub: &testKey(t).PublicKey, boxPub: pub, boxPriv: priv,
		now: time.Unix(1_800_000_000, 0), puts: map[string]map[string]any{}, updatedAt: "2026-11-26T10:11:12Z"}
	srv := httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(srv.Close)
	return f, srv
}

func (f *fakeGitHub) fail(w http.ResponseWriter, code int, format string, args ...any) {
	f.t.Errorf(format, args...)
	http.Error(w, `{"message":"test refused"}`, code)
}

// checkJWT checks the RS256 signature and the claims GitHub requires.
func (f *fakeGitHub) checkJWT(jwt string) error {
	parts := strings.Split(jwt, ".")
	if len(parts) != 3 {
		return errors.New("not three parts")
	}
	header, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return err
	}
	var h map[string]string
	if err := json.Unmarshal(header, &h); err != nil || h["alg"] != "RS256" {
		return errors.New("header is not RS256: " + string(header))
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return err
	}
	sum := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if err := rsa.VerifyPKCS1v15(f.appPub, crypto.SHA256, sum[:], sig); err != nil {
		return errors.New("bad signature")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return err
	}
	var c struct {
		Iat, Exp int64
		Iss      string
	}
	if err := json.Unmarshal(payload, &c); err != nil {
		return err
	}
	if c.Iss != "5" {
		return errors.New("iss is " + c.Iss)
	}
	now := f.now.Unix()
	if c.Iat > now || c.Exp <= now || c.Exp-c.Iat > 600 {
		return errors.New("bad iat/exp: " + string(payload))
	}
	return nil
}

func (f *fakeGitHub) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.paths = append(f.paths, r.Method+" "+r.URL.Path)
	for _, h := range []string{"Traceparent", "Tracestate", "Baggage"} {
		if v := r.Header.Get(h); v != "" {
			f.fail(w, 400, "%s %s: carries %s %q", r.Method, r.URL.Path, h, v)
			return
		}
	}
	if r.Header.Get("X-GitHub-Api-Version") != apiVersion || r.Header.Get("Accept") != "application/vnd.github+json" {
		f.fail(w, 400, "%s %s: headers %v", r.Method, r.URL.Path, r.Header)
		return
	}
	bearer, _ := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	if r.Method == http.MethodPost && r.URL.Path == "/app/installations/77/access_tokens" {
		if err := f.checkJWT(bearer); err != nil {
			f.fail(w, 401, "JWT: %v", err)
			return
		}
		var ask map[string]any
		if err := json.NewDecoder(r.Body).Decode(&ask); err != nil {
			f.fail(w, 400, "token body: %v", err)
			return
		}
		f.tokenAsks = append(f.tokenAsks, ask)
		perms := map[string]any{"metadata": "read"}
		for k, v := range ask["permissions"].(map[string]any) {
			perms[k] = v
		}
		if f.grantExtra {
			perms["contents"] = "write"
		}
		w.WriteHeader(201)
		_ = json.NewEncoder(w).Encode(map[string]any{"token": ghToken, "expires_at": "2026-11-26T11:00:00Z", "permissions": perms})
		return
	}
	if bearer != ghToken {
		f.fail(w, 401, "%s %s: bearer is not the installation token", r.Method, r.URL.Path)
		return
	}
	switch {
	case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/actions/secrets/public-key"):
		_ = json.NewEncoder(w).Encode(map[string]string{"key_id": "kid-1", "key": base64.StdEncoding.EncodeToString(f.boxPub[:])})
	case r.Method == http.MethodPut:
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			f.fail(w, 400, "put body: %v", err)
			return
		}
		f.puts[r.URL.Path] = body
		w.WriteHeader(201)
	case r.Method == http.MethodGet:
		if strings.HasSuffix(r.URL.Path, "/"+f.missingName) && f.missingName != "" {
			w.WriteHeader(404)
			_, _ = io.WriteString(w, `{"message":"Not Found"}`)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"name": "X", "created_at": "2026-01-01T00:00:00Z", "updated_at": f.updatedAt})
	default:
		f.fail(w, 404, "unexpected %s %s", r.Method, r.URL.Path)
	}
}

// open opens a sealed value with the fake's private key.
func (f *fakeGitHub) open(t *testing.T, sealed any) string {
	t.Helper()
	raw, err := base64.StdEncoding.DecodeString(sealed.(string))
	if err != nil {
		t.Fatal(err)
	}
	plain, ok := box.OpenAnonymous(nil, raw, f.boxPub, f.boxPriv)
	if !ok {
		t.Fatal("the sealed value does not open with the server's private key")
	}
	return string(plain)
}

func newWriter(t *testing.T, srv *httptest.Server, f *fakeGitHub, logs *bytes.Buffer) *Writer {
	t.Helper()
	w, err := New(Config{AppID: appID, InstallationID: installationID, PrivateKey: testKey(t), BaseURL: srv.URL,
		Logger: slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug})),
		Now:    func() time.Time { return f.now }})
	if err != nil {
		t.Fatal(err)
	}
	return w
}

func asJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

const value = "sk-ant-oat01-fake-value-for-the-test"

// noSecrets fails when any secret material is in s.
func noSecrets(t *testing.T, what, s string, extra ...string) {
	t.Helper()
	pemKey := string(pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(testKey(t))}))
	for _, bad := range append([]string{ghToken, value, "eyJ", pemKey[40:80]}, extra...) {
		if strings.Contains(s, bad) {
			t.Errorf("%s holds %q:\n%s", what, bad, s)
		}
	}
}

func TestSetRepoSecret(t *testing.T) {
	f, srv := newFake(t)
	var logs bytes.Buffer
	w := newWriter(t, srv, f, &logs)
	if err := w.Set(context.Background(), Target{Owner: "eh-homelab", Repo: "ScadBuddy", Name: "CLAUDE_CODE_OAUTH_TOKEN"}, []byte(value)); err != nil {
		t.Fatal(err)
	}
	if len(f.tokenAsks) != 1 {
		t.Fatalf("token asks: %v", f.tokenAsks)
	}
	if got, want := asJSON(f.tokenAsks[0]), `{"permissions":{"secrets":"write"},"repositories":["ScadBuddy"]}`; got != want {
		t.Errorf("token ask %s, want %s", got, want)
	}
	put := f.puts["/repos/eh-homelab/ScadBuddy/actions/secrets/CLAUDE_CODE_OAUTH_TOKEN"]
	if put == nil {
		t.Fatalf("no put: %v", f.paths)
	}
	if put["key_id"] != "kid-1" || len(put) != 2 {
		t.Errorf("put body %v", put)
	}
	if got := f.open(t, put["encrypted_value"]); got != value {
		t.Errorf("opened %q", got)
	}
	if !strings.Contains(logs.String(), "github secret set") {
		t.Errorf("no log line: %s", logs.String())
	}
	noSecrets(t, "the log", logs.String(), put["encrypted_value"].(string))
}

func TestSetOrgSecret(t *testing.T) {
	for _, tc := range []struct {
		name   string
		target Target
		want   string
	}{
		{"private by default", Target{Owner: "eh-homelab", Name: "TOKEN"}, `"visibility":"private"`},
		{"selected", Target{Owner: "eh-homelab", Name: "TOKEN", Visibility: "selected", SelectedRepositoryIDs: []int64{11, 12}},
			`"selected_repository_ids":[11,12],"visibility":"selected"`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f, srv := newFake(t)
			var logs bytes.Buffer
			if err := newWriter(t, srv, f, &logs).Set(context.Background(), tc.target, []byte(value)); err != nil {
				t.Fatal(err)
			}
			if got, want := asJSON(f.tokenAsks[0]), `{"permissions":{"organization_secrets":"write"}}`; got != want {
				t.Errorf("token ask %s, want %s", got, want)
			}
			put := f.puts["/orgs/eh-homelab/actions/secrets/TOKEN"]
			if put == nil {
				t.Fatalf("no put: %v", f.paths)
			}
			if !strings.Contains(asJSON(put), tc.want) {
				t.Errorf("put %s, want %s", asJSON(put), tc.want)
			}
			if got := f.open(t, put["encrypted_value"]); got != value {
				t.Errorf("opened %q", got)
			}
			noSecrets(t, "the log", logs.String(), put["encrypted_value"].(string))
		})
	}
}

func TestEveryCallMintsItsOwnToken(t *testing.T) {
	f, srv := newFake(t)
	var logs bytes.Buffer
	w := newWriter(t, srv, f, &logs)
	target := Target{Owner: "o", Repo: "r", Name: "N"}
	for range 2 {
		if err := w.Set(context.Background(), target, []byte(value)); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := w.UpdatedAt(context.Background(), target); err != nil {
		t.Fatal(err)
	}
	if len(f.tokenAsks) != 3 {
		t.Errorf("%d token asks for 3 calls", len(f.tokenAsks))
	}
	if got := asJSON(f.tokenAsks[2]); got != `{"permissions":{"secrets":"read"},"repositories":["r"]}` {
		t.Errorf("read token ask %s", got)
	}
}

// injecting adds trace context to every request, as telemetry's Transport does.
type injecting struct{ base http.RoundTripper }

func (i injecting) RoundTrip(r *http.Request) (*http.Response, error) {
	r = r.Clone(r.Context())
	r.Header.Set("traceparent", "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
	r.Header.Set("tracestate", "vendor=value")
	r.Header.Set("baggage", "user=someone")
	return i.base.RoundTrip(r)
}

// TestNoTraceContextToGitHub: a wrap that injects trace context (telemetry's CLIENT span)
// still sends GitHub no traceparent, tracestate or baggage.
func TestNoTraceContextToGitHub(t *testing.T) {
	f, srv := newFake(t)
	var logs bytes.Buffer
	wrapped := 0
	w, err := New(Config{AppID: appID, InstallationID: installationID, PrivateKey: testKey(t), BaseURL: srv.URL,
		Logger: slog.New(slog.NewTextHandler(&logs, nil)), Now: func() time.Time { return f.now },
		WrapTransport: func(base http.RoundTripper) http.RoundTripper { wrapped++; return injecting{base} }})
	if err != nil {
		t.Fatal(err)
	}
	if err := w.Set(context.Background(), Target{Owner: "o", Repo: "r", Name: "N"}, []byte(value)); err != nil {
		t.Fatal(err)
	}
	if _, err := w.UpdatedAt(context.Background(), Target{Owner: "o", Name: "N"}); err != nil {
		t.Fatal(err)
	}
	if wrapped != 1 {
		t.Errorf("wrapped %d times, want 1", wrapped)
	}
}

func TestUpdatedAt(t *testing.T) {
	f, srv := newFake(t)
	var logs bytes.Buffer
	w := newWriter(t, srv, f, &logs)
	got, err := w.UpdatedAt(context.Background(), Target{Owner: "eh-homelab", Name: "TOKEN"})
	if err != nil {
		t.Fatal(err)
	}
	if want := time.Date(2026, 11, 26, 10, 11, 12, 0, time.UTC); !got.Equal(want) {
		t.Errorf("got %v, want %v", got, want)
	}
	if asJSON(f.tokenAsks[0]) != `{"permissions":{"organization_secrets":"read"}}` {
		t.Errorf("token ask %s", asJSON(f.tokenAsks[0]))
	}
	f.missingName = "GONE"
	_, err = w.UpdatedAt(context.Background(), Target{Owner: "eh-homelab", Repo: "r", Name: "GONE"})
	if !errors.Is(err, ErrNotFound) {
		t.Errorf("got %v, want ErrNotFound", err)
	}
	noSecrets(t, "the error", err.Error())
}

func TestRefusesATokenWithMoreThanAsked(t *testing.T) {
	f, srv := newFake(t)
	f.grantExtra = true
	var logs bytes.Buffer
	err := newWriter(t, srv, f, &logs).Set(context.Background(), Target{Owner: "o", Repo: "r", Name: "N"}, []byte(value))
	if err == nil || !strings.Contains(err.Error(), `"contents"`) {
		t.Fatalf("got %v", err)
	}
	for _, p := range f.paths {
		if strings.Contains(p, "/actions/secrets") {
			t.Errorf("used the token: %s", p)
		}
	}
	noSecrets(t, "the error", err.Error())
}

func TestBadTargets(t *testing.T) {
	f, srv := newFake(t)
	var logs bytes.Buffer
	w := newWriter(t, srv, f, &logs)
	for _, target := range []Target{
		{Repo: "r", Name: "N"},
		{Owner: "o", Repo: "r", Name: "GITHUB_TOKEN"},
		{Owner: "o", Repo: "r", Name: "1ABC"},
		{Owner: "o", Repo: "r", Name: "A-B"},
		{Owner: "o", Repo: "r", Name: "N", Visibility: "all"},
		{Owner: "o", Name: "N", Visibility: "public"},
		{Owner: "o", Name: "N", SelectedRepositoryIDs: []int64{1}},
	} {
		if err := w.Set(context.Background(), target, []byte(value)); err == nil {
			t.Errorf("%+v: no error", target)
		}
	}
	if len(f.paths) != 0 {
		t.Errorf("sent %v", f.paths)
	}
}

func TestParsePrivateKey(t *testing.T) {
	k := testKey(t)
	pkcs8, err := x509.MarshalPKCS8PrivateKey(k)
	if err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string][]byte{
		"pkcs1": pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(k)}),
		"pkcs8": pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: pkcs8}),
	} {
		got, err := ParsePrivateKey(data)
		if err != nil || !got.Equal(k) {
			t.Errorf("%s: %v", name, err)
		}
	}
	if _, err := ParsePrivateKey([]byte("not pem")); err == nil {
		t.Error("not pem: no error")
	}
}
