package pgxtrace

import "testing"

func TestSummary(t *testing.T) {
	cases := []struct{ sql, op, table string }{
		{"SELECT seq FROM ai_session_events WHERE session_id = $1", "SELECT", "ai_session_events"},
		{"  -- the fence\n  select 1", "SELECT", ""},
		{"/* x */ (SELECT a FROM public.t)", "SELECT", "public.t"},
		{"SELECT extract(epoch FROM now()) FROM \"Quoted\"", "SELECT", `"Quoted"`},
		{"INSERT INTO ai_turns (id) VALUES ($1)", "INSERT", "ai_turns"},
		{"update only ai_sessions set x = $1", "UPDATE", "ai_sessions"},
		{"DELETE FROM ai_leases WHERE key = $1", "DELETE", "ai_leases"},
		{"WITH a AS (SELECT 1) SELECT * FROM a", "WITH", ""},
		{"LOCK TABLE ai_sessions IN SHARE MODE", "LOCK", "ai_sessions"},
		{"commit", "COMMIT", ""},
		{"", "SQL", ""},
	}
	for _, c := range cases {
		op, table := Summary(c.sql)
		if op != c.op || table != c.table {
			t.Errorf("%q: %q %q, want %q %q", c.sql, op, table, c.op, c.table)
		}
	}
}
