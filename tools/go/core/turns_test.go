package main

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

// The session-turns area's writes, as backend/tests/crud/test_crud_agent_turns.py checks them.

func turnsAPI(t *testing.T) *api { return newAPI(t, "session-turns") }

// row is a session as the turn tests make it: status, its turn, and columns by name.
func (a *api) row(status string, turn any, cols map[string]any) string {
	a.t.Helper()
	get := func(name string, fallback any) any {
		if v, ok := cols[name]; ok {
			return v
		}
		return fallback
	}
	id := uuid()
	a.sql(`INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,
		creator_id, status, max_turns, budget_usd, cost_usd, durable_turn_id, turn_attempt, runtime)
		VALUES ($1::text::uuid, 'chat', $2, $3, 'You', 'browser', 'browser', $4, 10, $5, $6,
		$7::text::uuid, $8, $9)`,
		id, get("owner_kind", "browser"), get("owner_id", "browser"), status, get("budget", 1.0),
		get("cost", 0.0), turn, get("attempt", nil), get("runtime", "unified"))
	return id
}

func (a *api) one(query string, args ...any) []any {
	a.t.Helper()
	rows, err := a.pool.Query(context.Background(), query, args...)
	if err != nil {
		a.t.Fatal(err)
	}
	defer rows.Close()
	if !rows.Next() {
		a.t.Fatalf("no row: %s", query)
	}
	values, err := rows.Values()
	if err != nil {
		a.t.Fatal(err)
	}
	return values
}

func (a *api) types(sid string) (out []string) {
	a.t.Helper()
	rows, err := a.pool.Query(context.Background(), `SELECT event::jsonb->>'type' FROM
		ai_session_events WHERE session_id = $1::text::uuid ORDER BY seq`, sid)
	if err != nil {
		a.t.Fatal(err)
	}
	defer rows.Close()
	for rows.Next() {
		var kind string
		_ = rows.Scan(&kind)
		out = append(out, kind)
	}
	return out
}

func statusText(sid, status string) string {
	return object("v", 1, "type", "session.status", "sessionId", sid, "status", status)
}

func closing(sid string) []string {
	result := object("v", 1, "type", "session.result", "sessionId", sid, "costUsd", 0, "turns", 0,
		"budgetUsd", 0)
	return []string{result, statusText(sid, "idle")}
}

func head(sid, turn string, author map[string]string, extra ...any) map[string]any {
	n := map[string]any{
		"turn_id": turn, "author": author, "bare": false,
		"started": []string{userTurn(turn, "hi"), statusText(sid, "running")},
	}
	for i := 0; i < len(extra); i += 2 {
		n[extra[i].(string)] = extra[i+1]
	}
	return n
}

func TestOnlyTheAgentReachesTheTurnRoutes(t *testing.T) {
	a := turnsAPI(t)
	sid := a.row("idle", nil, nil)
	none := a.do("GET", base+"/sessions/"+sid+"/turn", "", func(r *http.Request) { r.Header.Del("Authorization") })
	expect(t, none.status, 401)
	for _, caller := range []string{"render", "print", "projects", "storefront"} {
		token := a.credential(caller, "crud."+caller)
		with := func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+token) }
		expect(t, a.do("GET", base+"/sessions/"+sid+"/turn", "", with).status, 403)
		claim := object("author", browser, "turn_id", uuid(), "bare", false)
		expect(t, a.do("POST", base+"/sessions/"+sid+"/turn/send-claim", claim, with).status, 403)
	}
	expect(t, a.one(`SELECT status FROM ai_sessions`)[0], "idle")
}

