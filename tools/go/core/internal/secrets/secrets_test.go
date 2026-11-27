package secrets

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
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"slices"
	"strings"
	"sync"
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

const (
	base   = "/api/v1/internal/crud/agent"
	apiKey = "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaa1234"
)

var (
	kek = seal.NewKEK(bytes.Repeat([]byte{0x31}, 32))
	old = seal.NewKEK(bytes.Repeat([]byte{0x32}, 32))
)

type api struct {
	t     *testing.T
	pool  *pgxpool.Pool
	url   string
	token string
}

func newAPI(t *testing.T, keys *seal.Keys, areas ...string) *api {
	t.Helper()
	pool := testdb.New(t)
	return onPool(t, pool, keys, areas...)
}

func onPool(t *testing.T, pool *pgxpool.Pool, keys *seal.Keys, areas ...string) *api {
	t.Helper()
	router := &crud.Router{Resolver: &auth.Resolver{Pool: pool}, Next: http.NotFoundHandler()}
	all := Areas(pool, keys)
	if len(areas) == 0 {
		areas = []string{"settings", "credentials", "payload-keys", "keys"}
	}
	for _, a := range areas {
		router.Add("agent", all[a]...)
	}
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	a.token = a.credential("agent", "crud.agent")
	return a
}

func (a *api) credential(caller string, scopes ...string) string {
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

func (a *api) rows(query string, args ...any) []map[string]any {
	a.t.Helper()
	found, err := a.pool.Query(context.Background(), query, args...)
	if err != nil {
		a.t.Fatal(err)
	}
	out, err := pgx.CollectRows(found, pgx.RowToMap)
	if err != nil {
		a.t.Fatal(err)
	}
	return out
}

type answer struct {
	status int
	header http.Header
	body   map[string]any
	raw    string
}

func (a *api) call(method, path string, body any, token string) answer {
	a.t.Helper()
	var reader io.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		reader = bytes.NewReader(raw)
	}
	req, _ := http.NewRequest(method, a.url+base+path, reader)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	out := answer{status: res.StatusCode, header: res.Header, raw: string(raw)}
	_ = json.Unmarshal(raw, &out.body)
	return out
}

func (a *api) get(path string) answer         { return a.call("GET", path, nil, a.token) }
func (a *api) post(path string, b any) answer { return a.call("POST", path, b, a.token) }

type obj = map[string]any

func expect(t *testing.T, got, want any) {
	t.Helper()
	g, _ := json.Marshal(got)
	w, _ := json.Marshal(want)
	if string(g) != string(w) {
		t.Fatalf("got  %s\nwant %s", g, w)
	}
}

func (a *api) audit() []map[string]any {
	return a.rows(`SELECT kind, action, outcome, detail, principal_kind, principal_id,
		session_id::text AS session_id FROM ai_audit ORDER BY id`)
}

func keysOf(k ...*seal.KEK) *seal.Keys {
	keys := &seal.Keys{Current: k[0]}
	if len(k) > 1 {
		keys.Previous = k[1]
	}
	return keys
}

func uuid() string { return newUUID() }

func TestOnlyTheAgentsCredentialReachesThem(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	subject := "session-" + uuid()
	for token, code := range map[string]int{
		"": 401, a.credential("print", "crud.print"): 403, a.credential("projects", "crud.projects"): 403,
	} {
		expect(t, a.call("GET", "/settings?key=model", nil, token).status, code)
		expect(t, a.call("GET", "/credentials", nil, token).status, code)
		expect(t, a.call("POST", "/credentials/reveal", obj{"all": true, "purpose": "turn"}, token).status, code)
		expect(t, a.call("POST", "/payload-keys/"+subject, obj{"create": true}, token).status, code)
		expect(t, a.call("GET", "/keys/input-hash", nil, token).status, code)
	}
	expect(t, len(a.rows(`SELECT subject FROM ai_payload_keys`)), 0)
	expect(t, len(a.audit()), 0)
}

