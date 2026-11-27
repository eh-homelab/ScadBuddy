package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const base = "/api/v1/internal/crud/agent"

// api is the routes of every area, over a fresh database, as the agent calls them.
type api struct {
	t     *testing.T
	pool  *pgxpool.Pool
	url   string
	token string
}

func newAPI(t *testing.T, areas string) *api {
	t.Helper()
	pool := testdb.New(t)
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("X-From", "python")
		w.WriteHeader(http.StatusTeapot)
	})
	router, err := newRouter(pool, next, nil, areas, "", nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	a.token = a.credential("agent", "api", "crud.agent")
	return a
}

func all(t *testing.T) *api { return newAPI(t, "session-events, session-turns:read") }

func (a *api) credential(caller string, scopes ...string) string {
	a.t.Helper()
	id, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(id)
	_, _ = rand.Read(secret)
	token := "sbsvc_" + hex.EncodeToString(id) + "." + base64.RawURLEncoding.EncodeToString(secret)
	hash := sha256.Sum256([]byte(token))
	a.sql(`INSERT INTO service_credentials (id, caller, scopes, secret_hash, expires_at, proof)
		VALUES ($1, $2, $3, $4, now() + interval '15 minutes', 'inprocess')`,
		hex.EncodeToString(id), caller, scopes, hash[:])
	return token
}

func (a *api) sql(query string, args ...any) {
	a.t.Helper()
	if _, err := a.pool.Exec(context.Background(), query, args...); err != nil {
		a.t.Fatal(err)
	}
}

type answer struct {
	status int
	header http.Header
	body   map[string]any
	raw    string
}

func (a *api) do(method, path, body string, with ...func(*http.Request)) answer {
	a.t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req, err := http.NewRequest(method, a.url+path, reader)
	if err != nil {
		a.t.Fatal(err)
	}
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Authorization", "Bearer "+a.token)
	for _, f := range with {
		f(req)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(res.Body)
	if err != nil {
		a.t.Fatal(err)
	}
	out := answer{status: res.StatusCode, header: res.Header, raw: string(raw)}
	_ = json.Unmarshal(raw, &out.body)
	return out
}

func (a *api) ok(method, path, body string) answer {
	a.t.Helper()
	out := a.do(method, base+path, body)
	if out.status != http.StatusOK {
		a.t.Fatalf("%s %s: %d %s", method, path, out.status, out.raw)
	}
	return out
}

func (a *api) append(session string, events ...string) string {
	a.t.Helper()
	return a.ok("POST", "/sessions/"+session+"/events", object("events", events)).raw
}

func (a *api) session(cols map[string]any) string {
	a.t.Helper()
	id := uuid()
	a.sql(`INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,
		creator_id, status, max_turns, budget_usd, fork_parent_id, fork_cut, turn_attempt, durable_turn_id)
		VALUES ($1::text::uuid, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'idle', 10, 1,
		$2::text::uuid, CASE WHEN $2::text IS NULL THEN NULL ELSE 0 END, $3, $4::text::uuid)`,
		id, cols["parent"], cols["attempt"], cols["turn"])
	return id
}

func uuid() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}

// object is a JSON object of name, value pairs.
func object(pairs ...any) string {
	m := map[string]any{}
	for i := 0; i < len(pairs); i += 2 {
		m[pairs[i].(string)] = pairs[i+1]
	}
	b, err := json.Marshal(m)
	if err != nil {
		panic(err)
	}
	return string(b)
}

func delta(text string) string { return object("type", "assistant.text.delta", "text", text) }
func userTurn(turn, text string) string {
	return object("type", "user.turn", "turnId", turn, "text", text)
}

