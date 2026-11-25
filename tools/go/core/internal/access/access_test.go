package access

import (
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
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const base = "/api/v1/internal/crud/agent"

type api struct {
	t     *testing.T
	pool  *pgxpool.Pool
	store *Store
	url   string
	token string
}

func newAPI(t *testing.T) *api {
	t.Helper()
	pool := testdb.New(t)
	router := &crud.Router{
		Resolver: &auth.Resolver{Pool: pool},
		Next: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusTeapot)
		}),
	}
	areas := Areas(pool, nil)
	router.Add("agent", areas["access"]...)
	router.Add("agent", areas["plugin-sessions"]...)
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

func (a *api) count(query string) int {
	a.t.Helper()
	var n int
	if err := a.pool.QueryRow(context.Background(), query).Scan(&n); err != nil {
		a.t.Fatal(err)
	}
	return n
}

type answer struct {
	status int
	body   map[string]any
	raw    string
}

func (a *api) call(token, method, path string, body any) answer {
	a.t.Helper()
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			a.t.Fatal(err)
		}
		reader = strings.NewReader(string(raw))
	}
	req, err := http.NewRequest(method, a.url+base+path, reader)
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

func (a *api) do(method, path string, body any) answer { return a.call(a.token, method, path, body) }

func expect(t *testing.T, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v, want %#v", got, want)
	}
}

func uuid() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}

func jsonOf(v any) any {
	raw, _ := json.Marshal(v)
	var out any
	_ = json.Unmarshal(raw, &out)
	return out
}

var tools = []any{map[string]any{"raw": map[string]any{"name": "search"}, "omitted": []any{}}}

const (
	hash   = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	commit = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)

func (a *api) plugin(name, url string) {
	a.sql(`INSERT INTO ai_plugins (name, kind, url) VALUES ($1, 'remote_mcp', $2)`, name, url)
}

// listen is a LISTEN on the warm-evict channel; notes reads what came within half a second.
func (a *api) listen() func() []string {
	a.t.Helper()
	conn, err := a.pool.Acquire(context.Background())
	if err != nil {
		a.t.Fatal(err)
	}
	a.t.Cleanup(conn.Release)
	if _, err := conn.Exec(context.Background(), "LISTEN scadbuddy_warm_evict"); err != nil {
		a.t.Fatal(err)
	}
	return func() []string {
		out := []string{}
		for {
			ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
			n, err := conn.Conn().WaitForNotification(ctx)
			cancel()
			if err != nil {
				return out
			}
			out = append(out, n.Payload)
		}
	}
}

func TestOnlyTheAgentCredentialReachesTheRoutes(t *testing.T) {
	a := newAPI(t)
	body := map[string]any{"sources": []string{"remote:files"}}
	for _, caller := range []string{"print", "projects"} {
		got := a.call(a.credential(caller, "crud."+caller), "POST", "/access/rows", body)
		expect(t, got.status, 403)
	}
	expect(t, a.call("", "POST", "/access/rows", body).status, 401)
}