func TestSettingsReadAndWrite(t *testing.T) {
	a := newAPI(t, nil, "settings")
	expect(t, a.get("/settings?key=model&key=absent").body, obj{"values": obj{}})
	written := a.post("/settings", obj{"values": obj{"model": "opus", "limits": obj{"a": []any{1, nil}}}})
	expect(t, written.status, 200)
	expect(t, written.header.Get("X-ScadBuddy-Crud"), "1")
	expect(t, written.body, obj{"written": true})
	expect(t, a.get("/settings?key=model&key=limits&key=absent").body,
		obj{"values": obj{"model": "opus", "limits": obj{"a": []any{1, nil}}}})
	// Only the settings area is mounted without a KEK.
	expect(t, a.get("/credentials").status, 404)
}

func TestSettingsRefuseABadKeyOrBody(t *testing.T) {
	a := newAPI(t, nil, "settings")
	expect(t, a.get("/settings?key=has%20space").status, 422)
	expect(t, a.get("/settings").status, 422)
	expect(t, a.post("/settings", obj{"values": obj{}}).status, 422)
	expect(t, a.post("/settings", obj{"values": obj{"k": 1}, "extra": 1}).status, 422)
	expect(t, a.post("/settings", obj{"values": obj{"has space": 1}}).status, 422)
	over := strings.Repeat("x", settingValueMax+1)
	expect(t, a.post("/settings", obj{"values": obj{"k": over}}).status, 422)
	expect(t, a.post("/settings", obj{"values": obj{"k": 1},
		"expect": []any{obj{"key": "k", "present": true, "value": over}}}).status, 422)
	// As JSON in Python: the escapes count, so a value under the bound in bytes can be over it.
	expect(t, a.post("/settings", obj{"values": obj{"k": strings.Repeat("é", settingValueMax/6+1)}}).status, 422)
	expect(t, a.post("/settings", obj{"values": obj{"k": strings.Repeat("é", settingValueMax/6)}}).status, 200)
}

func TestACompareAndSetWritesOnlyWhatWasRead(t *testing.T) {
	a := newAPI(t, nil, "settings")
	a.post("/settings", obj{"values": obj{"mode": "bearer"}})
	stale := obj{"values": obj{"mode": "disabled"}, "expect": []any{obj{"key": "mode", "present": true, "value": "none"}}}
	expect(t, a.post("/settings", stale).body, obj{"written": false})
	absent := obj{"values": obj{"mode": "disabled"}, "expect": []any{obj{"key": "mode", "present": false}}}
	expect(t, a.post("/settings", absent).body, obj{"written": false})
	fresh := obj{"values": obj{"mode": "disabled", "cap": 3}, "expect": []any{
		obj{"key": "mode", "present": true, "value": "bearer"}, obj{"key": "cap", "present": false}}}
	expect(t, a.post("/settings", fresh).body, obj{"written": true})
	expect(t, a.get("/settings?key=mode&key=cap").body, obj{"values": obj{"cap": 3, "mode": "disabled"}})
	// jsonb equality: key order and 1 against 1.0 do not matter.
	a.post("/settings", obj{"values": obj{"obj": obj{"a": 1, "b": 2}}})
	raw := `{"values":{"obj":0},"expect":[{"key":"obj","present":true,"value":{"b":2,"a":1.0}}]}`
	expect(t, a.post("/settings", json.RawMessage(raw)).body, obj{"written": true})
}

