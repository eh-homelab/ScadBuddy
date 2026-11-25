package mcprelay

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const base = "/api/v1/internal/crud/agent"

var (
	hash    = strings.Repeat("b", 64)
	replica = uuid()
)

type api struct {
	t     *testing.T
	pool  *pgxpool.Pool
	url   string
	token string
}

func newAPI(t *testing.T) *api {
	t.Helper()
	pool := testdb.New(t)
	router := &crud.Router{
		Resolver: &auth.Resolver{Pool: pool},
		Next:     http.NotFoundHandler(),
	}
	router.Add("agent", Areas(pool, nil)["mcp-relay"]...)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	a.token = a.credential("agent", "crud.agent")
	return a
}

func (a *api) credential(caller string, scopes ...string) string {
	a.t.Helper()
	id, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(id)
	_, _ = rand.Read(secret)
	token := "sbsvc_" + hex.EncodeToString(id) + "." + base64.RawURLEncoding.EncodeToString(secret)
	sum := sha256.Sum256([]byte(token))
	a.sql(`INSERT INTO service_credentials (id, caller, scopes, secret_hash, expires_at, proof)
		VALUES ($1, $2, $3, $4, now() + interval '15 minutes', 'inprocess')`,
		hex.EncodeToString(id), caller, scopes, sum[:])
	return token
}

func (a *api) sql(query string, args ...any) {
	a.t.Helper()
	if _, err := a.pool.Exec(context.Background(), query, args...); err != nil {
		a.t.Fatal(err)
	}
}

func (a *api) strings(query string) []string {
	a.t.Helper()
	rows, err := a.pool.Query(context.Background(), query)
	if err != nil {
		a.t.Fatal(err)
	}
	out, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		a.t.Fatal(err)
	}
	return out
}

func (a *api) do(path string, body any, token string) (int, http.Header, any) {
	a.t.Helper()
	raw, _ := json.Marshal(body)
	req, _ := http.NewRequest("POST", a.url+base+path, strings.NewReader(string(raw)))
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer res.Body.Close()
	answer, _ := io.ReadAll(res.Body)
	var out any
	_ = json.Unmarshal(answer, &out)
	return res.StatusCode, res.Header, out
}

func (a *api) post(path string, body any) any {
	a.t.Helper()
	status, _, out := a.do(path, body, a.token)
	if status != http.StatusOK {
		a.t.Fatalf("%s: %d %v", path, status, out)
	}
	return out
}

func (a *api) status(path string, body any) int {
	a.t.Helper()
	status, _, _ := a.do(path, body, a.token)
	return status
}

type obj = map[string]any

func expect(t *testing.T, got, want any) {
	t.Helper()
	g, _ := json.Marshal(got)
	w, _ := json.Marshal(want)
	var gv, wv any
	_ = json.Unmarshal(g, &gv)
	_ = json.Unmarshal(w, &wv)
	if !reflect.DeepEqual(gv, wv) {
		t.Fatalf("got %s, want %s", g, w)
	}
}

func uuid() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6], b[8] = b[6]&0x0f|0x40, b[8]&0x3f|0x80
	h := hex.EncodeToString(b)
	return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}

// listen is a connection LISTENing on the relay's channel, as an agent replica's.
func listen(t *testing.T, a *api) *pgx.Conn {
	t.Helper()
	conn, err := pgx.ConnectConfig(context.Background(), a.pool.Config().ConnConfig)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close(context.Background()) })
	if _, err := conn.Exec(context.Background(), "LISTEN scadbuddy_mcp"); err != nil {
		t.Fatal(err)
	}
	return conn
}

func notified(conn *pgx.Conn, wait time.Duration) []string {
	var out []string
	for {
		ctx, cancel := context.WithTimeout(context.Background(), wait)
		n, err := conn.WaitForNotification(ctx)
		cancel()
		if err != nil {
			return out
		}
		out = append(out, n.Payload)
	}
}

func TestOnlyTheAgentCredentialReachesTheRoutes(t *testing.T) {
	a := newAPI(t)
	for _, caller := range []string{"print", "projects"} {
		status, header, _ := a.do("/mcp-relay/sessions/owner", obj{"hash": hash}, a.credential(caller, "crud."+caller))
		if status != http.StatusForbidden || header.Get("X-ScadBuddy-Crud") != "1" {
			t.Fatalf("%s: %d", caller, status)
		}
	}
	if status, _, _ := a.do("/mcp-relay/sessions/owner", obj{"hash": hash}, ""); status != http.StatusUnauthorized {
		t.Fatal(status)
	}
}

