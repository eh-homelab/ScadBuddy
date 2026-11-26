package flow

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"math/big"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// The base flow: ports of phase 2's tests/inventory/test_stock.py, one test per Python test
// (FIFO by (received_at, id) unless a lot is named, a shortfall on the newest lot, cost
// snapshots, reversals at the use's own cost, "Re-cost past uses"), and what K1 adds: every
// step goes through the resolver and the registry.

const by = `{"kind":"browser","principal":null,"session":null,"run_id":null}`

var bg = context.Background()

func setup(t *testing.T) (*Flow, *pgxpool.Pool) {
	t.Helper()
	pool := testdb.New(t)
	return New(pool), pool
}

func key() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// run is fn in a store.Write of its own: a refusal rolls back what fn wrote and is given
// back; any other error fails the test.
func run[T any](t *testing.T, pool *pgxpool.Pool, fn func(context.Context, pgx.Tx) (T, error)) (T, *store.Refused) {
	t.Helper()
	var out T
	var refused *store.Refused
	_, err := store.Write(bg, pool, store.WriteReq{Caller: "test", Key: key(), Route: "test", Input: []byte(`{}`), Actor: store.Actor(by)},
		func(tx pgx.Tx) (int, any, error) {
			v, err := fn(bg, tx)
			if errors.As(err, &refused) {
				return 0, nil, err
			}
			if err != nil {
				return 0, nil, err
			}
			out = v
			return 200, map[string]any{}, nil
		})
	if err != nil {
		t.Fatal(err)
	}
	return out, refused
}

// out is a call's two results, so a test can take them as one: out(take(...)).ok(t).
func out[T any](v T, refused *store.Refused) outcome[T] { return outcome[T]{v, refused} }

func wantRefused(t *testing.T, refused *store.Refused, status int, code string) {
	t.Helper()
	if refused == nil {
		t.Fatalf("got no refusal, want %d %s", status, code)
	}
	if refused.Status != status || refused.Code != code {
		t.Fatalf("got %d %s (%s), want %d %s", refused.Status, refused.Code, refused.Detail, status, code)
	}
}

func ptr[T any](v T) *T { return &v }

func day(d int) *time.Time {
	at := time.Date(2026, 11, d, 12, 0, 0, 0, time.UTC)
	return &at
}

func newItem(t *testing.T, pool *pgxpool.Pool, kind, name string) string {
	t.Helper()
	items := &store.Items{Cat: handlers.Builtin()}
	return out(run(t, pool, func(ctx context.Context, tx pgx.Tx) (string, error) {
		it, err := items.Create(ctx, tx, wire.ItemInput{KindID: kind, Name: name}, store.Actor(by))
		return it.ID, err
	})).ok(t)
}

func bolt(t *testing.T, pool *pgxpool.Pool) string { return newItem(t, pool, "hardware", "M3 bolt") }

func receiveLot(f *Flow, t *testing.T, item string, in wire.ReceiveLot) (wire.Lot, *store.Refused) {
	t.Helper()
	out, refused := run(t, f.Pool, func(ctx context.Context, tx pgx.Tx) (handlers.ReceiveOut, error) {
		return f.Receive(ctx, tx, item, handlers.ReceiveIn{Lot: &in, By: store.Actor(by), Shop: "USD"})
	})
	if refused != nil {
		return wire.Lot{}, refused
	}
	return *out.Lot, nil
}

func receiveUnits(f *Flow, t *testing.T, item string, in wire.ReceiveUnits) ([]wire.Unit, *store.Refused) {
	t.Helper()
	out, refused := run(t, f.Pool, func(ctx context.Context, tx pgx.Tx) (handlers.ReceiveOut, error) {
		return f.Receive(ctx, tx, item, handlers.ReceiveIn{Units: &in, By: store.Actor(by), Shop: "USD"})
	})
	return out.Units, refused
}

func lot(qty, cost string, at *time.Time) wire.ReceiveLot {
	in := wire.ReceiveLot{Qty: wire.Decimal(qty), ReceivedAt: at}
	if cost != "" {
		in.UnitCostMinor = ptr(wire.Decimal(cost))
	}
	return in
}

// twoLots is 10 @ 500 on day 1 and 10 @ 700 on day 2 (minor units).
func twoLots(f *Flow, t *testing.T, item string) (string, string) {
	t.Helper()
	first := out(receiveLot(f, t, item, lot("10", "500", day(1)))).ok(t)
	second := out(receiveLot(f, t, item, lot("10", "700", day(2)))).ok(t)
	return first.ID, second.ID
}

func take(f *Flow, t *testing.T, item, qty string, use wire.UseInput) ([]wire.Move, *store.Refused) {
	t.Helper()
	use.Qty = wire.Decimal(qty)
	return run(t, f.Pool, func(ctx context.Context, tx pgx.Tx) ([]wire.Move, error) {
		return f.Consume(ctx, tx, item, handlers.ConsumeIn{Use: use, By: store.Actor(by)})
	})
}

func count(f *Flow, t *testing.T, item, onHand, note string) ([]wire.Move, *store.Refused) {
	t.Helper()
	return run(t, f.Pool, func(ctx context.Context, tx pgx.Tx) ([]wire.Move, error) {
		return f.Count(ctx, tx, store.Target{Item: item},
			handlers.CountIn{Count: &wire.CountInput{OnHand: wire.Decimal(onHand), Note: note}, By: store.Actor(by)})
	})
}

func adjust(f *Flow, t *testing.T, lotID, onHand, note string) ([]wire.Move, *store.Refused) {
	t.Helper()
	return run(t, f.Pool, func(ctx context.Context, tx pgx.Tx) ([]wire.Move, error) {
		return f.Count(ctx, tx, store.Target{Lot: lotID},
			handlers.CountIn{Adjust: &wire.AdjustInput{OnHand: wire.Decimal(onHand), Note: note}, By: store.Actor(by)})
	})
}

func giveBack(f *Flow, t *testing.T, item, orderItem string) []wire.Move {
	t.Helper()
	return out(run(t, f.Pool, func(ctx context.Context, tx pgx.Tx) ([]wire.Move, error) {
		return f.GiveBack(ctx, tx, item, orderItem, store.Actor(by))
	})).ok(t)
}

func setCost(f *Flow, t *testing.T, lotID, cost string) (wire.Lot, *store.Refused) {
	t.Helper()
	return run(t, f.Pool, func(ctx context.Context, tx pgx.Tx) (wire.Lot, error) {
		return f.SetLotCost(ctx, tx, lotID, wire.Decimal(cost))
	})
}

func recost(f *Flow, t *testing.T, lotID string, orders ...string) (wire.RecostResult, *store.Refused) {
	t.Helper()
	return run(t, f.Pool, func(ctx context.Context, tx pgx.Tx) (wire.RecostResult, error) {
		return f.Recost(ctx, tx, lotID, orders)
	})
}

func retire(f *Flow, t *testing.T, unit string, in wire.UnitStatusInput) (wire.Unit, *store.Refused) {
	t.Helper()
	return run(t, f.Pool, func(ctx context.Context, tx pgx.Tx) (wire.Unit, error) {
		return f.Retire(ctx, tx, unit, handlers.RetireIn{Status: in, By: store.Actor(by)})
	})
}

func lots(t *testing.T, pool *pgxpool.Pool, item string) []wire.Lot {
	t.Helper()
	out, err := store.Lots(bg, pool, item)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func onHands(t *testing.T, pool *pgxpool.Pool, item string) []string {
	t.Helper()
	out := []string{}
	for _, l := range lots(t, pool, item) {
		out = append(out, l.OnHand)
	}
	return out
}

func moves(t *testing.T, pool *pgxpool.Pool, f store.MoveFilter) []wire.Move {
	t.Helper()
	out, err := store.Moves(bg, pool, f)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func countRows(t *testing.T, pool *pgxpool.Pool, sql string) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(bg, sql).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

// onHandIsTheSumOfMoves is every lot's on_hand equal to the sum of its moves.
func onHandIsTheSumOfMoves(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	if n := countRows(t, pool, `SELECT count(*) FROM stock_lots l WHERE l.on_hand <>
		(SELECT coalesce(sum(qty), 0) FROM stock_moves m WHERE m.lot_id = l.id)`); n != 0 {
		t.Fatalf("%d lots differ from their moves", n)
	}
}

type shape struct{ Qty, Cost string }

func shapes(ms []wire.Move) []shape {
	out := []shape{}
	for _, m := range ms {
		out = append(out, shape{m.Qty, m.UnitCostMinor})
	}
	return out
}

func lotIDs(ms []wire.Move) []string {
	out := []string{}
	for _, m := range ms {
		out = append(out, *m.LotID)
	}
	return out
}

func eq[T any](t *testing.T, got, want T) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v, want %+v", got, want)
	}
}