func TestAnAccessWriteEvictsAndWritesInOneTransaction(t *testing.T) {
	a := newAPI(t)
	notes := a.listen()
	got := a.do("POST", "/access/write", map[string]any{
		"by": "browser:browser",
		"edits": []any{
			map[string]any{"source": "remote:files", "kind": "items", "items": map[string]any{"*": "on"}},
			map[string]any{"source": "remote:files", "kind": "replace",
				"tools": map[string]any{"a": "read", "b": "off"}},
		},
	})
	expect(t, got.status, 200)
	expect(t, got.raw, `{"applied":[0,1]}`)
	expect(t, notes(), []string{"*"})
	rows := a.do("POST", "/access/rows", map[string]any{"sources": []string{"remote:files"}}).raw
	expect(t, rows, `{"rows":[{"source":"remote:files","item":"*","access":"on","updated_by":"browser:browser"},`+
		`{"source":"remote:files","item":"a","access":"read","updated_by":"browser:browser"},`+
		`{"source":"remote:files","item":"b","access":"off","updated_by":"browser:browser"}]}`)
	one := a.do("GET", "/access/row?source=remote:files&item=a", nil)
	expect(t, one.body["row"].(map[string]any)["access"], "read")
	expect(t, a.do("GET", "/access/row?source=remote:files&item=zz", nil).raw, `{"row":null}`)
	// A null deletes the row; a forget drops the source's listing too.
	a.sql(`INSERT INTO ai_tool_listings (source, tools) VALUES ('remote:files', '[]'::jsonb)`)
	a.do("POST", "/access/write", map[string]any{"by": "system", "edits": []any{
		map[string]any{"source": "remote:files", "kind": "items", "items": map[string]any{"a": nil}}}})
	expect(t, a.count(`SELECT count(*) FROM ai_access WHERE item = 'a'`), 0)
	a.do("POST", "/access/write", map[string]any{"by": "system", "edits": []any{
		map[string]any{"source": "remote:files", "kind": "forget"}}})
	expect(t, a.count(`SELECT count(*) FROM ai_access`), 0)
	expect(t, a.count(`SELECT count(*) FROM ai_tool_listings`), 0)
	expect(t, a.do("POST", "/access/rows", map[string]any{"sources": []string{}}).raw, `{"rows":[]}`)
}

func TestSkipUnchangedDropsAnEditThatChangesNothing(t *testing.T) {
	a := newAPI(t)
	first := map[string]any{"source": "package:p/mcp:s", "kind": "replace", "tools": map[string]any{"t": "write"}}
	other := map[string]any{"source": "package:q/mcp:s", "kind": "replace", "tools": map[string]any{"t": "read"}}
	a.do("POST", "/access/write", map[string]any{"by": "a", "edits": []any{first}})
	notes := a.listen()
	got := a.do("POST", "/access/write", map[string]any{"by": "b", "edits": []any{first, other}, "skip_unchanged": true})
	expect(t, got.raw, `{"applied":[1]}`)
	expect(t, notes(), []string{"*"})
	got = a.do("POST", "/access/write", map[string]any{"by": "b", "edits": []any{first}, "skip_unchanged": true})
	expect(t, got.raw, `{"applied":[]}`)
	// Nothing to write sends nothing.
	expect(t, notes(), []string{})
}

func TestAnAccessWriteOutOfTheModelIsRefused(t *testing.T) {
	a := newAPI(t)
	for _, e := range []map[string]any{
		{"source": "other:x", "kind": "forget"},
		{"source": "remote:files", "kind": "items", "items": map[string]any{"a": "always"}},
		{"source": "remote:files", "kind": "replace", "tools": map[string]any{"*": "read"}},
		{"source": "remote:files", "kind": "forget", "extra": 1},
		{"source": "remote:files", "kind": "other"},
	} {
		got := a.do("POST", "/access/write", map[string]any{"by": "x", "edits": []any{e}})
		if got.status != 422 {
			t.Errorf("%v: %d %s", e, got.status, got.raw)
		}
	}
	// The table's own rule: an item is on only in a package or the own plugin.
	on := map[string]any{"source": "remote:files", "kind": "items", "items": map[string]any{"a": "on"}}
	got := a.do("POST", "/access/write", map[string]any{"by": "x", "edits": []any{on}})
	expect(t, got.status, 422)
	if !strings.HasPrefix(got.body["detail"].(string), "the access table refused the write: ") {
		t.Fatal(got.raw)
	}
	expect(t, a.do("GET", "/access/row?source=nope&item=a", nil).status, 422)
	expect(t, a.do("GET", "/access/row?source=remote:files", nil).status, 422)
	expect(t, a.do("POST", "/access/write", map[string]any{"by": "x", "edits": []any{}}).status, 422)
}

func (a *api) lease(key, token string) answer {
	return a.do("POST", "/leases/"+key, map[string]any{"token": token, "ttl_s": 60})
}

