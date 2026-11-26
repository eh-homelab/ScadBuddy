package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"
)

func escalationsAPI(t *testing.T) *api { return newAPI(t, "escalations") }

// child is a child session's call at hop parent of parent, its hop until hopUntil (SQL).
func (a *api) child(parent, hopUntil string, n int) string {
	a.t.Helper()
	child := a.gateSession("running")
	request := rid(child, "run-1", "toolu_"+string(rune('0'+n)))
	a.sql(`INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id, kind,
		tool, responders, expires_at, flow_run_id, hop, hop_until, parent_session,
		parent_principal) VALUES ($1, $2::text::uuid, 'w', 'run-1', 'approval', 'print_output',
		ARRAY['browser'], now() + interval '1 hour', 'flow-1', 'parent', `+hopUntil+`,
		$3::text::uuid, 'browser')`, request, child, parent)
	return request
}

type hopState struct {
	hop                    string
	claimed, at, delivered bool
	hops                   []map[string]any
}

func (a *api) hopRow(request string) hopState {
	a.t.Helper()
	var r hopState
	var hops string
	if err := a.pool.QueryRow(context.Background(), `SELECT hop, coalesce(hop_until = 'infinity', false),
		hop_claimed_at IS NOT NULL, delivered_at IS NOT NULL, hops::text
		FROM ai_pending_input WHERE request_id = $1`, request).Scan(
		&r.hop, &r.claimed, &r.at, &r.delivered, &hops); err != nil {
		a.t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(hops), &r.hops); err != nil {
		a.t.Fatal(err)
	}
	return r
}

const fiveMinutes = "now() + interval '5 minutes'"

func TestEscalationsAreTheAgentsOnly(t *testing.T) {
	a := escalationsAPI(t)
	parent := a.gateSession("running")
	request := a.child(parent, fiveMinutes, 1)
	for _, caller := range []string{"print", "projects", "render"} {
		token := a.credential(caller, "crud."+caller)
		got := a.do("POST", base+"/escalations/pass-up", js(map[string]any{"request_id": request,
			"session": parent}), func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+token) })
		expect(t, got.status, 403)
	}
	none := a.do("POST", base+"/escalations/move-due", "", func(r *http.Request) { r.Header.Del("Authorization") })
	expect(t, none.status, 401)
	expect(t, a.hopRow(request).hop, "parent")
}

func TestAnEscalationBodyOutOfBoundsIsRefused(t *testing.T) {
	a := escalationsAPI(t)
	session := uuid()
	many := make([]string, 1001)
	for i := range many {
		many[i] = "r"
	}
	for _, c := range []struct {
		path string
		body map[string]any
	}{
		{"/escalations/pass-up", map[string]any{"request_id": strings.Repeat("x", 2049), "session": session}},
		{"/escalations/pass-up", map[string]any{"request_id": "r", "session": session, "extra": 1}},
		{"/escalations/claim", map[string]any{"session": session, "child": many}},
		{"/escalations/waiting", map[string]any{"grace_s": -1}},
		{"/escalations/waiting", map[string]any{}},
		{"/escalations/deliveries/page", map[string]any{"after": "", "limit": 0}},
		{"/escalations/deliveries/forget", map[string]any{"ids": many}},
		{"/escalations/release-deny", map[string]any{"request_id": "r", "session": session, "hop_until": "soon"}},
	} {
		if got := a.do("POST", base+c.path, js(c.body)); got.status != 422 {
			t.Errorf("%s: %d %s", c.path, got.status, got.raw)
		}
	}
	expect(t, a.do("GET", base+"/escalations/entry", "").status, 422)
	expect(t, a.do("GET", base+"/escalations/run-entries?flow_run_id=", "").status, 422)
}

func TestAPassUpMovesOnlyTheNamedParentsLiveEntry(t *testing.T) {
	a := escalationsAPI(t)
	parent := a.gateSession("running")
	request := a.child(parent, fiveMinutes, 1)
	entry := a.ok("GET", "/escalations/entry?request_id="+url.QueryEscape(request), "").body["entry"].(map[string]any)
	expect(t, entry, map[string]any{"session_id": entry["session_id"], "hop": "parent", "due": false,
		"claimed": false, "parent_session": parent, "parent_principal": "browser"})
	for _, other := range []string{uuid(), "../x"} {
		expect(t, a.post("/escalations/pass-up", map[string]any{"request_id": request, "session": other}),
			map[string]any{"moved": false})
	}
	body := map[string]any{"request_id": request, "session": parent, "reason": "not mine"}
	expect(t, a.post("/escalations/pass-up", body), map[string]any{"moved": true})
	row := a.hopRow(request)
	expect(t, row.hop, "person")
	expect(t, len(row.hops), 1)
	hop := row.hops[0]
	expect(t, []any{hop["hop"], hop["action"], hop["actor"], hop["reason"]},
		[]any{"parent", "pass_up", "session:" + parent, "not mine"})
	if !strings.HasSuffix(hop["at"].(string), "Z") {
		t.Fatalf("at %v", hop["at"])
	}
	expect(t, a.post("/escalations/pass-up", body), map[string]any{"moved": false})
	expect(t, a.ok("GET", "/escalations/entry?request_id=../x", "").raw, `{"entry":null}`)
}

