package transcripts

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
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
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const base = "/api/v1/internal/crud/agent"

var kek = seal.NewKEK(bytes.Repeat([]byte{0x41}, 32))

type api struct {
	t     *testing.T
	pool  *pgxpool.Pool
	url   string
	token string
}

func newAPI(t *testing.T, keys *seal.Keys) *api {
	t.Helper()
	pool := testdb.New(t)
	router := &crud.Router{
		Resolver: &auth.Resolver{Pool: pool},
		Next: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusTeapot)
		}),
	}
	router.Add("agent", Areas(pool, keys)["transcripts"]...)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	a.token = a.credential("agent", "crud.agent")
	return a
}

func agent(t *testing.T) *api { return newAPI(t, &seal.Keys{Current: kek}) }

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
	body   map[string]any
	raw    string
}

func (a *api) do(method, path string, body any) answer {
	a.t.Helper()
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			a.t.Fatal(err)
		}
		reader = bytes.NewReader(raw)
	}
	return a.raw(method, path, reader, a.token)
}

func (a *api) raw(method, path string, body io.Reader, token string) answer {
	a.t.Helper()
	req, err := http.NewRequest(method, a.url+base+path, body)
	if err != nil {
		a.t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	out := answer{status: res.StatusCode, raw: string(raw)}
	_ = json.Unmarshal(raw, &out.body)
	return out
}

func (a *api) ok(method, path string, body any) map[string]any {
	a.t.Helper()
	out := a.do(method, path, body)
	if out.status != http.StatusOK {
		a.t.Fatalf("%s %s: %d %s", method, path, out.status, out.raw)
	}
	return out.body
}

func texts(entries []map[string]any) []string {
	out := make([]string, len(entries))
	for i, e := range entries {
		raw, _ := json.Marshal(e)
		out[i] = string(raw)
	}
	return out
}

func (a *api) append(session string, entries []map[string]any, extra ...any) map[string]any {
	a.t.Helper()
	body := map[string]any{"project_key": "p", "subpath": "", "entries": texts(entries)}
	for i := 0; i < len(extra); i += 2 {
		body[extra[i].(string)] = extra[i+1]
	}
	return a.ok("POST", "/sessions/"+session+"/entries", body)
}

func (a *api) load(session string) []map[string]any {
	a.t.Helper()
	got := a.ok("GET", "/sessions/"+session+"/transcript", nil)["entries"]
	if got == nil {
		return nil
	}
	var out []map[string]any
	for _, text := range got.([]any) {
		var e map[string]any
		if err := json.Unmarshal([]byte(text.(string)), &e); err != nil {
			a.t.Fatal(err)
		}
		out = append(out, e)
	}
	return out
}

func (a *api) uuids(session string) []string {
	a.t.Helper()
	out := []string{}
	for _, e := range a.load(session) {
		out = append(out, e["uuid"].(string))
	}
	return out
}

func uuid() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}

type cols struct {
	parent, attempt, turn *string
	cut                   *int
}

func (a *api) session(c cols) string {
	a.t.Helper()
	id := uuid()
	a.sql(`INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,
		creator_id, status, max_turns, budget_usd, fork_parent_id, fork_cut, turn_attempt,
		durable_turn_id) VALUES ($1::text::uuid, 'chat', 'browser', 'browser', 'You', 'browser',
		'browser', 'idle', 10, 1, $2::text::uuid, $3, $4, $5::text::uuid)`,
		id, c.parent, c.cut, c.attempt, c.turn)
	return id
}

func ptr[T any](v T) *T { return &v }

func entriesOf(n int, tag string) []map[string]any {
	out := make([]map[string]any, n)
	for i := range out {
		out[i] = map[string]any{"type": "user", "uuid": fmt.Sprintf("%s-%d", tag, i), "message": fmt.Sprintf("%s %d", tag, i)}
	}
	return out
}

// dek is the session's data key, opened as the API opens it.
func (a *api) dek(session string) []byte {
	a.t.Helper()
	var sealed []byte
	if err := a.pool.QueryRow(context.Background(),
		`SELECT dek_sealed FROM ai_payload_keys WHERE subject = $1`, "session-"+session).Scan(&sealed); err != nil {
		a.t.Fatal(err)
	}
	dek, err := seal.OpenBytes(kek.Key(), sealed, seal.PayloadKeyContext("session-"+session))
	if err != nil {
		a.t.Fatal(err)
	}
	return dek
}

func expect(t *testing.T, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v, want %#v", got, want)
	}
}