func TestFifoTakesTheOldestLotFirst(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	first, second := twoLots(f, t, item)

	ms := out(take(f, t, item, "15", wire.UseInput{OrderID: ptr("o1"), OrderItemID: ptr("oi1")})).ok(t)

	eq(t, shapes(ms), []shape{{"-10", "500"}, {"-5", "700"}})
	eq(t, lotIDs(ms), []string{first, second})
	eq(t, []string{ms[0].AmountMinor, ms[1].AmountMinor}, []string{"-5000", "-3500"})
	for _, m := range ms {
		if m.Kind != "use" || *m.OrderItemID != "oi1" || m.NeedsReview {
			t.Fatalf("%+v", m)
		}
	}
	eq(t, onHands(t, pool, item), []string{"0", "5"})
	onHandIsTheSumOfMoves(t, pool)
}

func TestANamedLotIsTakenFirstThenFifo(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	first, second := twoLots(f, t, item)

	ms := out(take(f, t, item, "12", wire.UseInput{LotID: &second})).ok(t)

	eq(t, lotIDs(ms), []string{second, first})
	eq(t, shapes(ms), []shape{{"-10", "700"}, {"-2", "500"}})
	eq(t, onHands(t, pool, item), []string{"8", "0"})
}

func TestALotOfAnotherItemIsNotFound(t *testing.T) {
	f, pool := setup(t)
	item, other := bolt(t, pool), newItem(t, pool, "hardware", "M4 bolt")
	_, second := twoLots(f, t, item)

	_, refused := take(f, t, other, "1", wire.UseInput{LotID: &second})
	wantRefused(t, refused, 404, "lot_not_found")
}

func TestAShortfallGoesNegativeOnTheNewestLot(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	_, second := twoLots(f, t, item)

	ms := out(take(f, t, item, "25", wire.UseInput{})).ok(t)

	eq(t, shapes(ms), []shape{{"-10", "500"}, {"-10", "700"}, {"-5", "700"}})
	eq(t, []bool{ms[0].NeedsReview, ms[1].NeedsReview, ms[2].NeedsReview}, []bool{false, false, true})
	eq(t, *ms[2].LotID, second)
	eq(t, onHands(t, pool, item), []string{"0", "-5"})
	found, err := (&store.Items{Cat: handlers.Builtin()}).Get(bg, pool, item)
	if err != nil || found.OnHand != "-5" {
		t.Fatalf("%+v %v", found, err)
	}
	onHandIsTheSumOfMoves(t, pool)
}

func TestAnItemWithNoLotOpensACostUnknownLot(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)

	ms := out(take(f, t, item, "3", wire.UseInput{})).ok(t)

	ls := lots(t, pool, item)
	if len(ls) != 1 || len(ms) != 1 {
		t.Fatalf("%+v %+v", ls, ms)
	}
	l, m := ls[0], ms[0]
	eq(t, []any{l.Origin, l.UnitCostMinor, l.CostUnknown, l.OnHand}, []any{"adjustment", "0", true, "-3"})
	eq(t, []any{*m.LotID, m.Qty, m.CostUnknown, m.NeedsReview}, []any{l.ID, "-3", true, true})
	onHandIsTheSumOfMoves(t, pool)
}

func TestGiveBackReversesEachUseAtItsOwnCost(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	first, second := twoLots(f, t, item)
	uses := out(take(f, t, item, "15", wire.UseInput{OrderID: ptr("o1"), OrderItemID: ptr("oi1")})).ok(t)
	out(setCost(f, t, second, "900")).ok(t)

	back := giveBack(f, t, item, "oi1")

	type row struct{ Kind, Lot, Qty, Cost string }
	got := []row{}
	for _, m := range back {
		got = append(got, row{m.Kind, *m.LotID, m.Qty, m.UnitCostMinor})
	}
	eq(t, got, []row{{"return", first, "10", "500"}, {"return", second, "5", "700"}})
	eq(t, []string{*back[0].ReversesMoveID, *back[1].ReversesMoveID}, []string{uses[0].ID, uses[1].ID})
	for _, m := range back {
		if *m.OrderID != "o1" || *m.OrderItemID != "oi1" {
			t.Fatalf("%+v", m)
		}
	}
	eq(t, sumAmounts(t, append(uses, back...)), "0")
	eq(t, onHands(t, pool, item), []string{"10", "10"})
	eq(t, giveBack(f, t, item, "oi1"), []wire.Move{})
	onHandIsTheSumOfMoves(t, pool)
}

func sumAmounts(t *testing.T, ms []wire.Move) string {
	t.Helper()
	sum := new(big.Rat)
	for _, m := range ms {
		r, ok := new(big.Rat).SetString(m.AmountMinor)
		if !ok {
			t.Fatalf("amount %q", m.AmountMinor)
		}
		sum.Add(sum, r)
	}
	return sum.RatString()
}

func TestGiveBackReversesAShortfallToo(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	twoLots(f, t, item)
	uses := out(take(f, t, item, "25", wire.UseInput{OrderItemID: ptr("oi1")})).ok(t)

	back := giveBack(f, t, item, "oi1")

	eq(t, sumAmounts(t, append(uses, back...)), "0")
	eq(t, onHands(t, pool, item), []string{"10", "10"})
}

func TestSetLotCostMovesNoPastMove(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	_, second := twoLots(f, t, item)
	use := out(take(f, t, item, "5", wire.UseInput{LotID: &second, OrderItemID: ptr("oi1")})).ok(t)[0]

	l := out(setCost(f, t, second, "900")).ok(t)

	eq(t, []any{l.UnitCostMinor, l.CostUnknown}, []any{"900", false})
	again := moves(t, pool, store.MoveFilter{OrderItemID: "oi1"})
	eq(t, []string{again[0].ID, again[0].UnitCostMinor, again[0].AmountMinor}, []string{use.ID, "700", "-3500"})
}

func TestRecostResnapshotsAndNamesTheOrderItems(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	_, second := twoLots(f, t, item)
	out(take(f, t, item, "5", wire.UseInput{LotID: &second, OrderID: ptr("o1"), OrderItemID: ptr("oi1")})).ok(t)
	out(setCost(f, t, second, "900")).ok(t)

	result := out(recost(f, t, second, "o1")).ok(t)

	eq(t, result.Moves, 1)
	eq(t, result.Orders, []wire.RecostedItem{{OrderID: ptr("o1"), OrderItemID: "oi1", BeforeMinor: "3500", AfterMinor: "4500"}})
	eq(t, result.BeforeUnitCostsMinor, []string{"700"})
	eq(t, []any{result.ItemID, result.UnitCostMinor, result.CostUnknown}, []any{item, "900", false})
	use := moves(t, pool, store.MoveFilter{OrderItemID: "oi1"})[0]
	eq(t, []string{use.UnitCostMinor, use.AmountMinor}, []string{"900", "-4500"})
	for _, m := range moves(t, pool, store.MoveFilter{LotID: second}) {
		if m.Kind == "receive" && m.UnitCostMinor != "700" {
			t.Fatalf("the receive took the new cost: %+v", m)
		}
	}
	eq(t, out(recost(f, t, second, "o1")).ok(t).Moves, 0)
}