func TestACompareAndSetWaitsForTheTablesLock(t *testing.T) {
	a := newAPI(t, nil, "settings")
	a.post("/settings", obj{"values": obj{"mode": "bearer"}})
	ctx := context.Background()
	holder, err := a.pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := holder.Exec(ctx, `LOCK TABLE ai_settings IN SHARE ROW EXCLUSIVE MODE`); err != nil {
		t.Fatal(err)
	}
	done := make(chan answer, 1)
	go func() {
		done <- a.post("/settings", obj{"values": obj{"mode": "disabled"},
			"expect": []any{obj{"key": "mode", "present": true, "value": "bearer"}}})
	}()
	time.Sleep(500 * time.Millisecond)
	select {
	case <-done:
		t.Fatal("the write did not wait")
	default:
	}
	// A read is not blocked.
	expect(t, a.get("/settings?key=mode").body, obj{"values": obj{"mode": "bearer"}})
	_, _ = holder.Exec(ctx, `UPDATE ai_settings SET value = '"other"' WHERE key = 'mode'`)
	_ = holder.Commit(ctx)
	expect(t, (<-done).body, obj{"written": false})
}

func listen(t *testing.T, pool *pgxpool.Pool, channels ...string) *pgxpool.Conn {
	conn, err := pool.Acquire(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(conn.Release)
	for _, c := range channels {
		if _, err := conn.Exec(context.Background(), "LISTEN "+c); err != nil {
			t.Fatal(err)
		}
	}
	return conn
}

func heard(conn *pgxpool.Conn) [][2]string {
	var out [][2]string
	for {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		n, err := conn.Conn().WaitForNotification(ctx)
		cancel()
		if err != nil {
			return out
		}
		out = append(out, [2]string{n.Channel, n.Payload})
	}
}

func TestAWriteNotifiesInItsTransaction(t *testing.T) {
	a := newAPI(t, nil, "settings")
	conn := listen(t, a.pool, "scadbuddy_warm_evict", "scadbuddy_settings")
	a.post("/settings", obj{"values": obj{"quiet": 1}})
	a.post("/settings", obj{"values": obj{"model": "x"}, "notify": obj{"warm_evict": true}})
	a.post("/settings", obj{"values": obj{"warm_idle_seconds": 90}, "notify": obj{"runtime": true}})
	a.post("/settings", obj{"values": obj{"model": "y"}, "expect": []any{obj{"key": "model", "present": false}},
		"notify": obj{"warm_evict": true}})
	expect(t, heard(conn), [][2]string{{"scadbuddy_warm_evict", "*"}, {"scadbuddy_settings", "session-runtime"}})
}

func TestACredentialIsSealedHereAndNeverAnswered(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	created := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey, "name": "Work"})
	expect(t, created.status, 201)
	row := created.body["credential"].(obj)
	expect(t, []any{row["kind"], row["name"], row["last4"], row["priority"]}, []any{"anthropic_api_key", "Work", "1234", 0})
	expect(t, []any{row["kek_id"], row["legacy_format"], row["status"], row["epoch"]}, []any{kek.ID, false, "active", 0})
	if !strings.HasSuffix(row["updated_at"].(string), "+00:00") {
		t.Fatal(row["updated_at"])
	}
	listed := a.get("/credentials")
	if strings.Contains(created.raw+listed.raw, apiKey) {
		t.Fatal("a secret was answered")
	}
	expect(t, listed.body["credentials"].([]any)[0].(obj)["id"], row["id"])
	sealed := a.rows(`SELECT secret_sealed FROM ai_credentials`)
	if bytes.Contains(sealed[0]["secret_sealed"].([]byte), []byte(apiKey)) {
		t.Fatal("the secret is not sealed")
	}
	expect(t, a.get("/credentials/one").body["credential"].(obj)["id"], row["id"])
	expect(t, a.get("/credentials/one?id=nope").body, obj{"credential": nil})
	expect(t, a.get("/credentials/one?id=").status, 422)
	expect(t, len(a.audit()), 0)
}

