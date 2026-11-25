package notify

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
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const base = "/api/v1/internal/crud/agent"

var (
	kek    = seal.NewKEK(bytes.Repeat([]byte{0x42}, 32))
	point  = "04" + strings.Repeat("ab", 64)
	p256dh = "B" + strings.Repeat("A", 86)
	authK  = strings.Repeat("A", 22)
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
	router := &crud.Router{Resolver: &auth.Resolver{Pool: pool}, Next: http.NotFoundHandler()}
	router.Add("agent", Areas(pool, keys)["notify"]...)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
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

func held(t *testing.T) *api { return newAPI(t, &seal.Keys{Current: kek}) }

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
		raw, _ := json.Marshal(body)
		reader = bytes.NewReader(raw)
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

func (a *api) envelope(query string, args ...any) seal.Envelope {
	a.t.Helper()
	var e seal.Envelope
	if err := a.pool.QueryRow(context.Background(), query, args...).Scan(
		&e.SecretSealed, &e.DEKSealed, &e.KEKID); err != nil {
		a.t.Fatal(err)
	}
	return e
}

func opens(t *testing.T, e seal.Envelope, aad string) string {
	t.Helper()
	text, err := seal.OpenSecret(kek, e, aad)
	if err != nil {
		t.Fatal(err)
	}
	return text
}

func ownerOf(who string) map[string]any {
	return map[string]any{"kind": "browser", "id": who, "label": "You"}
}

func TestTheVapidPairIsStoredOnceAndSealedAsTheAgentSealsIt(t *testing.T) {
	a := held(t)
	expect(t, a.ok("GET", "/push/vapid", nil), map[string]any{"vapid": nil})
	first := map[string]any{"public_key": point, "private_key": strings.Repeat("p", 43)}
	expect(t, a.ok("POST", "/push/vapid", first), map[string]any{"vapid": first})
	second := map[string]any{"public_key": "04" + strings.Repeat("cd", 64), "private_key": strings.Repeat("q", 43)}
	expect(t, a.ok("POST", "/push/vapid", second), map[string]any{"vapid": first})
	e := a.envelope("SELECT secret_sealed, dek_sealed, kek_id FROM ai_push_vapid")
	expect(t, opens(t, e, "push-vapid:1"), strings.Repeat("p", 43))
}

func TestASubscriptionIsItsOwnersAndCapped(t *testing.T) {
	a := held(t)
	subscribe := func(endpoint, who string) answer {
		return a.do("POST", "/push/subscriptions", map[string]any{
			"owner": ownerOf(who), "endpoint": endpoint, "p256dh": p256dh, "auth": authK})
	}
	first := subscribe("https://push.example/1", "browser")
	if first.status != 200 {
		t.Fatal(first.raw)
	}
	id := first.body["id"]
	expect(t, subscribe("https://push.example/1", "browser").body, map[string]any{"id": id})
	taken := subscribe("https://push.example/1", "someone-else")
	expect(t, []any{taken.status, taken.body["reason"]}, []any{409, "subscription-taken"})

	e := a.envelope(`SELECT secret_sealed, dek_sealed, kek_id FROM ai_push_subscriptions
		WHERE id = $1::text::uuid`, id)
	expect(t, opens(t, e, "push-subscription:"+id.(string)+":https://push.example/1"),
		`{"p256dh":"`+p256dh+`","auth":"`+authK+`"}`)

	for n := 2; n < 8; n++ {
		subscribe("https://push.example/"+string(rune('0'+n)), "browser")
	}
	listed := a.ok("GET", "/push/subscriptions", nil)["subscriptions"].([]any)
	if len(listed) != 5 {
		t.Fatal(len(listed))
	}
	for _, s := range listed {
		s := s.(map[string]any)
		expect(t, []any{s["p256dh"], s["auth"], s["owner"]}, []any{p256dh, authK, ownerOf("browser")})
	}
	// A row sealed by another key is skipped, never an error.
	other := seal.SealSecret(seal.NewKEK(bytes.Repeat([]byte{9}, 32)), "{}", "x")
	a.sql(`UPDATE ai_push_subscriptions SET secret_sealed = $1, dek_sealed = $2, kek_id = $3
		WHERE endpoint = 'https://push.example/7'`, other.SecretSealed, other.DEKSealed, other.KEKID)
	if n := len(a.ok("GET", "/push/subscriptions", nil)["subscriptions"].([]any)); n != 4 {
		t.Fatal(n)
	}
	gone := map[string]any{"endpoint": "https://push.example/7", "owner": ownerOf("someone-else")}
	expect(t, a.ok("POST", "/push/subscriptions/remove", gone), map[string]any{"done": false})
	expect(t, a.ok("POST", "/push/subscriptions/remove", map[string]any{"endpoint": "https://push.example/7"}),
		map[string]any{"done": true})
}

func TestASentClaimIsWonOnce(t *testing.T) {
	a := held(t)
	body := map[string]any{"request_id": "question:1", "target": "push:x"}
	expect(t, a.ok("POST", "/push/sent", body), map[string]any{"claimed": true})
	expect(t, a.ok("POST", "/push/sent", body), map[string]any{"claimed": false})
	a.sql(`UPDATE ai_notifications_sent SET sent_at = now() - interval '3 days'`)
	expect(t, a.ok("POST", "/push/sent", body), map[string]any{"claimed": true})
}

func TestAPollLeaseIsHeldByOneCallerAWindow(t *testing.T) {
	a := held(t)
	body := map[string]any{"name": "waiting-poll", "seconds": 25}
	expect(t, a.ok("POST", "/push/poll-lease", body), map[string]any{"held": true})
	expect(t, a.ok("POST", "/push/poll-lease", body), map[string]any{"held": false})
	expect(t, a.ok("POST", "/push/poll-lease", map[string]any{"name": "other", "seconds": 25}),
		map[string]any{"held": true})
	a.sql(`UPDATE ai_notifications_sent SET sent_at = now() - interval '26 seconds'
		WHERE request_id = 'lease:waiting-poll'`)
	expect(t, a.ok("POST", "/push/poll-lease", body), map[string]any{"held": true})
	var n int
	_ = a.pool.QueryRow(context.Background(),
		`SELECT count(*) FROM ai_notifications_sent WHERE target = 'lease'`).Scan(&n)
	expect(t, n, 2)
	bad := a.do("POST", "/push/poll-lease", map[string]any{"name": "Bad Name", "seconds": 25})
	expect(t, bad.status, 422)
	a.token = ""
	if s := a.do("POST", "/push/poll-lease", body).status; s != 401 {
		t.Fatal(s)
	}
}

func (a *api) channel(extra map[string]any) string {
	a.t.Helper()
	body := map[string]any{"kind": "ntfy", "name": "Phone", "url": "https://ntfy.sh/topic"}
	for k, v := range extra {
		body[k] = v
	}
	out := a.do("POST", "/notify-channels", body)
	if out.status != 201 {
		a.t.Fatal(out.status, out.raw)
	}
	return out.body["id"].(string)
}

func TestAChannelsTokenIsSealedToItsKindAndURL(t *testing.T) {
	a := held(t)
	id := a.channel(map[string]any{"token": "tk_0123456789abcd"})
	views := a.ok("GET", "/notify-channels", nil)["channels"].([]any)
	view := views[0].(map[string]any)
	expect(t, []any{len(views), view["has_token"], view["token_last4"]}, []any{1, true, "abcd"})
	e := a.envelope("SELECT secret_sealed, dek_sealed, kek_id FROM ai_notify_channels")
	expect(t, opens(t, e, "notify-channel:"+id+":ntfy:https://ntfy.sh/topic"), "tk_0123456789abcd")
	opened := a.ok("POST", "/notify-channels/"+id+"/open", nil)["channel"].(map[string]any)
	expect(t, opened["token"], "tk_0123456789abcd")

	keep := map[string]any{"kind": "ntfy", "name": "Phone 2", "url": "https://ntfy.sh/topic", "keep_token": true}
	path := "/notify-channels/" + id + "/update"
	expect(t, a.ok("POST", path, keep), map[string]any{"done": true})
	keep["url"] = "https://ntfy.sh/other"
	moved := a.do("POST", path, keep)
	expect(t, []any{moved.status, moved.body["reason"]}, []any{409, "channel-moved"})
	dropped := map[string]any{"kind": "ntfy", "name": "Phone", "url": "https://ntfy.sh/other"}
	expect(t, a.ok("POST", path, dropped), map[string]any{"done": true})
	view = a.ok("GET", "/notify-channels", nil)["channels"].([]any)[0].(map[string]any)
	expect(t, []any{view["name"], view["has_token"], view["url"]}, []any{"Phone", false, "https://ntfy.sh/other"})
	expect(t, a.do("POST", path, map[string]any{"kind": "ntfy", "name": "P", "url": "https://ntfy.sh/x",
		"keep_token": true, "token": "abc"}).status, 422)
	expect(t, a.ok("POST", "/notify-channels/"+newUUID()+"/update", dropped), map[string]any{"done": false})
}

func TestTheEnabledChannelsOpenAndASendIsRecorded(t *testing.T) {
	a := held(t)
	on := a.channel(nil)
	a.channel(map[string]any{"enabled": false})
	enabled := a.ok("POST", "/notify-channels/open-enabled", nil)["channels"].([]any)
	expect(t, []any{len(enabled), enabled[0].(map[string]any)["id"]}, []any{1, on})
	record := "/notify-channels/" + on + "/record"
	expect(t, a.ok("POST", record, map[string]any{"error": strings.Repeat("HTTP 500", 200)}),
		map[string]any{"done": true})
	one := a.ok("GET", "/notify-channels/"+on, nil)["channel"].(map[string]any)
	expect(t, len(one["last_error"].(string)), 500)
	a.ok("POST", record, map[string]any{"error": nil})
	one = a.ok("GET", "/notify-channels/"+on, nil)["channel"].(map[string]any)
	expect(t, []any{one["last_error"], one["last_sent_at"] != nil}, []any{nil, true})
	expect(t, a.ok("POST", "/notify-channels/"+on+"/delete", nil), map[string]any{"done": true})
	expect(t, a.ok("GET", "/notify-channels/"+on, nil), map[string]any{"channel": nil})
	expect(t, a.ok("POST", "/notify-channels/"+on+"/open", nil), map[string]any{"channel": nil})
}

func TestHomeAssistantNeedsAServiceAndAToken(t *testing.T) {
	a := held(t)
	body := map[string]any{"kind": "home_assistant", "name": "HA", "url": "https://ha.local", "target": "mobile_app_x"}
	expect(t, a.do("POST", "/notify-channels", body).status, 422)
	expect(t, a.do("POST", "/notify-channels", map[string]any{"kind": "home_assistant", "name": "HA",
		"url": "https://ha.local", "target": nil, "token": strings.Repeat("t", 20)}).status, 422)
	body["token"] = strings.Repeat("t", 20)
	expect(t, a.do("POST", "/notify-channels", body).status, 201)
}

func TestTheBodiesAreChecked(t *testing.T) {
	a := held(t)
	for path, body := range map[string]any{
		"/push/vapid":         map[string]any{"public_key": "04ab", "private_key": strings.Repeat("p", 43)},
		"/push/subscriptions": map[string]any{"owner": ownerOf("b"), "endpoint": "https://x/1", "p256dh": p256dh, "auth": authK, "x": 1},
		"/push/sent":          map[string]any{"request_id": "", "target": "t"},
		"/notify-channels":    map[string]any{"kind": "slack", "name": "n", "url": "https://x.example"},
	} {
		if out := a.do("POST", path, body); out.status != 422 {
			t.Errorf("%s: %d %s", path, out.status, out.raw)
		}
	}
	if out := a.do("POST", "/notify-channels/NOT-A-UUID/delete", nil); out.status != 422 {
		t.Error(out.status)
	}
}

func TestWithoutAKEKTheAPIRefusesWhatItMustSeal(t *testing.T) {
	a := newAPI(t, nil)
	refused := a.do("GET", "/push/vapid", nil)
	expect(t, []any{refused.status, refused.body["reason"]}, []any{503, "kek-missing"})
	a.channel(nil)
	expect(t, len(a.ok("POST", "/notify-channels/open-enabled", nil)["channels"].([]any)), 1)
}
