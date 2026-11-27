package main

import (
	"io"
	"net/http"
	"net/http/httptest"
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
		router, err := newRouter(pool, next, nil, "test-public", mode, nil)
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
	if _, err := newRouter(pool, next, nil, "test-public", "enforced", nil); err == nil {
		t.Error("an unknown SCADBUDDY_AUTH_MODE was taken")
	}
}