func TestASendClaimsAFreeRowAndGivesItBack(t *testing.T) {
	a := turnsAPI(t)
	sid := a.row("failed", nil, map[string]any{"runtime": nil})
	turn := uuid()
	path := "/sessions/" + sid + "/turn/"
	claim := func(turn string) string {
		return object("author", browser, "turn_id", turn, "tiers", []string{"read"}, "bare", true)
	}
	expect(t, a.ok("POST", path+"send-claim", claim(turn)).raw,
		`{"claimed":{"prior_status":"failed","runtime":"unified"}}`)
	got := a.one(`SELECT status, durable_turn_id::text, turn_flow, turn_tiers::text FROM ai_sessions`)
	expect(t, got[:3], []any{"running", turn, true})
	var tiers any
	_ = json.Unmarshal([]byte(got[3].(string)), &tiers)
	expect(t, tiers, map[string]any{"by": map[string]any{"kind": "browser", "id": "browser"},
		"tiers": []any{"read"}})
	expect(t, a.ok("POST", path+"send-claim", claim(uuid())).raw, `{"claimed":null}`)
	expect(t, a.ok("POST", path+"give-back", object("prior", "failed", "turn_id", uuid())).raw,
		`{"logged":null}`)
	back := a.ok("POST", path+"give-back", object("prior", "failed", "turn_id", strings.ToUpper(turn)))
	want := `{"v":1,"type":"session.status","sessionId":"` + sid + `","status":"failed"}`
	expect(t, back.raw, `{"logged":{"events":[`+mustJSON(want)+`],"seqs":[1]}}`)
	expect(t, a.one(`SELECT durable_turn_id FROM ai_sessions`)[0], nil)
	expect(t, a.ok("POST", path+"give-back", object("prior", "idle", "turn_id", turn)).raw,
		`{"logged":null}`)
}

func mustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return string(b)
}

func TestAnAgentOlderThanTheFenceGivesBackUnfenced(t *testing.T) {
	a := turnsAPI(t)
	sid := a.row("running", uuid(), nil)
	got := a.ok("POST", "/sessions/"+sid+"/turn/give-back", object("prior", "failed")).body
	expect(t, got["logged"].(map[string]any)["seqs"], []any{1.0})
	expect(t, a.one(`SELECT status, durable_turn_id FROM ai_sessions`), []any{"failed", nil})
}

func TestASendClaimRefusesARowItCannotTake(t *testing.T) {
	for _, c := range []struct {
		status string
		cols   map[string]any
		author map[string]string
	}{
		{"done", nil, browser},
		{"running", nil, browser},
		{"idle", map[string]any{"cost": 1.0}, browser},
		{"idle", map[string]any{"owner_kind": "bearer", "owner_id": "a"}, map[string]string{"kind": "bearer", "id": "b"}},
	} {
		a := turnsAPI(t)
		sid := a.row(c.status, nil, c.cols)
		body := object("author", c.author, "turn_id", uuid(), "bare", false)
		expect(t, a.ok("POST", "/sessions/"+sid+"/turn/send-claim", body).raw, `{"claimed":null}`)
		archived := a.row("idle", nil, nil)
		a.sql(`UPDATE ai_sessions SET archived_at = now() WHERE id = $1::text::uuid`, archived)
		expect(t, a.ok("POST", "/sessions/"+archived+"/turn/send-claim", body).raw, `{"claimed":null}`)
	}
}

func TestASendMayQueueBehindARunningTurn(t *testing.T) {
	a := turnsAPI(t)
	sid := a.row("running", uuid(), nil)
	a.sql(`UPDATE ai_sessions SET event_seq = 7`)
	expect(t, a.ok("POST", "/sessions/"+sid+"/turn/queueable", object("author", browser)).raw, `{"seq":7}`)
	idle := a.row("idle", nil, nil)
	expect(t, a.ok("POST", "/sessions/"+idle+"/turn/queueable", object("author", browser)).raw, `{"seq":null}`)
}

func TestTheTurnsStateAndAStopsFlag(t *testing.T) {
	a := turnsAPI(t)
	turn := uuid()
	sid := a.row("running", turn, map[string]any{"attempt": "run:1"})
	state := a.ok("GET", "/sessions/"+sid+"/turn?grace_ms=60000", "").body["turn"].(map[string]any)
	expect(t, []any{state["durable_turn_id"], state["turn_attempt"], state["interrupt_requested"],
		state["settled"], state["event_seq"]}, []any{turn, "run:1", false, false, "0"})
	expect(t, a.ok("POST", "/sessions/"+sid+"/turn/interrupt", "{}").raw, `{"changed":true}`)
	expect(t, a.ok("GET", "/sessions/"+sid+"/turn", "").body["turn"].(map[string]any)["interrupt_requested"], true)
	idle := a.row("idle", nil, nil)
	expect(t, a.ok("POST", "/sessions/"+idle+"/turn/interrupt", "{}").raw, `{"changed":false}`)
	expect(t, a.ok("GET", "/sessions/"+uuid()+"/turn", "").raw, `{"turn":null}`)
}

