package notify

import (
	"net/http"
	"strconv"
	"testing"
	"time"
)

func TestTheWaitingGrantsAreListedByID(t *testing.T) {
	a := held(t)
	a.sql(`INSERT INTO workflow_definitions (id, name, version, created_by) VALUES ('d1', 'f', 1, '{}')`)
	for _, run := range []struct{ id, status string }{{"r1", "running"}, {"r2", "succeeded"}, {"r3", "running"}} {
		a.sql(`INSERT INTO workflow_runs (id, definition_id, version, name, status, workflow_id,
			workflow_run_id, started_by) VALUES ($1, 'd1', 1, 'f', $2, $1, $1, '{}')`, run.id, run.status)
	}
	grant := func(id, run, status, age string) {
		a.sql(`INSERT INTO flow_grants (id, run_id, status, spec, requested_by, created_at)
			VALUES ($1, $2, $3, '{}', '{}', now() - $4::interval)`, id, run, status, age)
	}
	grant("g-new", "r1", "requested", "1 minute")
	grant("g-ended", "r2", "requested", "1 minute")
	grant("g-old", "r3", "requested", "25 hours")
	a.sql(`INSERT INTO flow_triggers (id, name, definition_id, kind, schedule, created_by)
		VALUES ('t1', 'tr', 'd1', 'schedule', '{}', '{}'), ('t2', 'tr2', 'd1', 'schedule', '{}', '{}')`)
	// Its id carries the floored epoch second of a time with a fraction.
	asked := time.Now().UTC().Add(-time.Hour).Truncate(time.Second).Add(750 * time.Millisecond)
	a.sql(`INSERT INTO flow_trigger_grants (id, trigger_id, status, spec, requested_by, requested_at)
		VALUES ('sg1', 't1', 'requested', '{}', '{}', $1),
		('sg2', 't2', 'approved', '{}', '{}', now())`, asked)
	out := a.ok("GET", "/push/waiting", nil)
	got := out["requests"].([]any)
	if len(got) != 2 {
		t.Fatalf("requests %v", got)
	}
	first, second := got[0].(map[string]any), got[1].(map[string]any)
	if first["id"] != "run-grant:g-new" || first["reason"] != "grant" {
		t.Fatalf("first %v", first)
	}
	want := "trigger-grant:sg1:" + strconv.FormatInt(asked.Unix(), 10)
	if second["id"] != want || second["since"] != asked.Format("2006-01-02T15:04:05.000000Z") {
		t.Fatalf("second %v, want %s", second, want)
	}
	if res := a.do("GET", "/push/waiting", nil); res.status != http.StatusOK {
		t.Fatal(res.raw)
	}
}
