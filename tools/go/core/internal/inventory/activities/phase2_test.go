package activities

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// The rest of phase 2's tests/api/test_inventory.py and test_inventory_stock.py cases for
// kinds, items and stock, at the activity boundary: each refusal comes back as a recorded
// answer (status and code), never as an error Temporal would retry.

// unprocessable runs the activity and wants a 422 answer, not an error.
func (f *fixture) unprocessable(what, name string, params map[string]string, body any) map[string]any {
	f.t.Helper()
	answer, err := f.try(name, params, body)
	if err != nil {
		f.t.Fatalf("%s: %s failed (a retry): %v", what, name, err)
	}
	var out map[string]any
	_ = json.Unmarshal(answer.Body, &out)
	if answer.Status != 422 {
		f.t.Fatalf("%s: %s: %d %s, want 422", what, name, answer.Status, answer.Body)
	}
	return out
}

func TestAUseOfANamedEmptyLotIsLotEmpty(t *testing.T) {
	f := newFixture(t)
	bolt := item(f, "hardware", "M3 bolt")
	first := f.call("inventory.ReceiveLot", 201, p("item_id", bolt), map[string]any{"qty": "1"})
	f.call("inventory.ReceiveLot", 201, p("item_id", bolt), map[string]any{"qty": "10"})
	f.call("inventory.Take", 200, p("item_id", bolt), map[string]any{"qty": "1"})
	f.refused("inventory.Take", 409, "lot_empty", p("item_id", bolt), map[string]any{"qty": "1", "lot_id": id(first)})
}

func TestAnAmountPastTheColumnIsBadQuantity(t *testing.T) {
	f := newFixture(t)
	bolt := item(f, "hardware", "M3 bolt")
	huge := "10000000000000"
	f.refused("inventory.ReceiveLot", 422, "bad_quantity", p("item_id", bolt), map[string]any{"qty": huge, "unit_cost_minor": huge})
	lot := f.call("inventory.ReceiveLot", 201, p("item_id", bolt), map[string]any{"qty": huge})
	f.refused("inventory.LotUpdate", 422, "bad_quantity", p("lot_id", id(lot)), map[string]any{"unit_cost_minor": huge})
	most := "99999999999999"
	short := item(f, "hardware", "M4 bolt")
	f.call("inventory.Take", 200, p("item_id", short), map[string]any{"qty": most})
	f.refused("inventory.Count", 422, "bad_quantity", p("item_id", short), map[string]any{"on_hand": most, "note": "found"})
	nozzle := item(f, "nozzle", "1.0")
	f.call("inventory.ReceiveUnits", 201, p("item_id", nozzle), map[string]any{"count": 1, "cost_minor": huge})
}

func TestUseAndStatusNameAnEntity(t *testing.T) {
	f := newFixture(t)
	var supplier string
	if err := f.pool.QueryRow(context.Background(), `INSERT INTO filament_suppliers (name) VALUES ('Bolt Depot')
		RETURNING id::text`).Scan(&supplier); err != nil {
		t.Fatal(err)
	}
	ref := "supplier:" + supplier
	bolt := item(f, "hardware", "M3 bolt")
	f.call("inventory.ReceiveLot", 201, p("item_id", bolt), map[string]any{"qty": "10"})
	used := f.call("inventory.Take", 200, p("item_id", bolt), map[string]any{"qty": "1", "entity": ref})
	if movesOf(used)[0]["entity_id"] == nil {
		t.Fatalf("%v", used)
	}
	nozzle := item(f, "nozzle", "0.2")
	unit := id(f.call("inventory.ReceiveUnits", 201, p("item_id", nozzle), map[string]any{"count": 1})["units"].([]any)[0].(map[string]any))
	f.call("inventory.UnitStatus", 200, p("unit_id", unit), map[string]any{"status": "sold", "entity": ref})
	moves := movesOf(f.read("/moves?unit_id="+unit, 200))
	if moves[0]["entity_id"] == nil {
		t.Fatalf("%v", moves)
	}
	f.refused("inventory.Take", 422, "bad_entity_ref", p("item_id", bolt), map[string]any{"qty": "1", "entity": "nope"})
	f.refused("inventory.Take", 422, "unknown_entity_kind", p("item_id", bolt), map[string]any{"qty": "1", "entity": "planet:x"})
	f.refused("inventory.Take", 404, "entity_not_found", p("item_id", bolt),
		map[string]any{"qty": "1", "entity": "supplier:0b7f6a4e-5d3c-4b2a-9e1f-123456789abc"})
}