func TestOnlyTheAgentCredentialReachesTheRoutes(t *testing.T) {
	a := agent(t)
	path := "/sessions/" + a.session(cols{}) + "/transcript"
	expect(t, a.raw("GET", path, nil, "").status, http.StatusUnauthorized)
	for _, caller := range []string{"render", "print", "projects"} {
		expect(t, a.raw("GET", path, nil, a.credential(caller, "crud."+caller)).status, http.StatusForbidden)
	}
	if Areas(a.pool, nil) != nil {
		t.Fatal("the area has routes without a KEK")
	}
}

func TestAnAppendIsSealedWithTheAgentsAADAndLoadsDeepEqual(t *testing.T) {
	a := agent(t)
	sid := a.session(cols{})
	entries := []map[string]any{
		{"type": "user", "uuid": "u1", "cwd": "/w/a", "message": "nul \u0000 and é and 🙂"},
		{"type": "last-prompt", "text": "no uuid"},
		{"type": "Not A Token", "uuid": "u2"},
	}
	expect(t, a.append(sid, entries), map[string]any{"written": true})
	got, _ := json.Marshal(a.load(sid))
	want, _ := json.Marshal(entries)
	expect(t, string(got), string(want))
	rows, _ := a.pool.Query(context.Background(), `SELECT id, entry_type, entry IS NULL, sealed
		FROM ai_session_entries WHERE session_id = $1 ORDER BY id`, sid)
	type stored struct {
		ID     int64
		Type   *string
		NoText bool
		Sealed []byte
	}
	found, err := pgx.CollectRows(rows, pgx.RowToStructByPos[stored])
	if err != nil {
		t.Fatal(err)
	}
	expect(t, []*string{found[0].Type, found[1].Type, found[2].Type}, []*string{ptr("user"), ptr("last-prompt"), nil})
	dek := a.dek(sid)
	for i, bound := range []string{"u1", fmt.Sprintf("#%d", found[1].ID)} {
		if !found[i].NoText {
			t.Fatal("a sealed row holds plaintext")
		}
		plain, err := seal.OpenBytes(dek, found[i].Sealed, "ai_session_entries:"+sid+"::"+bound)
		if err != nil {
			t.Fatal(err)
		}
		expect(t, string(plain), texts(entries[i : i+1])[0])
	}
}

func TestAUUIDIsWrittenOnceAndARowWithoutOneEachTime(t *testing.T) {
	a := agent(t)
	sid := a.session(cols{})
	two := []map[string]any{{"type": "user", "uuid": "u1"}, {"type": "x"}}
	a.append(sid, two)
	a.append(sid, two)
	expect(t, len(a.load(sid)), 3)
}

func TestRowsWrittenByTheAgentAndBeforeSealingLoadToo(t *testing.T) {
	a := agent(t)
	sid := a.session(cols{})
	a.append(sid, []map[string]any{{"type": "user", "uuid": "a"}})
	a.sql(`INSERT INTO ai_session_entries (project_key, session_id, subpath, uuid, entry)
		VALUES ('p', $1, '', 'legacy', '{"type":"user","uuid":"legacy"}')`, sid)
	text := `{"type":"assistant","uuid":"agent"}`
	a.sql(`INSERT INTO ai_session_entries (project_key, session_id, subpath, uuid, entry_type, sealed)
		VALUES ('p', $1, '', 'agent', 'assistant', $2)`,
		sid, seal.SealBytes(a.dek(sid), []byte(text), "ai_session_entries:"+sid+"::agent"))
	expect(t, a.uuids(sid), []string{"a", "legacy", "agent"})
}

func TestTheFenceHoldsTheAppendToItsPlacement(t *testing.T) {
	a := agent(t)
	turn := uuid()
	sid := a.session(cols{attempt: ptr("run:1"), turn: &turn})
	one := func(id string) []map[string]any { return []map[string]any{{"type": "user", "uuid": id}} }
	fence := func(attempt, turn string) map[string]any { return map[string]any{"attempt": attempt, "turn_id": turn} }
	expect(t, a.append(sid, one("a"), "fence", fence("run:1", turn))["written"], true)
	expect(t, a.append(sid, one("b"), "fence", fence("run:0", turn))["written"], false)
	other := uuid()
	expect(t, a.append(sid, one("c"), "fence", fence("run:1", other))["written"], false)
	// Between turns the last placement still writes (what Claude Code writes as it exits).
	a.sql(`UPDATE ai_sessions SET durable_turn_id = NULL WHERE id = $1::text::uuid`, sid)
	expect(t, a.append(sid, one("d"), "fence", fence("run:1", other))["written"], true)
	expect(t, a.uuids(sid), []string{"a", "d"})
}

