package plugins

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
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const (
	base     = "/api/v1/internal/crud/agent"
	mcpURL   = "https://mcp.example/mcp/"
	docsAAD  = `ai_plugins:{"name":"docs","url":"https://mcp.example/mcp/","auth_header":"X-Key"}`
	stateH   = "abababababababababababababababababababababababababababababababab"
	ticketH  = "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
	bindingH = "efefefefefefefefefefefefefefefefefefefefefefefefefefefefefefefef"
)

var (
	kek   = seal.NewKEK(bytes.Repeat([]byte{0x42}, 32))
	other = seal.NewKEK(bytes.Repeat([]byte{0x07}, 32))
	meta  = map[string]any{
		"issuer":                 "https://auth.example",
		"authorization_endpoint": "https://auth.example/authorize",
		"token_endpoint":         "https://auth.example/token",
		"iss_supported":          true,
		"resource":               mcpURL,
		"scope":                  "read",
		"client_id":              "client-1",
		"auth_method":            "none",
		"registration":           "dynamic",
		"redirect_uri":           "https://scadbuddy.example/api/v1/ai/plugin-oauth/callback",
	}
)

type api struct {
	t     *testing.T
	pool  *pgxpool.Pool
	url   string
	token string
}

func newAPI(t *testing.T, keys *seal.Keys) *api {
	t.Helper()
	pool := testdb.New(t)
	a := &api{t: t, pool: pool}
	a.serve(keys)
	id, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(id)
	_, _ = rand.Read(secret)
	a.token = "sbsvc_" + hex.EncodeToString(id) + "." + base64.RawURLEncoding.EncodeToString(secret)
	hash := sha256.Sum256([]byte(a.token))
	a.sql(`INSERT INTO service_credentials (id, caller, scopes, secret_hash, expires_at, proof)
		VALUES ($1, 'agent', '{api,crud.agent}', $2, now() + interval '15 minutes', 'inprocess')`,
		hex.EncodeToString(id), hash[:])
	return a
}

// serve answers the area remote-plugins over the same database with keys.
func (a *api) serve(keys *seal.Keys) {
	router := &crud.Router{Resolver: &auth.Resolver{Pool: a.pool}, Next: http.NotFoundHandler()}
	router.Add("agent", Areas(a.pool, keys)["remote-plugins"]...)
	server := httptest.NewServer(router)
	a.t.Cleanup(server.Close)
	a.url = server.URL
}

func held(t *testing.T) *api { return newAPI(t, &seal.Keys{Current: kek}) }

func (a *api) sql(query string, args ...any) {
	a.t.Helper()
	if _, err := a.pool.Exec(context.Background(), query, args...); err != nil {
		a.t.Fatal(err)
	}
}

func (a *api) count(query string, args ...any) int {
	a.t.Helper()
	var n int
	if err := a.pool.QueryRow(context.Background(), query, args...).Scan(&n); err != nil {
		a.t.Fatal(err)
	}
	return n
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
		raw, ok := body.(string)
		if !ok {
			b, _ := json.Marshal(body)
			raw = string(b)
		}
		reader = strings.NewReader(raw)
	}
	req, _ := http.NewRequest(method, a.url+base+path, reader)
	req.Header.Set("Authorization", "Bearer "+a.token)
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
	if out.status/100 != 2 {
		a.t.Fatalf("%s %s: %d %s", method, path, out.status, out.raw)
	}
	return out.body
}

func expect(t *testing.T, got, want any) {
	t.Helper()
	g, _ := json.Marshal(got)
	w, _ := json.Marshal(want)
	if string(g) != string(w) {
		t.Fatalf("got %s, want %s", g, w)
	}
}

func refusal(t *testing.T, got answer, status int, reason string) {
	t.Helper()
	if got.status != status || got.body["reason"] != reason {
		t.Fatalf("got %d %s, want %d %s", got.status, got.raw, status, reason)
	}
}

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
			ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
			n, err := conn.Conn().WaitForNotification(ctx)
			cancel()
			if err != nil {
				return out
			}
			out = append(out, n.Payload)
		}
	}
}

