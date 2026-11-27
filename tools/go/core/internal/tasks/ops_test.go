package tasks

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const base = "/api/v1/internal/crud/agent"

type api struct {
	t     *testing.T
	pool  *pgxpool.Pool
	url   string
	token string
}

func newAPI(t *testing.T) *api {
	t.Helper()
	pool := testdb.New(t)
	router := &crud.Router{Resolver: &auth.Resolver{Pool: pool}, Next: http.NotFoundHandler()}
	router.Add("agent", Areas(pool, nil)["operations"]...)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	id, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(id)
	_, _ = rand.Read(secret)
	a.token = "sbsvc_" + hex.EncodeToString(id) + "." + base64.RawURLEncoding.EncodeToString(secret)
	hash := sha256.Sum256([]byte(a.token))
	a.exec(`INSERT INTO service_credentials (id, caller, scopes, secret_hash, expires_at, proof)
		VALUES ($1, 'agent', '{api,crud.agent}', $2, now() + interval '15 minutes', 'inprocess')`,
		hex.EncodeToString(id), hash[:])
	return a
}

func (a *api) exec(query string, args ...any) {
	a.t.Helper()
	if _, err := a.pool.Exec(context.Background(), query, args...); err != nil {
		a.t.Fatal(err)
	}
}

func (a *api) one(query string, args ...any) string {
	a.t.Helper()
	var out string
	if err := a.pool.QueryRow(context.Background(), query, args...).Scan(&out); err != nil {
		a.t.Fatal(err)
	}
	return out
}

type reply struct {
	status int
	body   map[string]any
	raw    string
}

func (a *api) post(path, body string) reply {
	a.t.Helper()
	req, _ := http.NewRequest("POST", a.url+base+path, strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+a.token)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	out := reply{status: res.StatusCode, raw: string(raw)}
	_ = json.Unmarshal(raw, &out.body)
	return out
}

var opKey = strings.Repeat("ab", 32)

func newOperation(id, run string, extra string) string {
	return `{"id":"` + id + `","kind":"fetch_asset","subject":"https://example.com/a.stl",` +
		`"operation_key":"` + opKey + `","request":{"url":"x","n":1E2,"f":1.50},` +
		`"workflow_id":"agent-op-1","workflow_run_id":"` + run + `"` + extra + `}`
}

func TestAnOperationIsRecordedOnceWithItsActivityRow(t *testing.T) {
	a := newAPI(t)
	out := a.post("/operations", newOperation("op1", "run1",
		`,"actor":{"principal":"ada","session":"s1","via_tool":"fetch_asset"}`))
	op, _ := out.body["operation"].(map[string]any)
	if out.status != 200 || op["id"] != "op1" || op["status"] != "running" || op["result"] != nil ||
		op["finished_at"] != nil {
		t.Fatalf("%d %s", out.status, out.raw)
	}
	// The request as Python's json.loads and Jsonb wrote it: 1E2 a float, 1.50 as 1.5.
	if got := a.one(`SELECT request::text FROM ai_operations WHERE id = 'op1'`); got != `{"f": 1.5, "n": 100.0, "url": "x"}` {
		t.Fatalf("request %s", got)
	}
	if got := a.one(`SELECT concat_ws('|', kind, subject, status, title, source, source_id, workflow_id,
		run_id, principal, session, via_tool, detail::text, facets::text, finished_at IS NULL)
		FROM activity WHERE id = 'agent_operation:op1'`); got != "agent.fetch_asset|https://example.com/a.stl|"+
		"running|Fetch asset https://example.com/a.stl|ai_operations|op1|agent-op-1|run1|ada|s1|fetch_asset|"+
		`{"error": null, "result": null, "request": {"f": 1.5, "n": 100.0, "url": "x"}}|{}|t` {
		t.Fatalf("activity %s", got)
	}
	if got := a.one(`SELECT kind || ' ' || (payload->>'activity_id') || ' ' || (payload->>'principal') FROM events`); got != "activity.changed agent_operation:op1 ada" {
		t.Fatalf("event %s", got)
	}
	// A retried activity gets the execution's row, and writes nothing.
	again := a.post("/operations", newOperation("op2", "run1", ""))
	if again.status != 200 || again.body["operation"].(map[string]any)["id"] != "op1" {
		t.Fatalf("%d %s", again.status, again.raw)
	}
	if got := a.one(`SELECT (SELECT count(*) FROM ai_operations) || ' ' || (SELECT count(*) FROM events)`); got != "1 1" {
		t.Fatalf("rows %s", got)
	}
}