func TestAPlacementTakesTheTurnOnlyWhileTheRowRunsIt(t *testing.T) {
	a := turnsAPI(t)
	turn := uuid()
	sid := a.row("running", turn, nil)
	path := "/sessions/" + sid + "/turn/placement"
	place := func(turn, attempt, key string) string {
		return a.ok("POST", path, object("turn_id", turn, "attempt", attempt, "mark_key", key)).raw
	}
	expect(t, place(turn, "run:1", turn), `{"mark":0}`)
	a.sql(`INSERT INTO ai_session_entries (project_key, session_id, subpath, entry) VALUES ('', $1, '', '{}')`, sid)
	expect(t, place(turn, "run:2", turn), `{"mark":0}`)
	expect(t, a.one(`SELECT turn_attempt FROM ai_sessions`)[0], "run:2")
	if place(turn, "run:2", turn+"/r1") == `{"mark":0}` {
		t.Fatal("a continuation's own key did not mark again")
	}
	a.sql(`UPDATE ai_sessions SET durable_turn_id = $1::text::uuid`, uuid())
	expect(t, place(turn, "run:3", turn), `{"mark":null}`)
	a.sql(`UPDATE ai_sessions SET status = 'idle', durable_turn_id = NULL`)
	expect(t, place(turn, "run:4", turn), `{"mark":null}`)
	expect(t, a.one(`SELECT turn_attempt FROM ai_sessions`)[0], "run:2")
}

func spendBody(key string, cost, unpriced float64, turns int) string {
	return object("key", key, "cost_usd", cost, "unpriced_usd", unpriced, "turns", turns)
}

func TestAPlacementsSpendIsAddedWhoeverHoldsTheRow(t *testing.T) {
	a := turnsAPI(t)
	sid := a.row("idle", nil, map[string]any{"cost": 0.5})
	expect(t, a.ok("POST", "/sessions/"+sid+"/turn/spend", spendBody("run:1:0", 0.25, 0.1, 2)).raw, `{"written":true}`)
	expect(t, a.one(`SELECT cost_usd, unpriced_cost_usd, turns FROM ai_sessions`), []any{0.75, 0.1, int32(2)})
	expect(t, a.ok("POST", "/sessions/"+uuid()+"/turn/spend", spendBody("run:1:0", 0.25, 0.1, 2)).raw, `{"written":false}`)
}

func TestASpendSentAgainUnderItsKeyIsAddedOnce(t *testing.T) {
	a := turnsAPI(t)
	sid := a.row("idle", nil, nil)
	path := "/sessions/" + sid + "/turn/spend"
	spend := func(key string) { a.ok("POST", path, spendBody(key, 0.25, 0, 1)) }
	figures := func() []any { return a.one(`SELECT cost_usd, turns FROM ai_sessions`) }
	spend("run:1:0")
	spend("run:1:0")
	expect(t, figures(), []any{0.25, int32(1)})
	spend("run:1:1")
	expect(t, figures(), []any{0.5, int32(2)})
	a.sql(`UPDATE ai_turn_spend_keys SET created_at = now() - interval '2 hours' WHERE key = 'run:1:0'`)
	spend("run:2:0")
	expect(t, a.one(`SELECT array_agg(key ORDER BY key) FROM ai_turn_spend_keys`)[0], []any{"run:1:1", "run:2:0"})
	a.sql(`UPDATE ai_turn_spend_keys SET created_at = now() - interval '2 hours' WHERE key = 'run:1:1'`)
	before := figures()[0]
	spend("run:1:1")
	expect(t, figures()[0], before)
}