func TestALeaseIsBusyForAnotherHolderAndNaturalForItsOwn(t *testing.T) {
	a := newAPI(t)
	key := "tool-listing:remote:files"
	mine, theirs := uuid(), uuid()
	expect(t, a.lease(key, mine).status, 200)
	held := a.lease(key, mine)
	expect(t, held.status, 200)
	expect(t, held.body["token"], mine)
	busy := a.lease(key, theirs)
	expect(t, []any{busy.status, busy.body["reason"]}, []any{409, "busy"})
	expect(t, a.do("PUT", "/leases/"+key, map[string]any{"token": mine}).status, 200)
	lost := a.do("PUT", "/leases/"+key, map[string]any{"token": theirs})
	expect(t, []any{lost.status, lost.body["reason"]}, []any{409, "lease-lost"})
	gone := a.do("DELETE", "/leases/"+key+"?token="+mine, nil)
	expect(t, []any{gone.status, gone.raw}, []any{204, ""})
	expect(t, a.lease(key, theirs).status, 200)
	// A lease past its expiry is taken over.
	a.sql(`UPDATE agent_leases SET expires_at = now() - interval '1 second'`)
	expect(t, a.lease(key, mine).status, 200)
	expect(t, a.lease("something-else", mine).status, 422)
	expect(t, a.do("POST", "/leases/"+key, map[string]any{"token": "nope"}).status, 422)
	expect(t, a.do("POST", "/leases/"+key, map[string]any{"token": mine, "ttl_s": 0}).status, 422)
	expect(t, a.do("DELETE", "/leases/"+key, nil).status, 422)
	// A token in another spelling is the same token.
	upper := strings.ToUpper(strings.ReplaceAll(mine, "-", ""))
	expect(t, a.lease(key, upper).body["token"], mine)
}

func TestALeaseIsBusyWhileAnSQLPodHoldsTheAdvisoryLock(t *testing.T) {
	a := newAPI(t)
	ctx := context.Background()
	old, err := a.pool.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer old.Release()
	old.Exec(ctx, `SELECT pg_advisory_lock(hashtextextended('scadbuddy:ai_tool_listings:test:files', 0))`)
	busy := a.lease("tool-listing:remote:files", uuid())
	expect(t, []any{busy.status, busy.body["reason"]}, []any{409, "busy"})
	server := "tool-listing:package:pkg/mcp:srv"
	old.Exec(ctx, `SELECT pg_advisory_lock(hashtextextended('scadbuddy:ai_tool_listings:test:package:pkg/mcp:srv', 0))`)
	expect(t, a.lease(server, uuid()).status, 409)
	old.Exec(ctx, `SELECT pg_advisory_unlock_all()`)
	expect(t, a.lease(server, uuid()).status, 200)
	// The other way round is not covered, by design: the lock is the acquire's own.
	expect(t, a.lease("tool-listing:remote:files", uuid()).status, 200)
	var got bool
	old.QueryRow(ctx, `SELECT pg_try_advisory_lock(hashtextextended('scadbuddy:ai_tool_listings:test:files', 0))`).Scan(&got)
	expect(t, got, true)
	old.Exec(ctx, `SELECT pg_advisory_unlock_all()`)
}

func TestAListingIsStoredOnlyWhileTheLeaseHoldsAndThePluginStands(t *testing.T) {
	a := newAPI(t)
	a.plugin("files", "https://mcp.example/")
	tok := uuid()
	body := map[string]any{"name": "files", "url": "https://mcp.example/", "tools": tools, "lease_token": tok}
	lost := a.do("POST", "/tool-listings/plugin", body)
	expect(t, []any{lost.status, lost.body["reason"]}, []any{409, "lease-lost"})
	expect(t, a.lease("tool-listing:remote:files", tok).status, 200)
	expect(t, a.do("POST", "/tool-listings/plugin", body).raw, `{"stored":true}`)
	moved := map[string]any{"name": "files", "url": "https://elsewhere.example/", "tools": tools, "lease_token": tok}
	expect(t, a.do("POST", "/tool-listings/plugin", moved).raw, `{"stored":false}`)
	read := a.do("POST", "/tool-listings/read", map[string]any{"sources": []string{"remote:files"}})
	listings := read.body["listings"].([]any)
	expect(t, len(listings), 1)
	l := listings[0].(map[string]any)
	expect(t, []any{l["source"], l["tools"], l["truncated"]}, []any{"remote:files", jsonOf(tools), false})
	if _, err := time.Parse(time.RFC3339Nano, l["listed_at"].(string)); err != nil {
		t.Fatal(err)
	}
	// An expired lease is lost too.
	a.sql(`UPDATE agent_leases SET expires_at = now() - interval '1 second'`)
	expect(t, a.do("POST", "/tool-listings/plugin", body).body["reason"], "lease-lost")
}