func (a *api) envelope(query string, args ...any) seal.Envelope {
	a.t.Helper()
	var e seal.Envelope
	if err := a.pool.QueryRow(context.Background(), query, args...).Scan(
		&e.SecretSealed, &e.DEKSealed, &e.KEKID); err != nil {
		a.t.Fatal(err)
	}
	return e
}

func (a *api) create(extra map[string]any) answer {
	a.t.Helper()
	body := map[string]any{"name": "docs", "url": mcpURL, "by": "system"}
	for k, v := range extra {
		body[k] = v
	}
	return a.do("POST", "/plugins", body)
}

func rowsOf(view map[string]any) [][3]string {
	out := [][3]string{}
	for _, r := range view["access_rows"].([]any) {
		m := r.(map[string]any)
		out = append(out, [3]string{m["item"].(string), m["access"].(string), m["updated_by"].(string)})
	}
	slices.SortFunc(out, func(a, b [3]string) int { return strings.Compare(a[0], b[0]) })
	return out
}

func TestAPluginIsMadeDisabledAndItsSecretSealedAsTheAgentSealsIt(t *testing.T) {
	a := held(t)
	heard := a.listen()
	made := a.create(map[string]any{"auth_header": "X-Key", "secret": "sk-secret-value-1234",
		"tools": map[string]any{"search": "read", "drop": "off"}})
	if made.status != 201 {
		t.Fatalf("%d %s", made.status, made.raw)
	}
	expect(t, []any{made.body["last4"], made.body["opens"], made.body["auth_header"], made.body["kind"]},
		[]any{"1234", true, "X-Key", "remote_mcp"})
	expect(t, rowsOf(made.body), [][3]string{{"*", "off", "system"}, {"drop", "off", "system"},
		{"search", "read", "system"}})
	if !slices.Contains(heard(), "*") {
		t.Fatal("no warm eviction")
	}
	text, err := seal.OpenSecret(kek, a.envelope(`SELECT secret_sealed, dek_sealed, kek_id
		FROM ai_plugins WHERE name = 'docs'`), docsAAD)
	if err != nil || text != "sk-secret-value-1234" {
		t.Fatalf("%q %v", text, err)
	}
	refusal(t, a.create(nil), 409, "plugin-refused")
	expect(t, a.ok("GET", "/plugins/nope", nil), map[string]any{"plugin": nil})
	list := a.ok("GET", "/plugins", nil)["plugins"].([]any)
	expect(t, len(list), 1)
	// A short secret keeps an empty last4, as secrets.ts last4.
	short := a.create(map[string]any{"name": "short", "auth_header": "X", "secret": "abc"})
	expect(t, short.body["last4"], "")
	// The answer's keys, in Python's order.
	raw := a.do("GET", "/plugins/docs", nil).raw
	for _, k := range []string{`"name"`, `"kind"`, `"url"`, `"auth_header"`, `"kek_id"`, `"last4"`,
		`"created_at"`, `"updated_at"`, `"access_rows"`, `"oauth"`, `"opens"`} {
		i := strings.Index(raw, k)
		if i < 0 {
			t.Fatalf("%s missing in %s", k, raw)
		}
		raw = raw[i:]
	}
}

func TestACreateIsCheckedAsPydanticChecksIt(t *testing.T) {
	a := held(t)
	bad := a.do("POST", "/plugins", map[string]any{"name": "Docs", "url": "", "by": "", "zz": 1,
		"tools": map[string]any{"": "read", "x": "none"}})
	expect(t, bad.status, 422)
	var locs []string
	for _, e := range bad.body["errors"].([]any) {
		m := e.(map[string]any)
		parts := []string{}
		for _, l := range m["loc"].([]any) {
			parts = append(parts, l.(string))
		}
		locs = append(locs, strings.Join(parts, ".")+": "+m["msg"].(string))
	}
	expect(t, locs, []string{
		"body.name: String should match pattern '^[a-z][a-z0-9-]{0,30}[a-z0-9]$'",
		"body.url: String should have at least 1 character",
		"body.tools..[key]: String should have at least 1 character",
		"body.tools.x: Input should be 'off', 'read', 'write' or 'outward'",
		"body.by: String should have at least 1 character",
		"body.zz: Extra inputs are not permitted",
	})
	tooMany := map[string]any{}
	for i := range 513 {
		tooMany[strings.Repeat("t", 1+i%100)+string(rune('a'+i/100))] = "read"
	}
	many := a.create(map[string]any{"tools": tooMany})
	expect(t, many.body["errors"].([]any)[0].(map[string]any)["msg"],
		"Dictionary should have at most 512 items after validation, not more")
	refusal(t, a.create(map[string]any{"secret": "sk-secret-value-1234"}), 400, "plugin-refused")
	expect(t, a.do("POST", "/plugins", "").status, 422)
}