func TestRevealOpensAndAuditsEachTime(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	first := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	second := a.post("/credentials", obj{"kind": "gateway", "base_url": "https://gw.example.com/v1",
		"secret": "gw-token-0123456789"}).body["credential"].(obj)
	session := uuid()
	every := a.post("/credentials/reveal", obj{"all": true, "purpose": "turn", "session_id": session})
	expect(t, every.status, 200)
	rows := every.body["credentials"].([]any)
	expect(t, []any{rows[0].(obj)["stored"].(obj)["id"], rows[1].(obj)["stored"].(obj)["id"]},
		[]any{first["id"], second["id"]})
	expect(t, rows[0].(obj)["credential"], obj{"kind": "anthropic_api_key", "base_url": nil, "secret": apiKey})
	expect(t, rows[1].(obj)["credential"], obj{"kind": "gateway", "base_url": "https://gw.example.com/v1",
		"secret": "gw-token-0123456789"})
	expect(t, every.body["credential"], nil)
	one := a.post("/credentials/reveal", obj{"id": second["id"], "purpose": "test"}).body
	expect(t, one["credential"].(obj)["secret"], "gw-token-0123456789")
	expect(t, one["credentials"], nil)
	expect(t, a.post("/credentials/reveal", obj{"id": "gone", "purpose": "test"}).body["credential"], nil)
	rowsOf := a.audit()
	var got [][3]any
	for _, r := range rowsOf {
		got = append(got, [3]any{r["kind"], r["action"], r["outcome"]})
		expect(t, []any{r["principal_kind"], r["principal_id"]}, []any{"service", "agent"})
	}
	expect(t, got, [][3]any{
		{"credential", "reveal.turn", "ok"}, {"credential", "reveal.test", "ok"}, {"credential", "reveal.test", "refused"},
	})
	expect(t, rowsOf[0]["detail"], "opened 2 of 2 credential(s)")
	expect(t, rowsOf[0]["session_id"], session)
	expect(t, rowsOf[1]["detail"], "credential 2 (gateway https://gw.example.com/v1 …6789)")
}

func TestARowAnotherKeySealedIsNotOpened(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	created := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	a.sql(`UPDATE ai_credentials SET kek_id = 'elsewhere'`)
	every := a.post("/credentials/reveal", obj{"all": true, "purpose": "redact"}).body
	expect(t, every["credentials"].([]any)[0].(obj)["credential"], nil)
	refused := a.post("/credentials/reveal", obj{"id": created["id"], "purpose": "test"})
	expect(t, []any{refused.status, refused.body["reason"]}, []any{409, "credential-sealed"})
	// Redaction reads at tool-call rate: one row a window.
	a.post("/credentials/reveal", obj{"all": true, "purpose": "redact"})
	var actions [][2]any
	for _, r := range a.audit() {
		actions = append(actions, [2]any{r["action"], r["outcome"]})
	}
	expect(t, actions, [][2]any{{"reveal.redact", "ok"}, {"reveal.test", "error"}})
}

func fixture(t *testing.T, name string, v any) {
	_, file, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(file), "../../../../../backend/tests/fixtures", name))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, v); err != nil {
		t.Fatal(err)
	}
}

func TestTheAgentsOwnRowsOpenHere(t *testing.T) {
	var v struct {
		KEK         string `json:"kek_b64"`
		Credentials []struct {
			Name, ID, Kind, Plaintext string
			Priority                  int
			BaseURL                   *string `json:"base_url"`
			Secret                    string  `json:"secret_sealed_b64"`
			DEK                       string  `json:"dek_sealed_b64"`
			KEKID                     string  `json:"kek_id"`
			Opens                     bool
		}
	}
	fixture(t, "secret-vectors.json", &v)
	vk, err := seal.FromBase64(v.KEK)
	if err != nil {
		t.Fatal(err)
	}
	a := newAPI(t, keysOf(vk))
	for _, c := range v.Credentials {
		s, _ := base64.StdEncoding.DecodeString(c.Secret)
		d, _ := base64.StdEncoding.DecodeString(c.DEK)
		a.sql(`INSERT INTO ai_credentials (id, priority, kind, base_url, secret_sealed, dek_sealed, kek_id, last4)
			VALUES ($1, $2, $3, $4, $5, $6, $7, '')`, c.ID, c.Priority, c.Kind, c.BaseURL, s, d, c.KEKID)
	}
	rows := a.post("/credentials/reveal", obj{"all": true, "purpose": "turn"}).body["credentials"].([]any)
	byID := map[string]any{}
	for _, r := range rows {
		byID[r.(obj)["stored"].(obj)["id"].(string)] = r.(obj)["credential"]
	}
	for _, c := range v.Credentials {
		got := byID[c.ID]
		if c.Opens != (got != nil) || (got != nil && got.(obj)["secret"] != c.Plaintext) {
			t.Errorf("%s: %v", c.Name, got)
		}
	}
}

