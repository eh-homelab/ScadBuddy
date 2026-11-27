package activities

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/routes"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// update rewrites testdata/answers from this run (go test ./internal/inventory/activities/
// -update). It is defined in this package's tests only: another package would fail on it.
var update = flag.Bool("update", false, "rewrite testdata/answers")

func TestMain(m *testing.M) { testdb.Main(m) }

const browser = `{"kind":"browser","principal":"p1","session":null,"run_id":null}`

// fixture is the activities over a fresh database, run as Temporal runs them.
type fixture struct {
	t    *testing.T
	pool *pgxpool.Pool
	env  *testsuite.TestActivityEnvironment
	// answers are the bodies the golden test records, by name.
	answers map[string]json.RawMessage
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	pool := testdb.New(t)
	env := (&testsuite.WorkflowTestSuite{}).NewTestActivityEnvironment()
	Register(env, New(pool, "USD"))
	f := &fixture{t: t, pool: pool, env: env, answers: map[string]json.RawMessage{}}
	fixtureOf[t] = f
	return f
}

func newKey() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return "browser:p1:" + hex.EncodeToString(b)
}

// try runs the activity name once with a key of its own and gives its answer and error.
func (f *fixture) try(name string, params map[string]string, body any) (store.Answer, error) {
	return f.tryKey(name, newKey(), params, body)
}

func (f *fixture) tryKey(name, key string, params map[string]string, body any) (store.Answer, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		f.t.Fatal(err)
	}
	if body == nil {
		raw = nil
	}
	value, err := f.env.ExecuteActivity(name, Input{Key: key, Actor: json.RawMessage(browser), Params: params, Body: raw})
	if err != nil {
		return store.Answer{}, err
	}
	var answer store.Answer
	if err := value.Get(&answer); err != nil {
		f.t.Fatal(err)
	}
	return answer, nil
}

// call runs the activity and fails the test on an error, or on an answer of another status.
func (f *fixture) call(name string, status int, params map[string]string, body any) map[string]any {
	f.t.Helper()
	answer, err := f.try(name, params, body)
	if err != nil {
		f.t.Fatalf("%s: %v", name, err)
	}
	if answer.Status != status {
		f.t.Fatalf("%s: %d %s, want %d", name, answer.Status, answer.Body, status)
	}
	f.keep(strings.TrimPrefix(name, "inventory."), answer.Body)
	var out map[string]any
	if err := json.Unmarshal(answer.Body, &out); err != nil {
		f.t.Fatal(err)
	}
	return out
}

// refused runs the activity and wants the problem code at status.
func (f *fixture) refused(name string, status int, code string, params map[string]string, body any) map[string]any {
	f.t.Helper()
	answer, err := f.try(name, params, body)
	if err != nil {
		f.t.Fatalf("%s: %v", name, err)
	}
	var out map[string]any
	if err := json.Unmarshal(answer.Body, &out); err != nil {
		f.t.Fatal(err)
	}
	if answer.Status != status || out["code"] != code {
		f.t.Fatalf("%s: %d %s, want %d %s", name, answer.Status, answer.Body, status, code)
	}
	// bad_property_schema and the entity reference codes keep the entities' type.
	entities := map[string]bool{"bad_property_schema": true, "bad_entity_ref": true, "unknown_entity_kind": true, "entity_not_found": true}
	if out["type"] != "https://scadbuddy.dev/problems/inventory/"+code && !entities[code] {
		f.t.Fatalf("%s: type %v", name, out["type"])
	}
	return out
}

// read is a read route's answer: GET path (after /inventory) through the routes.
func (f *fixture) read(path string, status int) map[string]any {
	f.t.Helper()
	router := &crud.Router{Resolver: anyone{}, Next: http.NotFoundHandler()}
	router.Add("inventory", routes.Reads(f.pool, nil)...)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, httptest.NewRequest("GET", crud.Prefix+"inventory"+path, nil))
	if w.Code != status {
		f.t.Fatalf("GET %s: %d %s, want %d", path, w.Code, w.Body, status)
	}
	var out map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		f.t.Fatal(err)
	}
	return out
}

func (f *fixture) keep(name string, body json.RawMessage) {
	if _, ok := f.answers[name]; !ok {
		f.answers[name] = body
	}
}

func str(v any) string { s, _ := v.(string); return s }

func id(m map[string]any) string { return str(m["id"]) }

func item(f *fixture, kind, name string) string {
	f.t.Helper()
	return id(f.call("inventory.ItemCreate", 201, nil, map[string]any{"kind_id": kind, "name": name}))
}