func TestRestoringAnItemIsRefusedUnderAnArchivedKindOrATakenName(t *testing.T) {
	f := newFixture(t)
	f.call("inventory.KindCreate", 201, nil, map[string]any{"id": "fastener", "name": "F", "parent_id": "consumable", "counting": "lot"})
	it := item(f, "fastener", "M3")
	f.call("inventory.ItemArchive", 200, p("item_id", it), nil)
	f.call("inventory.KindArchive", 200, p("kind_id", "fastener"), nil)
	f.refused("inventory.ItemRestore", 409, "kind_archived", p("item_id", it), nil)
	f.call("inventory.KindRestore", 200, p("kind_id", "fastener"), nil)
	item(f, "fastener", "M3")
	f.refused("inventory.ItemRestore", 409, "item_exists", p("item_id", it), nil)
}

func TestTheItemListFiltersLowAndCompatibleAndTakesALimit(t *testing.T) {
	f := newFixture(t)
	f.call("inventory.KindCreate", 201, nil, map[string]any{"id": "fastener", "name": "F", "parent_id": "consumable", "counting": "lot"})
	low := id(f.call("inventory.ItemCreate", 201, nil, map[string]any{"kind_id": "fastener", "name": "a low", "low_stock": "2", "compatible_with": []string{"X1C"}}))
	item(f, "fastener", "b stocked")
	ids := func(query string) []string {
		var out []string
		for _, it := range f.read("/items?kind=fastener"+query, 200)["items"].([]any) {
			out = append(out, id(it.(map[string]any)))
		}
		return out
	}
	if got := ids("&low=true"); fmt.Sprint(got) != fmt.Sprint([]string{low}) {
		t.Fatal(got)
	}
	if got := ids("&compatible=X1C"); fmt.Sprint(got) != fmt.Sprint([]string{low}) {
		t.Fatal(got)
	}
	if got := ids("&compatible=nope"); len(got) != 0 {
		t.Fatal(got)
	}
	if got := ids("&limit=1"); fmt.Sprint(got) != fmt.Sprint([]string{low}) {
		t.Fatal(got)
	}
	if got := ids("&limit=5000"); len(got) != 2 {
		t.Fatal(got)
	}
	for _, limit := range []string{"0", "5001"} {
		f.read("/items?limit="+limit, 422)
	}
}

func TestAnItemsFiguresAreNormalized(t *testing.T) {
	f := newFixture(t)
	// A life is a durable's step in K1 (R-K1-6): phase 2 took life_hours on any item; the
	// figures are a part's here.
	made := f.call("inventory.ItemCreate", 201, nil, map[string]any{"kind_id": "nozzle", "name": "M3", "low_stock": "4.5", "life_hours": "100"})
	if made["low_stock"] != "4.5" || made["life_hours"] != "100" || made["on_hand"] != "0" || made["stock_value_minor"] != "0" {
		t.Fatalf("%v", made)
	}
}

func TestItemTextsAndLifeMonthsAreCapped(t *testing.T) {
	f := newFixture(t)
	f.call("inventory.KindCreate", 201, nil, map[string]any{"id": "tooling", "name": "T", "parent_id": "durable"})
	many := make([]string, 101)
	for i := range many {
		many[i] = fmt.Sprintf("c%d", i)
	}
	for _, c := range []struct {
		field string
		value any
	}{
		{"life_months", 10_000_000_000},
		{"life_months", 1201},
		{"manufacturer", strings.Repeat("m", 201)},
		{"compatible_with", []string{strings.Repeat("c", 101)}},
		{"compatible_with", many},
		{"source", strings.Repeat("s", 101)},
		{"source_ref", strings.Repeat("r", 201)},
	} {
		f.unprocessable(c.field, "inventory.ItemCreate", nil, map[string]any{"kind_id": "tooling", "name": "N", c.field: c.value})
	}
	it := id(f.call("inventory.ItemCreate", 201, nil, map[string]any{"kind_id": "tooling", "name": "N", "life_months": 1200}))
	f.unprocessable("patch life_months", "inventory.ItemUpdate", p("item_id", it), map[string]any{"life_months": 10_000_000_000})
}

