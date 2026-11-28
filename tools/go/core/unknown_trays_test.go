package main

import (
	"bytes"
	"crypto/sha256"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// The unknown-trays area (#7143), through newRouter.

const (
	trayPrinter = "5d0c8f1e-0000-4000-8000-0000000071aa"
	blockGolden = "../../../backend/tests/fixtures/unknown-trays-block.json"
	blockPath   = "/api/v1/internal/crud/print/filament/printers/" + trayPrinter + "/unknown-trays"
	listPath    = "/api/v1/filament/unknown-trays"
	resolvePath = listPath + "/5d0c8f1e-0000-4000-8000-000000007143/resolve"
	resolveBody = `{"code":"S-12","net_weight_g":980,"seen_at":"2026-11-26T09:30:00.123456Z"}`
)

func seedTrays(a *api) {
	a.sql(`INSERT INTO printers (id, serial, name, model) VALUES ($1, 'SN1', 'H2C', 'H2C')`, trayPrinter)
	a.sql(`INSERT INTO filament_unknown_trays (id, printer_id, ams_id, tray_id, material, first_seen_at, seen_at)
		VALUES ('5d0c8f1e-0000-4000-8000-000000007144', $1, 128, 0, 'PETG', now(), now()),
		       ('5d0c8f1e-0000-4000-8000-000000007143', $1, 0, 1, 'PLA', now(), now())`, trayPrinter)
}

func asCaller(token string) func(*http.Request) {
	return func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+token) }
}

func TestTheBlockReadAnswersTheGoldenJSON(t *testing.T) {
	a := newAPI(t, "unknown-trays")
	seedTrays(a)
	want, err := os.ReadFile(blockGolden)
	if err != nil {
		t.Fatal(err)
	}
	got := a.do("GET", blockPath, "", asCaller(a.credential("print", "crud.print")))
	expect(t, got.status, 200)
	// The file ends with a newline; the route's body does not.
	if !bytes.Equal([]byte(got.raw), bytes.TrimSuffix(want, []byte("\n"))) {
		t.Errorf("body %s\nwant %s", got.raw, want)
	}
	// A printer with no row has an empty block, not null.
	empty := a.do("GET", strings.Replace(blockPath, trayPrinter, "5d0c8f1e-0000-4000-8000-0000000071bb", 1), "",
		asCaller(a.credential("print", "crud.print")))
	expect(t, empty.raw, `{"count":0,"trays":[]}`)
}

func TestTheBlockReadRefusesAnotherCallersCredential(t *testing.T) {
	a := newAPI(t, "unknown-trays")
	seedTrays(a)
	for _, caller := range []string{"render", "agent", "projects"} {
		got := a.do("GET", blockPath, "", asCaller(a.credential(caller, "crud."+caller)))
		expect(t, got.status, 403)
	}
	expect(t, a.do("GET", blockPath, "", func(r *http.Request) { r.Header.Del("Authorization") }).status, 401)
}

func TestTheReadAreaServesTheBlockAndTheListOnly(t *testing.T) {
	a := newAPI(t, "unknown-trays:read")
	seedTrays(a)
	expect(t, a.do("GET", blockPath, "", asCaller(a.credential("print", "crud.print"))).status, 200)
	expect(t, a.do("GET", listPath, "", func(r *http.Request) { r.Header.Set(auth.RequestHeader, "1") }).status, 200)
	// The dismiss and resolve routes are not in the read area: they go on to Python.
	expect(t, a.do("POST", listPath+"/5d0c8f1e-0000-4000-8000-000000007143/dismiss", "").status, http.StatusTeapot)
	expect(t, a.do("POST", resolvePath, resolveBody).status, http.StatusTeapot)
	expect(t, a.count(`SELECT count(*) FROM filament_unknown_trays`), 2)
}

func TestAnUnswitchedAreaForwardsToNext(t *testing.T) {
	a := newAPI(t, "")
	seedTrays(a)
	for _, c := range []struct{ method, path string }{
		{"GET", listPath},
		{"POST", listPath + "/5d0c8f1e-0000-4000-8000-000000007143/dismiss"},
		{"POST", resolvePath},
		{"GET", blockPath},
	} {
		got := a.do(c.method, c.path, "", func(r *http.Request) { r.Header.Set(auth.RequestHeader, "1") })
		expect(t, got.status, http.StatusTeapot)
	}
}

func TestAnUnknownAreaNameStillStopsTheStart(t *testing.T) {
	pool := testdb.New(t)
	next := http.HandlerFunc(func(http.ResponseWriter, *http.Request) {})
	if _, err := newRouter(pool, next, nil, "unknown-trayz", "", callerPackages, nil, nil, nil); err == nil {
		t.Fatal("an unknown area name was taken")
	}
}