func TestADenyClaimHoldsTheEntryUntilReleased(t *testing.T) {
	a := escalationsAPI(t)
	parent := a.gateSession("running")
	request := a.child(parent, fiveMinutes, 1)
	claim := map[string]any{"request_id": request, "session": parent}
	held, _ := a.post("/escalations/claim-deny", claim)["hop_until"].(string)
	if held == "" {
		t.Fatal("nothing was claimed")
	}
	expect(t, a.hopRow(request).claimed, true)
	expect(t, a.post("/escalations/claim-deny", claim), map[string]any{"hop_until": nil})
	expect(t, a.post("/escalations/pass-up", claim), map[string]any{"moved": false})
	before := a.hopRow(request)
	a.post("/escalations/skip", map[string]any{"session": parent, "child": []string{request}, "own": []string{}})
	expect(t, a.hopRow(request), before)
	expect(t, a.post("/escalations/move-due", map[string]any{}), map[string]any{"moved": 0.0})
	listed := a.ok("GET", "/escalations/run-entries?flow_run_id=flow-1", "").body["entries"].([]any)
	expect(t, listed[0].(map[string]any)["hop_until"], "infinity")
	other := map[string]any{"request_id": request, "session": uuid(), "hop_until": held}
	expect(t, a.post("/escalations/release-deny", other), map[string]any{"done": false})
	release := map[string]any{"request_id": request, "session": parent, "hop_until": held}
	expect(t, a.post("/escalations/release-deny", release), map[string]any{"done": true})
	expect(t, a.hopRow(request).claimed, false)
	listed = a.ok("GET", "/escalations/run-entries?flow_run_id=flow-1", "").body["entries"].([]any)
	back, err := time.Parse(time.RFC3339Nano, listed[0].(map[string]any)["hop_until"].(string))
	if err != nil {
		t.Fatal(err)
	}
	was, _ := time.Parse(time.RFC3339Nano, held)
	expect(t, back.Equal(was), true)
	a.post("/escalations/claim-deny", claim)
	far := map[string]any{"request_id": request, "session": parent, "hop_until": "2999-01-01T00:00:00+00:00"}
	expect(t, a.post("/escalations/release-deny", far), map[string]any{"done": true})
	expect(t, a.count(`SELECT count(*) FROM ai_pending_input WHERE request_id = $1
		AND hop_until = expires_at`, request), 1)
	expect(t, a.post("/escalations/claim-deny", map[string]any{"request_id": request, "session": uuid()}),
		map[string]any{"hop_until": nil})
}

func TestTheSweepMovesADueEntryAndAStaleClaim(t *testing.T) {
	a := escalationsAPI(t)
	parent := a.gateSession("running")
	due := a.child(parent, "now() - interval '1 second'", 1)
	stale := a.child(parent, "'infinity'", 2)
	a.sql(`UPDATE ai_pending_input SET hop_claimed_at = now() - interval '301 seconds'
		WHERE request_id = $1`, stale)
	live := a.child(parent, fiveMinutes, 3)
	expect(t, a.post("/escalations/move-due", map[string]any{}), map[string]any{"moved": 2.0})
	for _, request := range []string{due, stale} {
		row := a.hopRow(request)
		expect(t, []any{row.hop, row.at, row.hops[0]["action"], row.hops[0]["actor"]},
			[]any{"person", false, "timeout", "system"})
	}
	expect(t, a.hopRow(live).hop, "parent")
}

func TestADeliveryClaimsOnceHandsBackAndSkips(t *testing.T) {
	a := escalationsAPI(t)
	parent := a.gateSession("running")
	request := a.child(parent, fiveMinutes, 1)
	own := "flow:flow-9:wrun:" + uuid()
	calls := a.post("/escalations/waiting", map[string]any{"grace_s": 60})["calls"].([]any)
	expect(t, len(calls), 1)
	call := calls[0].(map[string]any)
	expect(t, []any{call["request_id"], call["parent_session"], call["past_grace"]},
		[]any{request, parent, false})
	body := map[string]any{"session": parent, "child": []string{request}, "own": []string{own}}
	expect(t, a.post("/escalations/claim", body), map[string]any{"claimed": []any{request, own}})
	expect(t, a.post("/escalations/claim", body), map[string]any{"claimed": []any{}})
	expect(t, a.post("/escalations/waiting", map[string]any{"grace_s": 60}), map[string]any{"calls": []any{}})
	expect(t, a.post("/escalations/deliveries/sent", map[string]any{"ids": []string{own, "flow:x"}}),
		map[string]any{"ids": []any{own}})
	a.post("/escalations/unclaim", map[string]any{"session": uuid(), "child": []string{request}, "own": []string{own}})
	expect(t, a.hopRow(request).delivered, true)
	a.post("/escalations/unclaim", body)
	expect(t, a.hopRow(request).delivered, false)
	expect(t, a.post("/escalations/deliveries/sent", map[string]any{"ids": []string{own}}),
		map[string]any{"ids": []any{}})
	a.post("/escalations/skip", body)
	row := a.hopRow(request)
	expect(t, []any{row.hop, row.hops[0]["actor"], row.hops[0]["reason"]},
		[]any{"person", "system", "the starting session cannot be asked"})
	expect(t, a.post("/escalations/deliveries/sent", map[string]any{"ids": []string{own}}),
		map[string]any{"ids": []any{own}})
	page := map[string]any{"after": "", "limit": 10}
	expect(t, a.post("/escalations/deliveries/page", page), map[string]any{"ids": []any{own}})
	a.post("/escalations/deliveries/forget", map[string]any{"ids": []string{own}})
	expect(t, a.post("/escalations/deliveries/page", page), map[string]any{"ids": []any{}})
	body["session"] = "../x"
	expect(t, a.post("/escalations/claim", body), map[string]any{"claimed": []any{}})
}