func TestRecostKeepsAUseAndItsReturnNettingToZero(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	_, second := twoLots(f, t, item)
	out(take(f, t, item, "5", wire.UseInput{LotID: &second, OrderID: ptr("o1"), OrderItemID: ptr("oi1")})).ok(t)
	giveBack(f, t, item, "oi1")
	out(setCost(f, t, second, "900")).ok(t)

	result := out(recost(f, t, second, "o1")).ok(t)

	eq(t, result.Moves, 2)
	eq(t, result.Orders, []wire.RecostedItem{})
	eq(t, sumAmounts(t, moves(t, pool, store.MoveFilter{OrderItemID: "oi1"})), "0")
}

// markUnknown makes moves of unknown cost that keep their amount, as the parts migration
// can leave them.
func markUnknown(t *testing.T, pool *pgxpool.Pool, where string, args ...any) {
	t.Helper()
	if _, err := pool.Exec(bg, `UPDATE stock_moves SET cost_unknown = true WHERE `+where, args...); err != nil {
		t.Fatal(err)
	}
}

func TestRecostReadsAnUnknownUseAsJobCostDoes(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	_, second := twoLots(f, t, item)
	use := out(take(f, t, item, "5", wire.UseInput{LotID: &second, OrderID: ptr("o1"), OrderItemID: ptr("oi1")})).ok(t)[0]
	markUnknown(t, pool, "id = $1", use.ID)

	result := out(recost(f, t, second, "o1")).ok(t)

	eq(t, result.Orders, []wire.RecostedItem{{OrderID: ptr("o1"), OrderItemID: "oi1", BeforeMinor: "0",
		AfterMinor: "3500", BeforeUnknown: true, AfterUnknown: false}})
}

func TestRecostIgnoresAnUnknownUseThatWasGivenBack(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	first, second := twoLots(f, t, item)
	out(take(f, t, item, "5", wire.UseInput{LotID: &first, OrderID: ptr("o1"), OrderItemID: ptr("oi1")})).ok(t)
	giveBack(f, t, item, "oi1")
	markUnknown(t, pool, "lot_id = $1 AND order_item_id = 'oi1'", first)
	out(take(f, t, item, "5", wire.UseInput{LotID: &second, OrderID: ptr("o1"), OrderItemID: ptr("oi1")})).ok(t)
	out(setCost(f, t, second, "900")).ok(t)

	result := out(recost(f, t, second, "o1")).ok(t)

	eq(t, result.Orders, []wire.RecostedItem{{OrderID: ptr("o1"), OrderItemID: "oi1", BeforeMinor: "3500", AfterMinor: "4500"}})
}

func TestAWholeNumberUomRefusesAFraction(t *testing.T) {
	f, pool := setup(t)
	b := bolt(t, pool)
	_, refused := receiveLot(f, t, b, lot("1.5", "", nil))
	wantRefused(t, refused, 422, "bad_quantity")
	_, refused = take(f, t, b, "1.5", wire.UseInput{})
	wantRefused(t, refused, 422, "bad_quantity")
	_, refused = take(f, t, b, "0", wire.UseInput{})
	wantRefused(t, refused, 422, "bad_quantity")
	eq(t, lots(t, pool, b), []wire.Lot{})

	grease := newItem(t, pool, "lubricant", "Grease")
	out(receiveLot(f, t, grease, lot("10", "0.333333", nil))).ok(t)
	ms := out(take(f, t, grease, "1.5", wire.UseInput{})).ok(t)
	// round(-1.5 * 0.333333, 6), half away from zero as Postgres rounds.
	eq(t, []string{ms[0].Qty, ms[0].AmountMinor}, []string{"-1.5", "-0.5"})
}

func TestALotIsCostedFromItsTotal(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)

	l := out(receiveLot(f, t, item, wire.ReceiveLot{Qty: "3", TotalCostMinor: ptr(wire.Decimal("1000"))})).ok(t)

	eq(t, []any{l.UnitCostMinor, l.CostUnknown, l.CostCurrency}, []any{"333.333333", false, (*string)(nil)})
	ms := moves(t, pool, store.MoveFilter{LotID: l.ID})
	eq(t, []string{ms[0].Kind, ms[0].Qty, ms[0].AmountMinor}, []string{"receive", "3", "999.999999"})
	_, refused := receiveLot(f, t, item, wire.ReceiveLot{Qty: "1", UnitCostMinor: ptr(wire.Decimal("1")), TotalCostMinor: ptr(wire.Decimal("1"))})
	wantRefused(t, refused, 422, "bad_input")
}

func TestAForeignReceiveKeepsWhatWasEntered(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)

	eur := lot("4", "250", nil)
	eur.Currency = ptr("EUR")
	foreign := out(receiveLot(f, t, item, eur)).ok(t)
	usd := lot("4", "250", nil)
	usd.Currency = ptr("USD")
	shop := out(receiveLot(f, t, item, usd)).ok(t)
	blank := out(receiveLot(f, t, item, lot("1", "", nil))).ok(t)
	total := out(receiveLot(f, t, item, wire.ReceiveLot{Qty: "3", TotalCostMinor: ptr(wire.Decimal("1000")), Currency: ptr("GBP")})).ok(t)

	// R-P2-11: the amount given in the other currency is kept.
	eq(t, []any{foreign.UnitCostMinor, foreign.CostUnknown, *foreign.CostCurrency, *foreign.ForeignUnitCostMinor},
		[]any{"0", true, "EUR", "250"})
	eq(t, []any{shop.UnitCostMinor, shop.CostUnknown, shop.CostCurrency, shop.ForeignUnitCostMinor},
		[]any{"250", false, (*string)(nil), (*string)(nil)})
	eq(t, []any{blank.UnitCostMinor, blank.CostUnknown, blank.ForeignUnitCostMinor}, []any{"0", true, (*string)(nil)})
	eq(t, []string{*total.CostCurrency, *total.ForeignUnitCostMinor}, []string{"GBP", "333.333333"})
	if ms := moves(t, pool, store.MoveFilter{LotID: foreign.ID}); !ms[0].CostUnknown {
		t.Fatalf("%+v", ms)
	}
}

func TestALotAtAnUnknownLocationIsRefused(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	in := lot("1", "", nil)
	in.LocationID = ptr("0b7f6a4e-5d3c-4b2a-9e1f-123456789abc")

	_, refused := receiveLot(f, t, item, in)

	wantRefused(t, refused, 404, "location_not_found")
	eq(t, lots(t, pool, item), []wire.Lot{})
}

func TestAdjustLotWritesTheDifference(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	first, _ := twoLots(f, t, item)

	out(adjust(f, t, first, "7", "Shelf count")).ok(t)
	same := out(adjust(f, t, first, "7", "Again")).ok(t)

	eq(t, same, []wire.Move{})
	l, found, err := store.Lot(bg, pool, first)
	if err != nil || !found || l.OnHand != "7" {
		t.Fatalf("%+v %v %v", l, found, err)
	}
	type row struct{ Qty, Cost, Reason string }
	got := []row{}
	for _, m := range moves(t, pool, store.MoveFilter{LotID: first}) {
		if m.Kind == "adjust" {
			got = append(got, row{m.Qty, m.UnitCostMinor, m.Reason})
		}
	}
	eq(t, got, []row{{"-3", "500", "Shelf count"}})
	_, refused := adjust(f, t, first, "1", "  ")
	wantRefused(t, refused, 422, "bad_input")
	_, refused = adjust(f, t, "0b7f6a4e-5d3c-4b2a-9e1f-123456789abc", "1", "x")
	wantRefused(t, refused, 404, "lot_not_found")
	onHandIsTheSumOfMoves(t, pool)
}

func TestACountTakesFifoDownAndAddsToTheNewestLotUp(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	first, second := twoLots(f, t, item)

	type row struct{ Kind, Lot, Qty, Extra string }
	down := out(count(f, t, item, "5", "Year end")).ok(t)
	got := []row{}
	for _, m := range down {
		got = append(got, row{m.Kind, *m.LotID, m.Qty, m.Reason})
	}
	eq(t, got, []row{{"adjust", first, "-10", "Year end"}, {"adjust", second, "-5", "Year end"}})
	up := out(count(f, t, item, "8", "Found some")).ok(t)
	got = []row{}
	for _, m := range up {
		got = append(got, row{m.Kind, *m.LotID, m.Qty, m.UnitCostMinor})
	}
	eq(t, got, []row{{"adjust", second, "3", "700"}})
	eq(t, out(count(f, t, item, "8", "Same")).ok(t), []wire.Move{})
	eq(t, onHands(t, pool, item), []string{"0", "8"})

	empty := newItem(t, pool, "hardware", "Washer")
	opened := out(count(f, t, empty, "4", "First count")).ok(t)
	ls := lots(t, pool, empty)
	eq(t, []any{ls[0].Origin, ls[0].CostUnknown, ls[0].OnHand, opened[0].CostUnknown}, []any{"adjustment", true, "4", true})
	onHandIsTheSumOfMoves(t, pool)
}