func TestPublicRoutesUseTheGate(t *testing.T) {
	pool := testdb.New(t)
	a := &api{t: t, pool: pool}
	seedTrays(a)
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusTeapot) })
	router, err := newRouter(pool, next, nil, "unknown-trays", "enforce", callerPackages, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(router)
	defer server.Close()
	token := "sbpat_0123456789abcdef." + strings.Repeat("A", 43)
	sum := sha256.Sum256([]byte(token))
	a.sql(`INSERT INTO personal_access_tokens (id, principal, name, scope, token_hash, expires_at)
		VALUES ('t1', 'user:test#me', 'read', 'read', $1, now() + interval '1 day')`, sum[:])
	call := func(method, path, bearer string) (int, string) {
		req, _ := http.NewRequest(method, server.URL+path, nil)
		req.Header.Set(auth.RequestHeader, "1")
		if bearer != "" {
			req.Header.Set("Authorization", "Bearer "+bearer)
		}
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		var b bytes.Buffer
		_, _ = b.ReadFrom(res.Body)
		return res.StatusCode, b.String()
	}
	dismissPath := listPath + "/5d0c8f1e-0000-4000-8000-000000007143/dismiss"
	if status, body := call("GET", listPath, ""); status != 401 || !strings.Contains(body, "signed-out") {
		t.Errorf("signed out: %d %s", status, body)
	}
	if status, _ := call("GET", listPath, token); status != 200 {
		t.Errorf("a read token reads the list: %d", status)
	}
	if status, body := call("POST", dismissPath, token); status != 403 || !strings.Contains(body, "out_of_scope") {
		t.Errorf("a read token dismisses: %d %s", status, body)
	}
	var n int
	if err := pool.QueryRow(t.Context(), `SELECT count(*) FROM filament_unknown_trays`).Scan(&n); err != nil || n != 2 {
		t.Errorf("the refused dismiss deleted a row: %d %v", n, err)
	}
}

// Prod's core has no Temporal address (#8489 U2): the area serves its reads, and a resolve
// answers 503 writes_not_configured.
func TestTheResolveWithoutTemporalIs503(t *testing.T) {
	a := newAPI(t, "unknown-trays")
	seedTrays(a)
	got := a.do("POST", resolvePath, resolveBody, func(r *http.Request) { r.Header.Set(auth.RequestHeader, "1") })
	expect(t, got.status, http.StatusServiceUnavailable)
	expect(t, got.body["code"], "writes_not_configured")
	expect(t, a.count(`SELECT count(*) FROM filament_unknown_trays`), 2)
}

func TestAReadTokenCannotResolve(t *testing.T) {
	pool := testdb.New(t)
	token := "sbpat_0123456789abcdef." + strings.Repeat("A", 43)
	sum := sha256.Sum256([]byte(token))
	if _, err := pool.Exec(t.Context(), `INSERT INTO personal_access_tokens (id, principal, name, scope, token_hash, expires_at)
		VALUES ('t1', 'user:test#me', 'read', 'read', $1, now() + interval '1 day')`, sum[:]); err != nil {
		t.Fatal(err)
	}
	post := func(handler http.Handler) (int, string) {
		server := httptest.NewServer(handler)
		defer server.Close()
		req, _ := http.NewRequest(http.MethodPost, server.URL+resolvePath, strings.NewReader(resolveBody))
		req.Header.Set("Authorization", "Bearer "+token)
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		var b bytes.Buffer
		_, _ = b.ReadFrom(res.Body)
		return res.StatusCode, b.String()
	}
	starter := &countingStarter{}
	enforce, err := newRouter(pool, teapot, nil, "unknown-trays", "enforce", callerPackages, nil, nil, nil, withLibraryRuns(starter))
	if err != nil {
		t.Fatal(err)
	}
	if status, body := post(enforce); status != http.StatusForbidden || !strings.Contains(body, "out_of_scope") || starter.count() != 0 {
		t.Errorf("enforce: %d %s, %d runs", status, body, starter.count())
	}
	rec := recordTelemetry(t)
	observe, err := newRouter(pool, teapot, nil, "unknown-trays", "observe", callerPackages, rec.tel.Meters, nil, nil, withLibraryRuns(starter))
	if err != nil {
		t.Fatal(err)
	}
	// Observe serves it: the counting starter runs nothing and answers an error, a pending resolve.
	if status, body := post(observe); status != http.StatusServiceUnavailable || !strings.Contains(body, "resolve_pending") || starter.count() != 1 {
		t.Errorf("observe: %d %s, %d runs", status, body, starter.count())
	}
	if _, ok := rec.metrics(t)[auth.WouldRefuseMetric]; !ok {
		t.Error("observe counted nothing")
	}
}