func TestAForgottenSessionIsNeverWrittenAndReadsAsForgotten(t *testing.T) {
	a := agent(t)
	sid := a.session(cols{})
	a.append(sid, []map[string]any{{"type": "user", "uuid": "a"}})
	a.sql(`INSERT INTO ai_forgotten_subjects (subject) VALUES ($1)`, "session-"+sid)
	body := map[string]any{"project_key": "p", "subpath": "", "entries": []string{`{"type":"x"}`}}
	refused := a.do("POST", "/sessions/"+sid+"/entries", body)
	expect(t, []any{refused.status, refused.body["reason"]}, []any{http.StatusGone, "forgotten"})
	a.sql(`DELETE FROM ai_payload_keys WHERE subject = $1`, "session-"+sid)
	expect(t, a.do("GET", "/sessions/"+sid+"/transcript", nil).status, http.StatusGone)
	var n int
	_ = a.pool.QueryRow(context.Background(), `SELECT count(*) FROM ai_session_entries WHERE session_id = $1`, sid).Scan(&n)
	expect(t, n, 1)
}

func TestAnAppendWaitsForAForgetInFlight(t *testing.T) {
	a := agent(t)
	sid := a.session(cols{})
	a.append(sid, []map[string]any{{"type": "user", "uuid": "a"}})
	ctx := context.Background()
	tx, err := a.pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	subject := "session-" + sid
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, seal.PayloadKeyLock(subject)); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(ctx, `INSERT INTO ai_forgotten_subjects (subject) VALUES ($1)`, subject); err != nil {
		t.Fatal(err)
	}
	done := make(chan int, 1)
	go func() {
		body := map[string]any{"project_key": "p", "subpath": "", "entries": []string{`{"type":"x"}`}}
		done <- a.do("POST", "/sessions/"+sid+"/entries", body).status
	}()
	select {
	case status := <-done:
		t.Fatalf("the append did not wait: %d", status)
	case <-time.After(500 * time.Millisecond):
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	expect(t, <-done, http.StatusGone)
}

func TestAKeySealedUnderAKEKTheAPILacksIsUnavailable(t *testing.T) {
	a := agent(t)
	sid := a.session(cols{})
	a.append(sid, entriesOf(1, "a"))
	a.sql(`UPDATE ai_payload_keys SET kek_id = 'another' WHERE subject = $1`, "session-"+sid)
	for _, c := range []struct {
		method, path string
		body         any
	}{
		{"GET", "/sessions/" + sid + "/transcript", nil},
		{"POST", "/sessions/" + sid + "/entries", map[string]any{"project_key": "p", "subpath": "", "entries": []string{"{}"}}},
	} {
		got := a.do(c.method, c.path, c.body)
		expect(t, []any{got.status, got.body["reason"]}, []any{http.StatusServiceUnavailable, "unavailable"})
	}
}

func TestAPreviousKEKStillOpens(t *testing.T) {
	old := seal.NewKEK(bytes.Repeat([]byte{0x42}, 32))
	a := newAPI(t, &seal.Keys{Current: old})
	sid := a.session(cols{})
	a.append(sid, entriesOf(1, "a"))
	rotated := &Store{Pool: a.pool, Keys: &seal.Keys{Current: kek, Previous: old}}
	got, err := rotated.transcript(context.Background(), sid)
	if err != nil || len(got) != 1 {
		t.Fatal(got, err)
	}
}

func TestAForkReadsItsParentsTranscriptThroughTheCut(t *testing.T) {
	a := agent(t)
	parent := a.session(cols{})
	a.append(parent, entriesOf(5, "p"))
	child := a.session(cols{parent: &parent, cut: ptr(3)})
	a.append(child, entriesOf(2, "c"))
	grand := a.session(cols{parent: &child, cut: ptr(4)})
	a.append(grand, entriesOf(1, "g"))
	expect(t, a.uuids(child), []string{"p-0", "p-1", "p-2", "c-0", "c-1"})
	expect(t, a.uuids(grand), []string{"p-0", "p-1", "p-2", "c-0", "g-0"})
	// The parent's rows written past the cut are never the child's.
	a.append(parent, entriesOf(1, "late"))
	expect(t, a.uuids(child), []string{"p-0", "p-1", "p-2", "c-0", "c-1"})
	segs, err := (&Store{Pool: a.pool, Keys: &seal.Keys{Current: kek}}).segments(context.Background(), child)
	if err != nil {
		t.Fatal(err)
	}
	expect(t, []int{len(segs[0].rows), len(segs[1].rows)}, []int{3, 2})
	expect(t, a.ok("GET", "/sessions/"+child+"/transcript/head", nil)["exists"], true)
}