func TestUnitStatusWritesItsMove(t *testing.T) {
	f, pool := setup(t)
	nozzle := newItem(t, pool, "nozzle", "0.4 hardened")
	units := out(receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 2, CostMinor: ptr(wire.Decimal("3000")), Serials: []string{"A1", " "}})).ok(t)
	one, two := units[0], units[1]
	eq(t, []any{*one.Serial, two.Serial, one.Status, one.CostMinor}, []any{"A1", (*string)(nil), "in_stock", "3000"})
	kinds := func(ms []wire.Move) []string {
		out := []string{}
		for _, m := range ms {
			out = append(out, m.Kind)
		}
		return out
	}
	eq(t, kinds(moves(t, pool, store.MoveFilter{ItemID: nozzle})), []string{"receive", "receive"})

	used := out(retire(f, t, one.ID, wire.UnitStatusInput{Status: "in_service"})).ok(t)
	if used.Status != "in_service" || used.InServiceAt == nil {
		t.Fatalf("%+v", used)
	}
	retired := out(retire(f, t, two.ID, wire.UnitStatusInput{Status: "retired", Note: "Worn"})).ok(t)
	if retired.RetiredAt == nil {
		t.Fatalf("%+v", retired)
	}

	type row struct{ Kind, Qty, Extra string }
	got := []row{}
	for _, m := range moves(t, pool, store.MoveFilter{UnitID: one.ID}) {
		got = append(got, row{m.Kind, m.Qty, m.UnitCostMinor})
	}
	eq(t, got, []row{{"use", "-1", "3000"}, {"receive", "1", "3000"}})
	r := moves(t, pool, store.MoveFilter{UnitID: two.ID})[0]
	eq(t, row{r.Kind, r.Qty, r.Reason}, row{"retire", "-1", "Worn"})
	// Out of stock to out of stock moves no stock: the unit already left.
	sold := out(retire(f, t, one.ID, wire.UnitStatusInput{Status: "sold"})).ok(t)
	eq(t, sold.Status, "sold")
	eq(t, len(moves(t, pool, store.MoveFilter{UnitID: one.ID})), 2)
	back := out(retire(f, t, one.ID, wire.UnitStatusInput{Status: "in_stock"})).ok(t)
	eq(t, back.Status, "in_stock")
	oneMoves := moves(t, pool, store.MoveFilter{UnitID: one.ID})
	eq(t, oneMoves[0].Kind, "return")
	qty := new(big.Rat)
	for _, m := range oneMoves {
		r, _ := new(big.Rat).SetString(m.Qty)
		qty.Add(qty, r)
	}
	eq(t, qty.RatString(), "1")
	found, err := (&store.Items{Cat: handlers.Builtin()}).Get(bg, pool, nozzle)
	if err != nil || found.OnHand != "1" {
		t.Fatalf("%+v %v", found, err)
	}
	_, refused := retire(f, t, "0b7f6a4e-5d3c-4b2a-9e1f-123456789abc", wire.UnitStatusInput{Status: "sold"})
	wantRefused(t, refused, 404, "unit_not_found")
}

func entityID(t *testing.T, pool *pgxpool.Pool, kind, ref string) string {
	t.Helper()
	var id string
	if err := pool.QueryRow(bg, `SELECT id::text FROM entities WHERE kind = $1 AND ref = $2`, kind, ref).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

func TestAUnitStatusNamesItsEntity(t *testing.T) {
	f, pool := setup(t)
	nozzle := newItem(t, pool, "nozzle", "0.4")
	unit := out(receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 1})).ok(t)[0]
	other := newItem(t, pool, "hotend", "Hotend")

	out(retire(f, t, unit.ID, wire.UnitStatusInput{Status: "in_service", Entity: ptr("item:" + other)})).ok(t)

	m := moves(t, pool, store.MoveFilter{UnitID: unit.ID})[0]
	eq(t, *m.EntityID, entityID(t, pool, "item", other))
}

func TestARepeatedSerialIsRefused(t *testing.T) {
	f, pool := setup(t)
	nozzle := newItem(t, pool, "nozzle", "0.4")
	out(receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 1, Serials: []string{"S1"}})).ok(t)

	_, refused := receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 2, Serials: []string{"S2", "S1"}})
	wantRefused(t, refused, 409, "serial_exists")
	units, err := store.Units(bg, pool, nozzle, "")
	if err != nil || len(units) != 1 {
		t.Fatalf("%+v %v", units, err)
	}
	_, refused = receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 2, Serials: []string{"only one"}})
	wantRefused(t, refused, 422, "bad_input")
}

func TestAnInstalledUnitIsRefused(t *testing.T) {
	f, pool := setup(t)
	nozzle := newItem(t, pool, "nozzle", "0.4")
	units := out(receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 2})).ok(t)
	if _, err := pool.Exec(bg, `UPDATE stock_units SET installed_in_unit_id = $1 WHERE id = $2`, units[0].ID, units[1].ID); err != nil {
		t.Fatal(err)
	}

	_, refused := retire(f, t, units[1].ID, wire.UnitStatusInput{Status: "retired"})
	wantRefused(t, refused, 409, "unit_installed")
}

// TestLotCallsOnAUnitItemAreRefused: a lot call on an item counted in units, or the
// reverse, is wrong_counting (R-K1-23); a step the class does not have is
// step_not_applicable.
func TestLotCallsOnAUnitItemAreRefused(t *testing.T) {
	f, pool := setup(t)
	nozzle := newItem(t, pool, "nozzle", "0.4")
	b := bolt(t, pool)

	_, refused := receiveLot(f, t, nozzle, lot("1", "", nil))
	wantRefused(t, refused, 409, "wrong_counting")
	_, refused = count(f, t, nozzle, "1", "x")
	wantRefused(t, refused, 409, "step_not_applicable")
	_, refused = receiveUnits(f, t, b, wire.ReceiveUnits{Count: 1})
	wantRefused(t, refused, 409, "wrong_counting")
	_, refused = receiveLot(f, t, "0b7f6a4e-5d3c-4b2a-9e1f-123456789abc", lot("1", "", nil))
	wantRefused(t, refused, 404, "item_not_found")
	_, refused = receiveLot(f, t, "not-a-uuid", lot("1", "", nil))
	wantRefused(t, refused, 404, "item_not_found")
}

// TestANotCountedItemTakesNoStock: a service has offers and prices, never lots, units or
// moves (Ruling 1); its stock steps refuse or are not built.
func TestANotCountedItemTakesNoStock(t *testing.T) {
	f, pool := setup(t)
	service := newItem(t, pool, "flat_service", "Laser cutting")
	target := newItem(t, pool, "nozzle", "A printer stand-in")

	_, refused := receiveLot(f, t, service, lot("1", "", nil))
	wantRefused(t, refused, 409, "step_not_applicable")
	_, refused = receiveUnits(f, t, service, wire.ReceiveUnits{Count: 1})
	wantRefused(t, refused, 409, "step_not_applicable")
	_, refused = take(f, t, service, "1", wire.UseInput{Entity: ptr("item:" + target)})
	wantRefused(t, refused, 409, "handler_not_built")
	_, refused = count(f, t, service, "1", "x")
	wantRefused(t, refused, 409, "step_not_applicable")

	for _, table := range []string{"stock_lots", "stock_units", "stock_moves", "entities"} {
		if n := countRows(t, pool, "SELECT count(*) FROM "+table); n != 0 {
			t.Fatalf("%s has %d rows", table, n)
		}
	}
}

