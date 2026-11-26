package bus

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// update rewrites testdata/history from what the tests publish (go test -run History -update).
var update = flag.Bool("update", false, "rewrite testdata/history")

func TestMain(m *testing.M) { testdb.Main(m) }

const actor = `{"kind":"browser"}`

func count(t *testing.T, pool *pgxpool.Pool, kind string) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(context.Background(), `SELECT count(*) FROM events WHERE kind = $1`, kind).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestPublishInRollsBackWithTheWrite(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	fields := []httpx.Field{
		{Key: "owner", Value: "item:00000000-0000-0000-0000-000000000001"},
		{Key: "step", Value: "life"},
		{Key: "handler", Value: "life.hours"},
		{Key: "old", Value: map[string]any{"hours": 1500}},
		{Key: "new", Value: map[string]any{"hours": 2000}},
		{Key: "actor", Value: json.RawMessage(actor)},
		{Key: "retention_seconds", Value: 0},
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := PublishIn(ctx, tx, "handler_config.changed", fields...); err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback(ctx); err != nil {
		t.Fatal(err)
	}
	if n := count(t, pool, "handler_config.changed"); n != 0 {
		t.Fatalf("a rolled-back publish left %d events", n)
	}
	var seq int64
	err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		seq, err = PublishIn(ctx, tx, "handler_config.changed", fields...)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if n := count(t, pool, "handler_config.changed"); n != 1 {
		t.Fatalf("a committed publish left %d events, want 1", n)
	}
	golden(t, pool, "handler_config.changed", seq)

	err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		seq, err = PublishIn(ctx, tx, "handler_value.changed",
			httpx.Field{Key: "entity_id", Value: "00000000-0000-0000-0000-000000000002"},
			httpx.Field{Key: "step", Value: "life"},
			httpx.Field{Key: "handler", Value: "life.hours"},
			httpx.Field{Key: "reason", Value: "use"},
			httpx.Field{Key: "old", Value: map[string]any{"used": 600}},
			httpx.Field{Key: "new", Value: map[string]any{"used": 1350}},
			httpx.Field{Key: "actor", Value: json.RawMessage(actor)},
			httpx.Field{Key: "retention_seconds", Value: 0},
		)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	golden(t, pool, "handler_value.changed", seq)
}

func TestPublishInRefusesAKindPythonCannotDecode(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		_, err := PublishIn(ctx, tx, "Bad")
		return err
	})
	if err == nil {
		t.Fatal("PublishIn took a kind Python cannot decode")
	}
}

// TestTheTriggersPublishTheHistoryKinds writes the two kinds the database's triggers publish.
func TestTheTriggersPublishTheHistoryKinds(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT set_config('scadbuddy.actor', $1, true)`, actor); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO entities (id, kind, ref) VALUES
			('00000000-0000-0000-0000-000000000003', 'item', '00000000-0000-0000-0000-000000000004')`); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO entity_properties (entity_id, key, value, updated_by)
			VALUES ('00000000-0000-0000-0000-000000000003', 'colour_name', '"Gold"', $1)`, actor); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, `UPDATE entity_property_schemas
			SET schema = '{"type": "object", "properties": {}}' WHERE kind = 'spool'`)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, kind := range []string{"entity_property.changed", "entity_property_schema.changed"} {
		var seq int64
		if err := pool.QueryRow(ctx, `SELECT max(seq) FROM events WHERE kind = $1`, kind).Scan(&seq); err != nil {
			t.Fatal(err)
		}
		golden(t, pool, kind, seq)
	}
}

// golden checks the logged payload of seq against testdata/history/<kind>.json, apart from
// its id and at, or rewrites the file with -update. backend/tests/test_pg_event_bus.py
// decodes these files.
func golden(t *testing.T, pool *pgxpool.Pool, kind string, seq int64) {
	t.Helper()
	var payload []byte
	if err := pool.QueryRow(context.Background(), `SELECT payload FROM events WHERE seq = $1`, seq).Scan(&payload); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join("testdata", "history", kind+".json")
	if *update {
		var out bytes.Buffer
		if err := json.Indent(&out, payload, "", "  "); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(out.Bytes(), '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (run go test -update to write it)", err)
	}
	got, wanted := withoutIDAndAt(t, payload), withoutIDAndAt(t, want)
	if !bytes.Equal(got, wanted) {
		t.Errorf("%s: payload\n%s\nwant (%s)\n%s", kind, got, path, wanted)
	}
}

func withoutIDAndAt(t *testing.T, payload []byte) []byte {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(payload, &m); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"id", "at"} {
		if _, ok := m[k]; !ok {
			t.Errorf("the payload has no %s", k)
		}
		delete(m, k)
	}
	out, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return out
}