func TestTheDirectoryIsWrittenCountedAndRemoved(t *testing.T) {
	a := newAPI(t)
	a.post("/mcp-relay/sessions", obj{"hash": hash, "replica": replica, "caller_key": "token:1"})
	a.post("/mcp-relay/sessions", obj{"hash": strings.Repeat("c", 64), "replica": replica, "caller_key": "token:2"})
	expect(t, a.post("/mcp-relay/sessions/owner", obj{"hash": hash}), obj{"replica": replica})
	expect(t, a.post("/mcp-relay/counts", obj{"caller_key": "token:1", "idle_seconds": 60}),
		obj{"total": 2, "caller": 1})
	// Registered again by another replica: the row moves.
	moved := uuid()
	a.post("/mcp-relay/sessions", obj{"hash": hash, "replica": moved, "caller_key": "token:1"})
	// A gone owner's removal never takes the new owner's row.
	expect(t, a.post("/mcp-relay/sessions/remove", obj{"hash": hash, "replica": replica}), obj{"done": false})
	expect(t, a.post("/mcp-relay/sessions/owner", obj{"hash": hash}), obj{"replica": moved})
	a.sql(`UPDATE ai_mcp_sessions SET last_seen = now() - interval '2 hours'`)
	a.post("/mcp-relay/sessions/touch", obj{"hash": hash})
	expect(t, a.post("/mcp-relay/counts", obj{"caller_key": "token:1", "idle_seconds": 60}).(obj)["total"], 1)
	// The sweep drops the idle row only.
	a.post("/mcp-relay/sweep", obj{"idle_seconds": 60})
	expect(t, a.strings(`SELECT id_hash FROM ai_mcp_sessions`), []string{hash})
	expect(t, a.post("/mcp-relay/sessions/remove", obj{"hash": hash}), obj{"done": true})
	expect(t, a.post("/mcp-relay/sessions/owner", obj{"hash": hash}), obj{"replica": nil})
}

func TestARequestRowAndItsNotifyCommitTogether(t *testing.T) {
	a := newAPI(t)
	heard := listen(t, a)
	req, row := uuid(), uuid()
	body := obj{"from": uuid(), "hash": hash, "principal": obj{"id": "x"}, "request": obj{}}
	a.post("/mcp-relay/send", obj{"header": obj{"t": "req", "req": req, "to": replica, "row": row}, "body": body})
	payloads := notified(heard, 500*time.Millisecond)
	// The payload is the header as the Python model writes it, member for member.
	expect(t, payloads, []string{`{"req":"` + req + `","to":"` + replica + `","t":"req","row":"` + row + `"}`})
	// The row was readable when the header was heard, and is taken once.
	expect(t, a.post("/mcp-relay/rows/take", obj{"row": row}), obj{"body": body})
	expect(t, a.post("/mcp-relay/rows/take", obj{"row": row}), obj{"body": nil})
	expect(t, a.post("/mcp-relay/rows/withdraw", obj{"row": row}), obj{"done": false})
}

func TestAnInlineChunkRidesInTheNotify(t *testing.T) {
	a := newAPI(t)
	heard := listen(t, a)
	req := uuid()
	a.post("/mcp-relay/send", obj{"header": obj{"t": "chunk", "req": req, "to": replica, "text": "data: 1\n\n"}})
	a.post("/mcp-relay/send", obj{"header": obj{
		"t": "head", "req": req, "to": replica, "status": 200, "headers": [][]string{{"a", "b"}},
	}})
	a.post("/mcp-relay/send", obj{"header": obj{"t": "end", "req": req, "to": replica, "error": true}})
	ids := `"req":"` + req + `","to":"` + replica + `"`
	expect(t, notified(heard, 500*time.Millisecond), []string{
		`{` + ids + `,"t":"chunk","text":"data: 1\n\n"}`,
		`{` + ids + `,"t":"head","status":200,"headers":[["a","b"]]}`,
		`{` + ids + `,"t":"end","error":true}`,
	})
}