func TestATurnIDIsReadLowercase(t *testing.T) {
	a := turnsAPI(t)
	turn := uuid()
	sid := a.row("running", turn, nil)
	up := strings.ToUpper(turn)
	expect(t, a.ok("POST", "/sessions/"+sid+"/turn/placement",
		object("turn_id", up, "attempt", "run:1", "mark_key", turn)).raw, `{"mark":0}`)
	ended := a.ok("POST", "/sessions/"+sid+"/turn/finish", object("turn_id", up, "tail", closing(sid)))
	if ended.body["ended"] == nil {
		t.Fatal(ended.raw)
	}
}

func TestATurnsEndIsWrittenOnceAndNeverOverAnotherTurn(t *testing.T) {
	a := turnsAPI(t)
	turn := uuid()
	sid := a.row("running", turn, map[string]any{"cost": 0.125, "budget": 2.0})
	a.sql(`UPDATE ai_sessions SET turns = 3`)
	path := "/sessions/" + sid + "/turn/finish"
	budget := `{"v":1,"type":"error","sessionId":"` + sid + `","code":"error_max_budget_usd","message":"x"}`
	tail := []string{closing(sid)[0], budget, statusText(sid, "idle")}
	ended := a.ok("POST", path, object("turn_id", turn, "tail", tail)).body
	expect(t, ended["next"], nil)
	logged := ended["ended"].(map[string]any)
	expect(t, logged["seqs"], []any{1.0, 2.0, 3.0})
	events := logged["events"].([]any)
	// JSON.stringify's text: the members in their order, an integral number with no fraction.
	expect(t, events[0], `{"budgetUsd":2,"costUsd":0.125,"sessionId":"`+sid+`","turns":3,"type":"session.result","v":1}`)
	expect(t, events[1], `{"v":1,"type":"error","sessionId":"`+sid+`","code":"error_max_budget_usd",`+
		`"message":"this chat used its $2.00 budget ($0.13 spent)"}`)
	expect(t, a.one(`SELECT status, durable_turn_id FROM ai_sessions`), []any{"idle", nil})
	again := object("turn_id", turn, "tail", closing(sid))
	expect(t, a.ok("POST", path, again).raw, `{"ended":null,"next":null}`)
	a.sql(`UPDATE ai_sessions SET status = 'running', durable_turn_id = $1::text::uuid`, uuid())
	expect(t, a.ok("POST", path, again).body["ended"], nil)
	expect(t, a.one(`SELECT status FROM ai_sessions`)[0], "running")
	expect(t, len(a.types(sid)), 3)
}

func TestATurnsEndClaimsTheQueuesHeadInItsTransaction(t *testing.T) {
	a := turnsAPI(t)
	turn, next := uuid(), uuid()
	sid := a.row("running", turn, nil)
	body := object("turn_id", turn, "tail", closing(sid), "next", head(sid, next, browser, "tiers", []string{"read"}))
	done := a.ok("POST", "/sessions/"+sid+"/turn/finish", body).body["next"].(map[string]any)
	expect(t, done["outcome"], "started")
	expect(t, done["logged"].(map[string]any)["seqs"], []any{3.0, 4.0})
	got := a.one(`SELECT status, durable_turn_id::text, turn_tiers->'tiers' FROM ai_sessions`)
	expect(t, got, []any{"running", next, []any{"read"}})
	expect(t, a.types(sid), []string{"session.result", "session.status", "user.turn", "session.status"})
	expect(t, a.ok("POST", "/sessions/"+sid+"/turn/finish", body).raw,
		`{"ended":null,"next":{"outcome":"started","logged":null}}`)
	other := object("next", head(sid, uuid(), browser))
	expect(t, a.ok("POST", "/sessions/"+sid+"/turn/begin", other).raw, `{"outcome":"busy","logged":null}`)
}

func TestAQueuedMessageTheSessionCannotRunIsDroppedWithWhy(t *testing.T) {
	a := turnsAPI(t)
	sid := a.row("idle", nil, map[string]any{"owner_kind": "bearer", "owner_id": "b"})
	next := object("next", head(sid, uuid(), map[string]string{"kind": "bearer", "id": "a"}))
	begun := a.ok("POST", "/sessions/"+sid+"/turn/begin", next).body
	expect(t, begun["outcome"], "dropped")
	expect(t, begun["logged"].(map[string]any)["events"], []any{`{"v":1,"type":"error","sessionId":"` + sid +
		`","code":"queued_message_dropped","message":"A queued message was not sent: the session was handed off."}`})
	expect(t, a.one(`SELECT status FROM ai_sessions`)[0], "idle")
	expect(t, a.ok("POST", "/sessions/"+uuid()+"/turn/begin", next).raw, `{"outcome":"dropped","logged":null}`)
}

