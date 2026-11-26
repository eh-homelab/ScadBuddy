package store

import (
	"context"
	"errors"
	"math/big"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// Stock: the lock order of phase 2's tests/inventory/test_stock.py (R-P2-28, R-P2-32) and
// the decimal rules every move keeps. The steps themselves are tested through the base
// flow (flow/flow_test.go).

func stockItem(t *testing.T, pool *pgxpool.Pool, name string) wire.Item {
	t.Helper()
	made := must(createItem(t, pool, itemStore(), wire.ItemInput{KindID: "hardware", Name: name}))
	return made
}

func stockLot(t *testing.T, pool *pgxpool.Pool, item wire.Item, qty string) {
	t.Helper()
	must(inTx(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.Lot, error) {
		if _, err := Lock(ctx, tx, Target{Item: item.ID}, true); err != nil {
			return wire.Lot{}, err
		}
		return Stock{}.ReceiveLot(ctx, tx, item, wire.ReceiveLot{Qty: wire.Decimal(qty)}, "USD", Actor(browser))
	}))
}

// holdLockItems is a transaction that has run LockItems on ids; the caller rolls it back.
func holdLockItems(t *testing.T, pool *pgxpool.Pool, ids ...string) pgx.Tx {
	t.Helper()
	ctx := context.Background()
	held, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = held.Rollback(ctx) })
	if err := LockItems(ctx, held, ids); err != nil {
		t.Fatal(err)
	}
	return held
}