func TestSavesKeepTheAgentsRules(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	refused := func(path string, body obj) (int, any, any) {
		r := a.post(path, body)
		return r.status, r.body["reason"], r.body["detail"]
	}
	code, reason, _ := refused("/credentials", obj{"kind": "gateway", "secret": "abcdefgh"})
	expect(t, []any{code, reason}, []any{400, "credential-refused"})
	code, _, _ = refused("/credentials", obj{"kind": "anthropic_api_key", "base_url": "https://x", "secret": "s"})
	expect(t, code, 400)
	_, _, detail := refused("/credentials", obj{"kind": "anthropic_api_key", "secret": "has space"})
	expect(t, detail, "secret must not contain whitespace")
	code, _, _ = refused("/credentials", obj{"kind": "gateway", "base_url": "https://u:p@x", "secret": "s"})
	expect(t, code, 400)
	code, _, _ = refused("/credentials", obj{"kind": "anthropic_api_key", "secret": "s", "name": " padded "})
	expect(t, code, 400)
	for _, bad := range []string{"https://gw.example.com/", "https://gw.example.com?x", "ftp://gw", "https://[gw"} {
		code, _, _ = refused("/credentials", obj{"kind": "gateway", "base_url": bad, "secret": "s"})
		expect(t, code, 400)
	}
	expect(t, a.post("/credentials", obj{"kind": "other", "secret": "s"}).status, 422)
	first := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	kept := a.post("/credentials/save", obj{"id": first["id"], "update": obj{"kind": "anthropic_api_key"}})
	expect(t, kept.body["credential"].(obj)["epoch"], first["epoch"])
	code, _, _ = refused("/credentials/save", obj{"id": first["id"],
		"update": obj{"kind": "gateway", "base_url": "https://gw.example.com"}})
	expect(t, code, 409)
	code, _, _ = refused("/credentials/save", obj{"id": "nope", "update": obj{"kind": "anthropic_api_key", "secret": "s"}})
	expect(t, code, 404)
	saved := a.post("/credentials/save", obj{"id": first["id"],
		"update": obj{"kind": "anthropic_api_key", "secret": apiKey + "x"}}).body["credential"].(obj)
	expect(t, []any{saved["epoch"], saved["last4"]}, []any{1, "234x"})
}