func TestUseNamesItsEntity(t *testing.T) {
	f, pool := setup(t)
	grease := newItem(t, pool, "lubricant", "Grease")
	target := newItem(t, pool, "nozzle", "Target")
	out(receiveLot(f, t, grease, lot("10", "5", nil))).ok(t)

	m := out(take(f, t, grease, "2", wire.UseInput{Entity: ptr("item:" + target), Reason: "Rails"})).ok(t)[0]

	eq(t, *m.EntityID, entityID(t, pool, "item", target))
	eq(t, []string{m.Kind, m.Reason}, []string{"use", "Rails"})
	_, refused := take(f, t, grease, "1", wire.UseInput{Entity: ptr("item:0b7f6a4e-5d3c-4b2a-9e1f-123456789abc")})
	wantRefused(t, refused, 404, "entity_not_found")
	_, refused = take(f, t, grease, "1", wire.UseInput{Entity: ptr("nosuchkind:1")})
	wantRefused(t, refused, 422, "unknown_entity_kind")
}

func rat(s string) *big.Rat {
	r, _ := new(big.Rat).SetString(s)
	return r
}

func estimate(f *Flow, t *testing.T, item, qty string) (string, bool) {
	t.Helper()
	unit, unknown, err := f.Estimate(bg, item, rat(qty))
	if err != nil {
		t.Fatal(err)
	}
	return unit.FloatString(6), unknown
}

func TestEstimateMatchesTake(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	twoLots(f, t, item)
	before := countRows(t, pool, "SELECT count(*) FROM stock_moves")

	u, unknown := estimate(f, t, item, "15")
	eq(t, []any{u, unknown}, []any{"566.666667", false})
	// Past the stock on hand, at the newest lot's cost (Review Focus 1).
	u, unknown = estimate(f, t, item, "25")
	eq(t, []any{u, unknown}, []any{"620.000000", false})

	eq(t, countRows(t, pool, "SELECT count(*) FROM stock_moves"), before)
	eq(t, onHands(t, pool, item), []string{"10", "10"})
	ms := out(take(f, t, item, "15", wire.UseInput{})).ok(t)
	total := new(big.Rat)
	for _, m := range ms {
		total.Sub(total, rat(m.AmountMinor))
	}
	eq(t, total.Quo(total, big.NewRat(15, 1)).FloatString(6), "566.666667")

	empty := newItem(t, pool, "hardware", "Washer")
	u, unknown = estimate(f, t, empty, "2")
	eq(t, []any{u, unknown}, []any{"0.000000", true})
}

// whileHeld runs first in an open transaction (no event-log lock, so only its row locks
// hold), then second in a goroutine, which must wait on it (pg_blocking_pids) and finish
// once it commits. It gives second's result.
func whileHeld[T any](t *testing.T, pool *pgxpool.Pool, first func(context.Context, pgx.Tx) error, second func() T) T {
	t.Helper()
	holder, err := pool.Begin(bg)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = holder.Rollback(bg) }()
	if err := first(bg, holder); err != nil {
		t.Fatal(err)
	}
	var pid uint32
	if err := holder.QueryRow(bg, `SELECT pg_backend_pid()`).Scan(&pid); err != nil {
		t.Fatal(err)
	}
	done := make(chan T, 1)
	go func() { done <- second() }()
	waited := false
	for range 200 {
		var n int
		if err := pool.QueryRow(bg, `SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))`,
			int(pid)).Scan(&n); err != nil {
			t.Fatal(err)
		}
		if n > 0 {
			waited = true
			break
		}
		if len(done) > 0 {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if err := holder.Commit(bg); err != nil {
		t.Fatal(err)
	}
	select {
	case out := <-done:
		if !waited {
			t.Fatal("the second call never waited on the first's locks")
		}
		return out
	case <-time.After(30 * time.Second):
		t.Fatal("the second call never finished")
	}
	panic("unreachable")
}

// consumeIn is a take of qty in tx, as an activity's would be.
func (f *Flow) consumeIn(ctx context.Context, tx pgx.Tx, item, qty string) error {
	_, err := f.Consume(ctx, tx, item, handlers.ConsumeIn{Use: wire.UseInput{Qty: wire.Decimal(qty)}, By: store.Actor(by)})
	return err
}

// outcome is a call's value and refusal.
type outcome[T any] struct {
	v       T
	refused *store.Refused
}

// ok is the value; a refusal fails the test.
func (o outcome[T]) ok(t *testing.T) T {
	t.Helper()
	if o.refused != nil {
		t.Fatalf("refused: %v", o.refused)
	}
	return o.v
}

func TestAWaitOnTheLotsTakesWhatTheFirstTakeLeft(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	out(receiveLot(f, t, item, lot("10", "500", nil))).ok(t)

	second := whileHeld(t, pool,
		func(ctx context.Context, tx pgx.Tx) error { return f.consumeIn(ctx, tx, item, "8") },
		func() outcome[[]wire.Move] {
			ms, refused := take(f, t, item, "5", wire.UseInput{})
			return outcome[[]wire.Move]{ms, refused}
		})

	ms := second.ok(t)
	eq(t, []any{ms[0].Qty, ms[0].NeedsReview, ms[1].Qty, ms[1].NeedsReview}, []any{"-2", false, "-3", true})
	eq(t, onHands(t, pool, item), []string{"-3"})
	onHandIsTheSumOfMoves(t, pool)
}

func TestTwoTakesOnAnItemWithNoLotOpenOneLot(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)

	second := whileHeld(t, pool,
		func(ctx context.Context, tx pgx.Tx) error { return f.consumeIn(ctx, tx, item, "2") },
		func() outcome[[]wire.Move] {
			ms, refused := take(f, t, item, "3", wire.UseInput{})
			return outcome[[]wire.Move]{ms, refused}
		})

	second.ok(t)
	ls := lots(t, pool, item)
	if len(ls) != 1 || ls[0].Origin != "adjustment" || ls[0].OnHand != "-5" {
		t.Fatalf("%+v", ls)
	}
	onHandIsTheSumOfMoves(t, pool)
}

func TestTwoGiveBacksReturnEachUseOnce(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	twoLots(f, t, item)
	out(take(f, t, item, "15", wire.UseInput{OrderItemID: ptr("oi1")})).ok(t)

	second := whileHeld(t, pool,
		func(ctx context.Context, tx pgx.Tx) error {
			_, err := f.GiveBack(ctx, tx, item, "oi1", store.Actor(by))
			return err
		},
		func() outcome[[]wire.Move] {
			return out(run(t, pool, func(ctx context.Context, tx pgx.Tx) ([]wire.Move, error) {
				return f.GiveBack(ctx, tx, item, "oi1", store.Actor(by))
			}))
		})

	eq(t, second.ok(t), []wire.Move{})
	eq(t, onHands(t, pool, item), []string{"10", "10"})
}

func TestAKindMadeByAPersonIsCountedByItsCounting(t *testing.T) {
	f, pool := setup(t)
	kinds := &store.Kinds{Cat: handlers.Builtin()}
	out(run(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.ItemKind, error) {
		return kinds.Create(ctx, tx, wire.KindInput{ID: "fastener", Name: "Fastener", ParentID: ptr("consumable"), Tracking: ptr("lot")}, store.Actor(by))
	})).ok(t)
	item := newItem(t, pool, "fastener", "Rivet")

	l := out(receiveLot(f, t, item, lot("2", "1", nil))).ok(t)

	got, found, err := store.Lot(bg, pool, l.ID)
	if err != nil || !found {
		t.Fatal(found, err)
	}
	eq(t, got, l)
	if _, found, err := store.Lot(bg, pool, "0b7f6a4e-5d3c-4b2a-9e1f-123456789abc"); found || err != nil {
		t.Fatal(found, err)
	}
	eq(t, []string{l.OnHand, l.UnitCostMinor}, []string{"2", "1"})
}

