package store

import (
	"context"
	"encoding/json"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/history"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// Items: ports of phase 2's tests/inventory/test_items.py, one test per Python test, and
// what K1 adds: the resolved steps, the model kinds of compatible_with, the properties an
// item lacks, and the parameter history.

func itemStore() *Items { return &Items{Cat: handlers.Builtin()} }

func createItem(t *testing.T, pool *pgxpool.Pool, s *Items, in wire.ItemInput) (wire.Item, error) {
	t.Helper()
	return inTx(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.Item, error) {
		return s.Create(ctx, tx, in, Actor(browser))
	})
}

func updateItem(t *testing.T, pool *pgxpool.Pool, s *Items, id string, p wire.InventoryItemPatch) (wire.Item, error) {
	t.Helper()
	return inTx(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.Item, error) {
		return s.Update(ctx, tx, id, p, Actor(browser))
	})
}

func archiveItem(t *testing.T, pool *pgxpool.Pool, s *Items, id string) (wire.Item, error) {
	t.Helper()
	return inTx(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.Item, error) {
		return s.Archive(ctx, tx, id)
	})
}

func restoreItem(t *testing.T, pool *pgxpool.Pool, s *Items, id string) (wire.Item, error) {
	t.Helper()
	return inTx(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.Item, error) {
		return s.Restore(ctx, tx, id)
	})
}

func getItem(t *testing.T, pool *pgxpool.Pool, s *Items, id string) wire.Item {
	t.Helper()
	return must(s.Get(context.Background(), pool, id))
}

func listItems(t *testing.T, pool *pgxpool.Pool, s *Items, f ItemFilter) []wire.Item {
	t.Helper()
	return must(s.List(context.Background(), pool, f))
}

func itemIDs(items []wire.Item) []string {
	out := []string{}
	for _, it := range items {
		out = append(out, it.ID)
	}
	return out
}

// setProps sets the item's properties as entities/properties.py apply_on does.
func setProps(t *testing.T, pool *pgxpool.Pool, itemID string, values map[string]any) {
	t.Helper()
	must(inTx(t, pool, func(ctx context.Context, tx pgx.Tx) (struct{}, error) {
		id, err := EnsureEntity(ctx, tx, "item", itemID)
		if err != nil {
			return struct{}{}, err
		}
		for key, v := range values {
			b, _ := json.Marshal(v)
			if _, err := tx.Exec(ctx, `INSERT INTO entity_properties (entity_id, key, value, updated_by)
				VALUES ($1, $2, $3::jsonb, $4::jsonb) ON CONFLICT (entity_id, key) DO UPDATE SET value = EXCLUDED.value`,
				id, key, string(b), browser); err != nil {
				return struct{}{}, err
			}
		}
		return struct{}{}, nil
	}))
}

func dec(s string) *wire.Decimal { d := wire.Decimal(s); return &d }

func TestAnUncountedItemHasNoStockAndIsNeverLow(t *testing.T) {
	pool := testdb.New(t)
	made := must(createItem(t, pool, itemStore(), wire.ItemInput{KindID: "flat_service", Name: "Laser cutting", LowStock: dec("5")}))
	if made.Tracking != "none" || made.OnHand != "0" || made.StockValueMinor != "0" || made.Low {
		t.Fatalf("%+v", made)
	}
}

func TestAnItemKnowsItsKindPathAndSaleable(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	made := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))

	got := getItem(t, pool, s, made.ID)
	if !reflect.DeepEqual(got.KindPath, []string{"consumable", "hardware"}) || got.Tracking != "lot" || got.UOM != "each" ||
		got.Saleable != nil || got.EffectiveSaleable || len(got.EditedFields) != 0 {
		t.Fatalf("%+v", got)
	}
	if sold := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 bolt", Saleable: ptr(true)})); !sold.EffectiveSaleable {
		t.Fatal("an item's own saleable does not count")
	}
	if printed := must(createItem(t, pool, s, wire.ItemInput{KindID: "printed_part", Name: "Hook"})); !printed.EffectiveSaleable {
		t.Fatal("the kind's saleable does not count")
	}
}