func TestAnEndIsWrittenOnceWithItsActivityRow(t *testing.T) {
	a := newAPI(t)
	a.post("/operations", newOperation("op1", "run1", ""))
	a.post("/operations", newOperation("op2", "run2", ""))
	done := a.post("/operations/op1/finish", `{"result":{"asset":7,"x":1e16}}`)
	op, _ := done.body["operation"].(map[string]any)
	if done.status != 200 || op["status"] != "succeeded" || op["finished_at"] == nil {
		t.Fatalf("%d %s", done.status, done.raw)
	}
	if got := a.one(`SELECT result::text FROM ai_operations WHERE id = 'op1'`); got != `{"x": 1e+16, "asset": 7}` &&
		got != `{"x": 10000000000000000, "asset": 7}` {
		t.Fatalf("result %s", got)
	}
	if got := a.one(`SELECT status || ' ' || (principal) || ' ' || (finished_at IS NOT NULL) FROM activity
		WHERE id = 'agent_operation:op1'`); got != "succeeded browser true" {
		t.Fatalf("activity %s", got)
	}
	failed := a.post("/operations/op2/finish", `{"error":{"status":502,"title":"Bad Gateway",`+
		`"detail":"no","extensions":{"reason":"r"},"more":[1.0]},"result":null}`)
	if failed.status != 200 || failed.body["operation"].(map[string]any)["status"] != "failed" {
		t.Fatalf("%d %s", failed.status, failed.raw)
	}
	if got := a.one(`SELECT error::text || ' ' || (result IS NULL) FROM ai_operations WHERE id = 'op2'`); got != `{"more": [1.0], "title": "Bad Gateway", "detail": "no", "status": 502, "extensions": {"reason": "r"}} true` {
		t.Fatalf("error %s", got)
	}
	// A retried end changes nothing.
	before := a.one(`SELECT count(*)::text FROM events`)
	again := a.post("/operations/op1/finish", `{"error":{"status":500,"title":"t","detail":"d"}}`)
	if again.status != 200 || again.body["operation"].(map[string]any)["status"] != "succeeded" ||
		a.one(`SELECT count(*)::text FROM events`) != before {
		t.Fatalf("%d %s", again.status, again.raw)
	}
	missing := a.post("/operations/none/finish", `{}`)
	if missing.status != 404 || missing.body["detail"] != "There is no operation none." {
		t.Fatalf("%d %s", missing.status, missing.raw)
	}
}

func TestBadOperationBodiesAreRefused(t *testing.T) {
	a := newAPI(t)
	for _, c := range []struct{ path, body, loc string }{
		{"/operations", `{"id":"x"}`, `["body","kind"]`},
		{"/operations", newOperation("", "r", ""), `["body","id"]`},
		{"/operations", strings.Replace(newOperation("x", "r", ""), opKey, "nope", 1), `["body","operation_key"]`},
		{"/operations", newOperation("x", "r", `,"retention_seconds":60`), `["body","retention_seconds"]`},
		{"/operations", newOperation("x", "r", `,"actor":{"session":"s"}`), `["body","actor","principal"]`},
		{"/operations", newOperation("x", "r", `,"actor":{"principal":"p","x":1}`), `["body","actor","x"]`},
		{"/operations/op/finish", `{"result":1,"error":{"status":500,"title":"t","detail":"d"}}`, `["body"]`},
		{"/operations/op/finish", `{"error":{"title":"t","detail":"d"}}`, `["body","error","status"]`},
		{"/operations/op/finish", `{"error":{"status":500,"title":1,"detail":"d"}}`, `["body","error","title"]`},
		{"/operations/op/finish", `{"x":1}`, `["body","x"]`},
	} {
		out := a.post(c.path, c.body)
		if out.status != 422 || !bytes.Contains([]byte(out.raw), []byte(`"loc":`+c.loc)) {
			t.Errorf("%s %s: %d %s", c.path, c.body, out.status, out.raw)
		}
	}
	if got := a.one(`SELECT count(*)::text FROM ai_operations`); got != "0" {
		t.Fatalf("rows %s", got)
	}
}