func TestRecostOfAnOrderItemWithUsesOnTwoLots(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	_, second := twoLots(f, t, item)
	out(take(f, t, item, "15", wire.UseInput{OrderID: ptr("o1"), OrderItemID: ptr("oi1")})).ok(t)
	out(setCost(f, t, second, "900")).ok(t)

	result := out(recost(f, t, second, "o1")).ok(t)

	eq(t, result.Moves, 1)
	eq(t, result.Orders, []wire.RecostedItem{{OrderID: ptr("o1"), OrderItemID: "oi1", BeforeMinor: "8500", AfterMinor: "9500"}})
}

func TestRecostOfACostUnknownLotClearsItsMovesFlag(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	use := out(take(f, t, item, "3", wire.UseInput{OrderID: ptr("o1"), OrderItemID: ptr("oi1")})).ok(t)[0]
	if !use.CostUnknown {
		t.Fatalf("%+v", use)
	}
	out(setCost(f, t, *use.LotID, "0")).ok(t)

	result := out(recost(f, t, *use.LotID, "o1")).ok(t)

	eq(t, result.Moves, 1)
	eq(t, result.Orders, []wire.RecostedItem{{OrderID: ptr("o1"), OrderItemID: "oi1", BeforeMinor: "0", AfterMinor: "0",
		BeforeUnknown: true, AfterUnknown: false}})
	if again := moves(t, pool, store.MoveFilter{OrderItemID: "oi1"}); again[0].CostUnknown {
		t.Fatalf("%+v", again)
	}
}

func TestRecostAndGiveBackWaitForEachOther(t *testing.T) {
	for _, giveBackFirst := range []bool{true, false} {
		t.Run(map[bool]string{true: "give back first", false: "recost first"}[giveBackFirst], func(t *testing.T) {
			f, pool := setup(t)
			item := bolt(t, pool)
			_, second := twoLots(f, t, item)
			out(take(f, t, item, "5", wire.UseInput{LotID: &second, OrderID: ptr("o1"), OrderItemID: ptr("oi1")})).ok(t)
			out(setCost(f, t, second, "900")).ok(t)

			back := func(ctx context.Context, tx pgx.Tx) error {
				_, err := f.GiveBack(ctx, tx, item, "oi1", store.Actor(by))
				return err
			}
			re := func(ctx context.Context, tx pgx.Tx) error {
				_, err := f.Recost(ctx, tx, second, []string{"o1"})
				return err
			}
			alone := func(fn func(context.Context, pgx.Tx) error) func() *store.Refused {
				return func() *store.Refused {
					_, refused := run(t, pool, func(ctx context.Context, tx pgx.Tx) (struct{}, error) {
						return struct{}{}, fn(ctx, tx)
					})
					return refused
				}
			}
			var refused *store.Refused
			if giveBackFirst {
				refused = whileHeld(t, pool, back, alone(re))
			} else {
				refused = whileHeld(t, pool, re, alone(back))
			}
			out(0, refused).ok(t)

			ms := moves(t, pool, store.MoveFilter{OrderItemID: "oi1"})
			for _, m := range ms {
				if m.UnitCostMinor != "900" {
					t.Fatalf("%+v", ms)
				}
			}
			eq(t, sumAmounts(t, ms), "0")
			eq(t, onHands(t, pool, item), []string{"10", "10"})
		})
	}
}

func TestAnEmptyNamedLotIsRefused(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	first, second := twoLots(f, t, item)
	out(take(f, t, item, "10", wire.UseInput{LotID: &first})).ok(t)
	out(take(f, t, item, "15", wire.UseInput{LotID: &second})).ok(t) // 5 short: second is at -5

	for _, empty := range []string{first, second} {
		_, refused := take(f, t, item, "3", wire.UseInput{LotID: &empty})
		wantRefused(t, refused, 409, "lot_empty")
	}
	eq(t, onHands(t, pool, item), []string{"0", "-5"})
}

func TestANamedOlderLotAndAShortfallTakeTheNewestLotsCost(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	first, second := twoLots(f, t, item)

	ms := out(take(f, t, item, "25", wire.UseInput{LotID: &first})).ok(t)

	type row struct {
		Lot, Qty, Cost string
		Short          bool
	}
	got := []row{}
	for _, m := range ms {
		got = append(got, row{*m.LotID, m.Qty, m.UnitCostMinor, m.NeedsReview})
	}
	eq(t, got, []row{{first, "-10", "500", false}, {second, "-10", "700", false}, {second, "-5", "700", true}})
}

func TestANamedLotOnAnItemWithNoLotOpensNone(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)

	tx, err := pool.Begin(bg)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback(bg) }()
	_, err = f.Consume(bg, tx, item, handlers.ConsumeIn{Use: wire.UseInput{Qty: "1", LotID: ptr("0b7f6a4e-5d3c-4b2a-9e1f-123456789abc")}, By: store.Actor(by)})
	var refused *store.Refused
	if !errors.As(err, &refused) || refused.Code != "lot_not_found" {
		t.Fatalf("got %v, want lot_not_found", err)
	}
	var n int
	if err := tx.QueryRow(bg, `SELECT count(*) FROM stock_lots`).Scan(&n); err != nil || n != 0 {
		t.Fatalf("%d lots, %v", n, err)
	}
}

func TestGiveBackReturnsOnlyTheNamedItem(t *testing.T) {
	f, pool := setup(t)
	b, nut := bolt(t, pool), newItem(t, pool, "hardware", "M3 nut")
	twoLots(f, t, b)
	twoLots(f, t, nut)
	out(take(f, t, b, "2", wire.UseInput{OrderItemID: ptr("oi1")})).ok(t)
	out(take(f, t, nut, "3", wire.UseInput{OrderItemID: ptr("oi1")})).ok(t)

	back := giveBack(f, t, b, "oi1")

	if len(back) != 1 || back[0].ItemID != b || back[0].Qty != "2" {
		t.Fatalf("%+v", back)
	}
	eq(t, onHands(t, pool, nut), []string{"7", "10"})
}

func TestACountNeedsANote(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)

	_, refused := count(f, t, item, "1", "  ")
	wantRefused(t, refused, 422, "bad_input")
}

func TestACostSetLaterKeepsTheCurrencyBoughtIn(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	eur := lot("2", "250", nil)
	eur.Currency = ptr("EUR")
	l := out(receiveLot(f, t, item, eur)).ok(t)
	usd := lot("1", "", nil)
	usd.Currency = ptr("USD")
	blank := out(receiveLot(f, t, item, usd)).ok(t)

	costed := out(setCost(f, t, l.ID, "270")).ok(t)

	eq(t, []any{costed.UnitCostMinor, costed.CostUnknown, *costed.CostCurrency, *costed.ForeignUnitCostMinor},
		[]any{"270", false, "EUR", "250"})
	eq(t, blank.CostCurrency, (*string)(nil))
}

func TestACountSettlesANegativeLotFirst(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	first, second := twoLots(f, t, item)
	out(take(f, t, item, "25", wire.UseInput{})).ok(t) // 0 and -5
	third := out(receiveLot(f, t, item, lot("10", "600", day(3)))).ok(t).ID

	down := out(count(f, t, item, "4", "Shelf")).ok(t)

	type row struct{ Lot, Qty, Cost string }
	got := []row{}
	for _, m := range down {
		got = append(got, row{*m.LotID, m.Qty, m.UnitCostMinor})
	}
	eq(t, got, []row{{second, "5", "700"}, {third, "-6", "600"}})
	eq(t, onHands(t, pool, item), []string{"0", "0", "4"})

	out(take(f, t, item, "6", wire.UseInput{LotID: &third})).ok(t) // 0, 0, -2: the newest is negative
	up := out(count(f, t, item, "3", "Found")).ok(t)

	if len(up) != 1 || *up[0].LotID != third || up[0].Qty != "5" {
		t.Fatalf("%+v", up)
	}
	eq(t, onHands(t, pool, item), []string{"0", "0", "3"})
	for _, m := range append(down, up...) {
		if *m.LotID == first {
			t.Fatalf("the count moved the first lot: %+v", m)
		}
	}
	onHandIsTheSumOfMoves(t, pool)
}