func TestAPackageServersListingNeedsItsPin(t *testing.T) {
	a := newAPI(t)
	a.sql(`INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref, fetch_url,
		commit_sha, content_hash, files, review) VALUES ('pkg', 'git', 'https://g.example/r',
		'main', 'https://g.example/r', $1, $2, '{}', '{}')`, commit, hash)
	tok := uuid()
	expect(t, a.lease("tool-listing:package:pkg/mcp:srv", tok).status, 200)
	body := map[string]any{"pkg": "pkg", "server": "srv", "content_hash": hash, "tools": tools,
		"truncated": true, "lease_token": tok}
	expect(t, a.do("POST", "/tool-listings/package-server", body).raw, `{"stored":true}`)
	body["content_hash"] = "sha256:" + strings.Repeat("c", 64)
	expect(t, a.do("POST", "/tool-listings/package-server", body).raw, `{"stored":false}`)
	var source string
	var truncated bool
	if err := a.pool.QueryRow(context.Background(),
		`SELECT source, truncated FROM ai_tool_listings`).Scan(&source, &truncated); err != nil {
		t.Fatal(err)
	}
	expect(t, []any{source, truncated}, []any{"package:pkg/mcp:srv", true})
	body["content_hash"] = hash
	body["tools"] = []any{map[string]any{"raw": map[string]any{"title": "x"}, "omitted": []any{}}}
	expect(t, a.do("POST", "/tool-listings/package-server", body).status, 422)
	body["tools"] = []any{map[string]any{"raw": map[string]any{"name": "x"}, "omitted": []any{"nope"}}}
	expect(t, a.do("POST", "/tool-listings/package-server", body).status, 422)
}

func (a *api) signedIn(plugin string) {
	a.plugin(plugin, "https://"+plugin+".example/mcp")
	a.sql(`INSERT INTO ai_plugin_oauth (plugin, plugin_url, resource, issuer, token_endpoint,
		client_id, auth_method, registration, secret_sealed, dek_sealed, kek_id, has_refresh,
		grant_id, connected_by) VALUES ($1, $2, $2, 'https://auth.example',
		'https://auth.example/token', 'c', 'none', 'dynamic', '\x00', '\x00', 'k', true,
		gen_random_uuid(), 'test')`, plugin, "https://"+plugin+".example/mcp")
}

func TestARefreshLeaseWaitsForAnSQLPodsRowLockOnlyAsLongAsAsked(t *testing.T) {
	a := newAPI(t)
	a.signedIn("docs")
	ctx := context.Background()
	ask := map[string]any{"token": uuid(), "ttl_s": 60, "wait_s": 0.5}
	old, err := a.pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := old.Exec(ctx, `SELECT 1 FROM ai_plugin_oauth WHERE plugin = 'docs' FOR UPDATE`); err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	busy := a.do("POST", "/leases/plugin-oauth:docs", ask)
	if time.Since(started) > 5*time.Second {
		t.Fatal("the acquire waited past its wait")
	}
	expect(t, []any{busy.status, busy.body["reason"]}, []any{409, "busy"})
	_ = old.Rollback(ctx)
	expect(t, a.count(`SELECT count(*) FROM agent_leases`), 0)
	expect(t, a.do("POST", "/leases/plugin-oauth:docs", ask).status, 200)
}