func TestRowsAForgetCopiedInAreReadFirst(t *testing.T) {
	a := agent(t)
	sid := a.session(cols{})
	a.append(sid, []map[string]any{{"type": "user", "uuid": "own"}})
	text := `{"type":"user","uuid":"copied"}`
	a.sql(`INSERT INTO ai_session_entries (project_key, session_id, subpath, uuid, entry_type,
		sealed, inherited) VALUES ('p', $1, '', 'copied', 'user', $2, true)`,
		sid, seal.SealBytes(a.dek(sid), []byte(text), "ai_session_entries:"+sid+"::copied"))
	expect(t, a.uuids(sid), []string{"copied", "own"})
}

func TestAnAncestorForgottenMidReadIsReadAgain(t *testing.T) {
	a := agent(t)
	parent := a.session(cols{})
	a.append(parent, entriesOf(2, "p"))
	child := a.session(cols{parent: &parent, cut: ptr(2)})
	a.sql(`DELETE FROM ai_payload_keys WHERE subject = $1`, "session-"+parent)
	expect(t, a.do("GET", "/sessions/"+child+"/transcript", nil).status, http.StatusGone)
}

func TestTheReads(t *testing.T) {
	a := agent(t)
	sid := a.session(cols{})
	a.append(sid, []map[string]any{
		{"type": "user", "uuid": "a", "cwd": "/w/one"},
		{"type": "cost-state", "usd": 1},
		{"type": "assistant", "uuid": "b", "cwd": "/w/two"},
		{"type": "cost-state", "usd": 2},
	})
	a.append(sid, []map[string]any{{"type": "user", "uuid": "s1"}}, "subpath", "subagents/agent-1")
	at := "/sessions/" + sid
	expect(t, a.ok("GET", at+"/entries/last?type=cost-state", nil), map[string]any{"entry": `{"type":"cost-state","usd":2}`})
	expect(t, a.ok("GET", at+"/entries/last?type=nothing", nil), map[string]any{"entry": nil})
	expect(t, a.ok("GET", at+"/cwd", nil), map[string]any{"cwd": "/w/two"})
	expect(t, a.ok("GET", at+"/subkeys", nil), map[string]any{"subkeys": []any{"subagents/agent-1"}})
	sub := a.ok("GET", at+"/entries?subpath=subagents/agent-1", nil)["entries"].([]any)
	expect(t, sub, []any{`{"type":"user","uuid":"s1"}`})
	expect(t, a.ok("GET", at+"/entries?subpath=none", nil), map[string]any{"entries": nil})
	listed := a.ok("GET", "/transcripts?project_key=p", nil)["sessions"].([]any)
	if len(listed) != 1 || listed[0].(map[string]any)["session_id"] != sid || listed[0].(map[string]any)["mtime"].(float64) <= 0 {
		t.Fatal(listed)
	}
	head := a.ok("GET", at+"/transcript/head", nil)
	since, _ := head["since"].(string)
	if head["exists"] != true || since == "" {
		t.Fatal(head)
	}
	moved := func(own ...string) any {
		return a.ok("POST", at+"/transcript/moved", map[string]any{"since": since, "own": append([]string{}, own...)})["moved"]
	}
	expect(t, moved(), false)
	a.append(sid, []map[string]any{{"type": "user", "uuid": "other"}})
	expect(t, moved(), true)
	expect(t, moved("other"), false)
	expect(t, a.ok("GET", at+"/entries/landed?after="+since, nil), map[string]any{"landed": true})
	var last int64
	_ = a.pool.QueryRow(context.Background(), `SELECT max(id) FROM ai_session_entries WHERE session_id = $1`, sid).Scan(&last)
	expect(t, a.ok("GET", fmt.Sprintf("%s/entries/landed?after=%d", at, last), nil), map[string]any{"landed": false})
	empty := "/sessions/" + a.session(cols{})
	expect(t, a.ok("GET", empty+"/transcript", nil), map[string]any{"entries": nil})
	expect(t, a.ok("GET", empty+"/transcript/head", nil), map[string]any{"exists": false, "since": nil})
	expect(t, a.ok("GET", empty+"/cwd", nil), map[string]any{"cwd": nil})
}

