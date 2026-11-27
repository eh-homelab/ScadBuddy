package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// A public area's routes are served behind the gate, in front of the CRUD router, and
// every other path still reaches Python.
func TestAPublicAreaIsServedBehindTheGate(t *testing.T) {
	pool := testdb.New(t)
	saved := publicPackages
	t.Cleanup(func() { publicPackages = saved })
	publicPackages = append(publicPackages, func(*pgxpool.Pool, *seal.Keys) map[string][]auth.Public {
		return map[string][]auth.Public{"test-public": {{
			Pattern: "GET /api/v1/test-public",
			Route:   auth.Route{Class: "api", Template: "/api/v1/test-public"},
			Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.WriteString(w, string(auth.CallerOf(r.Context()).Kind))
			}),
		}}}
	})
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusTeapot) })
	get := func(mode, path string) (int, string) {
		router, err := newRouter(pool, next, nil, "test-public", mode, nil, nil, nil)
		if err != nil {
			t.Fatal(err)
		}
		server := httptest.NewServer(router)
		defer server.Close()
		req, _ := http.NewRequest(http.MethodGet, server.URL+path, nil)
		req.Header.Set(auth.RequestHeader, "1")
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		body, _ := io.ReadAll(res.Body)
		return res.StatusCode, string(body)
	}
	if status, body := get("observe", "/api/v1/test-public"); status != 200 || body != "anonymous" {
		t.Errorf("observe: %d %q", status, body)
	}
	if status, _ := get("enforce", "/api/v1/test-public"); status != http.StatusUnauthorized {
		t.Errorf("enforce, signed out: %d", status)
	}
	if status, _ := get("", "/api/v1/elsewhere"); status != http.StatusTeapot {
		t.Errorf("another path: %d, not Python's", status)
	}
	// A path the mux would clean goes to Python unchanged, never to the mux's redirect.
	for _, path := range []string{"//api/v1/test-public", "/api/v1/x/../test-public", "/api/v1//elsewhere"} {
		if status, _ := get("", path); status != http.StatusTeapot {
			t.Errorf("%s: %d, not Python's", path, status)
		}
	}
	// A public pattern's other methods go to Python too.
	router, _ := newRouter(pool, next, nil, "test-public", "", nil, nil, nil)
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/test-public", nil))
	if rec.Code != http.StatusTeapot {
		t.Errorf("POST to a GET pattern: %d, not Python's", rec.Code)
	}
	if _, err := newRouter(pool, next, nil, "test-public", "enforced", nil, nil, nil); err == nil {
		t.Error("an unknown SCADBUDDY_AUTH_MODE was taken")
	}
}

// Area filament-tag-reads stands behind the gate on the tag read's path, and hands a read
// with no MIFARE blocks to Python unchanged (#8148).
func TestTheTagReadAreaForwardsAnNDEFRead(t *testing.T) {
	pool := testdb.New(t)
	var got string
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		got = r.URL.Path + " " + string(body)
		w.WriteHeader(http.StatusTeapot)
	})
	keys := &seal.Keys{Current: seal.NewKEK(make([]byte, 32))}
	router, err := newRouter(pool, next, keys, "filament-tag-reads", "enforce", nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(router)
	defer server.Close()
	post := func() int {
		req, _ := http.NewRequest(http.MethodPost, server.URL+"/api/v1/filament/tags/reads",
			strings.NewReader(`{"uid":"53F64B94420001","records":[]}`))
		req.Header.Set(auth.RequestHeader, "1")
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		return res.StatusCode
	}
	if status := post(); status != http.StatusUnauthorized || got != "" {
		t.Errorf("enforce, signed out: %d, Python got %q", status, got)
	}
	router, _ = newRouter(pool, next, keys, "filament-tag-reads", "", nil, nil, nil)
	server.Config.Handler = router
	if status := post(); status != http.StatusTeapot || got != `/api/v1/filament/tags/reads {"uid":"53F64B94420001","records":[]}` {
		t.Errorf("off: %d, Python got %q", status, got)
	}
}

// Area label-printing serves its queue list from Go, and without it the path goes on to
// Python (#7170). The stored server is bad, so Go answers 502 at once.
func TestTheLabelPrintingAreaServesItsQueues(t *testing.T) {
	pool := testdb.New(t)
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusTeapot) })
	if _, err := pool.Exec(t.Context(), `INSERT INTO settings (name, value) VALUES ('filament_label_server_url', '"cups:631"'::jsonb)`); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		areas  string
		status int
	}{{"label-printing, label-printing", http.StatusBadGateway}, {"", http.StatusTeapot}} {
		router, err := newRouter(pool, next, nil, c.areas, "", nil, nil, nil)
		if err != nil {
			t.Fatal(err)
		}
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/label-printing/queues", nil))
		if rec.Code != c.status {
			t.Errorf("areas %q: %d, want %d (%s)", c.areas, rec.Code, c.status, rec.Body)
		}
		if c.status == http.StatusBadGateway && !strings.Contains(rec.Body.String(), `"reason":"label_server_unreachable"`) {
			t.Errorf("body %s", rec.Body)
		}
	}
}
