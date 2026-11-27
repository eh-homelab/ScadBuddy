package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// Kinds and items (#6949, K1 Task 10): ports of phase 2's tests/inventory/test_kinds.py, one
// test per Python test, and the class and steps a kind save checks.

func ptr[T any](v T) *T { return &v }

// inTx runs fn in one write transaction as the browser (Tx), as every store write runs. An
// error rolls it back.
func inTx[T any](t *testing.T, pool *pgxpool.Pool, fn func(context.Context, pgx.Tx) (T, error)) (T, error) {
	t.Helper()
	ctx := context.Background()
	var out T
	err := Tx(ctx, pool, Actor(browser), func(tx pgx.Tx) error {
		var err error
		out, err = fn(ctx, tx)
		return err
	})
	return out, err
}

// must is v; an error panics, which fails the test (a test helper cannot take t beside a
// call's two results).
func must[T any](v T, err error) T {
	if err != nil {
		panic(err)
	}
	return v
}

// refusal is err as a *Refused of status and code.
func refusal(t *testing.T, err error, status int, code string) *Refused {
	t.Helper()
	var r *Refused
	if !errors.As(err, &r) || r.Status != status || r.Code != code {
		t.Fatalf("got %v, want %d %s", err, status, code)
	}
	return r
}

func kindStore() *Kinds { return &Kinds{Cat: handlers.Builtin()} }

func schema(props map[string]any) json.RawMessage {
	if props == nil {
		props = map[string]any{}
	}
	b, _ := json.Marshal(map[string]any{"type": "object", "properties": props})
	return b
}

func createKind(t *testing.T, pool *pgxpool.Pool, k *Kinds, in wire.KindInput) (wire.ItemKind, error) {
	t.Helper()
	return inTx(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.ItemKind, error) {
		return k.Create(ctx, tx, in, Actor(browser))
	})
}

func updateKind(t *testing.T, pool *pgxpool.Pool, k *Kinds, id string, p wire.KindPatch) (wire.ItemKind, error) {
	t.Helper()
	return inTx(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.ItemKind, error) {
		return k.Update(ctx, tx, id, p, Actor(browser))
	})
}

func archiveKind(t *testing.T, pool *pgxpool.Pool, k *Kinds, id string) (wire.ItemKind, error) {
	t.Helper()
	return inTx(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.ItemKind, error) {
		return k.Archive(ctx, tx, id)
	})
}

func restoreKind(t *testing.T, pool *pgxpool.Pool, k *Kinds, id string) (wire.ItemKind, error) {
	t.Helper()
	return inTx(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.ItemKind, error) {
		return k.Restore(ctx, tx, id)
	})
}

// fastener is Python's _fastener: a kind under part (durable since K1), with its own schema.
func fastener(t *testing.T, pool *pgxpool.Pool, k *Kinds, props map[string]any) {
	t.Helper()
	must(createKind(t, pool, k, wire.KindInput{
		ID: "fastener", Name: "Fastener", ParentID: ptr("part"), AttributeSchema: schema(props),
	}))
}