func TestAnUpdateKeepsTheSecretOnlyWhereItWasBound(t *testing.T) {
	a := held(t)
	a.create(map[string]any{"auth_header": "X-Key", "secret": "sk-secret-value-1234"})
	update := func(body map[string]any) answer { return a.do("POST", "/plugins/docs/update", body) }
	moved := update(map[string]any{"url": "https://other.example/mcp/"})
	refusal(t, moved, 409, "plugin-refused")
	if !strings.Contains(moved.body["detail"].(string), "needs the secret again") {
		t.Fatal(moved.raw)
	}
	expect(t, update(map[string]any{"auth_header": "X-New"}).status, 409)
	expect(t, update(map[string]any{"auth_header": nil}).status, 409)
	refusal(t, update(map[string]any{"secret": nil, "auth_header": "X-Key"}), 400, "plugin-refused")
	expect(t, update(map[string]any{"nope": 1}).status, 422)
	refusal(t, a.do("POST", "/plugins/gone/update", map[string]any{}), 404, "plugin-refused")
	expect(t, a.do("POST", "/plugins/Bad/update", map[string]any{}).status, 422)
	// A new secret at a new URL: sealed for the new URL, the sign-in and listing dropped.
	a.sql(`INSERT INTO ai_tool_listings (source, tools) VALUES ('remote:docs', '[]'::jsonb)`)
	signedIn(a)
	done := update(map[string]any{"url": "https://other.example/mcp/", "secret": "sk-another-value-9876"})
	if done.status != 200 {
		t.Fatal(done.raw)
	}
	expect(t, []any{done.body["last4"], done.body["oauth"]}, []any{"9876", nil})
	expect(t, a.count(`SELECT count(*) FROM ai_tool_listings`), 0)
	expect(t, a.count(`SELECT count(*) FROM ai_plugin_oauth`), 0)
	text, err := seal.OpenSecret(kek, a.envelope(`SELECT secret_sealed, dek_sealed, kek_id
		FROM ai_plugins WHERE name = 'docs'`),
		`ai_plugins:{"name":"docs","url":"https://other.example/mcp/","auth_header":"X-Key"}`)
	if err != nil || text != "sk-another-value-9876" {
		t.Fatal(err)
	}
	// Enabled and tiers: only the rows the patch names.
	on := update(map[string]any{"enabled": true, "tool_tiers": map[string]any{"search": "write"},
		"by": "browser:browser"})
	expect(t, rowsOf(on.body), [][3]string{{"*", "on", "browser:browser"}, {"search", "write", "browser:browser"}})
	off := update(map[string]any{"disabled_tools": []any{"search", "b", "b"}})
	expect(t, rowsOf(off.body), [][3]string{{"*", "on", "browser:browser"}, {"b", "off", "system"},
		{"search", "off", "system"}})
	removed := update(map[string]any{"secret": nil})
	expect(t, []any{removed.body["auth_header"], removed.body["last4"], removed.body["kek_id"]},
		[]any{nil, nil, nil})
	expect(t, a.count(`SELECT count(*) FROM ai_plugins WHERE secret_sealed IS NULL`), 1)
}