func TestTheQueueEventIsWrittenUnderTheRowsLockAndReadBack(t *testing.T) {
	a := turnsAPI(t)
	sid := a.row("idle", nil, nil)
	path := "/sessions/" + sid + "/turn/queue"
	expect(t, a.ok("GET", path, "").raw, `{"event":null}`)
	queued := object("v", 1, "type", "session.queue", "sessionId", sid, "version", 2, "queue", []any{})
	expect(t, a.ok("POST", path, object("event", queued)).body["logged"].(map[string]any)["seqs"], []any{1.0})
	expect(t, a.ok("GET", path, "").body["event"], queued)
	expect(t, a.ok("POST", "/sessions/"+uuid()+"/turn/queue", object("event", queued)).raw, `{"logged":null}`)
	expect(t, a.do("POST", base+path, object("event", statusText(sid, "idle"))).status, 422)
}

func TestTheSweepEndsALostTurnOnlyWhileTheRowIsAsItReadIt(t *testing.T) {
	a := turnsAPI(t)
	sid := a.row("waiting_input", uuid(), nil)
	a.sql(`UPDATE ai_sessions SET updated_at = now() - interval '1 hour' WHERE id = $1::text::uuid`, sid)
	fresh := a.row("running", uuid(), nil)
	rows := a.ok("GET", "/turns/stale?grace_ms=60000&limit=10", "").body["rows"].([]any)
	expect(t, rows, []any{map[string]any{"id": sid, "event_seq": "0"}})
	_ = fresh
	lost := object("v", 1, "type", "error", "sessionId", sid, "code", "turn_failed", "message", "gone")
	tail := []string{lost, statusText(sid, "idle")}
	a.sql(`UPDATE ai_sessions SET event_seq = 1 WHERE id = $1::text::uuid`, sid)
	path := "/sessions/" + sid + "/turn/end-lost"
	expect(t, a.ok("POST", path, object("event_seq", 0, "tail", tail)).raw, `{"logged":null}`)
	expect(t, a.one(`SELECT status FROM ai_sessions WHERE id = $1::text::uuid`, sid)[0], "waiting_input")
	ended := a.ok("POST", path, object("event_seq", 1, "tail", tail)).body
	expect(t, ended["logged"].(map[string]any)["seqs"], []any{2.0, 3.0})
	expect(t, a.one(`SELECT status FROM ai_sessions WHERE id = $1::text::uuid`, sid)[0], "idle")
	after := a.ok("GET", "/turns/stale?after="+sid+"&limit=1", "").body["rows"].([]any)
	for _, r := range after {
		if r.(map[string]any)["id"].(string) <= sid {
			t.Fatalf("a row at or before after: %v", r)
		}
	}
}

func TestATurnsEndWaitsForAClaimHoldingTheRow(t *testing.T) {
	a := turnsAPI(t)
	turn := uuid()
	sid := a.row("running", turn, nil)
	got := a.waits(func() answer {
		return a.do("POST", base+"/sessions/"+sid+"/turn/finish", object("turn_id", turn, "tail", closing(sid)))
	},
		"SELECT 1 FROM ai_sessions WHERE id = '"+sid+"' FOR UPDATE",
		"UPDATE ai_sessions SET durable_turn_id = '"+uuid()+"' WHERE id = '"+sid+"'")
	expect(t, got.raw, `{"ended":null,"next":null}`)
}