func TestListByKindTakesDescendantsUnlessToldNotTo(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	made := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))

	if got := itemIDs(listItems(t, pool, s, ItemFilter{Kind: "consumable"})); !reflect.DeepEqual(got, []string{made.ID}) {
		t.Fatalf("consumable %v", got)
	}
	if got := listItems(t, pool, s, ItemFilter{Kind: "consumable", Descendants: ptr(false)}); len(got) != 0 {
		t.Fatalf("consumable only %v", got)
	}
	if got := itemIDs(listItems(t, pool, s, ItemFilter{Kind: "hardware", Descendants: ptr(false)})); !reflect.DeepEqual(got, []string{made.ID}) {
		t.Fatalf("hardware only %v", got)
	}
	if got := listItems(t, pool, s, ItemFilter{Kind: "part"}); len(got) != 0 {
		t.Fatalf("part %v", got)
	}
}

func TestAnItemsNameIsUniqueInItsKindInAnyCase(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))

	_, err := createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "m3 NUT"})
	refusal(t, err, 409, "item_exists")
	must(createItem(t, pool, s, wire.ItemInput{KindID: "ams_part", Name: "M3 nut"}))
}

func TestAFilamentItemIsRefused(t *testing.T) {
	pool := testdb.New(t)
	_, err := createItem(t, pool, itemStore(), wire.ItemInput{KindID: "filament", Name: "PLA Basic Red"})
	refusal(t, err, 409, "filament_item_refused")
}

func TestAnItemOfAnUnknownOrArchivedKindIsRefused(t *testing.T) {
	pool, s, k := testdb.New(t), itemStore(), kindStore()
	_, err := createItem(t, pool, s, wire.ItemInput{KindID: "nope", Name: "Thing"})
	refusal(t, err, 404, "kind_not_found")
	must(createKind(t, pool, k, wire.KindInput{ID: "widget", Name: "Widget", Tracking: ptr("lot")}))
	must(archiveKind(t, pool, k, "widget"))
	_, err = createItem(t, pool, s, wire.ItemInput{KindID: "widget", Name: "Thing"})
	refusal(t, err, 409, "kind_archived")
}

func TestAnImportedItemsEditsAreRemembered(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	made := must(createItem(t, pool, s, wire.ItemInput{KindID: "nozzle", Name: "0.4 hardened", Source: "bambu_store", SourceRef: ptr("SKU-1")}))

	updated := must(updateItem(t, pool, s, made.ID, wire.InventoryItemPatch{Name: ptr("0.4 mm hardened"), Note: ptr("")}))
	if updated.Name != "0.4 mm hardened" || !reflect.DeepEqual(updated.EditedFields, []string{"name"}) {
		t.Fatalf("%s %v", updated.Name, updated.EditedFields)
	}
	updated = must(updateItem(t, pool, s, made.ID, wire.InventoryItemPatch{Name: ptr("0.4 mm hardened"), LowStock: dec("2")}))
	if !reflect.DeepEqual(updated.EditedFields, []string{"name", "low_stock"}) {
		t.Fatalf("%v", updated.EditedFields)
	}

	manual := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))
	if got := must(updateItem(t, pool, s, manual.ID, wire.InventoryItemPatch{Name: ptr("M3 nut DIN 934")})); len(got.EditedFields) != 0 {
		t.Fatalf("a manual item remembers %v", got.EditedFields)
	}
}

func TestABlankSourceRefIsNone(t *testing.T) {
	pool := testdb.New(t)
	made := must(createItem(t, pool, itemStore(), wire.ItemInput{KindID: "hardware", Name: "M3 nut", SourceRef: ptr("  ")}))
	if made.SourceRef != nil {
		t.Fatalf("source_ref %q", *made.SourceRef)
	}
}

func TestQMatchesNameManufacturerAndPropertyValues(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	bearing := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "608 bearing"}))
	must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut", Manufacturer: "Würth"}))
	setProps(t, pool, bearing.ID, map[string]any{"mpn": "X-0451"})

	if got := itemIDs(listItems(t, pool, s, ItemFilter{Q: "0451"})); !reflect.DeepEqual(got, []string{bearing.ID}) {
		t.Fatalf("0451 %v", got)
	}
	names := func(f ItemFilter) []string {
		var out []string
		for _, it := range listItems(t, pool, s, f) {
			out = append(out, it.Name)
		}
		return out
	}
	if got := names(ItemFilter{Q: "würth"}); !reflect.DeepEqual(got, []string{"M3 nut"}) {
		t.Fatalf("würth %v", got)
	}
	if got := names(ItemFilter{Q: "608"}); !reflect.DeepEqual(got, []string{"608 bearing"}) {
		t.Fatalf("608 %v", got)
	}
	if got := names(ItemFilter{Q: "%"}); len(got) != 0 {
		t.Fatalf("%% %v", got)
	}
}