func TestARecostWhoseAmountWouldNotFitIsBadQuantity(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	l := out(receiveLot(f, t, item, lot("1", "1", nil))).ok(t)
	out(take(f, t, item, "10000000000000", wire.UseInput{})).ok(t)
	out(adjust(f, t, l.ID, "0", "found")).ok(t)
	out(setCost(f, t, l.ID, "100")).ok(t)

	_, refused := recost(f, t, l.ID)
	wantRefused(t, refused, 422, "bad_quantity")
}

func TestACostWhoseLotValueWouldNotFitIsBadQuantity(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	l := out(receiveLot(f, t, item, lot("10000000000000", "", nil))).ok(t)

	_, refused := setCost(f, t, l.ID, "10000000000000")
	wantRefused(t, refused, 422, "bad_quantity")
}

func TestAnEntityRefWithANulIsABadRef(t *testing.T) {
	f, pool := setup(t)
	grease := newItem(t, pool, "lubricant", "Grease")
	out(receiveLot(f, t, grease, lot("10", "5", nil))).ok(t)

	_, refused := take(f, t, grease, "1", wire.UseInput{Entity: ptr("order:\x00")})
	wantRefused(t, refused, 422, "bad_entity_ref")
	if refused.Type != "https://scadbuddy.dev/problems/entities/bad_entity_ref" {
		t.Fatalf("type %q", refused.Type)
	}
}

// TestBadInputIsRefusedNotA500: what the public models refuse is a 4xx answer, never an
// error a retry would repeat.
func TestBadInputIsRefusedNotA500(t *testing.T) {
	f, pool := setup(t)
	b := bolt(t, pool)
	nozzle := newItem(t, pool, "nozzle", "0.4")
	first, _ := twoLots(f, t, b)
	unit := out(receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 1})).ok(t)[0]

	for name, call := range map[string]func() *store.Refused{
		"negative qty":  func() *store.Refused { _, r := receiveLot(f, t, b, lot("-1", "", nil)); return r },
		"seven places":  func() *store.Refused { _, r := receiveLot(f, t, b, lot("1", "0.1234567", nil)); return r },
		"negative cost": func() *store.Refused { _, r := receiveLot(f, t, b, lot("1", "-1", nil)); return r },
		"bad currency": func() *store.Refused {
			in := lot("1", "1", nil)
			in.Currency = ptr("usd")
			_, r := receiveLot(f, t, b, in)
			return r
		},
		"bad location": func() *store.Refused {
			in := lot("1", "1", nil)
			in.LocationID = ptr("shelf")
			_, r := receiveLot(f, t, b, in)
			return r
		},
		"nul note": func() *store.Refused {
			in := lot("1", "1", nil)
			in.Note = "a\x00b"
			_, r := receiveLot(f, t, b, in)
			return r
		},
		"no units":           func() *store.Refused { _, r := receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 0}); return r },
		"too many units":     func() *store.Refused { _, r := receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 101}); return r },
		"bad lot id":         func() *store.Refused { _, r := take(f, t, b, "1", wire.UseInput{LotID: ptr("x")}); return r },
		"no entity colon":    func() *store.Refused { _, r := take(f, t, b, "1", wire.UseInput{Entity: ptr("printer")}); return r },
		"negative count":     func() *store.Refused { _, r := count(f, t, b, "-1", "x"); return r },
		"negative adjust":    func() *store.Refused { _, r := adjust(f, t, first, "-1", "x"); return r },
		"bad status":         func() *store.Refused { _, r := retire(f, t, unit.ID, wire.UnitStatusInput{Status: "lost"}); return r },
		"ended is no status": func() *store.Refused { _, r := retire(f, t, unit.ID, wire.UnitStatusInput{Status: "ended"}); return r },
		"bad lot target":     func() *store.Refused { _, r := setCost(f, t, "x", "1"); return r },
		"seven places cost":  func() *store.Refused { _, r := setCost(f, t, first, "1.0000001"); return r },
		"long reason": func() *store.Refused {
			_, r := take(f, t, b, "1", wire.UseInput{Reason: string(make([]byte, 4001))})
			return r
		},
		"unit status nul ref": func() *store.Refused {
			_, r := retire(f, t, unit.ID, wire.UnitStatusInput{Status: "sold", Entity: ptr("item:\x00")})
			return r
		},
	} {
		refused := call()
		if refused == nil || refused.Status < 400 || refused.Status >= 500 {
			t.Errorf("%s: got %v, want a 4xx refusal", name, refused)
		}
	}
	eq(t, onHands(t, pool, b), []string{"10", "10"})
}

func TestAServiceCannotBeReceived(t *testing.T) {
	f, pool := setup(t)
	labour := newItem(t, pool, "labour", "Assembly hour")

	_, refused := receiveLot(f, t, labour, lot("1", "", nil))

	wantRefused(t, refused, 409, "step_not_applicable")
	eq(t, refused.Fields["step"], any("receive"))
	if n := countRows(t, pool, "SELECT count(*) FROM stock_lots"); n != 0 {
		t.Fatalf("%d lots", n)
	}
}

func TestADurableConsumeIsNotBuilt(t *testing.T) {
	f, pool := setup(t)
	nozzle := newItem(t, pool, "nozzle", "0.4")

	_, refused := take(f, t, nozzle, "1", wire.UseInput{})

	wantRefused(t, refused, 409, "handler_not_built")
	eq(t, refused.Fields["handler"], any("consume.use"))
}

func TestAnIncompleteStepIsRefused(t *testing.T) {
	f, pool := setup(t)
	kinds := &store.Kinds{Cat: handlers.Builtin()}
	made := out(run(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.ItemKind, error) {
		return kinds.Create(ctx, tx, wire.KindInput{ID: "wipes", Name: "Wipes", ParentID: ptr("consumable"),
			Counting: ptr("unit"), Steps: model.Steps{"count": {Handler: "count.remaining"}}}, store.Actor(by))
	})).ok(t)
	eq(t, made.IncompleteSteps, []string{"count"})
	item := newItem(t, pool, "wipes", "Lens wipes")

	_, refused := count(f, t, item, "1", "x")

	wantRefused(t, refused, 409, "step_incomplete")
	eq(t, refused.Fields["step"], any("count"))
	eq(t, refused.Fields["missing"], any([]string{"capacity"}))
	out(receiveUnits(f, t, item, wire.ReceiveUnits{Count: 2})).ok(t)
}

func newLocation(t *testing.T, pool *pgxpool.Pool) string {
	t.Helper()
	var id string
	if err := pool.QueryRow(bg, `WITH k AS (INSERT INTO filament_location_kinds (name) VALUES ('Test shelf ' || gen_random_uuid()) RETURNING id)
		INSERT INTO filament_locations (name, kind_id) SELECT 'Shelf 1', id FROM k RETURNING id::text`).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

func TestAPlaceChangeWritesNoMove(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	first, _ := twoLots(f, t, item)
	place := newLocation(t, pool)
	before := countRows(t, pool, "SELECT count(*) FROM stock_moves")

	out(run(t, pool, func(ctx context.Context, tx pgx.Tx) (struct{}, error) {
		return struct{}{}, f.Move(ctx, tx, store.Target{Lot: first}, handlers.MoveIn{LocationID: &place, By: store.Actor(by)})
	})).ok(t)

	l, _, err := store.Lot(bg, pool, first)
	if err != nil || l.LocationID == nil || *l.LocationID != place {
		t.Fatalf("%+v %v", l, err)
	}
	eq(t, countRows(t, pool, "SELECT count(*) FROM stock_moves"), before)

	// The lot's patch: a place, a note and a cost, in one write.
	got := out(run(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.Lot, error) {
		return f.LotUpdate(ctx, tx, first, wire.LotPatch{LocationID: wire.Null[string](), Note: ptr("Top shelf"),
			UnitCostMinor: ptr(wire.Decimal("550"))}, store.Actor(by))
	})).ok(t)
	if got.LocationID != nil || got.Note != "Top shelf" || got.UnitCostMinor != "550" {
		t.Fatalf("%+v", got)
	}
	_, refused := run(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.Lot, error) {
		return f.LotUpdate(ctx, tx, first, wire.LotPatch{LocationID: wire.Some("0b7f6a4e-5d3c-4b2a-9e1f-123456789abc")}, store.Actor(by))
	})
	wantRefused(t, refused, 404, "location_not_found")

	nozzle := newItem(t, pool, "nozzle", "0.4")
	unit := out(receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 1})).ok(t)[0]
	moved := out(run(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.Unit, error) {
		return f.UnitUpdate(ctx, tx, unit.ID, wire.UnitPatch{Name: ptr("Spare"), Serial: wire.Some(" S9 "),
			LocationID: wire.Some(place)}, store.Actor(by))
	})).ok(t)
	if moved.Name != "Spare" || moved.Serial == nil || *moved.Serial != "S9" || moved.LocationID == nil || *moved.LocationID != place {
		t.Fatalf("%+v", moved)
	}
	cleared := out(run(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.Unit, error) {
		return f.UnitUpdate(ctx, tx, unit.ID, wire.UnitPatch{Serial: wire.Some("  ")}, store.Actor(by))
	})).ok(t)
	if cleared.Serial != nil || cleared.LocationID == nil {
		t.Fatalf("%+v", cleared)
	}
	eq(t, len(moves(t, pool, store.MoveFilter{UnitID: unit.ID})), 1)
}