func expect(t *testing.T, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestOnlyTheAgentsCredentialReachesTheRoutes(t *testing.T) {
	a := all(t)
	path := base + "/sessions/" + a.session(nil) + "/events"
	none := a.do("GET", path, "", func(r *http.Request) { r.Header.Del("Authorization") })
	expect(t, none.status, 401)
	expect(t, none.header.Get("WWW-Authenticate"), `Bearer realm="scadbuddy"`)
	expect(t, none.header.Get("X-ScadBuddy-Crud"), "1")
	expect(t, none.header.Get("Content-Type"), "application/problem+json")
	expect(t, none.raw, `{"type":"https://scadbuddy.dev/problems/auth/signed-out","title":"Error",`+
		`"status":401,"detail":"This route needs a service credential.","instance":"`+path+
		`","reason":"no_credential"}`)

	for name, c := range map[string]struct {
		token  string
		status int
		reason string
	}{
		"another caller":  {a.credential("render", "crud.render", "crud.agent"), 403, "wrong_caller"},
		"another scope":   {a.credential("agent", "api"), 403, "out_of_scope"},
		"an unknown one":  {"sbsvc_0123456789abcdef." + strings.Repeat("A", 43), 401, "no_credential"},
		"a malformed one": {"sbsvc_x.y", 401, "no_credential"},
	} {
		got := a.do("GET", path, "", func(r *http.Request) { r.Header.Set("Authorization", "bearer "+c.token) })
		if got.status != c.status || got.body["reason"] != c.reason {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}

	a.sql(`INSERT INTO service_caller_suspensions (caller, suspended_by) VALUES ('agent', 'test')`)
	expect(t, a.do("GET", path, "").status, 401)
	a.sql(`DELETE FROM service_caller_suspensions`)
	expect(t, a.do("GET", path, "").status, 200)
	a.sql(`UPDATE service_credentials SET expires_at = now() - interval '1 second'`)
	expect(t, a.do("GET", path, "").status, 401)
}

func TestACredentialsUseIsWrittenOnceAMinute(t *testing.T) {
	a := all(t)
	path := "/sessions/" + a.session(nil) + "/events/last-seq"
	used := func() (at time.Time) {
		if err := a.pool.QueryRow(context.Background(),
			`SELECT last_used_at FROM service_credentials`).Scan(&at); err != nil {
			t.Fatal(err)
		}
		return at
	}
	a.ok("GET", path, "")
	first := used()
	a.ok("GET", path, "")
	expect(t, used(), first)
	a.sql(`UPDATE service_credentials SET last_used_at = now() - interval '61 seconds'`)
	a.ok("GET", path, "")
	if time.Since(used()) > 30*time.Second {
		t.Fatal("an old use was not written again")
	}
}

func TestAnAppendTakesTheNextSeqsAndReadsBackInOrder(t *testing.T) {
	a := all(t)
	sid := a.session(nil)
	started := `{"type":"session.started","title":"é 🙂 <b>"}`
	expect(t, a.append(sid, started), `{"seqs":[1]}`)
	expect(t, a.append(sid, delta("a"), delta("b")), `{"seqs":[2,3]}`)
	events := a.ok("GET", "/sessions/"+sid+"/events", "").body["events"].([]any)
	expect(t, len(events), 3)
	expect(t, events[0], map[string]any{"seq": 1.0, "event": started, "inherited": false})
	past := a.ok("GET", "/sessions/"+sid+"/events?after=2", "").body["events"].([]any)
	expect(t, len(past), 1)
	page := a.ok("GET", "/sessions/"+sid+"/events?after=0&limit=2", "").body["events"].([]any)
	expect(t, len(page), 2)
	expect(t, a.ok("GET", "/sessions/"+sid+"/events/last-seq", "").raw, `{"seq":3}`)
}

func TestASessionWithNoRowIsGone(t *testing.T) {
	a := all(t)
	missing := uuid()
	got := a.do("POST", base+"/sessions/"+missing+"/events", object("events", []string{`{"type":"x"}`}))
	expect(t, got.status, 404)
	expect(t, got.body["reason"], "session_gone")
	expect(t, a.ok("GET", "/sessions/"+missing+"/events", "").raw, `{"events":[]}`)
	expect(t, a.ok("GET", "/sessions/"+missing+"/events/last-seq", "").raw, `{"seq":0}`)
}

func TestTheFenceHoldsATurnsEventsToItsPlacement(t *testing.T) {
	a := all(t)
	turn := uuid()
	sid := a.session(map[string]any{"attempt": "run:1", "turn": turn})
	post := func(attempt, turnID string) answer {
		return a.do("POST", base+"/sessions/"+sid+"/events", object(
			"events", []string{delta("x")},
			"fence", map[string]string{"attempt": attempt, "turn_id": turnID}))
	}
	expect(t, post("run:1", turn).raw, `{"seqs":[1]}`)
	expect(t, post("run:1", strings.ToUpper(turn)).raw, `{"seqs":[2]}`)
	bad := post("run:1", "t")
	expect(t, bad.status, 422)
	expect(t, bad.body["reason"], "turn_id")
	expect(t, post("run:0", turn).raw, `{"seqs":null}`)
	a.sql(`UPDATE ai_sessions SET durable_turn_id = NULL`)
	expect(t, post("run:1", turn).raw, `{"seqs":null}`)
	expect(t, a.ok("GET", "/sessions/"+sid+"/events/last-seq", "").raw, `{"seq":2}`)
}

// waits runs request while a transaction that ran held is open, checks that the request does
// not answer until it commits, and returns its answer.
func (a *api) waits(request func() answer, held ...string) answer {
	a.t.Helper()
	tx, err := a.pool.Begin(context.Background())
	if err != nil {
		a.t.Fatal(err)
	}
	defer tx.Rollback(context.Background())
	for _, statement := range held {
		if _, err := tx.Exec(context.Background(), statement); err != nil {
			a.t.Fatal(err)
		}
	}
	done := make(chan answer, 1)
	go func() { done <- request() }()
	select {
	case got := <-done:
		a.t.Fatalf("answered before the lock was released: %d %s", got.status, got.raw)
	case <-time.After(300 * time.Millisecond):
	}
	if err := tx.Commit(context.Background()); err != nil {
		a.t.Fatal(err)
	}
	return <-done
}

func TestTheFenceWaitsForAWriterOfTheRow(t *testing.T) {
	a := all(t)
	turn := uuid()
	sid := a.session(map[string]any{"attempt": "run:1", "turn": turn})
	got := a.waits(func() answer {
		return a.do("POST", base+"/sessions/"+sid+"/events", object("events", []string{delta("x")},
			"fence", map[string]string{"attempt": "run:1", "turn_id": turn}))
	}, "UPDATE ai_sessions SET turn_attempt = 'run:2' WHERE id = '"+sid+"'")
	expect(t, got.raw, `{"seqs":null}`)
}

func TestAForkReadsItsParentsConversationThroughTheCut(t *testing.T) {
	a := all(t)
	parent, turn := a.session(nil), uuid()
	a.append(parent, `{"type":"session.started"}`)
	a.append(parent, userTurn(turn, "make a box"), delta("ok"))
	a.append(parent, `{"type":"session.status"}`)
	fork := func(of string, cut int, then ...string) (string, string) {
		child := a.session(map[string]any{"parent": of})
		if then == nil {
			then = []string{}
		}
		return child, a.ok("POST", "/sessions/"+child+"/fork-log", object("parent_id", of, "cut", cut,
			"started", []string{`{"type":"session.started"}`}, "then", then)).raw
	}
	child, seqs := fork(parent, 4, `{"type":"session.status"}`)
	expect(t, seqs, `{"seqs":[1,5]}`)
	a.append(parent, delta("late"))
	read := func(id string) (out []string) {
		for _, e := range a.ok("GET", "/sessions/"+id+"/events", "").body["events"].([]any) {
			row := e.(map[string]any)
			var event map[string]any
			_ = json.Unmarshal([]byte(row["event"].(string)), &event)
			out = append(out, fmt.Sprintf("%v %v %v", row["seq"], event["type"], row["inherited"]))
		}
		return out
	}
	expect(t, read(child), []string{
		"1 session.started false", "2 user.turn true", "3 assistant.text.delta true", "5 session.status false",
	})
	grand, seqs := fork(child, 2)
	expect(t, seqs, `{"seqs":[1]}`)
	expect(t, read(grand), []string{"1 session.started false", "2 user.turn true"})
	expect(t, a.ok("GET", "/sessions/"+grand+"/events/last-seq", "").raw, `{"seq":2}`)
}

func TestAForkLogWaitsForAForgetOfTheParentAndIsThenRefused(t *testing.T) {
	a := all(t)
	parent := a.session(nil)
	child := a.session(map[string]any{"parent": parent})
	got := a.waits(func() answer {
		return a.do("POST", base+"/sessions/"+child+"/fork-log", object("parent_id", parent, "cut", 0,
			"started", []string{`{"type":"x"}`}, "then", []string{}))
	},
		"SELECT pg_advisory_xact_lock(hashtext('scadbuddy:ai_payload_keys:session-"+parent+"'))",
		"UPDATE ai_sessions SET fork_parent_id = NULL, fork_cut = NULL WHERE id = '"+child+"'")
	expect(t, got.status, 404)
	expect(t, got.body["reason"], "parent_gone")
	expect(t, a.ok("GET", "/sessions/"+child+"/events", "").raw, `{"events":[]}`)
}

func TestATurnsWordsAndItsEvents(t *testing.T) {
	a := all(t)
	sid, turn := a.session(nil), uuid()
	path := "/sessions/" + sid + "/events"
	expect(t, a.ok("GET", path+"/user-turn?turn_id="+turn, "").raw, `{"text":null}`)
	expect(t, a.ok("GET", path+"/turn?turn_id="+turn, "").raw, `{"here":true,"after":null,"events":[]}`)
	a.append(sid, delta("before"))
	a.append(sid, userTurn(turn, "first"))
	a.append(sid, userTurn(turn, "again"), delta("a"))
	expect(t, a.ok("GET", path+"/user-turn?turn_id="+turn, "").raw, `{"text":"again"}`)
	first := a.ok("GET", path+"/turn?turn_id="+turn, "").body
	expect(t, first["after"], 4.0)
	expect(t, first["events"], []any{delta("a")})
	a.append(sid, delta("b"))
	expect(t, a.ok("GET", path+"/turn?after=4&turn_id="+turn, "").body["events"], []any{delta("b")})
	expect(t, a.ok("GET", path+"/turn?after=5&turn_id="+turn, "").raw, `{"here":true,"after":5,"events":[]}`)
	expect(t, a.ok("GET", "/sessions/"+uuid()+"/events/turn?turn_id="+turn, "").raw,
		`{"here":false,"after":null,"events":[]}`)
}

func TestTheBodiesAndIDsAreBounded(t *testing.T) {
	a := all(t)
	sid := a.session(nil)
	path := base + "/sessions/" + sid + "/events"
	many := make([]string, 1001)
	for i := range many {
		many[i] = `{"type":"x"}`
	}
	one := []string{`{"type":"x"}`}
	for name, body := range map[string]string{
		"none":          object("events", []string{}),
		"too many":      object("events", many),
		"no JSON":       object("events", []string{"not json"}),
		"a list":        object("events", []string{"[1, 2]"}),
		"no type":       object("events", []string{`{"no":"type"}`}),
		"a number type": object("events", []string{`{"type":1}`}),
		"short attempt": object("events", one, "fence", map[string]string{"attempt": "", "turn_id": "t"}),
		"an extra":      object("events", one, "extra", 1),
		"fence extra":   object("events", one, "fence", map[string]string{"attempt": "a", "turn_id": "t", "x": "y"}),
		"another case":  object("Events", one),
		"no object":     `[]`,
		"no body JSON":  `{`,
		"null events":   `{"events":null}`,
		"null event":    `{"events":[null]}`,
	} {
		got := a.do("POST", path, body)
		if got.status != 422 || got.header.Get("X-ScadBuddy-Crud") != "1" {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	issue := a.do("POST", path, object("events", one, "extra", 1)).body["errors"].([]any)[0].(map[string]any)
	expect(t, issue["loc"], []any{"body", "extra"})

	huge := a.do("POST", path, `{"x":"`+strings.Repeat("y", 32<<20)+`"}`)
	expect(t, huge.status, 413)
	expect(t, huge.header.Get("X-ScadBuddy-Crud"), "")
	chunked := a.do("POST", path, object("events", one), func(r *http.Request) { r.ContentLength = -1 })
	expect(t, chunked.status, 411)

	for _, query := range []string{"?limit=501", "?limit=0", "?after=-1", "?after=x"} {
		expect(t, a.do("GET", path+query, "").status, 422)
	}
	expect(t, a.do("GET", base+"/sessions/not-a-uuid/events", "").status, 422)
	expect(t, a.do("GET", base+"/sessions/"+strings.ToUpper(sid)+"/events", "").status, 422)
	fork := object("parent_id", "../x", "cut", 0, "started", one, "then", []string{})
	expect(t, a.do("POST", base+"/sessions/"+sid+"/fork-log", fork).status, 422)
	expect(t, a.ok("GET", "/sessions/"+sid+"/events/last-seq", "").raw, `{"seq":0}`)
}

func TestARunningTurnsState(t *testing.T) {
	a := all(t)
	turn := uuid()
	sid := a.session(map[string]any{"attempt": "run:1", "turn": turn})
	a.append(sid, delta("a"))
	a.sql(`UPDATE ai_sessions SET cost_usd = 0.25, updated_at = now() - interval '5 seconds'`)
	got := a.ok("GET", "/sessions/"+sid+"/turn?grace_ms=1000", "")
	expect(t, got.body["turn"], map[string]any{
		"status": "idle", "durable_turn_id": turn, "turn_attempt": "run:1",
		"interrupt_requested": false, "owner_kind": "browser", "runtime": "unified",
		"event_seq": "1", "cost_usd": 0.25, "settled": true,
	})
	expect(t, a.ok("GET", "/sessions/"+sid+"/turn?grace_ms=60000", "").body["turn"].(map[string]any)["settled"], false)
	expect(t, a.ok("GET", "/sessions/"+uuid()+"/turn", "").raw, `{"turn":null}`)
}

func TestADatabaseOutOfReachIsNoCredential(t *testing.T) {
	a := all(t)
	sid := a.session(nil)
	a.pool.Close()
	expect(t, a.do("GET", base+"/sessions/"+sid+"/events", "").status, 401)
}

func TestWhatIsNotListedGoesToPython(t *testing.T) {
	a := newAPI(t, "session-events:read")
	sid := a.session(nil)
	events := base + "/sessions/" + sid + "/events"
	for _, c := range []struct {
		method, path string
		python       bool
	}{
		{"GET", events, false},
		{"POST", events, true},
		{"DELETE", events, true},
		{"GET", events + "/", true},
		{"GET", "/" + events, true},
		{"GET", base + "/sessions/" + sid + "/turn", true},
		{"GET", base + "/sessions/%2E%2E/events", true},
		{"GET", "/api/v1/internal/crud/render/sessions/" + sid + "/events", true},
		{"GET", "/healthz", true},
	} {
		got := a.do(c.method, c.path, "")
		if (got.status == http.StatusTeapot) != c.python {
			t.Errorf("%s %s: %d", c.method, c.path, got.status)
		}
		if c.python && got.header.Get("X-ScadBuddy-Crud") != "" {
			t.Errorf("%s %s has the CRUD header", c.method, c.path)
		}
	}
	if _, err := newRouter(a.pool, nil, nil, "session-evnets", "", nil, nil, nil); err == nil {
		t.Error("an unknown area was taken")
	}
}

func TestASealedAreaNeedsTheKEKAndTheOthersDoNot(t *testing.T) {
	pool := testdb.New(t)
	for _, area := range []string{"credentials", "credentials:read", "payload-keys", "keys", "notify",
		"notify:read", "transcripts", "transcripts:read", "blobs"} {
		_, err := newRouter(pool, nil, nil, area, "", nil, nil, nil)
		if err == nil || !strings.Contains(err.Error(), "SCADBUDDY_SECRET_KEY_FILE") {
			t.Errorf("%s: %v", area, err)
		}
	}
	every := "settings, access, plugin-sessions, mcp-relay, session-events, session-turns:read"
	if _, err := newRouter(pool, nil, nil, every, "", nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	key := filepath.Join(t.TempDir(), "kek")
	if err := os.WriteFile(key, []byte(base64.StdEncoding.EncodeToString(make([]byte, 32))+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	keys, err := seal.LoadKeys(func(name string) string {
		return map[string]string{seal.Variable: key}[name]
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := newRouter(pool, nil, keys, every+", credentials, payload-keys, keys, notify, transcripts, blobs", "", nil, nil, nil); err != nil {
		t.Fatal(err)
	}
}

// python is an upstream on a Unix socket, as the Python API is in the pod.
func python(t *testing.T, handler http.HandlerFunc) (front string) {
	t.Helper()
	socket := filepath.Join(t.TempDir(), "s")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	upstream := &http.Server{Handler: handler}
	go upstream.Serve(listener)
	t.Cleanup(func() { upstream.Close() })
	next, err := newUpstream("unix://"+socket, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(next)
	t.Cleanup(server.Close)
	return server.URL
}

func TestAForwardedRequestKeepsItsHeadersAndNamesItsPeer(t *testing.T) {
	var got *http.Request
	var body string
	url := python(t, func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		got, body = r, string(b)
		w.Header().Set("X-Reply", "yes")
		w.WriteHeader(http.StatusAccepted)
	})
	req, _ := http.NewRequest("PUT", url+"/api/v1/a%2Fb//c/../d?x=1&y=%20", strings.NewReader("hello"))
	req.Host = "scadbuddy.example"
	req.Header.Set("X-Forwarded-For", "203.0.113.9, 198.51.100.2")
	req.Header.Set("X-Forwarded-Proto", "https")
	req.Header.Set("Authorization", "Bearer sbpat_x")
	res, err := http.DefaultTransport.RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	expect(t, res.StatusCode, http.StatusAccepted)
	expect(t, res.Header.Get("X-Reply"), "yes")
	expect(t, got.RequestURI, "/api/v1/a%2Fb//c/../d?x=1&y=%20")
	expect(t, got.Host, "scadbuddy.example")
	expect(t, body, "hello")
	// The front is no trusted proxy of its own here: Python is told the TCP peer.
	expect(t, got.Header.Values("X-Forwarded-For"), []string{"127.0.0.1"})
	expect(t, got.Header.Get("X-Forwarded-Proto"), "https")
	expect(t, got.Header.Get("Authorization"), "Bearer sbpat_x")
	if v := got.Header.Get("Accept-Encoding"); v != "gzip" && v != "" {
		t.Fatalf("Accept-Encoding %q", v)
	}
}

func TestAForwardedAnswerStreams(t *testing.T) {
	release := make(chan struct{})
	url := python(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: 1\n\n")
		w.(http.Flusher).Flush()
		<-release
	})
	res, err := http.Get(url + "/api/v1/events")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	first := make([]byte, 9)
	if _, err := io.ReadFull(res.Body, first); err != nil {
		t.Fatal(err)
	}
	close(release)
	expect(t, string(first), "data: 1\n\n")
}

func TestAForwardedUpgradeIsATunnel(t *testing.T) {
	url := python(t, func(w http.ResponseWriter, r *http.Request) {
		conn, rw, err := w.(http.Hijacker).Hijack()
		if err != nil {
			return
		}
		defer conn.Close()
		fmt.Fprintf(rw, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: %s\r\n\r\n",
			r.Header.Get("Upgrade"))
		rw.Flush()
		line, _ := rw.ReadString('\n')
		fmt.Fprint(rw, "echo "+line)
		rw.Flush()
	})
	conn, err := net.Dial("tcp", strings.TrimPrefix(url, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
	fmt.Fprint(conn, "GET /api/v1/ws HTTP/1.1\r\nHost: x\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
	reader := bufio.NewReader(conn)
	for line := ""; line != "\r\n"; {
		if line, err = reader.ReadString('\n'); err != nil {
			t.Fatal(err)
		}
	}
	fmt.Fprint(conn, "hi\n")
	echo, err := reader.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	expect(t, echo, "echo hi\n")
}

func TestAnUpstreamThatIsDownIsABadGateway(t *testing.T) {
	was := dialWait
	dialWait = 0
	t.Cleanup(func() { dialWait = was })
	next, err := newUpstream("unix://"+filepath.Join(t.TempDir(), "none"), nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(next)
	defer server.Close()
	res, err := http.Get(server.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	expect(t, res.StatusCode, http.StatusBadGateway)
	for _, bad := range []string{"", "ftp://x", "8080"} {
		if _, err := newUpstream(bad, nil, nil); err == nil {
			t.Errorf("%q was taken", bad)
		}
	}
}

func TestTheCRUDPortMayNotBeTheMainAddress(t *testing.T) {
	env := map[string]string{
		"SCADBUDDY_CORE_UPSTREAM":      "http://127.0.0.1:1",
		"SCADBUDDY_DATABASE_URL":       "postgres://postgres@127.0.0.1:1/postgres",
		"SCADBUDDY_CORE_LISTEN":        "127.0.0.1:0",
		"SCADBUDDY_CORE_CRUD_LISTEN":   "127.0.0.1:0",
		"SCADBUDDY_CORE_CRUD_UPSTREAM": "http://127.0.0.1:2",
	}
	err := run(context.Background(), func(k string) (string, bool) { v, ok := env[k]; return v, ok }, nil)
	if err == nil || !strings.Contains(err.Error(), "needs its own") {
		t.Fatalf("got %v", err)
	}
}

func TestAFailedBindLeavesNothingListening(t *testing.T) {
	taken, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer taken.Close()
	free, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := free.Addr().String()
	free.Close()
	ok := http.HandlerFunc(func(http.ResponseWriter, *http.Request) {})
	if err := serve(context.Background(), map[string]http.Handler{
		address: ok, taken.Addr().String(): ok,
	}, func(context.Context) {}); err == nil {
		t.Fatal("a taken address was served")
	}
	again, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatalf("the free address is still held: %v", err)
	}
	again.Close()
}

func TestTheCRUDPortNeedsPythonsCRUDListener(t *testing.T) {
	env := map[string]string{
		"SCADBUDDY_CORE_UPSTREAM":    "http://127.0.0.1:1",
		"SCADBUDDY_DATABASE_URL":     "postgres://postgres@127.0.0.1:1/postgres",
		"SCADBUDDY_CORE_LISTEN":      "127.0.0.1:0",
		"SCADBUDDY_CORE_CRUD_LISTEN": "127.0.0.1:1",
	}
	err := run(context.Background(), func(k string) (string, bool) { v, ok := env[k]; return v, ok }, nil)
	if err == nil || !strings.Contains(err.Error(), "SCADBUDDY_CORE_CRUD_UPSTREAM") {
		t.Fatalf("got %v", err)
	}
}

// A setting Python refuses at its start stops core's start too.
func TestABadSettingStopsTheStart(t *testing.T) {
	for name, value := range map[string]string{
		"SCADBUDDY_AUTH_MODE": "", "SCADBUDDY_PRICING_USD_RATE": "-1", "SCADBUDDY_ORDERS_CURRENCY": "XAU",
	} {
		env := map[string]string{name: value}
		err := run(context.Background(), func(k string) (string, bool) { v, ok := env[k]; return v, ok }, nil)
		if err == nil || !strings.Contains(err.Error(), name) {
			t.Errorf("%s=%q: %v", name, value, err)
		}
	}
}