func TestOrderDeleteResetRenameAndEvents(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	var ids []any
	for n := range 3 {
		ids = append(ids, a.post("/credentials", obj{"kind": "anthropic_api_key",
			"secret": apiKey + string(rune('0'+n))}).body["credential"].(obj)["id"])
	}
	expect(t, a.post("/credentials/order", obj{"ids": ids[:2]}).status, 409)
	expect(t, a.post("/credentials/order", obj{"ids": []any{ids[0], ids[0], ids[1]}}).status, 409)
	reversed := slices.Clone(ids)
	slices.Reverse(reversed)
	order := a.post("/credentials/order", obj{"ids": reversed}).body["credentials"].([]any)
	var got []any
	for _, c := range order {
		got = append(got, c.(obj)["id"])
	}
	expect(t, got, reversed)
	expect(t, a.post("/credentials/delete", obj{"id": ids[2]}).body, obj{"deleted": true})
	var priorities []any
	for _, c := range a.get("/credentials").body["credentials"].([]any) {
		priorities = append(priorities, c.(obj)["priority"])
	}
	expect(t, priorities, []any{0, 1})
	expect(t, a.post("/credentials/delete", obj{"id": ids[2]}).body, obj{"deleted": false})
	expect(t, a.post("/credentials/name", obj{"id": ids[0], "name": "Home"}).body["credential"].(obj)["name"], "Home")
	expect(t, a.post("/credentials/name", obj{"id": "nope", "name": nil}).body, obj{"credential": nil})
	cool := obj{"id": ids[0], "epoch": 0, "event": obj{"kind": "cooling_down", "until": "2099-01-01T00:00:00+00:00", "reason": "429"}}
	expect(t, a.post("/credentials/events", cool).body["transition"],
		obj{"before": "active", "after": "cooling_down", "recovered": false})
	cooling := a.get("/credentials/one?id=" + ids[0].(string)).body["credential"].(obj)
	expect(t, cooling["cooldown_until"], "2099-01-01T00:00:00+00:00")
	stale := obj{"id": ids[0], "epoch": 7, "event": cool["event"]}
	expect(t, a.post("/credentials/events", stale).body, obj{"transition": nil})
	disabled := obj{"id": ids[0], "epoch": 0, "event": obj{"kind": "disabled", "reason": "401"}}
	expect(t, a.post("/credentials/events", disabled).body["transition"].(obj)["after"], "disabled")
	// A rate limit never downgrades a disable.
	expect(t, a.post("/credentials/events", cool).body["transition"].(obj)["after"], "disabled")
	reset := a.post("/credentials/reset", obj{"id": ids[0]}).body["credential"].(obj)
	expect(t, []any{reset["status"], reset["epoch"], reset["last_error"]}, []any{"active", 1, nil})
	expect(t, a.post("/credentials/events", obj{"id": ids[0], "epoch": 0, "event": obj{"kind": "used"}}).body,
		obj{"transition": nil})
	noReason := a.post("/credentials/events", obj{"id": ids[0], "epoch": 1, "event": obj{"kind": "disabled"}})
	expect(t, []any{noReason.status, noReason.body["reason"]}, []any{422, "credential-refused"})
	noUntil := a.post("/credentials/events", obj{"id": ids[0], "epoch": 1, "event": obj{"kind": "cooling_down", "reason": "x"}})
	expect(t, noUntil.status, 422)
}

func TestTooManyCredentialsAreRefused(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	for n := range maxCredentials {
		a.sql(`INSERT INTO ai_credentials (id, priority, kind, secret_sealed, dek_sealed, kek_id, last4)
			VALUES ($1, $2, 'anthropic_api_key', '\x02', '\x02', 'k', '')`, "c"+string(rune('A'+n/26))+string(rune('a'+n%26)), n)
	}
	tooMany := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey})
	expect(t, []any{tooMany.status, tooMany.body["detail"]}, []any{409, tooManyMessage})
}

func TestTheKeyRouteNamesTheAPIsKey(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	expect(t, a.get("/credentials/key").body, obj{"ok": true, "kek_id": kek.ID, "reason": nil})
}

func key(t *testing.T, r answer) []byte {
	t.Helper()
	if r.status != 200 {
		t.Fatalf("%d %s", r.status, r.raw)
	}
	out, _ := base64.StdEncoding.DecodeString(r.body["data_key"].(string))
	return out
}