func TestAToolAccessWriteKeepsTheOtherRowsAuthor(t *testing.T) {
	a := held(t)
	a.create(map[string]any{"tools": map[string]any{"a": "read"}})
	a.ok("POST", "/plugins/docs/update", map[string]any{"tool_tiers": map[string]any{"a": "read"},
		"by": "browser:first"})
	setB := a.ok("POST", "/plugins/docs/tool-access", map[string]any{"tool": "b", "access": "off",
		"by": "browser:second"})
	expect(t, rowsOf(setB), [][3]string{{"*", "off", "system"}, {"a", "read", "browser:first"},
		{"b", "off", "browser:second"}})
	dropped := a.ok("POST", "/plugins/docs/tool-access", map[string]any{"tool": "b", "access": nil})
	expect(t, rowsOf(dropped), [][3]string{{"*", "off", "system"}, {"a", "read", "browser:first"}})
	// access is required, though it may be null.
	expect(t, a.do("POST", "/plugins/docs/tool-access", map[string]any{"tool": "b"}).status, 422)
	refusal(t, a.do("POST", "/plugins/gone/tool-access", map[string]any{"tool": "b", "access": nil}),
		404, "plugin-refused")
	// Past MAX_TOOL_ENTRIES off, a refusal; nothing written.
	for i := range 256 {
		a.sql(`INSERT INTO ai_access (source, item, access, updated_by) VALUES ('remote:docs', $1, 'off', 's')`,
			"t"+strings.Repeat("x", i))
	}
	refusal(t, a.do("POST", "/plugins/docs/tool-access", map[string]any{"tool": "new", "access": "off"}),
		400, "plugin-refused")
	expect(t, a.count(`SELECT count(*) FROM ai_access WHERE item = 'new'`), 0)
}

func TestRevealAndOpenEnabledOpenTheHeaderAndNeverFailOnABadRow(t *testing.T) {
	a := held(t)
	a.create(map[string]any{"auth_header": "X-Key", "secret": "sk-secret-value-1234"})
	expect(t, a.ok("POST", "/plugins/open-enabled", nil), map[string]any{"plugins": []any{}})
	a.ok("POST", "/plugins/docs/update", map[string]any{"enabled": true})
	opened := a.ok("POST", "/plugins/open-enabled", nil)["plugins"].([]any)
	expect(t, opened[0].(map[string]any)["header"], map[string]any{"name": "X-Key", "value": "sk-secret-value-1234"})
	revealed := a.ok("POST", "/plugins/docs/reveal", nil)["plugin"].(map[string]any)
	expect(t, []any{revealed["header"].(map[string]any)["value"], revealed["seal_error"]},
		[]any{"sk-secret-value-1234", nil})
	expect(t, a.ok("POST", "/plugins/nope/reveal", nil), map[string]any{"plugin": nil})
	// A row altered in the database fails to open: an answer, never a 500.
	a.sql(`UPDATE ai_plugins SET url = 'https://evil.example/' WHERE name = 'docs'`)
	bad := a.ok("POST", "/plugins/docs/reveal", nil)["plugin"].(map[string]any)
	expect(t, []any{bad["header"], bad["seal_error"]},
		[]any{nil, "sealed value failed authentication (wrong key, or altered)"})
	// Another KEK: listed, not usable, the error Python words.
	a.serve(&seal.Keys{Current: other})
	view := a.ok("GET", "/plugins/docs", nil)["plugin"].(map[string]any)
	expect(t, view["opens"], false)
	wrong := a.ok("POST", "/plugins/docs/reveal", nil)["plugin"].(map[string]any)
	expect(t, wrong["seal_error"], "secret was sealed with key "+kek.ID+
		", but SCADBUDDY_SECRET_KEY_FILE holds key "+other.ID)
	// No KEK on the API.
	a.serve(nil)
	noKey := a.ok("POST", "/plugins/docs/reveal", nil)["plugin"].(map[string]any)
	expect(t, noKey["seal_error"], noKEKToOpen)
	refusal(t, a.create(map[string]any{"name": "other", "auth_header": "X-Key", "secret": strings.Repeat("s", 20)}),
		503, "kek-missing")
	// A plugin with no secret needs no key.
	expect(t, a.create(map[string]any{"name": "plain"}).status, 201)
}