func p(kv ...string) map[string]string {
	out := map[string]string{}
	for i := 0; i+1 < len(kv); i += 2 {
		out[kv[i]] = kv[i+1]
	}
	return out
}

func movesOf(m map[string]any) []map[string]any {
	var out []map[string]any
	for _, v := range m["moves"].([]any) {
		out = append(out, v.(map[string]any))
	}
	return out
}

func TestAnActivityRunTwiceWritesOnce(t *testing.T) {
	f := newFixture(t)
	bolt := item(f, "hardware", "M3 bolt")
	f.call("inventory.ReceiveLot", 201, p("item_id", bolt), map[string]any{"qty": "10", "unit_cost_minor": "5"})

	key := newKey()
	first, err := f.tryKey("inventory.Take", key, p("item_id", bolt), map[string]any{"qty": "3"})
	if err != nil {
		t.Fatal(err)
	}
	second, err := f.tryKey("inventory.Take", key, p("item_id", bolt), map[string]any{"qty": "3"})
	if err != nil {
		t.Fatal(err)
	}
	if first.Status != 200 || second.Status != 200 || !bytes.Equal(first.Body, second.Body) {
		t.Fatalf("%d %s\n%d %s", first.Status, first.Body, second.Status, second.Body)
	}
	if got := f.read("/moves?item_id="+bolt, 200); len(movesOf(got)) != 2 {
		t.Fatalf("%v", got)
	}
	// The same key with another input is the ledger's 422, and nothing runs.
	other, err := f.tryKey("inventory.Take", key, p("item_id", bolt), map[string]any{"qty": "4"})
	if err != nil || other.Status != 422 || !strings.Contains(string(other.Body), "idempotency-key-reused") {
		t.Fatalf("%d %s %v", other.Status, other.Body, err)
	}
}

func TestARefusalIsAnAnswerNotAFailure(t *testing.T) {
	f := newFixture(t)
	labour := item(f, "labour", "Assembly hour")
	body := f.refused("inventory.ReceiveLot", 409, "step_not_applicable", p("item_id", labour), map[string]any{"qty": "1"})
	if body["step"] != "receive" {
		t.Fatalf("%v", body)
	}
}

func TestADatabaseOutOfReachFailsTheActivity(t *testing.T) {
	f := newFixture(t)
	f.pool.Close()
	_, err := f.try("inventory.ItemCreate", nil, map[string]any{"kind_id": "hardware", "name": "x"})
	if err == nil {
		t.Fatal("the activity did not fail")
	}
	// Temporal retries it: not a non-retryable application error.
	var app *temporal.ApplicationError
	if errors.As(err, &app) && app.NonRetryable() {
		t.Fatalf("the failure is not retried: %v", err)
	}
}

func TestABadInputIsA422AndNotARetry(t *testing.T) {
	f := newFixture(t)
	f.refused("inventory.ItemCreate", 422, "bad_input", nil, map[string]any{"kind_id": 7})
	f.refused("inventory.Take", 422, "bad_input", p("item_id", "x"), "not an object")
	// A bad actor is a fault of the caller: the activity fails and never writes.
	_, err := f.env.ExecuteActivity("inventory.ItemCreate", Input{Key: newKey(), Actor: json.RawMessage(`{"kind":"robot"}`),
		Body: json.RawMessage(`{"kind_id":"hardware","name":"x"}`)})
	if err == nil {
		t.Fatal("a bad actor was taken")
	}
}

// strictKind is a kind under hardware whose schema requires material.
func strictKind(f *fixture) {
	f.t.Helper()
	f.call("inventory.KindCreate", 201, nil, map[string]any{"id": "strict", "name": "Strict", "parent_id": "hardware",
		"attribute_schema": map[string]any{"type": "object", "properties": map[string]any{"material": map[string]any{"type": "string"}},
			"required": []string{"material"}}})
}