func TestListFiltersLowCompatibleAndArchived(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	low := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut", LowStock: dec("5")}))
	nozzle := must(createItem(t, pool, s, wire.ItemInput{KindID: "nozzle", Name: "0.4 nozzle", CompatibleWith: []string{"H2C", "H2D"}}))
	gone := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M2 nut"}))
	must(archiveItem(t, pool, s, gone.ID))

	if got := itemIDs(listItems(t, pool, s, ItemFilter{Low: true})); !reflect.DeepEqual(got, []string{low.ID}) {
		t.Fatalf("low %v", got)
	}
	if got := itemIDs(listItems(t, pool, s, ItemFilter{Compatible: "H2C"})); !reflect.DeepEqual(got, []string{nozzle.ID}) {
		t.Fatalf("compatible %v", got)
	}
	if slices.Contains(itemIDs(listItems(t, pool, s, ItemFilter{})), gone.ID) {
		t.Fatal("an archived item is listed")
	}
	if !slices.Contains(itemIDs(listItems(t, pool, s, ItemFilter{Archived: true})), gone.ID) {
		t.Fatal("an archived item is not listed with the archived")
	}
	must(restoreItem(t, pool, s, gone.ID))
	if !slices.Contains(itemIDs(listItems(t, pool, s, ItemFilter{})), gone.ID) {
		t.Fatal("a restored item is not listed")
	}
}