func signedIn(a *api) string {
	a.t.Helper()
	key := grantKey{"docs", mcpURL, mcpURL, "https://auth.example", "https://auth.example/token", "c"}
	e := seal.SealSecret(kek, `{"access_token":"at-1","refresh_token":"rt-1"}`, grantAAD(key))
	grant := wire.UUID4()
	a.sql(`INSERT INTO ai_plugin_oauth (plugin, plugin_url, resource, issuer, token_endpoint,
		client_id, auth_method, registration, secret_sealed, dek_sealed, kek_id, has_refresh,
		grant_id, connected_by) VALUES ('docs', $1, $1, 'https://auth.example',
		'https://auth.example/token', 'c', 'none', 'dynamic', $2, $3, $4, true, $5, 'b')`,
		mcpURL, e.SecretSealed, e.DEKSealed, e.KEKID, grant)
	return grant
}

func TestASignInRunsStartAuthorizeTakeAndStore(t *testing.T) {
	a := held(t)
	a.create(nil)
	start := map[string]any{"state_hash": stateH, "plugin": "docs", "plugin_url": mcpURL,
		"ticket_hash": ticketH, "meta": meta, "secret": map[string]any{"state": "st", "verifier": "ve"},
		"created_by": "browser", "ttl_ms": 600000}
	started := a.do("POST", "/plugin-oauth/flows", start)
	expect(t, []any{started.status, started.body}, []any{201, map[string]any{"done": true}})
	// The row opens as the agent seals it.
	text, err := seal.OpenSecret(kek, a.envelope(`SELECT secret_sealed, dek_sealed, kek_id
		FROM ai_plugin_oauth_flows`), `ai_plugin_oauth_flows:{"state_hash":"`+stateH+
		`","plugin":"docs","plugin_url":"https://mcp.example/mcp/","issuer":"https://auth.example",`+
		`"token_endpoint":"https://auth.example/token","client_id":"client-1",`+
		`"redirect_uri":"https://scadbuddy.example/api/v1/ai/plugin-oauth/callback"}`)
	if err != nil || text != `{"state":"st","verifier":"ve"}` {
		t.Fatalf("%q %v", text, err)
	}
	gone := map[string]any{}
	for k, v := range start {
		gone[k] = v
	}
	gone["plugin"], gone["state_hash"], gone["ticket_hash"] = "nope", strings.Repeat("12", 32), strings.Repeat("34", 32)
	refusal(t, a.do("POST", "/plugin-oauth/flows", gone), 409, "plugin-gone")
	ask := map[string]any{"ticket_hash": ticketH, "binding_hash": bindingH}
	flow := a.ok("POST", "/plugin-oauth/flows/authorize", ask)["flow"].(map[string]any)
	expect(t, flow["secret"], map[string]any{"state": "st", "verifier": "ve", "client_secret": nil})
	expect(t, flow["meta"], meta)
	// A ticket is good once.
	expect(t, a.ok("POST", "/plugin-oauth/flows/authorize", ask), map[string]any{"flow": nil})
	other := a.ok("POST", "/plugin-oauth/flows/take", map[string]any{"state_hash": stateH, "binding_hash": ""})
	expect(t, other, map[string]any{"flow": nil, "held": true})
	taken := a.do("POST", "/plugin-oauth/flows/take", map[string]any{"state_hash": stateH, "binding_hash": bindingH})
	got := taken.body["flow"].(map[string]any)
	expect(t, []any{got["secret"].(map[string]any)["verifier"], got["expired"], got["created_by"], taken.body["held"]},
		[]any{"ve", false, "browser", false})
	if !strings.HasPrefix(taken.raw, `{"flow":{"plugin":"docs","plugin_url":"https://mcp.example/mcp/","meta":{`) {
		t.Fatal(taken.raw)
	}
	heard := a.listen()
	grant := map[string]any{"plugin": "docs", "plugin_url": mcpURL, "resource": mcpURL,
		"issuer": meta["issuer"], "token_endpoint": meta["token_endpoint"], "client_id": "client-1",
		"auth_method": "none", "registration": "dynamic", "scope": "read",
		"secret":      map[string]any{"access_token": "at-1", "refresh_token": "rt-1"},
		"has_refresh": true, "access_expires_at": nil, "connected_by": "browser"}
	expect(t, a.ok("POST", "/plugin-oauth/grants", grant), map[string]any{"stored": true})
	if !slices.Contains(heard(), "*") {
		t.Fatal("no warm eviction")
	}
	// The URL changed during the sign-in: nothing is kept.
	moved := map[string]any{}
	for k, v := range grant {
		moved[k] = v
	}
	moved["plugin_url"] = "https://x/"
	expect(t, a.ok("POST", "/plugin-oauth/grants", moved), map[string]any{"stored": false})
	key := grantKey{"docs", mcpURL, mcpURL, "https://auth.example", "https://auth.example/token", "client-1"}
	stored, err := seal.OpenSecret(kek, a.envelope(`SELECT secret_sealed, dek_sealed, kek_id FROM ai_plugin_oauth`),
		grantAAD(key))
	if err != nil || stored != `{"access_token":"at-1","refresh_token":"rt-1"}` {
		t.Fatalf("%q %v", stored, err)
	}
	view := a.ok("GET", "/plugins/docs", nil)["plugin"].(map[string]any)["oauth"].(map[string]any)
	expect(t, []any{view["status"], view["scope"], view["has_refresh"], view["access_expires_at"]},
		[]any{"connected", "read", true, nil})
	secrets := a.ok("POST", "/plugin-oauth/stored-secrets", nil)
	expect(t, secrets, map[string]any{"secrets": []any{map[string]any{
		"access_token": "at-1", "refresh_token": "rt-1", "client_secret": nil}}})
	opened := a.ok("POST", "/plugin-oauth/grants/docs/open", map[string]any{"plugin_url": mcpURL,
		"grant": strings.ToUpper(view["grant_id"].(string))})
	expect(t, []any{opened["fail"], opened["grant"].(map[string]any)["secret"].(map[string]any)["access_token"]},
		[]any{nil, "at-1"})
}