func TestNULBytesInABodyAreRefused(t *testing.T) {
	f := newFixture(t)
	for _, c := range []struct {
		field string
		value any
	}{
		{"name", "M3\x00"}, {"manufacturer", "Ruthex\x00"}, {"note", "\x00"},
		{"compatible_with", []string{"X1\x00"}}, {"source", "csv\x00"}, {"source_ref", "r\x00"},
	} {
		f.unprocessable(c.field, "inventory.ItemCreate", nil, map[string]any{"kind_id": "hardware", "name": "N", c.field: c.value})
	}
	f.unprocessable("kind name", "inventory.KindCreate", nil, map[string]any{"id": "k_nul", "name": "K\x00", "parent_id": "consumable"})
	f.unprocessable("kind id", "inventory.KindUpdate", p("kind_id", "a\x00b"), map[string]any{})
	f.unprocessable("kind id", "inventory.KindArchive", p("kind_id", "a\x00b"), nil)
	it := item(f, "hardware", "M3")
	f.unprocessable("item note", "inventory.ItemUpdate", p("item_id", it), map[string]any{"note": "\x00"})

	// test_stock_texts_with_a_nul_are_refused
	lot := id(f.call("inventory.ReceiveLot", 201, p("item_id", it), map[string]any{"qty": "5"}))
	f.unprocessable("lot note", "inventory.ReceiveLot", p("item_id", it), map[string]any{"qty": "1", "note": "\x00"})
	f.unprocessable("use reason", "inventory.Take", p("item_id", it), map[string]any{"qty": "1", "reason": "\x00"})
	f.unprocessable("count note", "inventory.Count", p("item_id", it), map[string]any{"on_hand": "1", "note": "a\x00"})
	f.unprocessable("adjust note", "inventory.LotAdjust", p("lot_id", lot), map[string]any{"on_hand": "1", "note": "a\x00"})
	f.unprocessable("lot patch note", "inventory.LotUpdate", p("lot_id", lot), map[string]any{"note": "\x00"})
	nozzle := item(f, "nozzle", "0.8")
	f.unprocessable("unit name", "inventory.ReceiveUnits", p("item_id", nozzle), map[string]any{"count": 1, "name": "\x00"})
	f.unprocessable("unit serial", "inventory.ReceiveUnits", p("item_id", nozzle), map[string]any{"count": 1, "serials": []string{"S\x00"}})
	unit := id(f.call("inventory.ReceiveUnits", 201, p("item_id", nozzle), map[string]any{"count": 1})["units"].([]any)[0].(map[string]any))
	f.unprocessable("unit patch name", "inventory.UnitUpdate", p("unit_id", unit), map[string]any{"name": "\x00"})
	f.unprocessable("unit patch serial", "inventory.UnitUpdate", p("unit_id", unit), map[string]any{"serial": "\x00"})
	f.unprocessable("status note", "inventory.UnitStatus", p("unit_id", unit), map[string]any{"status": "sold", "note": "\x00"})
	f.read("/moves?order_item_id=a%00", 422)
}

func TestMissingUnitsAndMoveLimits(t *testing.T) {
	f := newFixture(t)
	f.refused("inventory.UnitUpdate", 404, "unit_not_found", p("unit_id", "0b7f6a4e-5d3c-4b2a-9e1f-123456789abc"), map[string]any{"name": "x"})
	f.read("/moves?limit=0", 422)
	f.read("/moves?limit=1001", 422)
	f.read("/moves?limit=1000", 200)
}

func TestAnEstimatePastAnAmountIsBadQuantity(t *testing.T) {
	f := newFixture(t)
	hook := item(f, "hardware", "hook")
	f.call("inventory.ReceiveLot", 201, p("item_id", hook), map[string]any{"qty": "1", "unit_cost_minor": "90000000000000"})
	got := f.read("/items/"+hook+"/estimate?qty=2000000000", 422)
	if got["code"] != "bad_quantity" {
		t.Fatalf("%v", got)
	}
}