func TestRecostRefusesAnUnlockedOrder(t *testing.T) {
	f, pool := setup(t)
	item := bolt(t, pool)
	_, second := twoLots(f, t, item)
	out(take(f, t, item, "2", wire.UseInput{LotID: &second, OrderID: ptr("o1"), OrderItemID: ptr("oi1")})).ok(t)
	out(take(f, t, item, "2", wire.UseInput{LotID: &second, OrderID: ptr("o2"), OrderItemID: ptr("oi2")})).ok(t)
	out(setCost(f, t, second, "900")).ok(t)

	_, refused := recost(f, t, second, "o1")

	wantRefused(t, refused, 409, "recost_orders_moved")
	for _, m := range moves(t, pool, store.MoveFilter{LotID: second}) {
		if m.UnitCostMinor != "700" {
			t.Fatalf("a move changed: %+v", m)
		}
	}
}

func newPrinter(t *testing.T, pool *pgxpool.Pool, serial string) string {
	t.Helper()
	var id string
	if err := pool.QueryRow(bg, `INSERT INTO printers (id, serial, name) VALUES (gen_random_uuid(), $1, $1) RETURNING id::text`,
		serial).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

func onPrinter(t *testing.T, pool *pgxpool.Pool, unit, printer string) {
	t.Helper()
	if _, err := pool.Exec(bg, `UPDATE stock_units SET printer_id = $2 WHERE id = $1`, unit, printer); err != nil {
		t.Fatal(err)
	}
}

func printerOf(t *testing.T, pool *pgxpool.Pool, unit string) string {
	t.Helper()
	var id string
	if err := pool.QueryRow(bg, `SELECT printer_id::text FROM stock_units WHERE id = $1`, unit).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

func repoint(f *Flow, t *testing.T, gone, kept string, only *Moved) Moved {
	t.Helper()
	return out(run(t, f.Pool, func(ctx context.Context, tx pgx.Tx) (Moved, error) {
		return f.Repoint(ctx, tx, "printer", gone, kept, only)
	})).ok(t)
}

func TestRepointMovesStockRowsOnce(t *testing.T) {
	f, pool := setup(t)
	a, b := newPrinter(t, pool, "A"), newPrinter(t, pool, "B")
	grease := newItem(t, pool, "lubricant", "Grease")
	out(receiveLot(f, t, grease, lot("10", "5", nil))).ok(t)
	use := out(take(f, t, grease, "1", wire.UseInput{Entity: ptr("printer:" + a)})).ok(t)[0]
	nozzle := newItem(t, pool, "nozzle", "0.4")
	unit := out(receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 1})).ok(t)[0]
	onPrinter(t, pool, unit.ID, a)

	moved := repoint(f, t, a, b, nil)

	eq(t, moved, Moved{Moves: []string{use.ID}, Units: []string{unit.ID}})
	eq(t, *moves(t, pool, store.MoveFilter{LotID: *use.LotID})[0].EntityID, entityID(t, pool, "printer", b))
	eq(t, printerOf(t, pool, unit.ID), b)
	eq(t, repoint(f, t, a, b, nil), Moved{Moves: []string{}, Units: []string{}})
}

func TestABackRepointNeverRefuses(t *testing.T) {
	f, pool := setup(t)
	a, b, c := newPrinter(t, pool, "A"), newPrinter(t, pool, "B"), newPrinter(t, pool, "C")
	nozzle := newItem(t, pool, "nozzle", "0.4")
	units := out(receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 3})).ok(t)
	u1, u2, u3 := units[0].ID, units[1].ID, units[2].ID
	for _, u := range []string{u1, u2, u3} {
		onPrinter(t, pool, u, b)
	}
	if _, err := pool.Exec(bg, `DELETE FROM stock_moves WHERE unit_id = $1`, u1); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(bg, `DELETE FROM stock_units WHERE id = $1`, u1); err != nil {
		t.Fatal(err)
	}
	onPrinter(t, pool, u2, c)

	moved := repoint(f, t, b, a, &Moved{Units: []string{u1, u2, u3}})

	eq(t, moved, Moved{Moves: []string{}, Units: []string{u3}})
	eq(t, printerOf(t, pool, u3), a)
	eq(t, printerOf(t, pool, u2), c)
}

func TestRepointOnlyMovesTheNamedRows(t *testing.T) {
	f, pool := setup(t)
	a, b := newPrinter(t, pool, "A"), newPrinter(t, pool, "B")
	nozzle := newItem(t, pool, "nozzle", "0.4")
	units := out(receiveUnits(f, t, nozzle, wire.ReceiveUnits{Count: 2})).ok(t)
	u0, u1 := units[0].ID, units[1].ID
	onPrinter(t, pool, u0, b)
	onPrinter(t, pool, u1, a)

	eq(t, repoint(f, t, a, b, nil), Moved{Moves: []string{}, Units: []string{u1}})
	eq(t, repoint(f, t, b, a, &Moved{Units: []string{u1}}), Moved{Moves: []string{}, Units: []string{u1}})

	eq(t, printerOf(t, pool, u1), a)
	eq(t, printerOf(t, pool, u0), b)
}

// TestEveryK1DefaultIsRegistered: the handlers K1 builds have implementations; the rest
// are ErrNotBuilt (R-K1-7).
func TestEveryK1DefaultIsRegistered(t *testing.T) {
	reg := New(nil).Reg
	built := []string{"refuse", "receive.lot", "receive.units", "consume.lot_fifo", "count.lot", "move.place",
		"retire.default", "cost.lot_fifo", "cost.unit"}
	for id, sp := range reg.Catalog.Specs {
		_, err := reg.For(sp.Steps[0], id)
		var notBuilt handlers.ErrNotBuilt
		if slices.Contains(built, id) != (err == nil) || (err != nil && !errors.As(err, &notBuilt)) {
			t.Errorf("%s: %v", id, err)
		}
	}
}

// TestASpecialTypeWhoseNeedsAreUnmetBlocksEveryStep: a problem with no step (a special
// type whose needs the class does not meet) blocks every step's call, not none. A save
// refuses it, so the rows are written as a broken migration could leave them.
func TestASpecialTypeWhoseNeedsAreUnmetBlocksEveryStep(t *testing.T) {
	f, pool := setup(t)
	var item string
	if err := pool.QueryRow(bg, `WITH k AS (INSERT INTO item_kinds (id, name, parent_id, tracking, special_type)
		VALUES ('odd_stand', 'Odd stand', 'hardware', 'lot', 'printer') RETURNING id)
		INSERT INTO items (kind_id, name) SELECT id, 'Stand' FROM k RETURNING id::text`).Scan(&item); err != nil {
		t.Fatal(err)
	}

	_, refused := count(f, t, item, "1", "Shelf count")

	wantRefused(t, refused, 422, "steps_invalid")
	if n := countRows(t, pool, "SELECT count(*) FROM stock_moves"); n != 0 {
		t.Fatalf("%d moves", n)
	}
}