func TestAKeyIsMadeOnceAndReleasedAuditedOnceAWindow(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	subject := "session-" + uuid()
	missing := a.post("/payload-keys/"+subject, obj{})
	expect(t, []any{missing.status, missing.body["reason"]}, []any{410, "subject-forgotten"})
	made := key(t, a.post("/payload-keys/"+subject, obj{"create": true}))
	again := key(t, a.post("/payload-keys/"+subject, obj{"create": true}))
	read := key(t, a.post("/payload-keys/"+subject, obj{}))
	if !bytes.Equal(made, again) || !bytes.Equal(made, read) || len(made) != 32 {
		t.Fatal("the key moved")
	}
	row := a.rows(`SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = $1`, subject)[0]
	expect(t, row["kek_id"], kek.ID)
	opened, err := seal.OpenBytes(kek.Key(), row["dek_sealed"].([]byte), seal.PayloadKeyContext(subject))
	if err != nil || !bytes.Equal(opened, made) {
		t.Fatal(err)
	}
	var got [][4]any
	for _, r := range a.audit() {
		got = append(got, [4]any{r["kind"], r["action"], r["outcome"], r["detail"]})
		expect(t, r["session_id"], strings.TrimPrefix(subject, "session-"))
	}
	expect(t, got, [][4]any{
		{"payload_key", "payload_key.release", "refused", subject + ": no key"},
		{"payload_key", "payload_key.create", "ok", subject},
	})
}

func TestFlowAndBridgeSubjectsAndBadOnes(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	for _, kind := range []string{"flow", "bridge"} {
		expect(t, len(key(t, a.post("/payload-keys/"+kind+"-"+uuid(), obj{"create": true}))), 32)
	}
	expect(t, a.post("/payload-keys/thing-1", obj{"create": true}).status, 422)
	expect(t, a.post("/payload-keys/session-"+uuid()+"x", obj{}).status, 422)
	expect(t, a.post("/payload-keys/flow-"+uuid(), obj{"create": true, "only_for_standing_session": true}).status, 422)
	expect(t, a.post("/payload-keys/flow-"+uuid(), obj{"create": 3}).status, 422)
}

func TestAForgottenSubjectNeverGetsAKey(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	subject := "session-" + uuid()
	key(t, a.post("/payload-keys/"+subject, obj{"create": true}))
	a.sql(`INSERT INTO ai_forgotten_subjects (subject) VALUES ($1)`, subject)
	a.sql(`DELETE FROM ai_payload_keys WHERE subject = $1`, subject)
	for _, body := range []obj{{"create": true}, {}} {
		gone := a.post("/payload-keys/"+subject, body)
		expect(t, []any{gone.status, gone.body["reason"]}, []any{410, "subject-forgotten"})
	}
	expect(t, len(a.rows(`SELECT 1 FROM ai_payload_keys WHERE subject = $1`, subject)), 0)
}

func TestACreateWaitsForAForgetThatIsCommitting(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	subject := "session-" + uuid()
	ctx := context.Background()
	forget, err := a.pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = forget.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, seal.PayloadKeyLock(subject))
	_, _ = forget.Exec(ctx, `INSERT INTO ai_forgotten_subjects (subject) VALUES ($1)`, subject)
	done := make(chan answer, 1)
	go func() { done <- a.post("/payload-keys/"+subject, obj{"create": true}) }()
	time.Sleep(500 * time.Millisecond)
	select {
	case <-done:
		t.Fatal("the create did not wait")
	default:
	}
	_ = forget.Commit(ctx)
	expect(t, (<-done).status, 410)
	expect(t, len(a.rows(`SELECT 1 FROM ai_payload_keys WHERE subject = $1`, subject)), 0)
}

func TestAStandingSessionKeyNeedsItsRow(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	session := uuid()
	standing := obj{"create": true, "only_for_standing_session": true}
	expect(t, a.post("/payload-keys/session-"+session, standing).status, 410)
	a.sql(`INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,
		creator_id, status, max_turns, budget_usd) VALUES ($1::text::uuid, 'chat', 'browser', 'browser',
		'You', 'browser', 'browser', 'running', 10, 1)`, session)
	expect(t, len(key(t, a.post("/payload-keys/session-"+session, standing))), 32)
}