// waits is fn in a transaction with a short lock_timeout: whether it gave up waiting for a
// lock (lock_not_available).
func waits(t *testing.T, pool *pgxpool.Pool, fn func(context.Context, pgx.Tx) error) bool {
	t.Helper()
	ctx := context.Background()
	err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SET LOCAL lock_timeout = '200ms'`); err != nil {
			return err
		}
		return fn(ctx, tx)
	})
	var pg *pgconn.PgError
	if err != nil && !(errors.As(err, &pg) && pg.Code == "55P03") {
		t.Fatal(err)
	}
	return err != nil
}

func receiveIn(item wire.Item) func(context.Context, pgx.Tx) error {
	return func(ctx context.Context, tx pgx.Tx) error {
		if _, err := Lock(ctx, tx, Target{Item: item.ID}, true); err != nil {
			return err
		}
		_, err := Stock{}.ReceiveLot(ctx, tx, item, wire.ReceiveLot{Qty: "1"}, "USD", Actor(browser))
		return err
	}
}

func lotCount(t *testing.T, pool *pgxpool.Pool, item wire.Item) int {
	t.Helper()
	ls, err := Lots(context.Background(), pool, item.ID)
	if err != nil {
		t.Fatal(err)
	}
	return len(ls)
}

// TestLockItemsLocksItemByItemInItemIDOrder: whatever order the caller names them in, each
// item's open lock and lots are held at the end (R-P2-28, R-P2-32). Phase 2 spied on the
// call order; here a receive of each item, named in any order, waits.
func TestLockItemsLocksItemByItemInItemIDOrder(t *testing.T) {
	pool := testdb.New(t)
	items := []wire.Item{stockItem(t, pool, "Bolt 0"), stockItem(t, pool, "Bolt 1"), stockItem(t, pool, "Bolt 2")}
	holdLockItems(t, pool, items[2].ID, items[0].ID, items[1].ID)

	for _, it := range items {
		if !waits(t, pool, receiveIn(it)) {
			t.Fatalf("a receive of %s did not wait", it.Name)
		}
	}
	var held int
	if err := pool.QueryRow(context.Background(), `SELECT count(*) FROM pg_locks
		WHERE locktype = 'advisory' AND granted AND classid::int = hashtext('stock_lots_open')`).Scan(&held); err != nil {
		t.Fatal(err)
	}
	if held != 3 {
		t.Fatalf("%d open locks held, want 3", held)
	}
}

func TestAReceiveWaitsForAHolderOfLockItems(t *testing.T) {
	pool := testdb.New(t)
	item := stockItem(t, pool, "M3 bolt")
	held := holdLockItems(t, pool, item.ID)

	if !waits(t, pool, receiveIn(item)) {
		t.Fatal("the receive did not wait")
	}
	if n := lotCount(t, pool, item); n != 0 {
		t.Fatalf("%d lots", n)
	}
	_ = held.Rollback(context.Background())
	stockLot(t, pool, item, "1")
	if n := lotCount(t, pool, item); n != 1 {
		t.Fatalf("%d lots", n)
	}
}

func TestLockItemsLocksAnItemWithLotsInTheSameOrder(t *testing.T) {
	pool := testdb.New(t)
	item := stockItem(t, pool, "M3 bolt")
	stockLot(t, pool, item, "10")
	stockLot(t, pool, item, "10")
	holdLockItems(t, pool, item.ID)

	if !waits(t, pool, receiveIn(item)) {
		t.Fatal("the receive did not wait")
	}
	if n := lotCount(t, pool, item); n != 2 {
		t.Fatalf("%d lots", n)
	}
}

// TestACountTakesTheOpenLockBeforeTheLots: a count waits for the open lock alone, and while
// it waits it holds no lot, with or without lots (M6, R-P2-32).
func TestACountTakesTheOpenLockBeforeTheLots(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	empty, stocked := stockItem(t, pool, "Empty"), stockItem(t, pool, "Stocked")
	stockLot(t, pool, stocked, "10")

	for _, it := range []wire.Item{stocked, empty} {
		held, err := pool.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = held.Rollback(ctx) })
		if err := openLock(ctx, held, it.ID); err != nil {
			t.Fatal(err)
		}
		done := make(chan error, 1)
		go func() {
			done <- pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
				if _, err := Lock(ctx, tx, Target{Item: it.ID}, true); err != nil {
					return err
				}
				_, err := Stock{}.CountItem(ctx, tx, it, wire.CountInput{OnHand: "3", Note: "Shelf count"}, Actor(browser))
				return err
			})
		}()
		waitForWaiter(t, pool, held)
		// The count waits on the open lock and holds none of the lots yet.
		if err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
			_, err := tx.Exec(ctx, `SELECT id FROM stock_lots WHERE item_id = $1 FOR UPDATE NOWAIT`, it.ID)
			return err
		}); err != nil {
			t.Fatalf("%s: the count holds a lot while it waits: %v", it.Name, err)
		}
		if err := held.Commit(ctx); err != nil {
			t.Fatal(err)
		}
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
}

func waitForWaiter(t *testing.T, pool *pgxpool.Pool, held pgx.Tx) {
	t.Helper()
	ctx := context.Background()
	var pid uint32
	if err := held.QueryRow(ctx, `SELECT pg_backend_pid()`).Scan(&pid); err != nil {
		t.Fatal(err)
	}
	for range 200 {
		var n int
		if err := pool.QueryRow(ctx, `SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))`,
			int(pid)).Scan(&n); err != nil {
			t.Fatal(err)
		}
		if n > 0 {
			return
		}
		pgSleep(t, pool)
	}
	t.Fatal("nothing waited on the holder")
}

func pgSleep(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	if _, err := pool.Exec(context.Background(), `SELECT pg_sleep(0.05)`); err != nil {
		t.Fatal(err)
	}
}

// TestSixPlacesRoundHalfAwayFromZero is _six and _amount: Postgres's round, and the
// numeric(20,6) bound.
func TestSixPlacesRoundHalfAwayFromZero(t *testing.T) {
	for in, want := range map[string]string{
		"-0.4999995": "-0.5", "0.4999995": "0.5", "0.49999949": "0.499999", "566.66666666": "566.666667", "-0": "0",
	} {
		r, _ := new(big.Rat).SetString(in)
		if got := text(six(r)); got != want {
			t.Errorf("six(%s) = %s, want %s", in, got, want)
		}
	}
	got, err := amount(rat("-1.5"), rat("0.333333"))
	if err != nil || text(got) != "-0.5" {
		t.Fatalf("%v %v", got, err)
	}
	var refused *Refused
	if _, err := amount(rat("100000000000000"), rat("0")); !errors.As(err, &refused) || refused.Code != "bad_quantity" {
		t.Fatalf("got %v, want bad_quantity", err)
	}
	if _, err := amount(rat("10000000"), rat("10000000")); !errors.As(err, &refused) || refused.Code != "bad_quantity" {
		t.Fatalf("got %v, want bad_quantity", err)
	}
}

func rat(s string) *big.Rat {
	r, _ := new(big.Rat).SetString(s)
	return r
}

// TestAReceiveWithNoShopCurrencyFails: an empty shop currency would make every named
// currency foreign and store the cost as unknown; it is a failure, not a refusal.
func TestAReceiveWithNoShopCurrencyFails(t *testing.T) {
	pool := testdb.New(t)
	item := stockItem(t, pool, "M3 bolt")
	_, err := inTx(t, pool, func(ctx context.Context, tx pgx.Tx) (wire.Lot, error) {
		if _, err := Lock(ctx, tx, Target{Item: item.ID}, true); err != nil {
			return wire.Lot{}, err
		}
		return Stock{}.ReceiveLot(ctx, tx, item, wire.ReceiveLot{Qty: "1", UnitCostMinor: ptr(wire.Decimal("5")),
			Currency: ptr("USD")}, "", Actor(browser))
	})
	var refused *Refused
	if err == nil || errors.As(err, &refused) {
		t.Fatalf("got %v, want a failure", err)
	}
	if n := lotCount(t, pool, item); n != 0 {
		t.Fatalf("%d lots", n)
	}
}
