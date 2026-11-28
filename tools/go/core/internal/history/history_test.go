package history

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const person = `{"kind":"browser"}`

var ctx = context.Background()

// entity makes an entities row of kind and returns its id.
func entity(t *testing.T, pool *pgxpool.Pool, kind, ref string) string {
	t.Helper()
	var id string
	if err := pool.QueryRow(ctx, `INSERT INTO entities (kind, ref) VALUES ($1, $2) RETURNING id::text`,
		kind, ref).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

// set writes key = value (JSON) on the entity as actor, in a transaction of its own.
func set(t *testing.T, pool *pgxpool.Pool, entityID, key, value, actor string) {
	t.Helper()
	write(t, pool, actor, `INSERT INTO entity_properties (entity_id, key, value, updated_by)
		VALUES ($1, $2, $3::jsonb, $4::jsonb) ON CONFLICT (entity_id, key) DO UPDATE
		SET value = EXCLUDED.value, updated_at = clock_timestamp(), updated_by = EXCLUDED.updated_by`,
		entityID, key, value, actor)
}

func remove(t *testing.T, pool *pgxpool.Pool, entityID, key, actor string) {
	t.Helper()
	write(t, pool, actor, `DELETE FROM entity_properties WHERE entity_id = $1 AND key = $2`, entityID, key)
}

func write(t *testing.T, pool *pgxpool.Pool, actor, sql string, args ...any) {
	t.Helper()
	err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT set_config('scadbuddy.actor', $1, true)`, actor); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, sql, args...)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
}

func scalar[T any](t *testing.T, pool *pgxpool.Pool, sql string, args ...any) T {
	t.Helper()
	var v T
	if err := pool.QueryRow(ctx, sql, args...).Scan(&v); err != nil {
		t.Fatal(err)
	}
	return v
}

func cursor(t *testing.T, pool *pgxpool.Pool) int64 {
	t.Helper()
	return scalar[int64](t, pool, `SELECT seq FROM event_subscribers WHERE name = 'history'`)
}

func newest(t *testing.T, pool *pgxpool.Pool) int64 {
	t.Helper()
	return scalar[int64](t, pool, `SELECT max(seq) FROM events`)
}

func rows(t *testing.T, pool *pgxpool.Pool) int {
	t.Helper()
	return scalar[int](t, pool, `SELECT count(*) FROM entity_property_history`)
}

func apply(t *testing.T, pool *pgxpool.Pool) int {
	t.Helper()
	n, err := Apply(ctx, pool, Options{})
	if err != nil {
		t.Fatal(err)
	}
	return n
}

// matching counts the history rows that equal their event's payload, field by field.
func matching(t *testing.T, pool *pgxpool.Pool) int {
	t.Helper()
	return scalar[int](t, pool, `SELECT count(*) FROM entity_property_history h
		JOIN events e ON e.seq = h.event_seq AND e.kind = 'entity_property.changed'
		WHERE h.entity_id = (e.payload->>'entity_id')::uuid AND h.kind = e.payload->>'entity_kind'
		AND h.ref = e.payload->>'ref' AND h.key = e.payload->>'key' AND h.op = e.payload->>'op'
		AND h.old IS NOT DISTINCT FROM nullif(e.payload->'old', 'null')
		AND h.new IS NOT DISTINCT FROM nullif(e.payload->'new', 'null')
		AND h.source IS NOT DISTINCT FROM e.payload->>'source'
		AND h.actor = e.payload->'actor' AND h.at = (e.payload->>'at')::timestamptz`)
}

func fourChanges(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	id := entity(t, pool, "item", "i1")
	set(t, pool, id, "colour_name", `"Gold"`, person)
	set(t, pool, id, "colour_name", `"Silk Gold"`, person)
	set(t, pool, id, "label_g", `1000`, person)
	remove(t, pool, id, "colour_name", person)
}

func TestTheSubscriberInsertsOneRowPerEvent(t *testing.T) {
	pool := testdb.New(t)
	fourChanges(t, pool)
	apply(t, pool)
	if n := rows(t, pool); n != 4 {
		t.Fatalf("%d rows, want 4", n)
	}
	if n := matching(t, pool); n != 4 {
		t.Fatalf("%d rows equal their event, want 4", n)
	}
	ops := scalar[[]string](t, pool, `SELECT array_agg(key || ' ' || op ORDER BY event_seq) FROM entity_property_history`)
	if want := []string{"colour_name set", "colour_name set", "label_g set", "colour_name remove"}; !reflect.DeepEqual(ops, want) {
		t.Fatalf("rows %v, want %v", ops, want)
	}
	if c, n := cursor(t, pool), newest(t, pool); c != n {
		t.Fatalf("cursor %d, want the last seq %d", c, n)
	}
}

func TestAReplayInsertsNothingTwice(t *testing.T) {
	pool := testdb.New(t)
	fourChanges(t, pool)
	apply(t, pool)
	if _, err := pool.Exec(ctx, `UPDATE event_subscribers SET seq = 0 WHERE name = 'history'`); err != nil {
		t.Fatal(err)
	}
	apply(t, pool)
	if n := rows(t, pool); n != 4 {
		t.Fatalf("%d rows after a replay, want 4", n)
	}
	if c, n := cursor(t, pool), newest(t, pool); c != n {
		t.Fatalf("cursor %d, want the last seq %d", c, n)
	}
}

func TestTwoSubscribersApplyOnce(t *testing.T) {
	pool := testdb.New(t)
	fourChanges(t, pool)
	var wg sync.WaitGroup
	applied := make([]int, 2)
	errs := make([]error, 2)
	for i := range 2 {
		wg.Go(func() { applied[i], errs[i] = Apply(ctx, pool, Options{}) })
	}
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if applied[0] != 0 && applied[1] != 0 {
		t.Fatalf("both applied: %v", applied)
	}
	if n := rows(t, pool); n != 4 {
		t.Fatalf("%d rows, want 4", n)
	}
}

func TestAHeldCursorAppliesNothing(t *testing.T) {
	pool := testdb.New(t)
	fourChanges(t, pool)
	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `SELECT seq FROM event_subscribers WHERE name = 'history' FOR UPDATE`); err != nil {
		t.Fatal(err)
	}
	if n := apply(t, pool); n != 0 {
		t.Fatalf("applied %d while another replica held the cursor", n)
	}
	if n := rows(t, pool); n != 0 {
		t.Fatalf("%d rows, want 0", n)
	}
}

func TestAnUnknownSubscriberIsAnError(t *testing.T) {
	pool := testdb.New(t)
	if _, err := Apply(ctx, pool, Options{Name: "nobody"}); err == nil {
		t.Fatal("Apply took a subscriber with no row")
	}
}

func TestOtherKindsMoveTheCursor(t *testing.T) {
	pool := testdb.New(t)
	id := entity(t, pool, "item", "i1")
	set(t, pool, id, "a", `1`, person)
	if _, err := pool.Exec(ctx, `INSERT INTO events (event_id, kind, at, payload)
		VALUES ('j1', 'job.progress', now(), '{"id": "j1", "kind": "job.progress"}')`); err != nil {
		t.Fatal(err)
	}
	set(t, pool, id, "b", `2`, person)
	apply(t, pool)
	if n := rows(t, pool); n != 2 {
		t.Fatalf("%d rows, want 2", n)
	}
	if c, n := cursor(t, pool), newest(t, pool); c != n {
		t.Fatalf("cursor %d, want the last seq %d", c, n)
	}
}

func TestABadPayloadStopsTheCursor(t *testing.T) {
	pool := testdb.New(t)
	id := entity(t, pool, "item", "i1")
	set(t, pool, id, "a", `1`, person)
	good := newest(t, pool)
	var bad int64
	if err := pool.QueryRow(ctx, `INSERT INTO events (event_id, kind, at, payload)
		SELECT 'bad', kind, at, payload - 'key' || '{"id": "bad"}' FROM events WHERE seq = $1
		RETURNING seq`, good).Scan(&bad); err != nil {
		t.Fatal(err)
	}
	set(t, pool, id, "b", `2`, person)
	_, err := Apply(ctx, pool, Options{})
	if err == nil || !strings.Contains(err.Error(), fmt.Sprint(bad)) {
		t.Fatalf("Apply = %v, want an error naming seq %d", err, bad)
	}
	if n := rows(t, pool); n != 1 {
		t.Fatalf("%d rows, want the 1 before the bad event", n)
	}
	if c := cursor(t, pool); c != bad-1 {
		t.Fatalf("cursor %d, want %d, just before the bad event", c, bad-1)
	}
	// #8074: the subscriber's row names the event it cannot pass, for the API's scrape.
	if s := stoppedAt(t, pool); s == nil || *s != bad {
		t.Fatalf("stopped_seq %v, want %d", s, bad)
	}
	// Once the payload is fixed, the event is applied, and the ones after it.
	if _, err := pool.Exec(ctx, `UPDATE events SET payload = payload || '{"key": "c"}' WHERE seq = $1`, bad); err != nil {
		t.Fatal(err)
	}
	apply(t, pool)
	if n := rows(t, pool); n != 3 {
		t.Fatalf("%d rows after the fix, want 3", n)
	}
	if s := stoppedAt(t, pool); s != nil {
		t.Fatalf("stopped_seq %d after the fix, want NULL", *s)
	}
}

func stoppedAt(t *testing.T, pool *pgxpool.Pool) *int64 {
	t.Helper()
	return scalar[*int64](t, pool, `SELECT stopped_seq FROM event_subscribers WHERE name = 'history'`)
}

func TestAnErrorBeforeTheMigrationsIsUnmigrated(t *testing.T) {
	for _, err := range []error{
		&noSubscriber{name: "history"},
		fmt.Errorf("read: %w", &pgconn.PgError{Code: "42P01"}),
		&pgconn.PgError{Code: "42703"},
	} {
		if !unmigrated(err) {
			t.Errorf("unmigrated(%v) = false, want true", err)
		}
	}
	for _, err := range []error{&badPayload{seq: 1}, &pgconn.PgError{Code: "57014"}} {
		if unmigrated(err) {
			t.Errorf("unmigrated(%v) = true, want false", err)
		}
	}
}

// counting counts the records at each level.
type counting struct {
	mu     sync.Mutex
	levels map[slog.Level]int
}

func (c *counting) Enabled(context.Context, slog.Level) bool { return true }
func (c *counting) Handle(_ context.Context, r slog.Record) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.levels[r.Level]++
	return nil
}
func (c *counting) WithAttrs([]slog.Attr) slog.Handler { return c }
func (c *counting) WithGroup(string) slog.Handler { return c }

func (c *counting) count(l slog.Level) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.levels[l]
}

// TestRunBeforeTheMigrationsWarnsOnce: a subscriber with no row yet (the API has not
// migrated) is one warning, not an error every Poll (#8116).
func TestRunBeforeTheMigrationsWarnsOnce(t *testing.T) {
	pool := testdb.New(t)
	logs := &counting{levels: map[slog.Level]int{}}
	previous := slog.Default()
	slog.SetDefault(slog.New(logs))
	t.Cleanup(func() { slog.SetDefault(previous) })
	run, stop := context.WithTimeout(ctx, 300*time.Millisecond)
	defer stop()
	Run(run, pool, Options{Name: "nobody", Poll: 10 * time.Millisecond})
	if w, e := logs.count(slog.LevelWarn), logs.count(slog.LevelError); w != 1 || e != 0 {
		t.Fatalf("%d warnings and %d errors, want 1 and 0", w, e)
	}
}

func TestTheHistoryReplaysToTheCurrentProperties(t *testing.T) {
	pool := testdb.New(t)
	ids := []string{entity(t, pool, "item", "i1"), entity(t, pool, "spool", "s1"), entity(t, pool, "order", "o1")}
	keys := []string{"a", "b", "c"}
	r := rand.New(rand.NewPCG(6949, 1))
	for range 20 {
		id, key := ids[r.IntN(len(ids))], keys[r.IntN(len(keys))]
		if r.IntN(3) == 0 {
			remove(t, pool, id, key, person)
			continue
		}
		set(t, pool, id, key, fmt.Sprint(r.IntN(4)), person)
	}
	apply(t, pool)
	for _, id := range ids {
		replayed := map[string]any{}
		history, err := pool.Query(ctx, `SELECT key, op, new FROM entity_property_history
			WHERE entity_id = $1 ORDER BY event_seq`, id)
		if err != nil {
			t.Fatal(err)
		}
		for history.Next() {
			var key, op string
			var value any
			if err := history.Scan(&key, &op, &value); err != nil {
				t.Fatal(err)
			}
			if op == "remove" {
				delete(replayed, key)
				continue
			}
			replayed[key] = value
		}
		if err := history.Err(); err != nil {
			t.Fatal(err)
		}
		current := scalar[map[string]any](t, pool, `SELECT coalesce(jsonb_object_agg(key, value), '{}')
			FROM entity_properties WHERE entity_id = $1`, id)
		if !reflect.DeepEqual(normal(t, replayed), normal(t, current)) {
			t.Errorf("%s: history replays to %v, entity_properties holds %v", id, replayed, current)
		}
	}
}

// normal is v through JSON, so numbers compare alike whatever decoded them.
func normal(t *testing.T, v any) any {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestSchemaEventsAreApplied(t *testing.T) {
	pool := testdb.New(t)
	write(t, pool, person, `UPDATE entity_property_schemas SET schema = '{"type": "object"}' WHERE kind = 'spool'`)
	write(t, pool, person, `UPDATE item_kinds SET attribute_schema = '{"type": "object"}' WHERE id = 'nozzle'`)
	apply(t, pool)
	events := scalar[int](t, pool, `SELECT count(*) FROM events WHERE kind = 'entity_property_schema.changed'`)
	if n := scalar[int](t, pool, `SELECT count(*) FROM entity_property_schema_history h
		JOIN events e ON e.seq = h.event_seq
		WHERE h.owner = e.payload->>'owner' AND h.new = e.payload->'new'
		AND h.old IS NOT DISTINCT FROM nullif(e.payload->'old', 'null') AND h.actor = e.payload->'actor'
		AND h.at = coalesce(e.payload->>'changed_at', e.payload->>'at')::timestamptz`); n != events || n < 2 {
		t.Fatalf("%d schema rows equal their event, of %d events", n, events)
	}
	owners := scalar[[]string](t, pool, `SELECT array_agg(owner ORDER BY event_seq DESC) FROM
		(SELECT owner, event_seq FROM entity_property_schema_history ORDER BY event_seq DESC LIMIT 2) last`)
	if want := []string{"item_kind:nozzle", "kind:spool"}; !reflect.DeepEqual(owners, want) {
		t.Fatalf("the last two owners %v, want %v", owners, want)
	}
}

// TestTheBackFillApplies applies the real back-fill: the migrations' schema events (with
// changed_at, item_kind: owners and the migration's actor), on a log that keeps them.
func TestTheBackFillApplies(t *testing.T) {
	pool, _ := testdb.Migrated(t, "")
	events := scalar[int](t, pool, `SELECT count(*) FROM events WHERE kind = 'entity_property_schema.changed'
		AND payload ? 'changed_at'`)
	itemKinds := scalar[int](t, pool, `SELECT count(*) FROM item_kinds`)
	if events < itemKinds+2 {
		t.Fatalf("%d back-filled schema events, want one per item kind (%d) and per kind schema", events, itemKinds)
	}
	for {
		n, err := Apply(ctx, pool, Options{Batch: 7})
		if err != nil {
			t.Fatal(err)
		}
		if n < 7 {
			break
		}
	}
	if c, n := cursor(t, pool), newest(t, pool); c != n {
		t.Fatalf("cursor %d, want the log's end %d", c, n)
	}
	if n := scalar[int](t, pool, `SELECT count(*) FROM entity_property_schema_history h JOIN events e
		ON e.seq = h.event_seq WHERE h.at = (e.payload->>'changed_at')::timestamptz`); n != events {
		t.Fatalf("%d schema rows at their changed_at, want %d", n, events)
	}
	if n := scalar[int](t, pool, `SELECT count(*) FROM entity_property_schema_history
		WHERE owner LIKE 'item_kind:%' AND actor = '{"kind": "provider", "principal": "migration"}'`); n != itemKinds {
		t.Fatalf("%d item kind rows by the migration, want %d", n, itemKinds)
	}
}

// applyAll applies until the log's end.
func applyAll(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	for {
		n, err := Apply(ctx, pool, Options{Batch: 7})
		if err != nil {
			t.Fatal(err)
		}
		if n < 7 {
			return
		}
	}
}

// TestTheBackFillOfOldRowsApplies seeds rows an older build wrote, then runs the history
// migration and applies its back-fill: one set per property, a non-object updated_by
// credited to the migration, and no row for a JSON null schema.
func TestTheBackFillOfOldRowsApplies(t *testing.T) {
	pool, rest := testdb.Migrated(t, "20261126T0308Z_property_history_events")
	if n := scalar[int](t, pool, `SELECT count(*) FROM information_schema.tables WHERE table_name = 'event_subscribers'`); n != 0 {
		t.Fatal("the history migration ran before the seed")
	}
	if _, err := pool.Exec(ctx, `
		INSERT INTO entities (id, kind, ref) VALUES
			('00000000-0000-0000-0000-0000000000a1', 'item', 'r1'),
			('00000000-0000-0000-0000-0000000000a2', 'spool', 's1');
		INSERT INTO entity_properties (entity_id, key, value, source, updated_at, updated_by) VALUES
			('00000000-0000-0000-0000-0000000000a1', 'a', '"x"', 'manual', '2026-11-01T00:00:00Z', '{"kind": "browser"}'),
			('00000000-0000-0000-0000-0000000000a1', 'b', '2', 'import', '2026-11-02T00:00:00Z', '"legacy"'),
			('00000000-0000-0000-0000-0000000000a2', 'c', 'true', 'manual', '2026-11-03T00:00:00Z', '{"kind": "agent", "principal": "p"}');
		INSERT INTO entity_property_schemas (kind, schema) VALUES ('nullkind', 'null');
		INSERT INTO item_kinds (id, name, tracking, attribute_schema) VALUES ('nullkind', 'Null', 'lot', 'null');
	`); err != nil {
		t.Fatal(err)
	}
	rest()
	applyAll(t, pool)
	if c, n := cursor(t, pool), newest(t, pool); c != n {
		t.Fatalf("cursor %d, want the log's end %d", c, n)
	}
	got := scalar[[]string](t, pool, `SELECT array_agg(h.ref || ' ' || h.key || ' ' || h.op || ' ' || h.new::text
		|| ' ' || h.source || ' ' || h.actor::text ORDER BY h.event_seq)
		FROM entity_property_history h JOIN entity_properties p ON p.entity_id = h.entity_id AND p.key = h.key
		WHERE h.old IS NULL AND h.at = p.updated_at`)
	want := []string{
		`r1 a set "x" manual {"kind": "browser"}`,
		`r1 b set 2 import {"kind": "provider", "principal": "migration"}`,
		`s1 c set true manual {"kind": "agent", "principal": "p"}`,
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("history rows\n%v\nwant\n%v", got, want)
	}
	if n := rows(t, pool); n != 3 {
		t.Fatalf("%d property rows, want 3", n)
	}
	if n := scalar[int](t, pool, `SELECT count(*) FROM entity_property_schema_history
		WHERE owner IN ('kind:nullkind', 'item_kind:nullkind')`); n != 0 {
		t.Fatalf("%d rows for a JSON null schema, want 0", n)
	}
	events := scalar[int](t, pool, `SELECT count(*) FROM events WHERE kind = 'entity_property_schema.changed'`)
	if n := scalar[int](t, pool, `SELECT count(*) FROM entity_property_schema_history`); n != events || n == 0 {
		t.Fatalf("%d schema rows, want one per schema event (%d)", n, events)
	}
}