func merged(t *testing.T, pool *pgxpool.Pool, k *Kinds, id string) map[string]any {
	t.Helper()
	m, _, _, err := k.Schema(context.Background(), pool, id)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(m, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func getKind(t *testing.T, pool *pgxpool.Pool, k *Kinds, id string) wire.ItemKind {
	t.Helper()
	return must(k.Get(context.Background(), pool, id))
}

func TestAChildKindMergesItsParentsSchema(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(updateKind(t, pool, k, "part", wire.KindPatch{AttributeSchema: schema(map[string]any{"mpn": map[string]any{"type": "string"}})}))
	fastener(t, pool, k, map[string]any{"thread": map[string]any{"type": "string"}})

	got := merged(t, pool, k, "fastener")
	want := map[string]any{"type": "object", "properties": map[string]any{
		"mpn": map[string]any{"type": "string"}, "thread": map[string]any{"type": "string"},
	}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v", got)
	}
}

func TestAChildsKeyOverridesItsParents(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(updateKind(t, pool, k, "part", wire.KindPatch{AttributeSchema: schema(map[string]any{"mpn": map[string]any{"type": "string"}})}))
	fastener(t, pool, k, map[string]any{"mpn": map[string]any{"type": "integer"}})

	if got := merged(t, pool, k, "fastener")["properties"].(map[string]any)["mpn"]; !reflect.DeepEqual(got, map[string]any{"type": "integer"}) {
		t.Fatalf("fastener's mpn: %v", got)
	}
	if got := merged(t, pool, k, "part")["properties"].(map[string]any)["mpn"]; !reflect.DeepEqual(got, map[string]any{"type": "string"}) {
		t.Fatalf("part's mpn: %v", got)
	}
}

func TestAKindUnderItsOwnDescendantIsACycle(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	fastener(t, pool, k, nil)
	must(createKind(t, pool, k, wire.KindInput{ID: "bolt", Name: "Bolt", ParentID: ptr("fastener")}))

	_, err := updateKind(t, pool, k, "part", wire.KindPatch{ParentID: wire.Some("bolt")})
	refusal(t, err, 422, "kind_cycle")
	_, err = updateKind(t, pool, k, "fastener", wire.KindPatch{ParentID: wire.Some("fastener")})
	refusal(t, err, 422, "kind_cycle")
	if part := getKind(t, pool, k, "part"); part.ParentID == nil || *part.ParentID != "durable" {
		t.Fatalf("part's parent: %v", part.ParentID)
	}
}

func TestARestrictingSchemaIsRefusedAndTheOldOneStays(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(updateKind(t, pool, k, "part", wire.KindPatch{AttributeSchema: schema(map[string]any{"mpn": map[string]any{"type": "string"}})}))

	_, err := updateKind(t, pool, k, "part", wire.KindPatch{AttributeSchema: json.RawMessage(`{"type": "object", "additionalProperties": false}`)})
	refusal(t, err, 422, "bad_property_schema")
	_, err = createKind(t, pool, k, wire.KindInput{
		ID: "bearing", Name: "Bearing", Tracking: ptr("lot"),
		AttributeSchema: json.RawMessage(`{"type": "object", "required": ["mpn"]}`),
	})
	refusal(t, err, 422, "bad_property_schema")
	var got any
	if err := json.Unmarshal(getKind(t, pool, k, "part").AttributeSchema, &got); err != nil {
		t.Fatal(err)
	}
	var want any
	_ = json.Unmarshal(schema(map[string]any{"mpn": map[string]any{"type": "string"}}), &want)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("part's schema: %v", got)
	}
	_, err = k.Get(context.Background(), pool, "bearing")
	refusal(t, err, 404, "kind_not_found")
}

func TestABuiltinKindsTrackingNeverChanges(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	_, err := updateKind(t, pool, k, "nozzle", wire.KindPatch{Tracking: ptr("lot")})
	refusal(t, err, 409, "kind_in_use")
	if got := getKind(t, pool, k, "nozzle").Tracking; got != "unit" {
		t.Fatalf("nozzle's tracking: %s", got)
	}
}

func TestAKindWithItemsKeepsItsTracking(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	fastener(t, pool, k, nil)
	must(createItem(t, pool, itemStore(), wire.ItemInput{KindID: "fastener", Name: "M3 bolt"}))

	_, err := updateKind(t, pool, k, "fastener", wire.KindPatch{Tracking: ptr("lot")})
	refusal(t, err, 409, "kind_in_use")
	must(updateKind(t, pool, k, "fastener", wire.KindPatch{Name: ptr("Fasteners")}))
}

func TestAKindWithItemsIsNotArchived(t *testing.T) {
	pool, k, items := testdb.New(t), kindStore(), itemStore()
	fastener(t, pool, k, nil)
	made := must(createItem(t, pool, items, wire.ItemInput{KindID: "fastener", Name: "M3 bolt"}))

	_, err := archiveKind(t, pool, k, "fastener")
	refusal(t, err, 409, "kind_in_use")
	must(archiveItem(t, pool, items, made.ID))
	must(archiveKind(t, pool, k, "fastener"))
	ids := func(archived bool) []string {
		var out []string
		for _, kind := range must(k.List(context.Background(), pool, archived)) {
			out = append(out, kind.ID)
		}
		return out
	}
	if slices.Contains(ids(false), "fastener") || !slices.Contains(ids(true), "fastener") {
		t.Fatal("the archived kind is listed, or not listed with the archived")
	}
	must(restoreKind(t, pool, k, "fastener"))
	if !slices.Contains(ids(false), "fastener") {
		t.Fatal("the restored kind is not listed")
	}
}

func TestCreateRefusesATakenIdAndAnUnknownParent(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	_, err := createKind(t, pool, k, wire.KindInput{ID: "part", Name: "Part", ParentID: ptr("durable")})
	refusal(t, err, 409, "kind_exists")
	_, err = createKind(t, pool, k, wire.KindInput{ID: "widget", Name: "Widget", ParentID: ptr("nope"), Tracking: ptr("lot")})
	refusal(t, err, 404, "kind_not_found")
	_, err = updateKind(t, pool, k, "nope", wire.KindPatch{Name: ptr("Nope")})
	refusal(t, err, 404, "kind_not_found")
}

func TestListCountsEachKindsItems(t *testing.T) {
	pool, k, items := testdb.New(t), kindStore(), itemStore()
	must(createItem(t, pool, items, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))
	must(createItem(t, pool, items, wire.ItemInput{KindID: "hardware", Name: "M3 bolt"}))

	counts := map[string]int{}
	for _, kind := range must(k.List(context.Background(), pool, false)) {
		counts[kind.ID] = kind.ItemCount
	}
	if counts["hardware"] != 2 || counts["part"] != 0 {
		t.Fatalf("counts %v", counts)
	}
	if hw := getKind(t, pool, k, "hardware"); !hw.Builtin || hw.ItemCount != 2 {
		t.Fatalf("hardware %+v", hw)
	}
}

func TestAnItemsSchemaIsItsKindsMerged(t *testing.T) {
	pool, k, items := testdb.New(t), kindStore(), itemStore()
	ctx := context.Background()
	must(updateKind(t, pool, k, "consumable", wire.KindPatch{AttributeSchema: schema(map[string]any{"mpn": map[string]any{"type": "string"}})}))
	made := must(createItem(t, pool, items, wire.ItemInput{KindID: "hardware", Name: "M3 nut"}))

	for id, want := range map[string]map[string]any{
		made.ID:                                {"mpn": map[string]any{"type": "string"}},
		"not-a-uuid":                           {},
		"00000000-0000-0000-0000-000000000000": {},
	} {
		raw := must(items.Schema(ctx, pool, id))
		var got map[string]any
		if err := json.Unmarshal(raw, &got); err != nil {
			t.Fatal(err)
		}
		if got["type"] != "object" || !reflect.DeepEqual(got["properties"], want) {
			t.Errorf("%s: %v", id, got)
		}
	}
}

func TestANullParentMakesARoot(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	fastener(t, pool, k, nil)

	moved := must(updateKind(t, pool, k, "fastener", wire.KindPatch{ParentID: wire.Null[string]()}))
	if moved.ParentID != nil {
		t.Fatalf("parent %v", *moved.ParentID)
	}
}

func TestATrackingChangeCountsArchivedItems(t *testing.T) {
	pool, k, items := testdb.New(t), kindStore(), itemStore()
	fastener(t, pool, k, nil)
	made := must(createItem(t, pool, items, wire.ItemInput{KindID: "fastener", Name: "M3 bolt"}))
	must(archiveItem(t, pool, items, made.ID))

	_, err := updateKind(t, pool, k, "fastener", wire.KindPatch{Tracking: ptr("lot")})
	refusal(t, err, 409, "kind_in_use")
}

func TestArchiveAndRestoreNameAnUnknownKind(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	_, err := archiveKind(t, pool, k, "nope")
	refusal(t, err, 404, "kind_not_found")
	_, err = restoreKind(t, pool, k, "nope")
	refusal(t, err, 404, "kind_not_found")
}

func TestAnUncountedKindTakesAnItem(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(createKind(t, pool, k, wire.KindInput{ID: "design_fee", Name: "Design fee", Tracking: ptr("none")}))

	made := must(createItem(t, pool, itemStore(), wire.ItemInput{KindID: "design_fee", Name: "One hour"}))
	if made.Tracking != "none" {
		t.Fatalf("tracking %s", made.Tracking)
	}
}

func TestAnArchivedParentIsRefused(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(createKind(t, pool, k, wire.KindInput{ID: "widget", Name: "Widget", Tracking: ptr("lot")}))
	must(archiveKind(t, pool, k, "widget"))

	_, err := createKind(t, pool, k, wire.KindInput{ID: "gizmo", Name: "Gizmo", ParentID: ptr("widget"), Tracking: ptr("lot")})
	refusal(t, err, 409, "kind_archived")
	fastener(t, pool, k, nil)
	_, err = updateKind(t, pool, k, "fastener", wire.KindPatch{ParentID: wire.Some("widget")})
	refusal(t, err, 409, "kind_archived")
}

// The class and steps a kind save checks (§3, §4.2, R-K1-6, R-K1-8).

func TestAKindWithLifeHoursAndNoHoursIsIncomplete(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	made := must(createKind(t, pool, k, wire.KindInput{
		ID: "spindle", Name: "Spindle", ParentID: ptr("durable"),
		Steps: model.Steps{"life": {Handler: "life.hours", Params: map[string]any{"source": "manual"}}},
	}))
	if !reflect.DeepEqual(made.IncompleteSteps, []string{"life"}) {
		t.Fatalf("incomplete_steps %v", made.IncompleteSteps)
	}
	if got := getKind(t, pool, k, "spindle"); !reflect.DeepEqual(got.IncompleteSteps, []string{"life"}) {
		t.Fatalf("read incomplete_steps %v", got.IncompleteSteps)
	}
}

func TestAKindWithAWrongHandlerIsRefused(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	_, err := createKind(t, pool, k, wire.KindInput{
		ID: "lathe", Name: "Lathe", ParentID: ptr("durable"),
		Steps: model.Steps{"count": {Handler: "count.lot"}},
	})
	r := refusal(t, err, 422, "steps_invalid")
	want := []StepProblem{{Step: "count", Handler: "count.lot", Reason: "not_applicable"}}
	if !reflect.DeepEqual(r.Problems, want) {
		t.Fatalf("problems %#v", r.Problems)
	}
	if n := count(t, pool, `SELECT count(*) FROM item_kinds WHERE id = 'lathe'`); n != 0 {
		t.Fatalf("%d rows", n)
	}
}

func TestAParentChangeThatChangesAClassIsRefused(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(createKind(t, pool, k, wire.KindInput{ID: "glue", Name: "Glue", ParentID: ptr("general_consumable")}))
	must(createItem(t, pool, itemStore(), wire.ItemInput{KindID: "glue", Name: "CA glue"}))

	_, err := updateKind(t, pool, k, "glue", wire.KindPatch{ParentID: wire.Some("durable")})
	r := refusal(t, err, 422, "class_changed")
	want := []ClassProblem{{Key: "role", From: ptr("consumable"), To: ptr("durable")}}
	if !reflect.DeepEqual(r.Problems, want) {
		t.Fatalf("problems %#v", r.Problems)
	}
	if got := getKind(t, pool, k, "glue"); got.ParentID == nil || *got.ParentID != "general_consumable" {
		t.Fatalf("the parent moved: %v", got.ParentID)
	}

	// A kind with no items of its own, whose child has one.
	must(createKind(t, pool, k, wire.KindInput{ID: "sealant", Name: "Sealant", ParentID: ptr("general_consumable")}))
	must(createKind(t, pool, k, wire.KindInput{ID: "silicone", Name: "Silicone", ParentID: ptr("sealant")}))
	must(createItem(t, pool, itemStore(), wire.ItemInput{KindID: "silicone", Name: "Clear silicone"}))
	_, err = updateKind(t, pool, k, "sealant", wire.KindPatch{ParentID: wire.Some("durable")})
	if r := refusal(t, err, 422, "class_changed"); !reflect.DeepEqual(r.Problems, want) {
		t.Fatalf("descendant problems %#v", r.Problems)
	}
	// With no items below it, the move is no class change anyone holds.
	must(createKind(t, pool, k, wire.KindInput{ID: "empty", Name: "Empty", ParentID: ptr("general_consumable")}))
	must(updateKind(t, pool, k, "empty", wire.KindPatch{ParentID: wire.Some("durable")}))
}

func TestTrackingIsDerived(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	svc := must(createKind(t, pool, k, wire.KindInput{ID: "consult", Name: "Consulting", Nature: ptr("service"), Billing: ptr("flat")}))
	if svc.Tracking != "none" {
		t.Fatalf("a service tracks %s", svc.Tracking)
	}
	tool := must(createKind(t, pool, k, wire.KindInput{ID: "vise", Name: "Vise", ParentID: ptr("durable"), Tracking: ptr("unit")}))
	if tool.Tracking != "unit" || tool.Nature != nil || tool.Role != nil || tool.Counting != nil {
		t.Fatalf("vise %+v: a tracking sets only the keys its parents leave open", tool)
	}
	cls := model.Class{Nature: "physical", Role: "durable", Counting: "unit"}
	chain := must(loadKinds(context.Background(), pool)).chain("vise")
	if got, _, err := chain.Class(); err != nil || got != cls {
		t.Fatalf("vise's class %+v %v", got, err)
	}
	lens := must(createKind(t, pool, k, wire.KindInput{
		ID: "lens", Name: "Lens", ParentID: ptr("part"),
		Steps: model.Steps{"life": {Handler: "life.hours", Params: map[string]any{"hours": 500, "source": "manual"}}},
	}))
	if lens.LifeBasis != "hours" {
		t.Fatalf("life_basis %s", lens.LifeBasis)
	}
	if got := getKind(t, pool, k, "lens"); got.LifeBasis != "hours" || got.Tracking != "unit" {
		t.Fatalf("read lens %s %s", got.Tracking, got.LifeBasis)
	}
	_, err := createKind(t, pool, k, wire.KindInput{ID: "oil", Name: "Oil", ParentID: ptr("durable"), Tracking: ptr("lot")})
	refusal(t, err, 422, "class_changed")
}

// Fix round 1.

func TestAHandlerSwapThatBreaksAChildKindIsRefused(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(createKind(t, pool, k, wire.KindInput{ID: "spindle", Name: "Spindle", ParentID: ptr("durable"),
		Steps: model.Steps{"life": {Handler: "life.hours", Params: map[string]any{"hours": 100, "source": "manual"}}}}))
	must(createKind(t, pool, k, wire.KindInput{ID: "fast_spindle", Name: "Fast spindle", ParentID: ptr("spindle"),
		Steps: model.Steps{"life": {Params: map[string]any{"hours": 50}}}}))

	_, err := updateKind(t, pool, k, "spindle", wire.KindPatch{Steps: &model.Steps{"life": {Handler: "life.months", Params: map[string]any{"months": 6}}}})
	r := refusal(t, err, 422, "steps_invalid")
	want := []StepProblem{{Owner: "item_kind:fast_spindle", Step: "life", Handler: "life.months", Reason: "unknown_param", Missing: []string{"hours"}}}
	if !reflect.DeepEqual(r.Problems, want) {
		t.Fatalf("problems %#v", r.Problems)
	}
	if got := getKind(t, pool, k, "spindle").Steps["life"].Handler; got != "life.hours" {
		t.Fatalf("the swap was saved: %s", got)
	}
}

func TestAHandlerSwapThatBreaksAnItemIsRefused(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(createKind(t, pool, k, wire.KindInput{ID: "spindle", Name: "Spindle", ParentID: ptr("durable"),
		Steps: model.Steps{"life": {Handler: "life.hours", Params: map[string]any{"hours": 100, "source": "manual"}}}}))
	made := must(createItem(t, pool, itemStore(), wire.ItemInput{KindID: "spindle", Name: "S1",
		Steps: model.Steps{"life": {Params: map[string]any{"hours": 50}}}}))

	_, err := updateKind(t, pool, k, "spindle", wire.KindPatch{Steps: &model.Steps{"life": {Handler: "life.months", Params: map[string]any{"months": 6}}}})
	r := refusal(t, err, 422, "steps_invalid")
	want := []StepProblem{{Owner: "item:" + made.ID, Step: "life", Handler: "life.months", Reason: "unknown_param", Missing: []string{"hours"}}}
	if !reflect.DeepEqual(r.Problems, want) {
		t.Fatalf("problems %#v", r.Problems)
	}
}

func TestALifeBasisNoneUnderPrinterReadsNone(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	made := must(createKind(t, pool, k, wire.KindInput{ID: "printer_dummy", Name: "Dummy", ParentID: ptr("printer"), LifeBasis: ptr("none")}))
	if made.LifeBasis != "none" {
		t.Fatalf("life_basis %s", made.LifeBasis)
	}
	if got := getKind(t, pool, k, "printer_dummy").LifeBasis; got != "none" {
		t.Fatalf("read life_basis %s", got)
	}
}

func TestABuiltinKindKeepsItsResolvedClass(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	_, err := updateKind(t, pool, k, "part", wire.KindPatch{ParentID: wire.Some("general_consumable")})
	refusal(t, err, 409, "kind_in_use")
	_, err = updateKind(t, pool, k, "nozzle", wire.KindPatch{SpecialType: wire.Some("printer")})
	refusal(t, err, 409, "kind_in_use")
	if got := getKind(t, pool, k, "part"); *got.ParentID != "durable" {
		t.Fatalf("part moved to %s", *got.ParentID)
	}
	// A move that keeps the resolved class is no class change.
	must(updateKind(t, pool, k, "spool_part", wire.KindPatch{ParentID: wire.Some("part")}))
}

// Fix round 2.

func TestABuiltinKindBelowKeepsItsResolvedClass(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(createKind(t, pool, k, wire.KindInput{ID: "mypart", Name: "My part", ParentID: ptr("durable")}))
	must(updateKind(t, pool, k, "nozzle", wire.KindPatch{ParentID: wire.Some("mypart")}))

	_, err := updateKind(t, pool, k, "mypart", wire.KindPatch{SpecialType: wire.Some("printer")})
	r := refusal(t, err, 409, "kind_in_use")
	want := []ClassProblem{{Owner: "item_kind:nozzle", Key: "special_type", To: ptr("printer")}}
	if !reflect.DeepEqual(r.Problems, want) {
		t.Fatalf("special problems %#v", r.Problems)
	}
	_, err = updateKind(t, pool, k, "mypart", wire.KindPatch{ParentID: wire.Some("general_consumable")})
	r = refusal(t, err, 409, "kind_in_use")
	want = []ClassProblem{{Owner: "item_kind:nozzle", Key: "role", From: ptr("durable"), To: ptr("consumable")}}
	if !reflect.DeepEqual(r.Problems, want) {
		t.Fatalf("class problems %#v", r.Problems)
	}
	if got := getKind(t, pool, k, "mypart"); *got.ParentID != "durable" || got.SpecialType != nil {
		t.Fatalf("mypart changed: %+v", got)
	}
}

// Required properties merge down the tree (R-K1-33).

func TestAChildMayRequireAKeyItInherits(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(createKind(t, pool, k, wire.KindInput{ID: "parent", Name: "Parent", ParentID: ptr("hardware"),
		AttributeSchema: schema(map[string]any{"mpn": map[string]any{"type": "string"}})}))
	must(createKind(t, pool, k, wire.KindInput{ID: "child", Name: "Child", ParentID: ptr("parent"),
		AttributeSchema: json.RawMessage(`{"type": "object", "required": ["mpn"]}`)}))
	if got := merged(t, pool, k, "child")["required"]; !reflect.DeepEqual(got, []any{"mpn"}) {
		t.Fatalf("required %v", got)
	}
}

func TestRequiredMergesDownTheTree(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(createKind(t, pool, k, wire.KindInput{ID: "parent", Name: "Parent", ParentID: ptr("hardware"),
		AttributeSchema: json.RawMessage(`{"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}`)}))
	must(createKind(t, pool, k, wire.KindInput{ID: "child", Name: "Child", ParentID: ptr("parent"),
		AttributeSchema: json.RawMessage(`{"type": "object", "properties": {"b": {"type": "string"}}, "required": ["b"]}`)}))
	_, err := createItem(t, pool, itemStore(), wire.ItemInput{KindID: "child", Name: "X"})
	if missing, _ := incompleteness(t, err); !reflect.DeepEqual(missing, []string{"a", "b"}) {
		t.Fatalf("missing %v", missing)
	}
}

func TestAnAncestorThatDropsARequiredKeyIsRefused(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(createKind(t, pool, k, wire.KindInput{ID: "parent", Name: "Parent", ParentID: ptr("hardware"),
		AttributeSchema: schema(map[string]any{"mpn": map[string]any{"type": "string"}})}))
	must(createKind(t, pool, k, wire.KindInput{ID: "child", Name: "Child", ParentID: ptr("parent"),
		AttributeSchema: json.RawMessage(`{"type": "object", "required": ["mpn"]}`)}))
	_, err := updateKind(t, pool, k, "parent", wire.KindPatch{AttributeSchema: schema(nil)})
	if r := refusal(t, err, 422, "bad_property_schema"); r.Detail != "kind 'child' would require 'mpn', which it no longer defines" {
		t.Fatalf("%q", r.Detail)
	}
	if got := merged(t, pool, k, "parent")["properties"]; !reflect.DeepEqual(got, map[string]any{"mpn": map[string]any{"type": "string"}}) {
		t.Fatalf("the parent's schema changed: %v", got)
	}
}

func TestAMoveThatDropsAGrandchildsKeyIsRefused(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	must(createKind(t, pool, k, wire.KindInput{ID: "a", Name: "A", ParentID: ptr("hardware"),
		AttributeSchema: schema(map[string]any{"mpn": map[string]any{"type": "string"}})}))
	must(createKind(t, pool, k, wire.KindInput{ID: "b", Name: "B", ParentID: ptr("a")}))
	must(createKind(t, pool, k, wire.KindInput{ID: "c", Name: "C", ParentID: ptr("b"),
		AttributeSchema: json.RawMessage(`{"type": "object", "required": ["mpn"]}`)}))
	must(createKind(t, pool, k, wire.KindInput{ID: "bare", Name: "Bare", ParentID: ptr("hardware")}))
	_, err := updateKind(t, pool, k, "b", wire.KindPatch{ParentID: wire.Some("bare")})
	if r := refusal(t, err, 422, "bad_property_schema"); r.Detail != "kind 'c' would require 'mpn', which it no longer defines" {
		t.Fatalf("%q", r.Detail)
	}
	if got := getKind(t, pool, k, "b"); got.ParentID == nil || *got.ParentID != "a" {
		t.Fatalf("b moved to %v", got.ParentID)
	}
}

// The owner's kind numbers (2026-11-27): every kind has a number and the code K-<number>; a
// kind made without an id is k<number>.

func TestAKindReadCarriesItsNumberAndCode(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	got := getKind(t, pool, k, "printer")
	var n int64
	if err := pool.QueryRow(context.Background(), `SELECT number FROM item_kinds WHERE id = 'printer'`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if got.Number != n || got.Code != fmt.Sprintf("K-%d", n) {
		t.Fatalf("printer reads number %d code %q, want %d", got.Number, got.Code, n)
	}
	for _, kind := range must(k.List(context.Background(), pool, true)) {
		if kind.Number == 0 || kind.Code != fmt.Sprintf("K-%d", kind.Number) {
			t.Fatalf("%s reads number %d code %q", kind.ID, kind.Number, kind.Code)
		}
	}
}

func TestAKindMadeWithNoIDIsKAndItsNumber(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	made := must(createKind(t, pool, k, wire.KindInput{Name: "Fastener", ParentID: ptr("part")}))
	if made.ID != fmt.Sprintf("k%d", made.Number) || made.Code != fmt.Sprintf("K-%d", made.Number) {
		t.Fatalf("made %q number %d code %q", made.ID, made.Number, made.Code)
	}
	next := must(createKind(t, pool, k, wire.KindInput{Name: "Washer", ParentID: ptr("part")}))
	if next.Number <= made.Number || next.ID != fmt.Sprintf("k%d", next.Number) {
		t.Fatalf("next %q number %d after %d", next.ID, next.Number, made.Number)
	}
}

func TestATypedIDOfKAndDigitsIsRefused(t *testing.T) {
	pool, k := testdb.New(t), kindStore()
	for _, id := range []string{"k12", "k1", "k007"} {
		_, err := createKind(t, pool, k, wire.KindInput{ID: id, Name: "X", ParentID: ptr("part")})
		refusal(t, err, 422, "bad_input")
	}
	must(createKind(t, pool, k, wire.KindInput{ID: "kit", Name: "Kit", ParentID: ptr("part")}))
}