func TestAFlowIsCheckedAsPydanticChecksIt(t *testing.T) {
	a := held(t)
	bad := a.do("POST", "/plugin-oauth/flows", map[string]any{"state_hash": "x", "plugin": "docs",
		"plugin_url": "u", "ticket_hash": ticketH, "meta": map[string]any{"issuer": "a", "x": 1},
		"secret": 5, "created_by": "b", "ttl_ms": 0})
	var msgs []string
	for _, e := range bad.body["errors"].([]any) {
		m := e.(map[string]any)
		loc := []string{}
		for _, l := range m["loc"].([]any) {
			loc = append(loc, l.(string))
		}
		msgs = append(msgs, strings.Join(loc, ".")+": "+m["msg"].(string))
	}
	want := []string{"body.state_hash: String should match pattern '^[0-9a-f]{64}$'"}
	for _, f := range []string{"authorization_endpoint", "token_endpoint", "iss_supported", "resource",
		"scope", "client_id", "auth_method", "registration", "redirect_uri"} {
		want = append(want, "body.meta."+f+": Field required")
	}
	want = append(want, "body.meta.x: Extra inputs are not permitted",
		"body.secret: Input should be a valid dictionary or instance of FlowSecret",
		"body.ttl_ms: Input should be greater than 0")
	expect(t, msgs, want)
	// The take's binding may be empty, nothing else.
	expect(t, a.do("POST", "/plugin-oauth/flows/take", map[string]any{"state_hash": stateH, "binding_hash": "x"}).status, 422)
	// Without a KEK, an authorize spends nothing.
	a.serve(nil)
	refusal(t, a.do("POST", "/plugin-oauth/flows/authorize", map[string]any{"ticket_hash": ticketH,
		"binding_hash": bindingH}), 503, "kek-missing")
}

