package turns

import (
	"context"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

func TestNumbersAreWrittenAsJavaScriptWritesThem(t *testing.T) {
	for _, c := range []struct {
		value float64
		text  string
	}{
		{0, "0"}, {math.Copysign(0, -1), "0"}, {5, "5"}, {0.1, "0.1"},
		{0.30000000000000004, "0.30000000000000004"}, {1e-7, "1e-7"}, {1.5e-7, "1.5e-7"},
		{1.234e-8, "1.234e-8"}, {0.000001, "0.000001"}, {1e20, "100000000000000000000"},
		{123456789012345680000.0, "123456789012345680000"}, {1e21, "1e+21"},
		{1.5e300, "1.5e+300"}, {-2.5e-10, "-2.5e-10"}, {float64(1<<53 + 2), "9007199254740994"},
		{math.NaN(), "null"}, {math.Inf(1), "null"},
	} {
		if got := jsNumber(c.value); got != c.text {
			t.Errorf("%v: %s, want %s", c.value, got, c.text)
		}
		want := `{"type":"x","n":` + c.text + `,"l":[` + c.text + `]}`
		if got := dumps(newObject("type", "x", "n", c.value, "l", []any{c.value})); got != want {
			t.Errorf("%v: %s, want %s", c.value, got, want)
		}
	}
}

func TestAnEventReadsAndWritesAsPythonDoes(t *testing.T) {
	text := `{"type":"session.result","n":1.0,"big":123456789012345678901234567890,` +
		`"s":"é\u0001\n\"/<b>","type":"x","e":1e400,"l":[true,null,-0]}`
	event, err := parse(text)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"type":"x","n":1,"big":123456789012345678901234567890,` +
		`"s":"é\u0001\n\"/<b>","e":null,"l":[true,null,0]}`
	if got := dumps(event); got != want {
		t.Fatalf("got %s", got)
	}
}

func TestUSDRoundsTheExactValueATieAwayFromZero(t *testing.T) {
	for value, want := range map[float64]string{
		0.125: "$0.13", 2: "$2.00", 0.005: "$0.01", 0.015: "$0.01", 1234.5: "$1234.50",
		-0.125: "$-0.13", -0.001: "$-0.00", 0: "$0.00",
	} {
		if got := usd(value); got != want {
			t.Errorf("%v: %s, want %s", value, got, want)
		}
	}
}

func post(t *testing.T, handle crud.Handler, session, body string) (any, error) {
	t.Helper()
	r := httptest.NewRequest("POST", "/", strings.NewReader(body))
	return handle(r, crud.Params{"session_id": session})
}

func reason(err error) (int, any) {
	var p *httpx.Problem
	if !errors.As(err, &p) {
		return 0, err
	}
	for _, f := range p.Extensions {
		if f.Key == "reason" {
			return p.Status, f.Value
		}
	}
	return p.Status, nil
}

const session = "7d3e3c41-6a43-4c1c-9a5e-5f3f2b0c7a10"
const turn = "0b6a1f4e-2a0c-4d55-8f63-0f8f5a2a9c11"

func TestAWriteThatWaitsPastTheLockTimeoutIsRefused(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	if _, err := pool.Exec(ctx, `INSERT INTO ai_sessions (id, origin, owner_kind, owner_id,
		owner_label, creator_kind, creator_id, status, max_turns, budget_usd, durable_turn_id)
		VALUES ($1, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'running', 10, 1, $2)`,
		session, turn); err != nil {
		t.Fatal(err)
	}
	old := lockTimeout
	lockTimeout = 200 * time.Millisecond
	t.Cleanup(func() { lockTimeout = old })
	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `SELECT 1 FROM ai_sessions WHERE id = $1 FOR UPDATE`, session); err != nil {
		t.Fatal(err)
	}
	turns := &Turns{Pool: pool}
	tail := `["{\"type\":\"session.status\",\"status\":\"idle\"}"]`
	_, err = post(t, turns.finish, session, `{"turn_id":"`+turn+`","tail":`+tail+`}`)
	if status, why := reason(err); status != http.StatusServiceUnavailable || why != "row_busy" {
		t.Fatalf("%d %v", status, why)
	}
	_ = tx.Rollback(ctx)
	var status string
	var n int
	if err := pool.QueryRow(ctx, `SELECT status, (SELECT count(*) FROM ai_session_events)
		FROM ai_sessions`).Scan(&status, &n); err != nil {
		t.Fatal(err)
	}
	if status != "running" || n != 0 {
		t.Fatalf("%s %d", status, n)
	}
}

func TestACallThatWaitsPastThePoolsBoundIsRefused(t *testing.T) {
	pool := testdb.New(t)
	old := poolWait
	poolWait = 100 * time.Millisecond
	t.Cleanup(func() { poolWait = old })
	var held []interface{ Release() }
	for range pool.Config().MaxConns {
		c, err := pool.Acquire(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		held = append(held, c)
	}
	defer func() {
		for _, c := range held {
			c.Release()
		}
	}()
	_, err := post(t, (&Turns{Pool: pool}).interrupt, session, `{}`)
	if status, why := reason(err); status != http.StatusServiceUnavailable || why != "pool_busy" {
		t.Fatalf("%d %v", status, why)
	}
}
