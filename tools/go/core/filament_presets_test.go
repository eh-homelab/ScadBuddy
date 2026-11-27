package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

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
	enforce, err := newRouter(pool, teapot, keys, filamentpresets.Area, "enforce", nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if status, body, cache := sendCache(t, enforce, http.MethodGet, "/api/v1/filament/presets"); status != http.StatusUnauthorized ||
		!strings.Contains(body, "signed-out") || cache != "no-store" {
		t.Errorf("enforce, signed out: %d %s, Cache-Control %q", status, body, cache)
	}
	rec := recordTelemetry(t)
	observe, err := newRouter(pool, teapot, keys, filamentpresets.Area, "observe", rec.tel.Meters, nil, nil)
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
	router, err := newRouter(pool, teapot, presetKeys(t), filamentpresets.Area, "", nil, presetEnv(s3.URL), nil)
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
	router, err := newRouter(pool, teapot, presetKeys(t), "", "", nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if status, _ := send(t, router, http.MethodGet, "/api/v1/filament/presets"); status != http.StatusTeapot {
		t.Errorf("%d, not Python's", status)
	}
}

func TestTheAreaNeedsTheKEK(t *testing.T) {
	pool := testdb.New(t)
	_, err := newRouter(pool, teapot, nil, filamentpresets.Area, "", nil, nil, nil)
	if err == nil || !strings.Contains(err.Error(), seal.Variable) {
		t.Fatalf("%v", err)
	}
}

func TestTheImportPostStillReachesPython(t *testing.T) {
	pool := testdb.New(t)
	router, err := newRouter(pool, teapot, presetKeys(t), filamentpresets.Area+", "+filamentpresets.Area, "", nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if status, _ := send(t, router, http.MethodPost, "/api/v1/filament/presets/bambuddy-import"); status != http.StatusTeapot {
		t.Errorf("%d, not Python's", status)
	}
}