func TestAGrantOpenSaysWhyItCannot(t *testing.T) {
	a := held(t)
	a.create(nil)
	open := func(url, grant string) answer {
		return a.do("POST", "/plugin-oauth/grants/docs/open", map[string]any{"plugin_url": url, "grant": grant})
	}
	none := wire.UUID4()
	expect(t, open(mcpURL, none).body, map[string]any{"grant": nil, "fail": "it is not signed in"})
	grant := signedIn(a)
	expect(t, open("https://x/", grant).body["fail"], "it was signed in at another URL")
	expect(t, open(mcpURL, none).body["fail"], "it was signed in again or out since this run loaded it")
	got := open(mcpURL, grant).body["grant"].(map[string]any)
	expect(t, got, map[string]any{"plugin": "docs", "plugin_url": mcpURL, "resource": mcpURL,
		"issuer": "https://auth.example", "token_endpoint": "https://auth.example/token",
		"client_id": "c", "auth_method": "none", "grant_id": grant, "access_expires_at": nil,
		"secret": map[string]any{"access_token": "at-1", "refresh_token": "rt-1", "client_secret": nil}})
	a.sql(`UPDATE ai_plugin_oauth SET client_id = 'other'`)
	expect(t, open(mcpURL, grant).body["fail"],
		"its sign-in cannot be opened (sealed value failed authentication (wrong key, or altered))")
	// The stored-secrets list skips a row the key does not open.
	expect(t, a.ok("POST", "/plugin-oauth/stored-secrets", nil), map[string]any{"secrets": []any{}})
	a.ok("POST", "/plugin-oauth/grants/docs/mark", map[string]any{"reason": "refused"})
	expect(t, open(mcpURL, grant).body["fail"], "refused")
	expect(t, open(mcpURL, "nope").status, 422)
	a.serve(nil)
	expect(t, open(mcpURL, grant).body["fail"], noKEKToOpenSignIn)
	expect(t, a.ok("POST", "/plugin-oauth/stored-secrets", nil), map[string]any{"secrets": []any{}})
}

func TestARefreshIsKeptOnlyUnderItsLease(t *testing.T) {
	a := held(t)
	a.create(nil)
	grant := signedIn(a)
	lease := wire.UUID4()
	refreshed := map[string]any{"lease_token": lease, "grant": grant,
		"secret":      map[string]any{"access_token": "at-2", "refresh_token": "rt-2"},
		"has_refresh": true, "access_expires_at": "2030-01-02T03:04:05Z", "scope": nil}
	refusal(t, a.do("POST", "/plugin-oauth/grants/docs/refreshed", refreshed), 409, "lease-lost")
	// Another holder's lease, or an expired one of this token's, is no lease.
	a.sql(`INSERT INTO agent_leases (key, token, expires_at) VALUES ('plugin-oauth:docs', $1, now() + interval '1 minute')`,
		wire.UUID4())
	refusal(t, a.do("POST", "/plugin-oauth/grants/docs/refreshed", refreshed), 409, "lease-lost")
	a.sql(`UPDATE agent_leases SET token = $1, expires_at = now() - interval '1 second'`, lease)
	refusal(t, a.do("POST", "/plugin-oauth/grants/docs/refreshed", refreshed), 409, "lease-lost")
	a.sql(`UPDATE agent_leases SET expires_at = now() + interval '1 minute'`)
	expect(t, a.ok("POST", "/plugin-oauth/grants/docs/refreshed", refreshed), map[string]any{"stored": true})
	key := grantKey{"docs", mcpURL, mcpURL, "https://auth.example", "https://auth.example/token", "c"}
	text, err := seal.OpenSecret(kek, a.envelope(`SELECT secret_sealed, dek_sealed, kek_id FROM ai_plugin_oauth`),
		grantAAD(key))
	if err != nil || text != `{"access_token":"at-2","refresh_token":"rt-2"}` {
		t.Fatalf("%q %v", text, err)
	}
	var expires time.Time
	var refreshedAt *time.Time
	if err := a.pool.QueryRow(context.Background(), `SELECT access_expires_at, refreshed_at
		FROM ai_plugin_oauth`).Scan(&expires, &refreshedAt); err != nil {
		t.Fatal(err)
	}
	if !expires.Equal(time.Date(2030, 1, 2, 3, 4, 5, 0, time.UTC)) || refreshedAt == nil {
		t.Fatal(expires, refreshedAt)
	}
	// A grant replaced or marked since: nothing kept.
	refreshed["grant"] = wire.UUID4()
	expect(t, a.ok("POST", "/plugin-oauth/grants/docs/refreshed", refreshed), map[string]any{"stored": false})
	refreshed["grant"] = grant
	a.sql(`UPDATE ai_plugin_oauth SET status = 'error'`)
	expect(t, a.ok("POST", "/plugin-oauth/grants/docs/refreshed", refreshed), map[string]any{"stored": false})
}