func TestStockFiguresReadLotsAndUnits(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	ctx := context.Background()
	nut := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut", LowStock: dec("10")}))
	nozzle := must(createItem(t, pool, s, wire.ItemInput{KindID: "nozzle", Name: "0.4 nozzle"}))
	if _, err := pool.Exec(ctx, `INSERT INTO stock_lots (item_id, received_qty, on_hand, unit_cost_minor,
		cost_unknown, origin, created_by) VALUES
		($1, 4, 4, 2.5, false, 'manual', $2), ($1, 6, 3, 1, true, 'manual', $2), ($1, 2, 0, 9, true, 'manual', $2)`,
		nut.ID, browser); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO stock_units (item_id, cost_minor, status, origin, created_by) VALUES
		($1, 1500, 'in_stock', 'manual', $2), ($1, 900, 'retired', 'manual', $2)`, nozzle.ID, browser); err != nil {
		t.Fatal(err)
	}

	got := getItem(t, pool, s, nut.ID)
	if got.OnHand != "7" || got.StockValueMinor != "13" || !got.CostUnknown || !got.Low {
		t.Fatalf("nut %s %s %v %v", got.OnHand, got.StockValueMinor, got.CostUnknown, got.Low)
	}
	unit := getItem(t, pool, s, nozzle.ID)
	if unit.OnHand != "1" || unit.StockValueMinor != "1500" || unit.CostUnknown {
		t.Fatalf("nozzle %s %s %v", unit.OnHand, unit.StockValueMinor, unit.CostUnknown)
	}
}

func TestUpdateAndArchiveNameAMissingItem(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	missing := "6a0c3c9e-8f4e-4c39-9d3a-3c3f3f3f3f3f"
	_, err := s.Get(context.Background(), pool, missing)
	refusal(t, err, 404, "item_not_found")
	_, err = updateItem(t, pool, s, missing, wire.InventoryItemPatch{Name: ptr("x")})
	refusal(t, err, 404, "item_not_found")
	_, err = archiveItem(t, pool, s, missing)
	refusal(t, err, 404, "item_not_found")
}

func TestARenameIntoATakenNameIsRefused(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))
	bolt := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 bolt"}))

	_, err := updateItem(t, pool, s, bolt.ID, wire.InventoryItemPatch{Name: ptr("m3 NUT")})
	refusal(t, err, 409, "item_exists")
}

func TestARestoreIntoATakenNameIsRefused(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	old := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))
	must(archiveItem(t, pool, s, old.ID))
	must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 NUT"}))

	_, err := restoreItem(t, pool, s, old.ID)
	if r := refusal(t, err, 409, "item_exists"); !strings.Contains(r.Detail, "M3 nut") {
		t.Fatalf("detail %q", r.Detail)
	}
}

func TestARestoreIntoAnArchivedKindIsRefused(t *testing.T) {
	pool, s, k := testdb.New(t), itemStore(), kindStore()
	must(createKind(t, pool, k, wire.KindInput{ID: "widget", Name: "Widget", Tracking: ptr("lot")}))
	made := must(createItem(t, pool, s, wire.ItemInput{KindID: "widget", Name: "Thing"}))
	must(archiveItem(t, pool, s, made.ID))
	must(archiveKind(t, pool, k, "widget"))

	_, err := restoreItem(t, pool, s, made.ID)
	refusal(t, err, 409, "kind_archived")
}

func TestANullClearsOnlyANullableField(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	made := must(createItem(t, pool, s, wire.ItemInput{KindID: "nozzle", Name: "0.4 nozzle", LifeHours: dec("500")}))
	if made.LifeHours == nil || *made.LifeHours != "500" {
		t.Fatalf("life_hours %v", made.LifeHours)
	}

	updated := must(updateItem(t, pool, s, made.ID, wire.InventoryItemPatch{LifeHours: wire.Null[wire.Decimal]()}))
	if updated.LifeHours != nil || updated.Name != "0.4 nozzle" {
		t.Fatalf("%v %s", updated.LifeHours, updated.Name)
	}
}

func TestQEscapesLikeWildcards(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))
	snake := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "snake_case"}))

	if got := itemIDs(listItems(t, pool, s, ItemFilter{Q: "_"})); !reflect.DeepEqual(got, []string{snake.ID}) {
		t.Fatalf("%v", got)
	}
}

func TestQMatchesValuesNotJsonText(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	tagged := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))
	must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M4 nut"}))
	setProps(t, pool, tagged.ID, map[string]any{"mpn": "X-0451", "tags": []string{"zinc", "din934"}})

	for q, want := range map[string][]string{
		"din934": {tagged.ID}, `"X-`: {}, `zinc"`: {}, "[": {}, "X-0451": {tagged.ID},
	} {
		if got := itemIDs(listItems(t, pool, s, ItemFilter{Q: q})); !reflect.DeepEqual(got, want) {
			t.Errorf("%s: %v", q, got)
		}
	}
}

func lots(t *testing.T, pool *pgxpool.Pool, itemID string, rows ...[4]any) {
	t.Helper()
	for _, r := range rows {
		if _, err := pool.Exec(context.Background(), `INSERT INTO stock_lots (item_id, received_qty, on_hand,
			unit_cost_minor, cost_unknown, origin, created_by) VALUES ($1, $2, $3, $4, $5, 'manual', $6)`,
			itemID, r[0], r[1], r[2], r[3], browser); err != nil {
			t.Fatal(err)
		}
	}
}

func TestANegativeLotCountsOnHandNotValue(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	nut := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))
	lots(t, pool, nut.ID, [4]any{4, 4, 2.5, false}, [4]any{2, -3, 7, true})

	got := getItem(t, pool, s, nut.ID)
	if got.OnHand != "1" || got.StockValueMinor != "10" || got.CostUnknown {
		t.Fatalf("%s %s %v", got.OnHand, got.StockValueMinor, got.CostUnknown)
	}
}

func TestAnEmptyLotOfUnknownCostIsNotUnknown(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	nut := must(createItem(t, pool, s, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))
	lots(t, pool, nut.ID, [4]any{4, 4, 2.5, false}, [4]any{2, 0, 0, true})

	got := getItem(t, pool, s, nut.ID)
	if got.OnHand != "4" || got.StockValueMinor != "10" || got.CostUnknown {
		t.Fatalf("%s %s %v", got.OnHand, got.StockValueMinor, got.CostUnknown)
	}
}

// What K1 adds.

func TestCompatibleWithBecomesRelations(t *testing.T) {
	pool, s, k := testdb.New(t), itemStore(), kindStore()
	made := must(createItem(t, pool, s, wire.ItemInput{KindID: "nozzle", Name: "0.4 nozzle", CompatibleWith: []string{"H2C"}}))
	if !reflect.DeepEqual(made.CompatibleWith, []string{"H2C"}) {
		t.Fatalf("made %v", made.CompatibleWith)
	}
	if got := getItem(t, pool, s, made.ID).CompatibleWith; !reflect.DeepEqual(got, []string{"H2C"}) {
		t.Fatalf("read %v", got)
	}
	if got := itemIDs(listItems(t, pool, s, ItemFilter{Compatible: "H2C"})); !reflect.DeepEqual(got, []string{made.ID}) {
		t.Fatalf("list %v", got)
	}
	if n := count(t, pool, `SELECT count(*) FROM relations r JOIN entities f ON f.id = r.from_entity
		JOIN entities o ON o.id = r.to_entity WHERE r.type_id = 'compatible_with' AND r.ended_at IS NULL
		AND f.kind = 'item' AND f.ref = $1 AND o.kind = 'item_kind' AND o.ref = 'printer_h2c'`, made.ID); n != 1 {
		t.Fatalf("%d relations", n)
	}

	updated := must(updateItem(t, pool, s, made.ID, wire.InventoryItemPatch{CompatibleWith: &[]string{"H2C", "P1S"}}))
	if !reflect.DeepEqual(updated.CompatibleWith, []string{"H2C", "P1S"}) {
		t.Fatalf("updated %v", updated.CompatibleWith)
	}
	p1s := getKind(t, pool, k, "printer_p1s")
	if p1s.Name != "P1S" || p1s.ParentID == nil || *p1s.ParentID != "printer" || p1s.Tracking != "unit" || p1s.LifeBasis != "hours" {
		t.Fatalf("printer_p1s %+v", p1s)
	}
	updated = must(updateItem(t, pool, s, made.ID, wire.InventoryItemPatch{CompatibleWith: &[]string{"P1S"}}))
	if !reflect.DeepEqual(updated.CompatibleWith, []string{"P1S"}) {
		t.Fatalf("narrowed %v", updated.CompatibleWith)
	}
	if got := listItems(t, pool, s, ItemFilter{Compatible: "H2C"}); len(got) != 0 {
		t.Fatalf("an ended relation is still found: %v", itemIDs(got))
	}
}

// recorder is a catalog that records which resolution each save asks for.
type recorder struct {
	*handlers.Catalog
	mu    sync.Mutex
	calls []string
}

func (r *recorder) note(call string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls = append(r.calls, call)
}

func (r *recorder) Resolve(chain model.Chain, item, unit model.Steps) (handlers.Resolution, []handlers.Problem, error) {
	r.note("Resolve")
	return r.Catalog.Resolve(chain, item, unit)
}

func (r *recorder) ResolveKind(chain model.Chain) (handlers.Resolution, []handlers.Problem, error) {
	r.note("ResolveKind")
	return r.Catalog.ResolveKind(chain)
}

func (r *recorder) take() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := slices.Compact(r.calls)
	r.calls = nil
	return out
}

func TestAnItemNamesItsMissingProperties(t *testing.T) {
	pool := testdb.New(t)
	// A stand-in for filament_spool, whose items stay refused until K3: a special type that
	// requires two properties.
	cat := handlers.Builtin()
	cat.Special["tagged_part"] = handlers.SpecialType{
		ID:                 "tagged_part",
		Needs:              model.Class{Nature: "physical", Role: "consumable", Counting: "lot"},
		RequiresProperties: []string{"rgba", "diameter_mm"},
	}
	rec := &recorder{Catalog: cat}
	k, s := &Kinds{Cat: rec}, &Items{Cat: rec}

	must(createKind(t, pool, k, wire.KindInput{
		ID: "tagged", Name: "Tagged", ParentID: ptr("hardware"), SpecialType: ptr("tagged_part"),
		AttributeSchema: json.RawMessage(`{"type": "object", "properties": {"material": {"type": "string"}}, "required": ["material"]}`),
	}))
	if got := rec.take(); !reflect.DeepEqual(got, []string{"ResolveKind"}) {
		t.Fatalf("a kind save called %v", got)
	}
	made := must(createItem(t, pool, s, wire.ItemInput{KindID: "tagged", Name: "Tagged bolt"}))
	if got := rec.take(); !slices.Contains(got, "Resolve") || slices.Contains(got, "ResolveKind") {
		t.Fatalf("an item save called %v", got)
	}
	want := []string{"diameter_mm", "material", "rgba"}
	if !reflect.DeepEqual(made.MissingProperties, want) || made.SpecialType == nil || *made.SpecialType != "tagged_part" {
		t.Fatalf("made: missing %v, special %v", made.MissingProperties, made.SpecialType)
	}
	if n := count(t, pool, `SELECT count(*) FROM items WHERE id = $1`, made.ID); n != 1 {
		t.Fatal("a missing property refused the save")
	}

	setProps(t, pool, made.ID, map[string]any{"material": "steel", "rgba": "FF0000FF"})
	if got := getItem(t, pool, s, made.ID).MissingProperties; !reflect.DeepEqual(got, []string{"diameter_mm"}) {
		t.Fatalf("after two: %v", got)
	}
	setProps(t, pool, made.ID, map[string]any{"diameter_mm": 1.75})
	if got := getItem(t, pool, s, made.ID).MissingProperties; got == nil || len(got) != 0 {
		t.Fatalf("after all: %#v", got)
	}
}

func TestAStepChangeIsAConfigEvent(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	ctx := context.Background()
	made := must(createItem(t, pool, s, wire.ItemInput{
		KindID: "nozzle", Name: "0.4 nozzle",
		Steps: model.Steps{"life": {Params: map[string]any{"hours": 1500}}},
	}))
	events := func() int {
		return count(t, pool, `SELECT count(*) FROM events WHERE kind = 'handler_config.changed'`)
	}
	before := events()
	var since int64
	if err := pool.QueryRow(ctx, `SELECT coalesce(max(seq), 0) FROM events`).Scan(&since); err != nil {
		t.Fatal(err)
	}

	must(updateItem(t, pool, s, made.ID, wire.InventoryItemPatch{Steps: &model.Steps{"life": {Params: map[string]any{"hours": 2000}}}}))
	if n := events() - before; n != 1 {
		t.Fatalf("%d events, want 1", n)
	}
	var payload map[string]any
	var raw []byte
	if err := pool.QueryRow(ctx, `SELECT payload FROM events WHERE kind = 'handler_config.changed' AND seq > $1`, since).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{
		"owner": "item:" + made.ID, "step": "life", "handler": "life.hours",
		"old": map[string]any{"hours": 1500.0, "source": "container_run_hours", "warn_pct": 90.0},
		"new": map[string]any{"hours": 2000.0, "source": "container_run_hours", "warn_pct": 90.0},
	}
	for key, v := range want {
		if !reflect.DeepEqual(payload[key], v) {
			t.Errorf("%s: got %v, want %v", key, payload[key], v)
		}
	}

	for range 2 {
		if _, err := history.Apply(ctx, pool, history.Options{}); err != nil {
			t.Fatal(err)
		}
		if n := count(t, pool, `SELECT count(*) FROM handler_config_history WHERE owner = $1 AND step = 'life' AND event_seq > $2`,
			"item:"+made.ID, since); n != 1 {
			t.Fatalf("%d history rows, want 1", n)
		}
	}

	// A save that rolls back publishes nothing.
	at := events()
	err := Tx(ctx, pool, Actor(browser), func(tx pgx.Tx) error {
		if _, err := s.Update(ctx, tx, made.ID, wire.InventoryItemPatch{Steps: &model.Steps{"life": {Params: map[string]any{"hours": 2500}}}}, Actor(browser)); err != nil {
			return err
		}
		return &Refused{Status: 409, Code: "test_rollback"}
	})
	refusal(t, err, 409, "test_rollback")
	if n := events() - at; n != 0 {
		t.Fatalf("a rolled-back save published %d events", n)
	}
	_, err = updateItem(t, pool, s, made.ID, wire.InventoryItemPatch{Steps: &model.Steps{"life": {Params: map[string]any{"hours": "ten"}}}})
	refusal(t, err, 422, "steps_invalid")
	if n := events() - at; n != 0 {
		t.Fatalf("a refused save published %d events", n)
	}
}

func TestALimitOutOfRangeIsRefused(t *testing.T) {
	pool, s := testdb.New(t), itemStore()
	for _, n := range []int{-1, 5001} {
		_, err := s.List(context.Background(), pool, ItemFilter{Limit: n})
		refusal(t, err, 422, "bad_limit")
	}
}