func TestItemCreateCarriesProperties(t *testing.T) {
	f := newFixture(t)
	strictKind(f)
	key := newKey()
	body := map[string]any{"kind_id": "strict", "name": "Bolt", "properties": map[string]any{"material": "steel"}}
	var answers []store.Answer
	for range 2 {
		answer, err := f.tryKey("inventory.ItemCreate", key, nil, body)
		if err != nil {
			t.Fatal(err)
		}
		answers = append(answers, answer)
	}
	if answers[0].Status != 201 || !bytes.Equal(answers[0].Body, answers[1].Body) {
		t.Fatalf("%d %s\n%s", answers[0].Status, answers[0].Body, answers[1].Body)
	}
	var made map[string]any
	if err := json.Unmarshal(answers[0].Body, &made); err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(made["missing_properties"], made["invalid_properties"]) != "[] []" {
		t.Fatalf("%v", made)
	}
	for sql, want := range map[string]int{
		`SELECT count(*) FROM items WHERE name = 'Bolt'`:                                                  1,
		`SELECT count(*) FROM entity_properties p JOIN entities e ON e.id = p.entity_id WHERE e.ref = $1`: 1,
		`SELECT count(*) FROM events WHERE kind = 'entity_property.changed' AND payload->>'ref' = $1`:     1,
	} {
		var n int
		args := []any{id(made)}
		if !strings.Contains(sql, "$1") {
			args = nil
		}
		if err := f.pool.QueryRow(context.Background(), sql, args...).Scan(&n); err != nil || n != want {
			t.Fatalf("%s: %d %v, want %d", sql, n, err, want)
		}
	}
}

func TestAMissingPropertyIsAnAnswerNotAFailure(t *testing.T) {
	f := newFixture(t)
	strictKind(f)
	key := newKey()
	body := map[string]any{"kind_id": "strict", "name": "Bolt"}
	first, err := f.tryKey("inventory.ItemCreate", key, nil, body)
	if err != nil {
		t.Fatal(err)
	}
	var problem map[string]any
	if err := json.Unmarshal(first.Body, &problem); err != nil {
		t.Fatal(err)
	}
	if first.Status != 422 || problem["code"] != "missing_required_properties" ||
		problem["type"] != "https://scadbuddy.dev/problems/entities/missing_required_properties" ||
		fmt.Sprint(problem["missing"], problem["invalid"]) != "[material] []" {
		t.Fatalf("%d %s", first.Status, first.Body)
	}
	f.keep("ItemCreateMissingProperties", first.Body)
	rows := func() int {
		var n int
		if err := f.pool.QueryRow(context.Background(), `SELECT count(*) FROM crud_requests WHERE key = $1`, key).Scan(&n); err != nil {
			t.Fatal(err)
		}
		return n
	}
	if n := rows(); n != 1 {
		t.Fatalf("%d crud_requests rows", n)
	}
	// The kind no longer requires material, so a store that ran again would save the item.
	f.call("inventory.KindUpdate", 200, p("kind_id", "strict"), map[string]any{"attribute_schema": map[string]any{"type": "object",
		"properties": map[string]any{"material": map[string]any{"type": "string"}}}})
	second, err := f.tryKey("inventory.ItemCreate", key, nil, body)
	if err != nil || second.Status != 422 || !bytes.Equal(first.Body, second.Body) || rows() != 1 {
		t.Fatalf("%d %s %v", second.Status, second.Body, err)
	}
	var n int
	if err := f.pool.QueryRow(context.Background(), `SELECT count(*) FROM items WHERE name = 'Bolt'`).Scan(&n); err != nil || n != 0 {
		t.Fatalf("%d items %v", n, err)
	}
}

// Ports of phase 2's tests/api/test_inventory_stock.py.

func TestALotRoundTripsThroughUseAdjustAndCount(t *testing.T) {
	f := newFixture(t)
	bolt := item(f, "hardware", "M3 bolt")
	first := f.call("inventory.ReceiveLot", 201, p("item_id", bolt), map[string]any{"qty": "10", "unit_cost_minor": "5"})
	second := f.call("inventory.ReceiveLot", 201, p("item_id", bolt), map[string]any{"qty": "10", "total_cost_minor": "70"})
	if first["on_hand"] != "10" || first["unit_cost_minor"] != "5" || first["cost_unknown"] != false || second["unit_cost_minor"] != "7" {
		t.Fatalf("%v %v", first, second)
	}
	lots := f.read("/items/"+bolt+"/lots", 200)["lots"].([]any)
	if len(lots) != 2 || id(lots[0].(map[string]any)) != id(first) {
		t.Fatalf("%v", lots)
	}
	f.keep("LotList", mustJSON(t, map[string]any{"lots": lots}))
	f.keep("Lot", mustJSON(t, f.read("/lots/"+id(first), 200)))

	patched := f.call("inventory.LotUpdate", 200, p("lot_id", id(second)), map[string]any{"unit_cost_minor": "8", "note": "box 2"})
	if patched["unit_cost_minor"] != "8" || patched["note"] != "box 2" {
		t.Fatalf("%v", patched)
	}
	used := f.call("inventory.Take", 200, p("item_id", bolt), map[string]any{"qty": "12", "reason": "lid"})
	ms := movesOf(used)
	if len(ms) != 2 || ms[0]["lot_id"] != id(first) || ms[0]["qty"] != "-10" || ms[1]["qty"] != "-2" || ms[1]["unit_cost_minor"] != "8" {
		t.Fatalf("%v", ms)
	}
	adjusted := f.call("inventory.LotAdjust", 200, p("lot_id", id(second)), map[string]any{"on_hand": "7", "note": "recount"})
	if adjusted["on_hand"] != "7" {
		t.Fatalf("%v", adjusted)
	}
	f.call("inventory.Count", 200, p("item_id", bolt), map[string]any{"on_hand": "4", "note": "stocktake"})
	got := f.read("/items/"+bolt, 200)
	if got["on_hand"] != "4" {
		t.Fatalf("%v", got)
	}
	f.keep("Item", mustJSON(t, got))
	all := f.read("/moves?item_id="+bolt, 200)
	if len(movesOf(all)) != 6 {
		t.Fatalf("%v", all)
	}
	f.keep("MoveList", mustJSON(t, all))
	if one := f.read("/moves?lot_id="+id(first)+"&limit=1", 200); len(movesOf(one)) != 1 {
		t.Fatalf("%v", one)
	}
	estimate := f.read("/items/"+bolt+"/estimate?qty=2", 200)
	if estimate["unit_cost_minor"] != "8" || estimate["cost_unknown"] != false {
		t.Fatalf("%v", estimate)
	}
}

