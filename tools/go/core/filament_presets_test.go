package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/filamentpresets"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// presetKeys is a KEK of zeros, as SCADBUDDY_SECRET_KEY_FILE gives one.
func presetKeys(t *testing.T) *seal.Keys {
	t.Helper()
	file := filepath.Join(t.TempDir(), "kek")
	if err := os.WriteFile(file, []byte(base64.StdEncoding.EncodeToString(make([]byte, 32))+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	keys, err := seal.LoadKeys(func(name string) string { return map[string]string{seal.Variable: file}[name] })
	if err != nil {
		t.Fatal(err)
	}
	return keys
}

// presetStore serves objects at path-style paths and counts the GETs, as S3 answers a
// presigned GET (internal/blobstore's read_test.go readStore).
type presetStore struct {
	mu      sync.Mutex
	objects map[string][]byte
	gets    int
}

func (s *presetStore) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.gets++
	body, ok := s.objects[r.URL.Path]
	if !ok {
		w.WriteHeader(http.StatusNotFound)
		return
	}
	_, _ = w.Write(body)
}

// presetEnv is the store's settings, from the environment, for a store at url.
func presetEnv(url string) func(string) (string, bool) {
	env := map[string]string{
		"SCADBUDDY_STORE_BACKEND":              "s3",
		"SCADBUDDY_STORE_S3_ENDPOINT":          url,
		"SCADBUDDY_STORE_S3_BUCKET":            "sb-test",
		"SCADBUDDY_STORE_S3_PREFIX":            "t",
		"SCADBUDDY_STORE_S3_ACCESS_KEY_ID":     "testing",
		"SCADBUDDY_STORE_S3_SECRET_ACCESS_KEY": "testing",
	}
	return func(name string) (string, bool) {
		v, ok := env[name]
		return v, ok
	}
}

// keepPreset writes a one-version preset whose document is in store as Python keeps it:
// the zip with member "file", its store_blobs row, the preset row and its version.
func keepPreset(t *testing.T, pool *pgxpool.Pool, store *presetStore, doc string) int64 {
	t.Helper()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	f, _ := w.Create("file")
	_, _ = io.WriteString(f, doc)
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	docSum := sha256.Sum256([]byte(doc))
	docSHA := hex.EncodeToString(docSum[:])
	zipSum := sha256.Sum256(buf.Bytes())
	zipSHA := hex.EncodeToString(zipSum[:])
	key, object := "attachment-"+docSHA, "t/attachment/"+zipSHA
	store.mu.Lock()
	store.objects["/sb-test/"+object] = buf.Bytes()
	store.mu.Unlock()
	ctx := context.Background()
	if _, err := pool.Exec(ctx, `INSERT INTO store_blobs (key, sha256, kind, backend, backend_id, size)
		VALUES ($1, $2, 'attachment', 's3', $3, $4)`, key, zipSHA, object, buf.Len()); err != nil {
		t.Fatal(err)
	}
	var id int64
	if err := pool.QueryRow(ctx, `INSERT INTO filament_presets (name, source, blob_key, sha256)
		VALUES ('PLA', 'manual', $1, $2) RETURNING id`, key, docSHA).Scan(&id); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO filament_preset_versions (preset_id, version, blob_key, sha256)
		VALUES ($1, 1, $2, $3)`, id, key, docSHA); err != nil {
		t.Fatal(err)
	}
	return id
}

func send(t *testing.T, handler http.Handler, method, path string) (int, string) {
	status, body, _ := sendCache(t, handler, method, path)
	return status, body
}

func sendCache(t *testing.T, handler http.Handler, method, path string) (int, string, string) {
	t.Helper()
	server := httptest.NewServer(handler)
	defer server.Close()
	req, _ := http.NewRequest(method, server.URL+path, nil)
	req.Header.Set(auth.RequestHeader, "1")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	body, _ := io.ReadAll(res.Body)
	return res.StatusCode, string(body), res.Header.Get("Cache-Control")
}

var teapot = http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusTeapot) })

func TestThePresetsRoutesUseTheGate(t *testing.T) {
	pool := testdb.New(t)
	keys := presetKeys(t)
	enforce, err := newRouter(pool, teapot, keys, filamentpresets.Area, "enforce", nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if status, body, cache := sendCache(t, enforce, http.MethodGet, "/api/v1/filament/presets"); status != http.StatusUnauthorized ||
		!strings.Contains(body, "signed-out") || cache != "no-store" {
		t.Errorf("enforce, signed out: %d %s, Cache-Control %q", status, body, cache)
	}
	rec := recordTelemetry(t)
	observe, err := newRouter(pool, teapot, keys, filamentpresets.Area, "observe", nil, rec.tel.Meters, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if status, body := send(t, observe, http.MethodGet, "/api/v1/filament/presets"); status != http.StatusOK || body != "[]" {
		t.Errorf("observe: %d %s", status, body)
	}
	if _, ok := rec.metrics(t)[auth.WouldRefuseMetric]; !ok {
		t.Error("observe counted nothing")
	}
}

func TestTheDetailReadsItsDocumentFromTheStore(t *testing.T) {
	pool := testdb.New(t)
	store := &presetStore{objects: map[string][]byte{}}
	s3 := httptest.NewServer(store)
	t.Cleanup(s3.Close)
	id := keepPreset(t, pool, store, `{"name":"PLA","printhost_password":"p","nozzle_temperature":["220"]}`)
	router, err := newRouter(pool, teapot, presetKeys(t), filamentpresets.Area, "", nil, nil, presetEnv(s3.URL), nil)
	if err != nil {
		t.Fatal(err)
	}
	status, body := send(t, router, http.MethodGet, fmt.Sprintf("/api/v1/filament/presets/%d", id))
	if status != http.StatusOK ||
		!strings.Contains(body, `"document":{"name":"PLA","nozzle_temperature":["220"]},"document_error":null`) {
		t.Fatalf("%d %s", status, body)
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.gets != 1 {
		t.Errorf("%d GETs", store.gets)
	}
}

func TestAnUnswitchedPresetsAreaForwardsToNext(t *testing.T) {
	pool := testdb.New(t)
	router, err := newRouter(pool, teapot, presetKeys(t), "", "", nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if status, _ := send(t, router, http.MethodGet, "/api/v1/filament/presets"); status != http.StatusTeapot {
		t.Errorf("%d, not Python's", status)
	}
}

func TestTheAreaNeedsTheKEK(t *testing.T) {
	pool := testdb.New(t)
	_, err := newRouter(pool, teapot, nil, filamentpresets.Area, "", nil, nil, nil, nil)
	if err == nil || !strings.Contains(err.Error(), seal.Variable) {
		t.Fatalf("%v", err)
	}
}

func TestTheImportPostStillReachesPython(t *testing.T) {
	pool := testdb.New(t)
	router, err := newRouter(pool, teapot, presetKeys(t), filamentpresets.Area+", "+filamentpresets.Area, "", nil, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if status, _ := send(t, router, http.MethodPost, "/api/v1/filament/presets/bambuddy-import"); status != http.StatusTeapot {
		t.Errorf("%d, not Python's", status)
	}
}

// countingStarter is a Temporal that counts the writes it is asked to run and runs none.
type countingStarter struct {
	mu   sync.Mutex
	runs int
}

func (s *countingStarter) Run(context.Context, filamentpresets.Write) (json.RawMessage, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.runs++
	return nil, errors.New("no Temporal here")
}

func (s *countingStarter) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.runs
}

func TestAReadTokenCannotWrite(t *testing.T) {
	pool := testdb.New(t)
	keys := presetKeys(t)
	token := "sbpat_0123456789abcdef." + strings.Repeat("A", 43)
	sum := sha256.Sum256([]byte(token))
	if _, err := pool.Exec(t.Context(), `INSERT INTO personal_access_tokens (id, principal, name, scope, token_hash, expires_at)
		VALUES ('t1', 'user:test#me', 'read', 'read', $1, now() + interval '1 day')`, sum[:]); err != nil {
		t.Fatal(err)
	}
	post := func(handler http.Handler) (int, string) {
		server := httptest.NewServer(handler)
		defer server.Close()
		req, _ := http.NewRequest(http.MethodPost, server.URL+"/api/v1/filament/presets/1/copy", strings.NewReader(`{"name":"Mine"}`))
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("Idempotency-Key", "k")
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		body, _ := io.ReadAll(res.Body)
		return res.StatusCode, string(body)
	}
	starter := &countingStarter{}
	enforce, err := newRouter(pool, teapot, keys, filamentpresets.Area, "enforce", nil, nil, nil, nil, withPresetWrites(starter))
	if err != nil {
		t.Fatal(err)
	}
	if status, body := post(enforce); status != http.StatusForbidden || !strings.Contains(body, "out_of_scope") || starter.count() != 0 {
		t.Errorf("enforce: %d %s, %d writes", status, body, starter.count())
	}
	rec := recordTelemetry(t)
	observe, err := newRouter(pool, teapot, keys, filamentpresets.Area, "observe", nil, rec.tel.Meters, nil, nil, withPresetWrites(starter))
	if err != nil {
		t.Fatal(err)
	}
	if status, body := post(observe); status != http.StatusServiceUnavailable || starter.count() != 1 {
		t.Errorf("observe: %d %s, %d writes", status, body, starter.count())
	}
	if _, ok := rec.metrics(t)[auth.WouldRefuseMetric]; !ok {
		t.Error("observe counted nothing")
	}
}

// Prod's core has no Temporal address, and the area is on (plan ruling P22): core starts, the
// reads serve, and each write answers 503 writes_not_configured.
func TestThePresetsAreaStartsWithoutTemporal(t *testing.T) {
	pool := testdb.New(t)
	kek := filepath.Join(t.TempDir(), "kek")
	if err := os.WriteFile(kek, []byte(base64.StdEncoding.EncodeToString(make([]byte, 32))+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	env := map[string]string{
		"SCADBUDDY_CORE_UPSTREAM": "http://127.0.0.1:1",
		"SCADBUDDY_DATABASE_URL":  pool.Config().ConnString(),
		"SCADBUDDY_CORE_AREAS":    filamentpresets.Area,
		seal.Variable:             kek,
	}
	// Core binds its own address, so the test cannot hand it a listener. A port taken between
	// the pick and the bind makes run fail at once; the test then picks another.
	var address string
	var cancel context.CancelFunc
	var done chan error
	for attempt := 0; ; attempt++ {
		free, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		address = free.Addr().String()
		free.Close()
		env["SCADBUDDY_CORE_LISTEN"] = address
		var ctx context.Context
		ctx, cancel = context.WithCancel(context.Background())
		done = make(chan error, 1)
		go func() { done <- run(ctx, func(k string) (string, bool) { v, ok := env[k]; return v, ok }, nil) }()
		if !waitServing(address, done) {
			cancel()
			if attempt >= 4 {
				t.Fatal("core did not start")
			}
			continue
		}
		break
	}
	req, _ := http.NewRequest(http.MethodPost, "http://"+address+"/api/v1/filament/presets/1/copy", strings.NewReader(`{"name":"Mine"}`))
	req.Header.Set("Idempotency-Key", "k")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(res.Body)
	res.Body.Close()
	if res.StatusCode != http.StatusServiceUnavailable || !strings.Contains(string(body), `"code":"writes_not_configured"`) {
		t.Errorf("%d %s", res.StatusCode, body)
	}
	// No wait here has a limit of its own, so a slow runner cannot fail the test: a core that
	// never serves or never stops ends at the go test -timeout.
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

// waitServing is true once the area's list answers 200 at address, and false when run ended
// first (its bind failed) or the address answered another status. It waits for one of these
// with no limit of its own.
func waitServing(address string, done chan error) bool {
	for {
		select {
		case err := <-done:
			done <- err
			return false
		default:
		}
		res, err := http.Get("http://" + address + "/api/v1/filament/presets")
		if err == nil {
			res.Body.Close()
			return res.StatusCode == http.StatusOK
		}
		time.Sleep(20 * time.Millisecond)
	}
}