func TestANonASCIIChunkTheAgentSizedRidesInTheNotify(t *testing.T) {
	a := newAPI(t)
	heard := listen(t, a)
	// Written as \uXXXX escapes this one would pass 7 999 bytes.
	text := "data: " + strings.Repeat("漢°—", 900) + "\n\n"
	a.post("/mcp-relay/send", obj{"header": obj{"t": "chunk", "req": uuid(), "to": replica, "text": text}})
	payloads := notified(heard, 500*time.Millisecond)
	if len(payloads) != 1 {
		t.Fatal(payloads)
	}
	var got obj
	_ = json.Unmarshal([]byte(payloads[0]), &got)
	expect(t, got["text"], text)
}

func TestAWithdrawnRequestIsGone(t *testing.T) {
	a := newAPI(t)
	row := uuid()
	header := obj{"t": "req", "req": uuid(), "to": replica, "row": row}
	a.post("/mcp-relay/send", obj{"header": header, "body": obj{"x": 1}})
	expect(t, a.post("/mcp-relay/rows/withdraw", obj{"row": row}), obj{"done": true})
	expect(t, a.post("/mcp-relay/rows/take", obj{"row": row}), obj{"body": nil})
	// The same row again is refused, never a second row under one id.
	a.post("/mcp-relay/send", obj{"header": header, "body": obj{"x": 1}})
	if s := a.status("/mcp-relay/send", obj{"header": header, "body": obj{"x": 2}}); s != http.StatusConflict {
		t.Fatal(s)
	}
}

func TestAnUppercaseOrHyphenlessIDIsWrittenLowerCase(t *testing.T) {
	a := newAPI(t)
	heard := listen(t, a)
	req := uuid()
	a.post("/mcp-relay/send", obj{"header": obj{
		"t": "ack", "req": strings.ToUpper(req), "to": strings.ReplaceAll(replica, "-", ""),
	}})
	expect(t, notified(heard, 500*time.Millisecond),
		[]string{`{"req":"` + req + `","to":"` + replica + `","t":"ack"}`})
}

func TestAMalformedSendNotifiesNothing(t *testing.T) {
	for i, send := range []obj{
		// A malformed id, a kind the relay never sends, an unknown field.
		{"header": obj{"t": "ack", "req": "../x", "to": replica}},
		{"header": obj{"t": "nope", "req": uuid(), "to": replica}},
		{"header": obj{"t": "ack", "req": uuid(), "to": replica, "extra": 1}},
		// A body with no row to put it in, a row with no body.
		{"header": obj{"t": "ack", "req": uuid(), "to": replica}, "body": obj{"x": 1}},
		{"header": obj{"t": "req", "req": uuid(), "to": replica, "row": uuid()}},
		// Past what a NOTIFY carries.
		{"header": obj{"t": "chunk", "req": uuid(), "to": replica, "text": strings.Repeat("x", 8000)}},
	} {
		a := newAPI(t)
		heard := listen(t, a)
		if s := a.status("/mcp-relay/send", send); s != http.StatusUnprocessableEntity {
			t.Fatalf("%d: %d", i, s)
		}
		if n := notified(heard, 200*time.Millisecond); len(n) != 0 {
			t.Fatalf("%d: %v", i, n)
		}
		expect(t, a.strings(`SELECT id::text FROM ai_mcp_relay_messages`), []string{})
	}
}

func TestAMalformedIDOrBoundIsRefused(t *testing.T) {
	a := newAPI(t)
	for _, c := range []struct {
		path string
		body obj
	}{
		{"/mcp-relay/sessions/owner", obj{"hash": strings.Repeat("B", 64)}},
		{"/mcp-relay/sessions/owner", obj{"hash": "../" + strings.Repeat("b", 61)}},
		{"/mcp-relay/sessions", obj{"hash": hash, "replica": "x", "caller_key": "k"}},
		{"/mcp-relay/sessions", obj{"hash": hash, "replica": replica, "caller_key": strings.Repeat("k", 1025)}},
		{"/mcp-relay/counts", obj{"caller_key": "k", "idle_seconds": 0}},
		{"/mcp-relay/sweep", obj{"idle_seconds": 1e9}},
		{"/mcp-relay/rows/take", obj{"row": "1"}},
	} {
		if s := a.status(c.path, c.body); s != http.StatusUnprocessableEntity {
			t.Errorf("%s %v: %d", c.path, c.body, s)
		}
	}
}