func TestALegacyUserRowLands(t *testing.T) {
	a := agent(t)
	sid := a.session(cols{})
	a.sql(`INSERT INTO ai_session_entries (project_key, session_id, subpath, entry)
		VALUES ('p', $1, '', '{"type":"user","message":"old"}')`, sid)
	expect(t, a.ok("GET", "/sessions/"+sid+"/entries/landed?after=0", nil), map[string]any{"landed": true})
	expect(t, a.ok("GET", "/sessions/"+sid+"/entries/last?type=user", nil)["entry"], `{"type":"user","message":"old"}`)
}

func TestAForkWithNoRowsOfItsOwnTakesItsCwdThroughTheChain(t *testing.T) {
	a := agent(t)
	parent := a.session(cols{})
	a.append(parent, []map[string]any{{"type": "user", "uuid": "a", "cwd": "/w/parent"}})
	child := a.session(cols{parent: &parent, cut: ptr(1)})
	expect(t, a.ok("GET", "/sessions/"+child+"/cwd", nil), map[string]any{"cwd": "/w/parent"})
	expect(t, a.ok("GET", "/sessions/"+child+"/transcript/head", nil), map[string]any{"exists": true, "since": nil})
}

func TestADeleteTakesASubpathOrEverything(t *testing.T) {
	a := agent(t)
	sid := a.session(cols{})
	a.append(sid, entriesOf(1, "m"))
	a.append(sid, entriesOf(1, "s"), "subpath", "subagents/a")
	expect(t, a.ok("POST", "/sessions/"+sid+"/entries/delete", map[string]any{"subpath": "subagents/a"}), map[string]any{"ok": true})
	expect(t, a.ok("GET", "/sessions/"+sid+"/subkeys", nil), map[string]any{"subkeys": []any{}})
	if a.load(sid) == nil {
		t.Fatal("the main transcript went with the subpath")
	}
	a.ok("POST", "/sessions/"+sid+"/entries/delete", map[string]any{"subpath": nil})
	if a.load(sid) != nil {
		t.Fatal("the main transcript stayed")
	}
	expect(t, a.do("POST", "/sessions/"+sid+"/entries/delete", map[string]any{}).status, http.StatusUnprocessableEntity)
}

func TestTheBodiesAndIDsAreBounded(t *testing.T) {
	a := agent(t)
	sid := a.session(cols{})
	path := "/sessions/" + sid + "/entries"
	body := func(k string, v any) map[string]any {
		b := map[string]any{"project_key": "p", "subpath": "", "entries": []string{`{"type":"x"}`}}
		b[k] = v
		return b
	}
	many := make([]string, entriesMax+1)
	for i := range many {
		many[i] = `{"type":"x"}`
	}
	for _, bad := range []map[string]any{
		body("entries", []string{}),
		body("entries", many),
		body("entries", []string{"not json"}),
		body("subpath", strings.Repeat("x", subpathMax+1)),
		body("fence", map[string]any{"attempt": "", "turn_id": "t"}),
		body("fence", map[string]any{"attempt": "a", "turn_id": "t", "more": 1}),
		body("extra", 1),
	} {
		if got := a.do("POST", path, bad); got.status != http.StatusUnprocessableEntity {
			t.Errorf("%v: %d %s", bad, got.status, got.raw)
		}
	}
	huge := a.raw("POST", path, strings.NewReader(`{"x":"`+strings.Repeat("y", bodyBytesMax)+`"}`), a.token)
	expect(t, huge.status, http.StatusRequestEntityTooLarge)
	own := make([]string, ownMax+1)
	for i := range own {
		own[i] = "u"
	}
	expect(t, a.do("POST", "/sessions/"+sid+"/transcript/moved", map[string]any{"since": nil, "own": own}).status,
		http.StatusUnprocessableEntity)
	expect(t, a.do("POST", "/sessions/"+sid+"/transcript/moved", map[string]any{"since": "x1", "own": []string{}}).status,
		http.StatusUnprocessableEntity)
	expect(t, a.do("GET", "/sessions/"+sid+"/entries/last?type=Bad+Type", nil).status, http.StatusUnprocessableEntity)
	expect(t, a.do("GET", "/sessions/"+sid+"/entries/landed", nil).status, http.StatusUnprocessableEntity)
	expect(t, a.do("GET", "/sessions/"+sid+"/entries", nil).status, http.StatusUnprocessableEntity)
	expect(t, a.do("GET", "/transcripts", nil).status, http.StatusUnprocessableEntity)
	expect(t, a.do("GET", "/sessions/not-a-uuid/transcript", nil).status, http.StatusUnprocessableEntity)
	var n int
	_ = a.pool.QueryRow(context.Background(), `SELECT count(*) FROM ai_session_entries WHERE session_id = $1`, sid).Scan(&n)
	expect(t, n, 0)
}