func TestUnitsRoundTripThroughAStatus(t *testing.T) {
	f := newFixture(t)
	nozzle := item(f, "nozzle", "0.4 hardened")
	made := f.call("inventory.ReceiveUnits", 201, p("item_id", nozzle), map[string]any{"count": 2, "cost_minor": "1500", "serials": []string{"A1", ""}})
	units := made["units"].([]any)
	if len(units) != 2 || units[0].(map[string]any)["serial"] != "A1" || units[1].(map[string]any)["serial"] != nil {
		t.Fatalf("%v", made)
	}
	listed := f.read("/items/"+nozzle+"/units", 200)
	if len(listed["units"].([]any)) != 2 {
		t.Fatalf("%v", listed)
	}
	f.keep("UnitList", mustJSON(t, listed))
	one := id(units[1].(map[string]any))
	if got := f.read("/units/"+one, 200); got["status"] != "in_stock" {
		t.Fatalf("%v", got)
	}
	patched := f.call("inventory.UnitUpdate", 200, p("unit_id", one), map[string]any{"name": "Spare", "serial": "B2"})
	if patched["name"] != "Spare" || patched["serial"] != "B2" {
		t.Fatalf("%v", patched)
	}
	if blanked := f.call("inventory.UnitUpdate", 200, p("unit_id", one), map[string]any{"serial": " "}); blanked["serial"] != nil {
		t.Fatalf("%v", blanked)
	}
	status := f.call("inventory.UnitStatus", 200, p("unit_id", one), map[string]any{"status": "in_service", "note": "X1C"})
	if status["status"] != "in_service" {
		t.Fatalf("%v", status)
	}
	f.keep("Unit", mustJSON(t, f.read("/units/"+one, 200)))
	ms := movesOf(f.read("/moves?unit_id="+one, 200))
	if len(ms) != 2 || ms[0]["kind"] != "use" || ms[0]["qty"] != "-1" || ms[1]["kind"] != "receive" {
		t.Fatalf("%v", ms)
	}
	if got := f.read("/items/"+nozzle, 200); got["on_hand"] != "1" {
		t.Fatalf("%v", got)
	}
	// Only the units of a status.
	if got := f.read("/items/"+nozzle+"/units?status=in_service", 200); len(got["units"].([]any)) != 1 {
		t.Fatalf("%v", got)
	}
}