func TestTheTurnBodiesAndIDsAreBounded(t *testing.T) {
	a := turnsAPI(t)
	turn := uuid()
	sid := a.row("running", turn, nil)
	path := base + "/sessions/" + sid + "/turn"
	tail := closing(sid)
	many := make([]string, 17)
	for i := range many {
		many[i] = "r"
	}
	long := append(append(append([]string{}, tail...), tail...), append(tail, append(tail, append(tail, tail...)...)...)...)
	empty := head(sid, turn, browser, "started", []string{})
	for _, c := range []struct{ tail, body string }{
		{"/send-claim", object("author", browser, "turn_id", "t", "bare", false)},
		{"/send-claim", object("author", map[string]string{"kind": "x", "id": "a"}, "turn_id", turn, "bare", false)},
		{"/send-claim", object("author", browser, "turn_id", turn, "bare", false, "extra", 1)},
		{"/send-claim", object("author", browser, "turn_id", turn, "bare", false, "tiers", many)},
		{"/send-claim", object("author", browser, "turn_id", turn, "bare", "yes")},
		{"/give-back", object("prior", "sideways", "turn_id", turn)},
		{"/give-back", object("prior", "idle", "turn_id", "t")},
		{"/placement", object("turn_id", turn, "attempt", "", "mark_key", turn)},
		{"/placement", object("turn_id", "../x", "attempt", "a", "mark_key", turn)},
		{"/spend", object("key", "k", "cost_usd", 1e9, "unpriced_usd", 0, "turns", 0)},
		{"/spend", object("cost_usd", 0, "unpriced_usd", 0, "turns", 0)},
		{"/spend", object("key", strings.Repeat("k", 600), "cost_usd", 0, "unpriced_usd", 0, "turns", 0)},
		{"/queue", object("event", statusText(sid, "idle"))},
		{"/finish", object("turn_id", turn, "tail", []string{})},
		{"/finish", object("turn_id", turn, "tail", []string{"not json"})},
		{"/finish", object("turn_id", turn, "tail", []string{tail[0]})},
		{"/finish", object("turn_id", turn, "tail", []string{statusText(sid, "running")})},
		{"/finish", object("turn_id", turn, "tail", long)},
		{"/finish", object("turn_id", turn, "tail", tail, "next", map[string]any{"turn_id": turn})},
		{"/begin", object("next", empty)},
		{"/begin", `{}`},
		{"/end-lost", object("event_seq", -1, "tail", tail)},
		{"/end-lost", `[]`},
		{"/end-lost", `{`},
		{"/end-lost", ``},
	} {
		got := a.do("POST", path+c.tail, c.body)
		if got.status != 422 || got.header.Get("X-ScadBuddy-Crud") != "1" {
			t.Errorf("%s %s: %d %s", c.tail, c.body, got.status, got.raw)
		}
	}
	issue := a.do("POST", path+"/give-back", object("prior", "sideways")).body["errors"].([]any)[0]
	expect(t, issue.(map[string]any)["loc"], []any{"body", "prior"})
	huge := a.do("POST", path+"/finish", `{"x":"`+strings.Repeat("y", 32<<20)+`"}`)
	expect(t, huge.status, 413)
	chunked := a.do("POST", path+"/interrupt", "{}", func(r *http.Request) { r.ContentLength = -1 })
	expect(t, chunked.status, 411)
	expect(t, a.do("GET", base+"/turns/stale?limit=1001", "").status, 422)
	expect(t, a.do("GET", base+"/turns/stale?after="+strings.Repeat("a", 37), "").status, 422)
	for _, bad := range []string{"not-a-uuid", "..", "%2E%2E", strings.ToUpper(sid), sid + "%2F..%2F"} {
		if got := a.do("GET", base+"/sessions/"+bad+"/turn", "").status; got != 404 && got != 422 && got != http.StatusTeapot {
			t.Errorf("GET %s: %d", bad, got)
		}
		claim := object("author", browser, "turn_id", turn, "bare", false)
		if got := a.do("POST", base+"/sessions/"+bad+"/turn/send-claim", claim).status; got != 404 && got != 422 && got != http.StatusTeapot {
			t.Errorf("POST %s: %d", bad, got)
		}
	}
	expect(t, a.one(`SELECT status, durable_turn_id::text, turn_attempt FROM ai_sessions`), []any{"running", turn, nil})
	expect(t, len(a.types(sid)), 0)
}
