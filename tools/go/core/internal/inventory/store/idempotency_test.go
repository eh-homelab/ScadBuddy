package store

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const browser = `{"kind":"browser","principal":null,"session":null,"run_id":null}`

// setup is a migrated database with a table a write can insert into.
func setup(t *testing.T) *pgxpool.Pool {
	t.Helper()
	pool := testdb.New(t)
	if _, err := pool.Exec(context.Background(), `CREATE TABLE store_test (n serial PRIMARY KEY, v text)`); err != nil {
		t.Fatal(err)
	}
	return pool
}

func req(key string, input string) WriteReq {
	return WriteReq{Caller: "inventory", Key: key, Route: "inventory.Test", Input: []byte(input), Actor: Actor(browser)}
}

// insert is a write that adds one row and answers 201 with its number.
func insert(ctx context.Context, runs *int) func(pgx.Tx) (int, any, error) {
	return func(tx pgx.Tx) (int, any, error) {
		*runs++
		var n int
		if err := tx.QueryRow(ctx, `INSERT INTO store_test (v) VALUES ('x') RETURNING n`).Scan(&n); err != nil {
			return 0, nil, err
		}
		return 201, map[string]any{"n": n, "word": "made"}, nil
	}
}

func count(t *testing.T, pool *pgxpool.Pool, sql string, args ...any) int {
	t.Helper()
	var n int
	if err := pool.QueryRow(context.Background(), sql, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestARetryReplaysTheFirstAnswer(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	runs := 0
	first, err := Write(ctx, pool, req("k", `{"a":1}`), insert(ctx, &runs))
	if err != nil {
		t.Fatal(err)
	}
	second, err := Write(ctx, pool, req("k", `{ "a" : 1 }`), insert(ctx, &runs))
	if err != nil {
		t.Fatal(err)
	}
	if runs != 1 {
		t.Fatalf("the write ran %d times", runs)
	}
	if first.Status != 201 || second.Status != 201 || !bytes.Equal(first.Body, second.Body) {
		t.Fatalf("first %d %s, second %d %s", first.Status, first.Body, second.Status, second.Body)
	}
	if n := count(t, pool, `SELECT count(*) FROM store_test`); n != 1 {
		t.Fatalf("%d rows", n)
	}
	if n := count(t, pool, `SELECT count(*) FROM crud_requests WHERE caller = 'inventory' AND key = 'k'`); n != 1 {
		t.Fatalf("%d crud_requests rows", n)
	}
}

func TestAKeyWithAnotherInputIsRefused(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	runs := 0
	if _, err := Write(ctx, pool, req("k", `{"a":1}`), insert(ctx, &runs)); err != nil {
		t.Fatal(err)
	}
	for _, other := range []WriteReq{req("k", `{"a":2}`), {Caller: "inventory", Key: "k", Route: "inventory.Other", Input: []byte(`{"a":1}`), Actor: Actor(browser)}} {
		answer, err := Write(ctx, pool, other, insert(ctx, &runs))
		if err != nil {
			t.Fatal(err)
		}
		var problem map[string]any
		if err := json.Unmarshal(answer.Body, &problem); err != nil {
			t.Fatal(err)
		}
		if answer.Status != 422 || problem["type"] != "https://scadbuddy.dev/problems/crud/idempotency-key-reused" || problem["status"] != float64(422) {
			t.Fatalf("answer %d %s", answer.Status, answer.Body)
		}
	}
	if runs != 1 {
		t.Fatalf("the write ran %d times", runs)
	}
	if n := count(t, pool, `SELECT count(*) FROM store_test`); n != 1 {
		t.Fatalf("%d rows", n)
	}
}

func TestTwoRequestsWithOneKeyWriteOnce(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	var mu sync.Mutex
	runs := 0
	do := func(tx pgx.Tx) (int, any, error) {
		mu.Lock()
		runs++
		mu.Unlock()
		time.Sleep(50 * time.Millisecond)
		var n int
		if err := tx.QueryRow(ctx, `INSERT INTO store_test (v) VALUES ('x') RETURNING n`).Scan(&n); err != nil {
			return 0, nil, err
		}
		return 201, map[string]any{"n": n}, nil
	}
	answers := make([]Answer, 2)
	errs := make([]error, 2)
	var wg sync.WaitGroup
	for i := range 2 {
		wg.Go(func() { answers[i], errs[i] = Write(ctx, pool, req("k", `{"a":1}`), do) })
	}
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if runs != 1 || !bytes.Equal(answers[0].Body, answers[1].Body) || answers[0].Status != answers[1].Status {
		t.Fatalf("runs %d, answers %d %s and %d %s", runs, answers[0].Status, answers[0].Body, answers[1].Status, answers[1].Body)
	}
	if n := count(t, pool, `SELECT count(*) FROM store_test`); n != 1 {
		t.Fatalf("%d rows", n)
	}
}

func TestARefusedErrorIsRecorded(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	runs := 0
	do := func(tx pgx.Tx) (int, any, error) {
		runs++
		// What it wrote before it refused does not commit.
		if _, err := tx.Exec(ctx, `INSERT INTO store_test (v) VALUES ('partial')`); err != nil {
			return 0, nil, err
		}
		return 0, nil, &Refused{Status: 422, Code: "steps_invalid", Detail: "the steps do not resolve",
			Problems: []map[string]string{{"step": "count", "reason": "not_applicable"}}}
	}
	first, err := Write(ctx, pool, req("k", `{"a":1}`), do)
	if err != nil {
		t.Fatal(err)
	}
	var problem map[string]any
	if err := json.Unmarshal(first.Body, &problem); err != nil {
		t.Fatal(err)
	}
	if first.Status != 422 || problem["code"] != "steps_invalid" ||
		problem["type"] != "https://scadbuddy.dev/problems/inventory/steps_invalid" ||
		problem["title"] != "Unprocessable Content" || problem["detail"] != "the steps do not resolve" ||
		problem["problems"] == nil {
		t.Fatalf("answer %d %s", first.Status, first.Body)
	}
	if n := count(t, pool, `SELECT count(*) FROM crud_requests WHERE caller = 'inventory'`); n != 1 {
		t.Fatalf("%d crud_requests rows", n)
	}
	if n := count(t, pool, `SELECT count(*) FROM store_test`); n != 0 {
		t.Fatalf("a refused write left %d rows", n)
	}
	second, err := Write(ctx, pool, req("k", `{"a":1}`), do)
	if err != nil {
		t.Fatal(err)
	}
	if runs != 1 || second.Status != 422 || !bytes.Equal(first.Body, second.Body) {
		t.Fatalf("runs %d, second %d %s", runs, second.Status, second.Body)
	}
}

func TestARefusalIsRecorded(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	runs := 0
	do := func(pgx.Tx) (int, any, error) {
		runs++
		return 409, map[string]any{"code": "lot_empty"}, nil
	}
	first, err := Write(ctx, pool, req("k", `{}`), do)
	if err != nil {
		t.Fatal(err)
	}
	second, err := Write(ctx, pool, req("k", `{}`), do)
	if err != nil {
		t.Fatal(err)
	}
	if runs != 1 || first.Status != 409 || second.Status != 409 || !bytes.Equal(first.Body, second.Body) {
		t.Fatalf("runs %d, first %d %s, second %d %s", runs, first.Status, first.Body, second.Status, second.Body)
	}
	if n := count(t, pool, `SELECT count(*) FROM crud_requests WHERE caller = 'inventory'`); n != 1 {
		t.Fatalf("%d crud_requests rows", n)
	}
}

func TestAFailedWriteRecordsNothing(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	runs := 0
	boom := errors.New("boom")
	do := func(tx pgx.Tx) (int, any, error) {
		runs++
		if _, err := tx.Exec(ctx, `INSERT INTO store_test (v) VALUES ('x')`); err != nil {
			return 0, nil, err
		}
		return 0, nil, boom
	}
	if _, err := Write(ctx, pool, req("k", `{}`), do); !errors.Is(err, boom) {
		t.Fatalf("err = %v", err)
	}
	if n := count(t, pool, `SELECT count(*) FROM crud_requests`); n != 0 {
		t.Fatalf("%d crud_requests rows", n)
	}
	if n := count(t, pool, `SELECT count(*) FROM store_test`); n != 0 {
		t.Fatalf("a failed write left %d rows", n)
	}
	if _, err := Write(ctx, pool, req("k", `{}`), do); !errors.Is(err, boom) {
		t.Fatalf("err = %v", err)
	}
	if runs != 2 {
		t.Fatalf("the write ran %d times", runs)
	}
}

func TestTheActorIsSetInTheTransaction(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	actor := `{"kind":"agent","principal":"assistant","session":"s1","run_id":null}`
	var got string
	w := req("k", `{}`)
	w.Actor = Actor(actor)
	_, err := Write(ctx, pool, w, func(tx pgx.Tx) (int, any, error) {
		err := tx.QueryRow(ctx, `SELECT current_setting('scadbuddy.actor')`).Scan(&got)
		return 200, map[string]any{}, err
	})
	if err != nil {
		t.Fatal(err)
	}
	if got != actor {
		t.Fatalf("scadbuddy.actor = %q, want %q", got, actor)
	}
	// The setting is the transaction's own.
	var after *string
	if err := pool.QueryRow(ctx, `SELECT nullif(current_setting('scadbuddy.actor', true), '')`).Scan(&after); err != nil {
		t.Fatal(err)
	}
	if after != nil {
		t.Fatalf("the actor outlived its transaction: %q", *after)
	}
}

func TestABadActorWritesNothing(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	runs := 0
	w := req("k", `{}`)
	w.Actor = Actor(`{"kind":"browser","extra":1}`)
	if _, err := Write(ctx, pool, w, insert(ctx, &runs)); err == nil {
		t.Fatal("a bad actor was taken")
	}
	if runs != 0 {
		t.Fatalf("the write ran %d times", runs)
	}
}

// advisory lists the advisory locks that the session pid holds and waits for: the event log, or a key.
func advisory(t *testing.T, pool *pgxpool.Pool, pid int32) (granted, waiting []string) {
	t.Helper()
	rows, err := pool.Query(context.Background(),
		`SELECT classid::bigint, objid::bigint, objsubid, granted FROM pg_locks
		  WHERE locktype = 'advisory' AND pid = $1 ORDER BY 1, 2`, pid)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	for rows.Next() {
		var class, obj int64
		var sub int16
		var ok bool
		if err := rows.Scan(&class, &obj, &sub, &ok); err != nil {
			t.Fatal(err)
		}
		name := "key"
		if sub == 1 && class == 0x53434144 && obj == 0x45564E54 {
			name = "event_log"
		}
		if ok {
			granted = append(granted, name)
		} else {
			waiting = append(waiting, name)
		}
	}
	return granted, waiting
}

// TestTheEventLogLockIsTakenFirst checks the lock order (core/pg_events.py "Lock order"): a
// write waits for the event log before it takes its key's lock or runs, as set_actor does.
func TestTheEventLogLockIsTakenFirst(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	holder, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = holder.Rollback(ctx) }()
	if _, err := holder.Exec(ctx, `SELECT pg_advisory_xact_lock(5999710890064498260)`); err != nil {
		t.Fatal(err)
	}
	pids := make(chan int32, 1)
	done := make(chan error, 1)
	runs := 0
	go func() {
		_, err := Write(ctx, pool, req("k", `{}`), func(tx pgx.Tx) (int, any, error) {
			runs++
			granted, _ := advisory(t, pool, func() int32 {
				var pid int32
				_ = tx.QueryRow(ctx, `SELECT pg_backend_pid()`).Scan(&pid)
				return pid
			}())
			if !slices.Contains(granted, "event_log") || !slices.Contains(granted, "key") {
				return 0, nil, errors.New("the write runs without both locks: " + strings.Join(granted, ","))
			}
			return 200, map[string]any{}, nil
		})
		done <- err
	}()
	// Find the writer: the session that waits for the event log.
	deadline := time.Now().Add(10 * time.Second)
	for {
		var pid int32
		err := pool.QueryRow(ctx, `SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND NOT granted LIMIT 1`).Scan(&pid)
		if err == nil {
			pids <- pid
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the write never waited for the event log")
		}
		time.Sleep(10 * time.Millisecond)
	}
	pid := <-pids
	granted, waiting := advisory(t, pool, pid)
	if len(granted) != 0 || !reflect.DeepEqual(waiting, []string{"event_log"}) {
		t.Fatalf("while it waits for the event log, the write holds %v and waits for %v", granted, waiting)
	}
	if runs != 0 {
		t.Fatal("the write ran before it held the event log")
	}
	if err := holder.Rollback(ctx); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestEnsureEntityMakesOneRow(t *testing.T) {
	pool := setup(t)
	ctx := context.Background()
	var ids []string
	for range 2 {
		err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
			id, err := EnsureEntity(ctx, tx, "printer", "7")
			ids = append(ids, id)
			return err
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	if ids[0] == "" || ids[0] != ids[1] || !regexp.MustCompile(`^[0-9a-f-]{36}$`).MatchString(ids[0]) {
		t.Fatalf("ids %v", ids)
	}
}

// actorFacts is testdata/actors.json, which backend/tests/crud/test_shared_sql.py also
// reads: the fields and kinds of Python's Actor (orders/models.py), and actors as
// ReadActor writes them, each of which Python's strict model reads and writes back unchanged.
type actorFacts struct {
	Fields   []string          `json:"fields"`
	Kinds    []string          `json:"kinds"`
	Accepted map[string]string `json:"accepted"`
	Refused  []string          `json:"refused"`
}

func TestTheActorIsPythonsActor(t *testing.T) {
	text, err := os.ReadFile("testdata/actors.json")
	if err != nil {
		t.Fatal(err)
	}
	var facts actorFacts
	if err := json.Unmarshal(text, &facts); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(facts.Fields, actorFields) || !reflect.DeepEqual(facts.Kinds, actorKinds) {
		t.Fatalf("Go's actor has fields %v and kinds %v; Python's has %v and %v", actorFields, actorKinds, facts.Fields, facts.Kinds)
	}
	for in, want := range facts.Accepted {
		got, err := ReadActor([]byte(in))
		if err != nil || string(got) != want {
			t.Errorf("ReadActor(%s) = %s, %v; want %s", in, got, err, want)
		}
	}
	for _, in := range facts.Refused {
		if got, err := ReadActor([]byte(in)); err == nil {
			t.Errorf("ReadActor(%s) = %s, want an error", in, got)
		}
	}
}

// shared is a statement of backend/scadbuddy/<path>, as Go runs it: psycopg's %s
// placeholders numbered, and its white space normalized.
func shared(t *testing.T, path string) string {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	text, err := os.ReadFile(filepath.Join(filepath.Dir(file), "../../../../../../backend/scadbuddy", path))
	if err != nil {
		t.Fatal(err)
	}
	return normalize(numbered(string(text)))
}

func normalize(s string) string { return strings.Join(strings.Fields(s), " ") }

// numbered is text with each psycopg %s placeholder written as Postgres's $1, $2, ….
func numbered(text string) string {
	n := 0
	return regexp.MustCompile(`%s`).ReplaceAllStringFunc(text, func(string) string {
		n++
		return "$" + strconv.Itoa(n)
	})
}

func TestSharedStatementsMatch(t *testing.T) {
	for path, ours := range map[string]string{
		"entities/shared/ensure_entity.sql":    ensureEntitySQL,
		"crud/shared/crud_requests_select.sql": requestSelectSQL,
		"crud/shared/crud_requests_insert.sql": requestInsertSQL,
	} {
		if got := normalize(ours); got != shared(t, path) {
			t.Errorf("%s:\n go:   %s\n file: %s", path, got, shared(t, path))
		}
	}
}