func TestStockRefusals(t *testing.T) {
	f := newFixture(t)
	service := item(f, "flat_service", "Laser cutting")
	f.refused("inventory.ReceiveLot", 409, "step_not_applicable", p("item_id", service), map[string]any{"qty": "1"})
	// A service's consume is consume.service, which K1 does not build (R-K1-7); phase 2
	// answered item_not_counted.
	f.refused("inventory.Take", 409, "handler_not_built", p("item_id", service), map[string]any{"qty": "1"})

	bolt := item(f, "hardware", "M3 bolt")
	f.refused("inventory.ReceiveUnits", 409, "wrong_counting", p("item_id", bolt), map[string]any{"count": 1})
	f.refused("inventory.ReceiveLot", 422, "bad_quantity", p("item_id", bolt), map[string]any{"qty": "1.5"})
	f.refused("inventory.ReceiveLot", 404, "location_not_found", p("item_id", bolt),
		map[string]any{"qty": "1", "location_id": "0b7f6a4e-5d3c-4b2a-9e1f-123456789abc"})
	f.refused("inventory.Take", 404, "lot_not_found", p("item_id", bolt),
		map[string]any{"qty": "1", "lot_id": "0b7f6a4e-5d3c-4b2a-9e1f-123456789abc"})
	missing := "0b7f6a4e-5d3c-4b2a-9e1f-123456789abc"
	f.refused("inventory.ReceiveLot", 404, "item_not_found", p("item_id", missing), map[string]any{"qty": "1"})
	f.refused("inventory.LotUpdate", 404, "lot_not_found", p("lot_id", missing), map[string]any{"note": "x"})
	f.refused("inventory.LotAdjust", 404, "lot_not_found", p("lot_id", missing), map[string]any{"on_hand": "1", "note": "x"})
	f.refused("inventory.UnitStatus", 404, "unit_not_found", p("unit_id", missing), map[string]any{"status": "sold"})

	nozzle := item(f, "nozzle", "0.6")
	units := f.call("inventory.ReceiveUnits", 201, p("item_id", nozzle), map[string]any{"count": 2, "serials": []string{"S1", "S2"}})["units"].([]any)
	first, second := id(units[0].(map[string]any)), id(units[1].(map[string]any))
	f.refused("inventory.UnitUpdate", 409, "serial_exists", p("unit_id", second), map[string]any{"serial": "S1"})
	f.refused("inventory.ReceiveLot", 409, "wrong_counting", p("item_id", nozzle), map[string]any{"qty": "1"})
	if _, err := f.pool.Exec(context.Background(), `UPDATE stock_units SET installed_in_unit_id = $1 WHERE id = $2`, first, second); err != nil {
		t.Fatal(err)
	}
	f.refused("inventory.UnitStatus", 409, "unit_installed", p("unit_id", second), map[string]any{"status": "sold"})
}

func TestGiveBackAndRecostAnswerAsPhase2(t *testing.T) {
	f := newFixture(t)
	bolt := item(f, "hardware", "M3 bolt")
	lot := f.call("inventory.ReceiveLot", 201, p("item_id", bolt), map[string]any{"qty": "10", "unit_cost_minor": "500"})
	f.call("inventory.Take", 200, p("item_id", bolt), map[string]any{"qty": "3", "order_id": "o1", "order_item_id": "oi1"})
	f.call("inventory.LotUpdate", 200, p("lot_id", id(lot)), map[string]any{"unit_cost_minor": "600"})
	result := f.call("inventory.Recost", 200, p("lot_id", id(lot)), map[string]any{"orders": []string{"o1"}})
	if result["moves"] != 1.0 {
		t.Fatalf("%v", result)
	}
	f.keep("RecostResult", mustJSON(t, result))
	f.refused("inventory.Recost", 409, "recost_orders_moved", p("lot_id", id(lot)), map[string]any{"orders": []string{}})
	back := f.call("inventory.GiveBack", 200, p("item_id", bolt), map[string]any{"order_item_id": "oi1"})
	if ms := movesOf(back); len(ms) != 1 || ms[0]["qty"] != "3" {
		t.Fatalf("%v", back)
	}
	// A repoint names its kind, the entity gone and the one kept.
	for _, body := range []map[string]any{{"kind": "", "gone": "1", "kept": "2"}, {"kind": "printer", "gone": "", "kept": "2"},
		{"kind": "printer", "gone": "1", "kept": ""}, {"gone": "1", "kept": "2"}} {
		f.refused("inventory.Repoint", 422, "bad_input", nil, body)
	}
	// A repoint of a kind no stock row names moves nothing.
	moved := f.call("inventory.Repoint", 200, nil, map[string]any{"kind": "printer", "gone": "1", "kept": "2"})
	if fmt.Sprint(moved["moved"]) != "map[moves:[] units:[]]" {
		t.Fatalf("%v", moved)
	}
}

// Ports of phase 2's tests/api/test_inventory.py (kinds and items).