func TestARefreshLeasePutsBackTheLockTimeoutInForce(t *testing.T) {
	a := newAPI(t)
	a.signedIn("docs")
	ctx := context.Background()
	err := pgx.BeginFunc(ctx, a.pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SET LOCAL lock_timeout = '7s'`); err != nil {
			return err
		}
		expires, err := acquire(ctx, tx, "plugin-oauth:docs", uuid(), 60, 0.5)
		if err != nil || expires == nil {
			return fmt.Errorf("not held: %v", err)
		}
		var setting string
		if err := tx.QueryRow(ctx, `SELECT current_setting('lock_timeout')`).Scan(&setting); err != nil {
			return err
		}
		expect(t, setting, "7s")
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestPastMaxRowWaitsAnAcquireDoesNotWaitOnTheGrantRow(t *testing.T) {
	a := newAPI(t)
	s := &Store{Pool: a.pool}
	expect(t, s.rowWait("plugin-oauth:docs"), true)
	expect(t, s.rowWait("plugin-oauth:docs"), true)
	expect(t, s.rowWait("plugin-oauth:docs"), false)
	s.rowWaitDone()
	expect(t, s.rowWait("tool-listing:remote:files"), false)
	expect(t, s.rowWait("plugin-oauth:docs"), true)
}

func TestPluginSessionsAreRecordedAndSweptByReplica(t *testing.T) {
	a := newAPI(t)
	seen := func(plugin, id, replica string) {
		t.Helper()
		got := a.do("POST", "/plugin-sessions/seen", map[string]any{"plugin": plugin, "id": id, "replica": replica})
		expect(t, got.raw, `{"ok":true}`)
	}
	seen("files", "s1", "dead-host")
	seen("files", "s1", "dead-host")
	seen("pkg:srv", "s2", "live-host")
	seen("files", "s3", "me")
	a.sql(`UPDATE ai_plugin_sessions SET created_at = now() - interval '10 minutes'`)
	dead := a.do("POST", "/plugin-sessions/dead", map[string]any{"live": []string{"live-host", "me"},
		"grace_ms": 300_000, "give_up_ms": 86_400_000})
	expect(t, dead.raw, `{"rows":[{"plugin_id":"files","upstream_session_id":"s1","old":false}]}`)
	mark := a.do("GET", "/plugin-sessions/mark", nil).body["mark"].(string)
	seen("files", "s4", "me")
	own := a.do("POST", "/plugin-sessions/own", map[string]any{"replica": "me", "mark": mark})
	expect(t, own.raw, `{"rows":[{"plugin_id":"files","upstream_session_id":"s3","old":false}]}`)
	a.do("POST", "/plugin-sessions/ended", map[string]any{"plugin": "files", "id": "s1"})
	expect(t, a.count(`SELECT count(*) FROM ai_plugin_sessions`), 3)
	bad := a.do("POST", "/plugin-sessions/own", map[string]any{"replica": "me", "mark": "not a time!"})
	expect(t, []any{bad.status, bad.body["detail"]}, []any{422, "That is not a mark."})
	expect(t, a.do("POST", "/plugin-sessions/dead", map[string]any{"live": []string{},
		"grace_ms": 1, "give_up_ms": 1}).status, 422)
}

func TestARestSegmentTakesTheWholeKey(t *testing.T) {
	a := newAPI(t)
	key := "tool-listing:package:a/b/c/mcp:srv"
	held := a.lease(key, uuid())
	expect(t, held.status, 200)
	var got string
	if err := a.pool.QueryRow(context.Background(), `SELECT key FROM agent_leases`).Scan(&got); err != nil {
		t.Fatal(err)
	}
	expect(t, got, key)
	// An empty key is not this route's: Python answers it.
	expect(t, a.do("POST", "/leases/", map[string]any{"token": uuid()}).status, http.StatusTeapot)
}