func TestAKeyUnderThePreviousKEKStillOpens(t *testing.T) {
	pool := testdb.New(t)
	subject := "flow-" + uuid()
	dek := bytes.Repeat([]byte{0x44}, 32)
	if _, err := pool.Exec(context.Background(),
		`INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES ($1, $2, $3)`,
		subject, seal.SealBytes(old.Key(), dek, seal.PayloadKeyContext(subject)), old.ID); err != nil {
		t.Fatal(err)
	}
	without := onPool(t, pool, keysOf(kek)).post("/payload-keys/"+subject, obj{})
	expect(t, []any{without.status, without.body["reason"]}, []any{409, "key-not-held"})
	expect(t, key(t, onPool(t, pool, keysOf(kek, old)).post("/payload-keys/"+subject, obj{})), dek)
}

func TestTwoCreatesAtOnceMakeOneKey(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	subject := "flow-" + uuid()
	var mu sync.Mutex
	var keys [][]byte
	var wg sync.WaitGroup
	for range 4 {
		wg.Go(func() {
			k := key(t, a.post("/payload-keys/"+subject, obj{"create": true}))
			mu.Lock()
			keys = append(keys, k)
			mu.Unlock()
		})
	}
	wg.Wait()
	for _, k := range keys {
		if !bytes.Equal(k, keys[0]) {
			t.Fatal("two keys")
		}
	}
	expect(t, len(keys), 4)
}

func TestTheInputHashKeyIsTheAgentsOwn(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	answer := a.get("/keys/input-hash")
	expect(t, answer.body, obj{"key": "Sl3Jx+pP/OVUmJWyOmEw4ogfnYnnhW/M0UZfNJrtyEw="})
	if !strings.Contains(answer.header.Get("Cache-Control"), "no-store") {
		t.Fatal(answer.header)
	}
	// A fetch grant is internal/fetchgrants's, not this package's.
	expect(t, a.post("/fetch-grants", obj{"host": "a.example", "url": "https://a.example/x"}).status, 404)
}

func TestAWindowRowNeverStandsForAnUncommittedOne(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	ctx := context.Background()
	actions := []string{"reveal.redact"}
	holder, _ := a.pool.Begin(ctx)
	if lately, err := recordedLately(ctx, holder, "credential", actions, redactAuditWindow, nil); err != nil || lately {
		t.Fatal(lately, err)
	}
	_ = audit(ctx, holder, "credential", "reveal.redact", "ok", "opened 1 of 1 credential(s)", audited{})
	other, _ := a.pool.Begin(ctx)
	_, _ = other.Exec(ctx, `SET LOCAL lock_timeout = '1s'`)
	started := time.Now()
	if lately, err := recordedLately(ctx, other, "credential", actions, redactAuditWindow, nil); err != nil || lately {
		t.Fatal(lately, err)
	}
	if time.Since(started) > time.Second {
		t.Fatal("it waited")
	}
	_ = audit(ctx, other, "credential", "reveal.redact", "ok", "opened 1 of 1 credential(s)", audited{})
	_ = other.Commit(ctx)
	_ = holder.Rollback(ctx)
	expect(t, len(a.audit()), 1)
	_ = pgx.BeginFunc(ctx, a.pool, func(tx pgx.Tx) error {
		lately, err := recordedLately(ctx, tx, "credential", actions, redactAuditWindow, nil)
		expect(t, []any{lately, err}, []any{true, nil})
		return nil
	})
}

func TestPythonsJSONLength(t *testing.T) {
	for raw, want := range map[string]int{
		`{"a": [1, 2.50, 1e20, 1e-5, null, true]}`: len(`{"a": [1, 2.5, 1e+20, 1e-05, null, true]}`),
		`"é😀\n"`:         22,
		`{"a":1,"a":22}`: len(`{"a": 22}`),
	} {
		if got := pyDumpsLen(json.RawMessage(raw)); got != want {
			t.Errorf("%s: %d, want %d", raw, got, want)
		}
	}
	if !reflect.DeepEqual(pyFloat(100000.0), "100000.0") || pyFloat(1e16) != "1e+16" {
		t.Fatal(pyFloat(100000.0), pyFloat(1e16))
	}
}