func TestAKindRoundTrips(t *testing.T) {
	f := newFixture(t)
	mpn := map[string]any{"type": "object", "properties": map[string]any{"mpn": map[string]any{"type": "string"}}}
	f.call("inventory.KindCreate", 201, nil, map[string]any{"id": "fastener", "name": "Fastener", "parent_id": "consumable", "tracking": "lot", "attribute_schema": mpn})
	f.call("inventory.KindCreate", 201, nil, map[string]any{"id": "bolt", "name": "Bolt", "parent_id": "fastener",
		"attribute_schema": map[string]any{"type": "object", "properties": map[string]any{"thread": map[string]any{"type": "string"}}}})
	listed := f.read("/kinds", 200)
	f.keep("KindList", mustJSON(t, listed))
	f.keep("ItemKind", mustJSON(t, f.read("/kinds/bolt", 200)))
	if patched := f.call("inventory.KindUpdate", 200, p("kind_id", "bolt"), map[string]any{"name": "Bolts"}); patched["name"] != "Bolts" {
		t.Fatalf("%v", patched)
	}
	schema := f.read("/kinds/bolt/schema", 200)
	f.keep("KindSchema", mustJSON(t, schema))
	if schema["kind_id"] != "bolt" || len(schema["schema"].(map[string]any)["properties"].(map[string]any)) != 2 ||
		fmt.Sprint(schema["keys_in_use"]) != "[]" {
		t.Fatalf("%v", schema)
	}
	archived := f.call("inventory.KindArchive", 200, p("kind_id", "bolt"), nil)
	if archived["archived_at"] == nil {
		t.Fatalf("%v", archived)
	}
	if ids := kindIDs(f.read("/kinds", 200)); strings.Contains(ids, " bolt ") {
		t.Fatal(ids)
	}
	if ids := kindIDs(f.read("/kinds?archived=true", 200)); !strings.Contains(ids, " bolt ") {
		t.Fatal(ids)
	}
	if restored := f.call("inventory.KindRestore", 200, p("kind_id", "bolt"), nil); restored["archived_at"] != nil {
		t.Fatalf("%v", restored)
	}
}

func kindIDs(list map[string]any) string {
	out := " "
	for _, k := range list["kinds"].([]any) {
		out += id(k.(map[string]any)) + " "
	}
	return out
}

func TestKindRefusals(t *testing.T) {
	f := newFixture(t)
	f.call("inventory.KindCreate", 201, nil, map[string]any{"id": "fastener", "name": "F", "parent_id": "consumable", "counting": "lot"})
	f.refused("inventory.KindCreate", 409, "kind_exists", nil, map[string]any{"id": "fastener", "name": "X", "parent_id": "consumable"})
	f.refused("inventory.KindCreate", 404, "kind_not_found", nil, map[string]any{"id": "orphan", "name": "X", "parent_id": "nope_kind"})
	f.read("/kinds/nope_kind/schema", 404)
	f.refused("inventory.KindUpdate", 404, "kind_not_found", p("kind_id", "nope_kind"), map[string]any{})
	f.call("inventory.KindCreate", 201, nil, map[string]any{"id": "rivet", "name": "R", "parent_id": "fastener"})
	f.refused("inventory.KindUpdate", 422, "kind_cycle", p("kind_id", "fastener"), map[string]any{"parent_id": "rivet"})
	item(f, "fastener", "M3")
	f.refused("inventory.KindArchive", 409, "kind_in_use", p("kind_id", "fastener"), nil)
	f.refused("inventory.KindCreate", 422, "bad_property_schema", nil,
		map[string]any{"id": "strict", "name": "S", "parent_id": "consumable", "attribute_schema": map[string]any{"type": "object", "required": []string{"mpn"}}})
	f.read("/kinds/strict", 404)
}

func TestAnItemRoundTrips(t *testing.T) {
	f := newFixture(t)
	made := f.call("inventory.ItemCreate", 201, nil, map[string]any{"kind_id": "hardware", "name": "Brass insert M3", "low_stock": "5", "manufacturer": "Ruthex"})
	if made["tracking"] != "lot" || made["on_hand"] != "0" || made["low"] != true {
		t.Fatalf("%v", made)
	}
	it := id(made)
	patched := f.call("inventory.ItemUpdate", 200, p("item_id", it), map[string]any{"note": "for lids", "low_stock": "0"})
	if patched["note"] != "for lids" || patched["low"] != false {
		t.Fatalf("%v", patched)
	}
	list := f.read("/items?kind=consumable", 200)
	f.keep("ItemList", mustJSON(t, list))
	if !strings.Contains(fmt.Sprint(list), it) {
		t.Fatalf("%v", list)
	}
	if got := f.read("/items?kind=consumable&descendants=false", 200); strings.Contains(fmt.Sprint(got), it) {
		t.Fatalf("%v", got)
	}
	if got := f.read("/items?q=ruthex", 200); len(got["items"].([]any)) != 1 {
		t.Fatalf("%v", got)
	}
	f.call("inventory.ItemArchive", 200, p("item_id", it), nil)
	if got := f.read("/items", 200); strings.Contains(fmt.Sprint(got), it) {
		t.Fatalf("%v", got)
	}
	if got := f.read("/items?archived=true", 200); !strings.Contains(fmt.Sprint(got), it) {
		t.Fatalf("%v", got)
	}
	if restored := f.call("inventory.ItemRestore", 200, p("item_id", it), nil); restored["archived_at"] != nil {
		t.Fatalf("%v", restored)
	}
}

