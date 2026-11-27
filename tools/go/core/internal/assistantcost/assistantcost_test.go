package assistantcost

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
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const (
	base    = "/api/v1/internal/crud/agent"
	session = "11111111-2222-4333-8444-555555555555"
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
	router := &crud.Router{Resolver: &auth.Resolver{Pool: pool}, Next: http.NotFoundHandler()}
	router.Add("agent", Areas(pool, nil)["order-costs"]...)
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

func (a *api) sql(query string, args ...any) {
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

func (a *api) post(path string, body string) reply {
	a.t.Helper()
	req, _ := http.NewRequest("POST", a.url+base+path, bytes.NewReader([]byte(body)))
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

func (a *api) order(id, currency string, extra string) {
	a.t.Helper()
	if extra != "" {
		a.sql(`INSERT INTO orders (id, status, currency, created_by, source)
			VALUES ($1, 'draft', $2, '{"kind":"visitor"}', 'storefront')`, id, currency)
		return
	}
	a.sql(`INSERT INTO customers (id, display_name) VALUES ('c1', 'Ada') ON CONFLICT DO NOTHING`)
	a.sql(`INSERT INTO orders (id, number, customer_id, status, currency, created_by)
		VALUES ($1, 'SB-'||$1, 'c1', 'accepted', $2, '{"kind":"browser"}')`, id, currency)
}

func (a *api) item(id, order string) {
	a.t.Helper()
	a.sql(`INSERT INTO order_items (id, order_id, position, quantity, title, status, kind)
		VALUES ($1, $2, 0, 1, 'Box', 'quoted', 'custom')`, id, order)
}

func cost(usd string, extra string) string {
	return `{"session_id":"` + session + `","principal":"ada","cost_usd":` + usd + extra + `}`
}

func TestASpendIsWrittenOnceAndToldAfterItsCommit(t *testing.T) {
	a := newAPI(t)
	a.order("o1", "USD", "")
	a.item("i1", "o1")
	first := a.post("/orders/o1/assistant-cost", cost("0.125", `,"order_item_id":"i1"`))
	if first.status != http.StatusCreated {
		t.Fatalf("%d %s", first.status, first.raw)
	}
	entry := first.body["entry"].(map[string]any)
	if first.body["written"] != true || entry["amount_minor"] != 13.0 || entry["kind"] != "assistant" ||
		entry["order_item_id"] != "i1" || entry["source"] != "ai_session:"+session ||
		entry["note"] != "assistant session "+session+": 0.125 USD" {
		t.Fatalf("entry %s", first.raw)
	}
	by, _ := json.Marshal(entry["created_by"])
	if string(by) != `{"kind":"agent","principal":"ada","run_id":null,"session":"`+session+`"}` {
		t.Fatalf("created_by %s", by)
	}
	// The answer's members are in pydantic's order.
	if !bytes.HasPrefix([]byte(first.raw), []byte(`{"entry":{"order_item_id":"i1","kind":"assistant","amount_minor":13,`)) {
		t.Fatalf("order %s", first.raw)
	}
	if got := a.one(`SELECT kind || ' ' || (detail->>'amount_minor') || ' ' || (by->>'session')
		FROM order_events WHERE order_id = 'o1'`); got != "cost.added 13 "+session {
		t.Fatalf("event %s", got)
	}
	if got := a.one(`SELECT string_agg(kind || ':' || (payload->>'status'), ',' ORDER BY seq) FROM events`); got != "order.changed:accepted,order_item.changed:quoted" {
		t.Fatalf("bus %s", got)
	}
	// A second report of the session, whatever its amount, writes nothing.
	again := a.post("/orders/o1/assistant-cost", cost("9", ""))
	if again.status != http.StatusOK || again.body["written"] != false ||
		again.body["entry"].(map[string]any)["id"] != entry["id"] {
		t.Fatalf("again %d %s", again.status, again.raw)
	}
	if got := a.one(`SELECT count(*)::text FROM order_cost_entries`); got != "1" {
		t.Fatalf("entries %s", got)
	}
	if got := a.one(`SELECT count(*)::text FROM events`); got != "2" {
		t.Fatalf("events %s", got)
	}
}

func TestASpendUnderOneMinorUnitWritesNothing(t *testing.T) {
	a := newAPI(t)
	a.order("o1", "USD", "")
	out := a.post("/orders/o1/assistant-cost", cost("0.004", ""))
	if out.status != http.StatusOK || out.raw != `{"entry":null,"written":false}` {
		t.Fatalf("%d %s", out.status, out.raw)
	}
	if got := a.one(`SELECT (SELECT count(*) FROM order_cost_entries) + (SELECT count(*) FROM order_events)
		+ (SELECT count(*) FROM events)`); got != "0" {
		t.Fatalf("written %s", got)
	}
}

func TestTheSpendIsConvertedAtTheStoredRate(t *testing.T) {
	a := newAPI(t)
	a.sql(`INSERT INTO settings (name, value) VALUES ('pricing_usd_rate', '" 1.35 "'),
		('orders_currency', '"EUR"')`)
	a.order("o1", "EUR", "")
	a.order("o2", "GBP", "")
	a.item("other", "o2")
	out := a.post("/orders/o1/assistant-cost", cost("2.5", `,"order_item_id":"other"`))
	entry, _ := out.body["entry"].(map[string]any)
	// 2.5 * 1.35 = 3.375 EUR, half up: 338 cents, on the order: the item is another order's.
	if out.status != http.StatusCreated || entry["amount_minor"] != 338.0 || entry["order_item_id"] != nil {
		t.Fatalf("%d %s", out.status, out.raw)
	}
	if got := a.one(`SELECT string_agg(kind, ',') FROM events`); got != "order.changed" {
		t.Fatalf("bus %s", got)
	}
	refused := a.post("/orders/o2/assistant-cost", cost("1", ""))
	if refused.status != http.StatusConflict ||
		refused.body["type"] != "https://scadbuddy.dev/problems/orders/currency_unconvertible" ||
		refused.body["detail"] != "SB-o2 is in GBP; the USD rate is in EUR" {
		t.Fatalf("%d %s", refused.status, refused.raw)
	}
}

func TestRefusals(t *testing.T) {
	a := newAPI(t)
	a.order("cart", "USD", `UPDATE orders SET source = 'storefront', number = NULL WHERE id = $1`)
	for _, c := range []struct {
		path, body string
		status     int
		want       string
	}{
		{"/orders/none/assistant-cost", cost("1", ""), 404, `"type":"https://scadbuddy.dev/problems/orders/order_not_found"`},
		{"/orders/none/assistant-cost", cost("1", ""), 404, `"detail":"no order none"`},
		{"/orders/cart/assistant-cost", cost("1", ""), 409, `"detail":"cart cart is not yet submitted"`},
		{"/orders/cart/assistant-cost", cost("-1", ""), 422, `"loc":["body","cost_usd"]`},
		{"/orders/cart/assistant-cost", cost("0.000000001", ""), 422, `"loc":["body","cost_usd"]`},
		{"/orders/cart/assistant-cost", cost("1000001", ""), 422, `"loc":["body","cost_usd"]`},
		{"/orders/cart/assistant-cost", cost("1", `,"x":1`), 422, `"loc":["body","x"]`},
		{"/orders/cart/assistant-cost", `{"session_id":"nope","cost_usd":1}`, 422, `"loc":["body","session_id"]`},
		{"/orders/cart/assistant-cost", `{"session_id":"` + session + `"}`, 422, `"loc":["body","cost_usd"]`},
	} {
		out := a.post(c.path, c.body)
		if out.status != c.status || !bytes.Contains([]byte(out.raw), []byte(c.want)) {
			t.Errorf("%s %s: %d %s", c.path, c.body, out.status, out.raw)
		}
	}
	if got := a.one(`SELECT count(*)::text FROM order_cost_entries`); got != "0" {
		t.Fatalf("entries %s", got)
	}
}