func TestARowSealedUnderThePreviousKEKStillOpens(t *testing.T) {
	a := held(t)
	a.create(map[string]any{"auth_header": "X-Key", "secret": "sk-secret-value-1234"})
	a.ok("POST", "/plugins/docs/update", map[string]any{"enabled": true})
	grant := signedIn(a)
	a.ok("POST", "/plugin-oauth/flows", map[string]any{"state_hash": stateH, "plugin": "docs",
		"plugin_url": mcpURL, "ticket_hash": ticketH, "meta": meta,
		"secret": map[string]any{"state": "st", "verifier": "ve"}, "created_by": "browser", "ttl_ms": 600000})
	a.serve(&seal.Keys{Current: other, Previous: kek})
	view := a.ok("GET", "/plugins/docs", nil)["plugin"].(map[string]any)
	expect(t, []any{view["opens"], view["kek_id"]}, []any{true, kek.ID})
	revealed := a.ok("POST", "/plugins/docs/reveal", nil)["plugin"].(map[string]any)
	expect(t, revealed["header"], map[string]any{"name": "X-Key", "value": "sk-secret-value-1234"})
	opened := a.ok("POST", "/plugin-oauth/grants/docs/open", map[string]any{"plugin_url": mcpURL, "grant": grant})
	expect(t, opened["fail"], nil)
	expect(t, len(a.ok("POST", "/plugin-oauth/stored-secrets", nil)["secrets"].([]any)), 1)
	flow := a.ok("POST", "/plugin-oauth/flows/authorize", map[string]any{"ticket_hash": ticketH,
		"binding_hash": bindingH})["flow"].(map[string]any)
	expect(t, flow["secret"].(map[string]any)["verifier"], "ve")
	// What is written now is sealed under the current KEK.
	a.ok("POST", "/plugins/docs/update", map[string]any{"secret": "sk-rotated-value-5678"})
	expect(t, a.envelope(`SELECT secret_sealed, dek_sealed, kek_id FROM ai_plugins`).KEKID, other.ID)
}

func TestPythonsSealedRowsOpenThroughTheRoutes(t *testing.T) {
	v, pyKEK := loadVectors(t)
	a := newAPI(t, &seal.Keys{Current: pyKEK})
	p := v.Plugins[0]
	e := p.envelope(t)
	a.sql(`INSERT INTO ai_plugins (name, kind, url, auth_header, secret_sealed, dek_sealed, kek_id, last4)
		VALUES ($1, 'remote_mcp', $2, $3, $4, $5, $6, '')`, p.Name, p.URL, p.AuthHeader,
		e.SecretSealed, e.DEKSealed, e.KEKID)
	revealed := a.ok("POST", "/plugins/"+p.Name+"/reveal", nil)["plugin"].(map[string]any)
	expect(t, revealed["header"], map[string]any{"name": p.AuthHeader, "value": p.Secret})
	g := v.Grants[0]
	ge := g.envelope(t)
	a.sql(`INSERT INTO ai_plugin_oauth (plugin, plugin_url, resource, issuer, token_endpoint,
		client_id, auth_method, registration, secret_sealed, dek_sealed, kek_id, has_refresh,
		grant_id, connected_by) VALUES ($1, $2, $3, $4, $5, $6, 'none', 'dynamic', $7, $8, $9,
		false, gen_random_uuid(), 'b')`, g.Plugin, g.PluginURL, g.Resource, g.Issuer,
		g.TokenEndpoint, g.ClientID, ge.SecretSealed, ge.DEKSealed, ge.KEKID)
	secrets := a.ok("POST", "/plugin-oauth/stored-secrets", nil)["secrets"].([]any)
	expect(t, secrets[0].(map[string]any)["access_token"], g.Secret.AccessToken)
}

func TestOnlyTheAgentCredentialReachesTheRoutes(t *testing.T) {
	a := held(t)
	a.token = ""
	expect(t, a.do("GET", "/plugins", nil).status, 401)
}