func TestItemRefusals(t *testing.T) {
	f := newFixture(t)
	item(f, "hardware", "M3 bolt")
	f.refused("inventory.ItemCreate", 409, "item_exists", nil, map[string]any{"kind_id": "hardware", "name": "M3 BOLT"})
	missing := "0b7f6a4e-5d3c-4b2a-9e1f-123456789abc"
	f.read("/items/"+missing, 404)
	f.refused("inventory.ItemUpdate", 404, "item_not_found", p("item_id", missing), map[string]any{})
	f.refused("inventory.ItemArchive", 404, "item_not_found", p("item_id", missing), nil)
	f.refused("inventory.ItemCreate", 409, "filament_item_refused", nil, map[string]any{"kind_id": "filament", "name": "PLA"})
	f.refused("inventory.ItemCreate", 404, "kind_not_found", nil, map[string]any{"kind_id": "nope_kind", "name": "X"})
}

// The golden answers: one body per activity and per read family, which layer 5 validates
// with the pydantic models (Task 13 test_go_answers.py).

var (
	uuidText = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	timeText = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$`)
)

// normal is body with each UUID numbered by its first place and each time fixed, so a run
// writes the same file.
func normal(t *testing.T, body json.RawMessage) []byte {
	t.Helper()
	var v any
	if err := json.Unmarshal(body, &v); err != nil {
		t.Fatal(err)
	}
	ids := map[string]string{}
	var walk func(any) any
	walk = func(v any) any {
		switch x := v.(type) {
		case map[string]any:
			// By key, so each run numbers the UUIDs alike.
			keys := make([]string, 0, len(x))
			for k := range x {
				keys = append(keys, k)
			}
			slices.Sort(keys)
			for _, k := range keys {
				x[k] = walk(x[k])
			}
		case []any:
			for i, e := range x {
				x[i] = walk(e)
			}
		case string:
			switch {
			case uuidText.MatchString(x):
				if _, ok := ids[x]; !ok {
					ids[x] = fmt.Sprintf("00000000-0000-4000-8000-%012d", len(ids)+1)
				}
				return ids[x]
			case timeText.MatchString(x):
				return "2026-11-26T12:00:00Z"
			}
		}
		return v
	}
	out, err := json.MarshalIndent(walk(v), "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(out, '\n')
}

func mustJSON(t *testing.T, v any) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// TestAKindWithNoIDIsNumbered: the owner's kind numbers (2026-11-27). A kind made with no
// id is k<number> with the code K-<number>; a typed id of k and digits is 422 bad_input, as
// KindInput refuses it.
func TestAKindWithNoIDIsNumbered(t *testing.T) {
	f := newFixture(t)
	made := f.call("inventory.KindCreate", 201, nil, map[string]any{"name": "Fastener", "parent_id": "consumable", "counting": "lot"})
	n, _ := made["number"].(float64)
	if n < 1 || made["id"] != fmt.Sprintf("k%d", int64(n)) || made["code"] != fmt.Sprintf("K-%d", int64(n)) {
		t.Fatalf("made %v", made)
	}
	answer, err := f.try("inventory.KindCreate", nil, map[string]any{"id": "k12", "name": "X", "parent_id": "consumable"})
	if err != nil {
		t.Fatal(err)
	}
	var problem map[string]any
	if err := json.Unmarshal(answer.Body, &problem); err != nil || answer.Status != 422 || problem["code"] != "bad_input" {
		t.Fatalf("%d %s", answer.Status, answer.Body)
	}
	f.keep("KindCreateMadeID", answer.Body)
}

func TestTheAnswersMatchTheirGoldenFiles(t *testing.T) {
	all := map[string]json.RawMessage{}
	for _, run := range []func(*testing.T) *fixture{
		func(t *testing.T) *fixture { return golden(t, TestALotRoundTripsThroughUseAdjustAndCount) },
		func(t *testing.T) *fixture { return golden(t, TestUnitsRoundTripThroughAStatus) },
		func(t *testing.T) *fixture { return golden(t, TestGiveBackAndRecostAnswerAsPhase2) },
		func(t *testing.T) *fixture { return golden(t, TestAKindRoundTrips) },
		func(t *testing.T) *fixture { return golden(t, TestAnItemRoundTrips) },
		func(t *testing.T) *fixture { return golden(t, TestAMissingPropertyIsAnAnswerNotAFailure) },
		func(t *testing.T) *fixture { return golden(t, TestABadPropertyValueIsAnAnswer) },
		func(t *testing.T) *fixture { return golden(t, TestAKindWithNoIDIsNumbered) },
	} {
		for k, v := range run(t).answers {
			if _, ok := all[k]; !ok {
				all[k] = v
			}
		}
	}
	want := []string{"ItemKind", "KindList", "Item", "ItemList", "Lot", "LotList", "Unit", "UnitList", "MoveList", "KindSchema", "RecostResult", "ItemCreateMissingProperties", "ItemCreateBadPropertyValue", "KindCreateMadeID"}
	for _, n := range names {
		want = append(want, strings.TrimPrefix(n, "inventory."))
	}
	for _, name := range want {
		body, ok := all[name]
		if !ok {
			t.Errorf("no answer of %s was recorded", name)
			continue
		}
		path := filepath.Join("testdata", "answers", name+".json")
		got := normal(t, body)
		if *update {
			if err := os.WriteFile(path, got, 0o644); err != nil {
				t.Fatal(err)
			}
			continue
		}
		file, err := os.ReadFile(path)
		if err != nil {
			t.Errorf("%s: %v (run with -update)", name, err)
			continue
		}
		if !bytes.Equal(file, got) {
			t.Errorf("%s differs from %s (run with -update):\n%s", name, path, got)
		}
	}
}

// fixtureOf is the fixture a test function made; golden runs the test and keeps it.
var fixtureOf = map[*testing.T]*fixture{}

func golden(t *testing.T, test func(*testing.T)) *fixture {
	t.Helper()
	var made *fixture
	t.Run("", func(t *testing.T) {
		test(t)
		made = fixtureOf[t]
	})
	if made == nil {
		t.Fatal("the test made no fixture")
	}
	return made
}

// anyone resolves every request to the inventory caller with its scope.
type anyone struct{}

func (anyone) Resolve(context.Context, string) *auth.Service {
	return &auth.Service{Caller: "inventory", Scopes: []string{"crud.inventory"}}
}

// The kind schema names the keys an item of the kind must have: its kinds' merged required
// keys and its special type's requires_properties (R-K1-33), so a form can ask for them
// before the save, as a printer's model_code.
func TestAKindSchemaNamesTheItemsRequiredKeys(t *testing.T) {
	f := newFixture(t)
	strictKind(f)
	if got := fmt.Sprint(f.read("/kinds/strict/schema", 200)["item_required"]); got != "[material]" {
		t.Fatalf("strict: %s", got)
	}
	// The built-in printer kind names no special type; a kind under it that names the
	// printer special type requires its model_code.
	f.call("inventory.KindCreate", 201, nil, map[string]any{"id": "fdm", "name": "FDM printer", "parent_id": "printer",
		"counting": "unit", "special_type": "printer"})
	if got := fmt.Sprint(f.read("/kinds/fdm/schema", 200)["item_required"]); got != "[model_code]" {
		t.Fatalf("printer: %s", got)
	}
	if got := fmt.Sprint(f.read("/kinds/hardware/schema", 200)["item_required"]); got != "[]" {
		t.Fatalf("hardware: %s", got)
	}
}

// A sent value that does not fit its key is an answer: 422 bad_property_value, the entities'
// problem type, and nothing is written.
func TestABadPropertyValueIsAnAnswer(t *testing.T) {
	f := newFixture(t)
	strictKind(f)
	answer, err := f.try("inventory.ItemCreate", nil, map[string]any{"kind_id": "strict", "name": "Bolt", "properties": map[string]any{"material": 7}})
	if err != nil {
		t.Fatal(err)
	}
	var problem map[string]any
	if err := json.Unmarshal(answer.Body, &problem); err != nil {
		t.Fatal(err)
	}
	if answer.Status != 422 || problem["code"] != "bad_property_value" ||
		problem["type"] != "https://scadbuddy.dev/problems/entities/bad_property_value" {
		t.Fatalf("%d %s", answer.Status, answer.Body)
	}
	f.keep("ItemCreateBadPropertyValue", answer.Body)
	if n := countRows(t, f, `SELECT count(*) FROM items WHERE name = 'Bolt'`); n != 0 {
		t.Fatalf("%d items", n)
	}
}

func countRows(t *testing.T, f *fixture, sql string) int {
	t.Helper()
	var n int
	if err := f.pool.QueryRow(context.Background(), sql).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}
