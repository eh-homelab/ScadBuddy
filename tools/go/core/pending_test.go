package main

import (
	"net/http"
	"net/url"
	"strings"
	"testing"
)

// The pending-input routes, as backend/tests/crud/test_crud_agent_pending.py checks them.

const trace = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

func pendingAPI(t *testing.T) *api { return newAPI(t, "pending-input, approvals, gate, audit") }

// get is a GET of path with query's pairs, checked 200.
func (a *api) get(path string, query ...string) map[string]any {
	a.t.Helper()
	v := url.Values{}
	for i := 0; i < len(query); i += 2 {
		v.Set(query[i], query[i+1])
	}
	return a.ok("GET", path+"?"+v.Encode(), "").body
}

func (a *api) status(method, path string, body any) int {
	a.t.Helper()
	text := ""
	if body != nil {
		text = js(body)
	}
	return a.do(method, base+path, text).status
}

func (a *api) resolveAs(request, outcome string) {
	a.t.Helper()
	got := a.post("/gate/resolve", map[string]any{"request_id": request, "outcome": outcome, "responder": me})
	expect(a.t, got["resolved"], true)
}

func (a *api) statusOf(session string) string {
	return a.text(`SELECT status FROM ai_sessions WHERE id = $1::text::uuid`, session)
}

func TestPendingInputIsTheAgentsOnly(t *testing.T) {
	a := pendingAPI(t)
	for _, caller := range []string{"print", "projects", "render"} {
		token := a.credential(caller, "crud."+caller)
		got := a.do("POST", base+"/pending-input/entries", "{}", func(r *http.Request) {
			r.Header.Set("Authorization", "Bearer "+token)
		})
		expect(t, got.status, 403)
	}
	none := func(r *http.Request) { r.Header.Del("Authorization") }
	expect(t, a.do("POST", base+"/pending-input/entries", "{}", none).status, 401)
	expect(t, a.do("GET", base+"/pending-input/approval?request_id=x&input_hash="+hash64, "", none).status, 401)
}

func TestAPendingBodyOutOfBoundsIsRefused(t *testing.T) {
	a := pendingAPI(t)
	many := make([]string, 1001)
	for i := range many {
		many[i] = "r"
	}
	expect(t, a.status("POST", "/pending-input/in-run", map[string]any{"request_ids": many}), 422)
	expect(t, a.status("POST", "/pending-input/entries", map[string]any{"session_id": "../not-a-session"}), 422)
	expect(t, a.status("POST", "/pending-input/entries", map[string]any{"extra": 1}), 422)
	long := map[string]any{"request_id": "r", "traceparent": strings.Repeat("x", 257)}
	expect(t, a.status("POST", "/pending-input/decision-trace", long), 422)
	session := a.gateSession("running")
	both := map[string]any{"run": map[string]any{"workflow_id": "w", "run_id": "r"}, "session_id": session}
	expect(t, a.status("POST", "/pending-input/orphans/cancel", both), 422)
	expect(t, a.status("POST", "/pending-input/orphans/cancel", map[string]any{}), 422)
	expect(t, a.status("GET", "/pending-input/sweep/runs?min_age_s=0", nil), 422)
	expect(t, a.status("GET", "/pending-input/sweep/runs?min_age_s=0&recheck_s=86401", nil), 422)
}

func requestIDs(entries any) []string {
	out := []string{}
	for _, e := range entries.([]any) {
		out = append(out, e.(map[string]any)["request_id"].(string))
	}
	return out
}

func TestEntriesAnswerTheFilter(t *testing.T) {
	a := pendingAPI(t)
	session, other := a.gateSession("running"), a.gateSession("running")
	a.post("/gate/open", openBody(session, "run-1", "approval", "toolu_1", nil))
	a.post("/gate/open", openBody(other, "run-1", "answer", "toolu_1", nil))
	everything := a.post("/pending-input/entries", map[string]any{})["entries"].([]any)
	expect(t, len(everything), 2)
	expect(t, everything[0].(map[string]any)["session_id"], session)
	expect(t, everything[1].(map[string]any)["session_id"], other)
	one := a.post("/pending-input/entries", map[string]any{"session_id": session})["entries"].([]any)
	expect(t, len(one), 1)
	e := one[0].(map[string]any)
	expect(t, e["request_id"], rid(session, "run-1", "toolu_1"))
	expect(t, e["input_hash"], hash64)
	expect(t, e["expiring"], false)
	expect(t, e["flow_run_id"], nil)
	expect(t, e["requested_by"], me)
	stranger := map[string]any{"owned_by": map[string]any{"kind": "bearer", "id": "someone"}}
	expect(t, a.post("/pending-input/entries", stranger)["entries"], []any{})
	stranger["all_approvals"] = true
	expect(t, requestIDs(a.post("/pending-input/entries", stranger)["entries"]),
		[]string{rid(session, "run-1", "toolu_1")})
	mine := map[string]any{"owned_by": map[string]any{"kind": "browser", "id": "browser"}}
	expect(t, len(a.post("/pending-input/entries", mine)["entries"].([]any)), 2)
	// An answer outlives a handoff: not the browser user's to answer.
	a.sql(`UPDATE ai_sessions SET owner_kind = 'bearer' WHERE id = $1::text::uuid`, other)
	expect(t, requestIDs(a.post("/pending-input/entries", map[string]any{"own_answers": true})["entries"]),
		[]string{rid(session, "run-1", "toolu_1")})
}

func TestInRunDecidedAndRecorded(t *testing.T) {
	a := pendingAPI(t)
	session := a.gateSession("running")
	request := rid(session, "run-1", "toolu_1")
	a.post("/gate/open", openBody(session, "run-1", "approval", "toolu_1", nil))
	ids := map[string]any{"request_ids": []string{request, "../../areas", "never"}}
	none := map[string]any{"request_ids": []any{}}
	expect(t, a.post("/pending-input/in-run", ids), none)
	expect(t, a.post("/pending-input/in-run", none), none)
	expect(t, a.post("/pending-input/decided", ids), none)
	expect(t, a.get("/pending-input/recorded", "request_id", request), map[string]any{"recorded": nil})
	a.sql(`UPDATE ai_pending_input SET flow_run_id = 'run-9' WHERE request_id = $1`, request)
	found := map[string]any{"request_ids": []any{request}}
	expect(t, a.post("/pending-input/in-run", ids), found)
	a.resolveAs(request, "approved")
	expect(t, a.post("/pending-input/decided", ids), found)
	expect(t, a.get("/pending-input/recorded", "request_id", request)["recorded"], map[string]any{
		"kind": "approval", "outcome": "approved", "response": nil, "response_hash": nil,
		"responder": me, "reason": nil, "decision_traceparent": nil,
	})
	expect(t, a.get("/pending-input/recorded", "request_id", "../../areas"), map[string]any{"recorded": nil})
}

func TestATabWaitIsAnAgentSessionsOnly(t *testing.T) {
	a := pendingAPI(t)
	session := a.gateSession("running")
	attention := map[string]any{"reason": "tab_disconnected", "on_timeout": nil}
	a.post("/gate/open", openBody(session, "run-1", "answer", "toolu_1", map[string]any{"attention": attention}))
	// A row names its runtime since #7323 ('unified' by default): a 'durable' one waits for no tab.
	a.sql(`UPDATE ai_sessions SET runtime = 'durable' WHERE id = $1::text::uuid`, session)
	expect(t, a.get("/pending-input/tab-wait", "session_id", session), map[string]any{"waits": false})
	a.sql(`UPDATE ai_sessions SET runtime = 'unified' WHERE id = $1::text::uuid`, session)
	expect(t, a.get("/pending-input/tab-wait", "session_id", session), map[string]any{"waits": true})
	expect(t, a.status("GET", "/pending-input/tab-wait?session_id=../x", nil), 422)
	expect(t, a.status("GET", "/pending-input/tab-wait", nil), 422)
}

func TestTheTracesOfAnEntry(t *testing.T) {
	a := pendingAPI(t)
	session := a.gateSession("running")
	request := rid(session, "run-1", "toolu_1")
	a.post("/gate/open", openBody(session, "run-1", "approval", "toolu_1", nil))
	got := a.get("/pending-input/park-trace", "request_id", request)["trace"].(map[string]any)
	expect(t, got["tool"], "print_output")
	expect(t, got["traceparent"], nil)
	if _, ok := got["created_at"].(string); !ok {
		t.Fatalf("created_at %v", got["created_at"])
	}
	body := map[string]any{"request_id": request, "traceparent": trace}
	expect(t, a.post("/pending-input/decision-trace", body), map[string]any{"done": true})
	a.resolveAs(request, "approved")
	expect(t, a.get("/pending-input/recorded", "request_id", request)["recorded"].(map[string]any)["decision_traceparent"], trace)
	expect(t, a.get("/pending-input/park-trace", "request_id", request), map[string]any{"trace": nil})
}

func TestASessionIsReadAndAMalformedIDNamesNone(t *testing.T) {
	a := pendingAPI(t)
	session := a.gateSession("running")
	expect(t, a.get("/pending-input/session", "session_id", session)["session"], map[string]any{
		"owner_kind": "browser", "owner_id": "browser", "owner_label": "You",
		"creator_kind": "browser", "creator_id": "browser",
	})
	for _, missing := range []string{uuid(), "../../areas", "not-a-uuid"} {
		expect(t, a.get("/pending-input/session", "session_id", missing), map[string]any{"session": nil})
	}
}

func (a *api) placed(session, attempt string) string {
	turn := uuid()
	a.sql(`UPDATE ai_sessions SET turn_attempt = $1, durable_turn_id = $2::text::uuid
		WHERE id = $3::text::uuid`, attempt, turn, session)
	return turn
}

func mark(session, turn, attempt string, extra map[string]any) map[string]any {
	body := map[string]any{"request_id": rid(session, "run-1", "toolu_1"), "traceparent": trace,
		"session_id": session, "attempt": attempt, "turn_id": turn}
	for k, v := range extra {
		body[k] = v
	}
	return body
}

func (a *api) traceparent(session string) string {
	return a.text(`SELECT coalesce(traceparent, '') FROM ai_pending_input WHERE session_id = $1::text::uuid`, session)
}

func TestATraceMarkLandsOnlyForThePlacementThatHoldsTheTurn(t *testing.T) {
	a := pendingAPI(t)
	session, other := a.gateSession("running"), a.gateSession("running")
	a.post("/gate/open", openBody(session, "run-1", "approval", "toolu_1", nil))
	turn, otherTurn := a.placed(session, "run-1/1"), a.placed(other, "run-1/1")
	for _, refused := range []map[string]any{
		mark(session, turn, "run-1/2", nil),
		mark(session, uuid(), "run-1/1", nil),
		mark(session, "not-a-uuid", "run-1/1", nil),
		mark(session, turn, "run-1/1", map[string]any{"session_id": "../x"}),
		// Another session's placement: the entry is not its own.
		mark(other, otherTurn, "run-1/1", map[string]any{"request_id": rid(session, "run-1", "toolu_1")}),
	} {
		expect(t, a.post("/pending-input/trace", refused), map[string]any{"marked": false})
	}
	expect(t, a.traceparent(session), "")
	expect(t, a.post("/pending-input/trace", mark(session, turn, "run-1/1", nil)), map[string]any{"marked": true})
	expect(t, a.traceparent(session), trace)
}

func TestATraceMarkWaitsForAClaimAndThenFindsItMoved(t *testing.T) {
	a := pendingAPI(t)
	session := a.gateSession("running")
	a.post("/gate/open", openBody(session, "run-1", "approval", "toolu_1", nil))
	turn := a.placed(session, "run-1/1")
	got := a.waits(func() answer {
		return a.do("POST", base+"/pending-input/trace", js(mark(session, turn, "run-1/1", nil)))
	}, "UPDATE ai_sessions SET turn_attempt = 'run-1/2' WHERE id = '"+session+"'")
	expect(t, got.raw, `{"marked":false}`)
	expect(t, a.traceparent(session), "")
}

func TestTheSweepListsStampsAndCancelsARunsEntriesOnce(t *testing.T) {
	a := pendingAPI(t)
	session := a.gateSession("running")
	request := rid(session, "run-1", "toolu_1")
	a.post("/gate/open", openBody(session, "run-1", "approval", "toolu_1", nil))
	runs := a.get("/pending-input/sweep/runs", "min_age_s", "0", "recheck_s", "600")["runs"]
	expect(t, runs, []any{map[string]any{"workflow_id": "session-" + session, "workflow_run_id": "run-1"}})
	checked := map[string]any{"workflow_id": "session-" + session, "run_id": "run-1"}
	expect(t, a.post("/pending-input/sweep/checked", checked), map[string]any{"done": true})
	expect(t, a.get("/pending-input/sweep/runs", "min_age_s", "0", "recheck_s", "600")["runs"], []any{})
	expect(t, a.statusOf(session), "waiting_approval")

	cancelled := a.post("/pending-input/orphans/cancel", map[string]any{"run": checked})
	expect(t, cancelled["removed"], 1.0)
	batches := cancelled["logged"].([]any)
	expect(t, len(batches), 1)
	expect(t, eventTypes(batches[0]), []string{"approval.resolved", "input.resolved", "session.status"})
	evs := batches[0].(map[string]any)["events"].([]any)
	expect(t, evs[0], map[string]any{"v": 1.0, "type": "approval.resolved", "sessionId": session,
		"id": request, "approved": false, "decision": "cancelled",
		"reason": "the session's workflow run has ended"})
	expect(t, evs[2].(map[string]any)["status"], "idle")
	expect(t, a.statusOf(session), "idle")
	expect(t, a.text(`SELECT outcome || ' ' || responder::text || ' ' || reason FROM ai_input_responses`),
		`cancelled {"id": "scadbuddy", "kind": "system", "label": "ScadBuddy"} the session's workflow run has ended`)
	expect(t, a.text(`SELECT concat_ws('|', kind, action, surface, outcome, input_hash, detail) FROM ai_audit`),
		"approval|cancelled|system|refused|"+hash64+"|print_output: the session's workflow run has ended (requested by You)")
	// A second sweep, or the workflow's own resolve, writes nothing more.
	expect(t, a.ok("POST", "/pending-input/orphans/cancel", js(map[string]any{"run": checked})).raw,
		`{"removed":0,"logged":[]}`)
	types := a.types(session)
	expect(t, types[len(types)-3:], []string{"approval.resolved", "input.resolved", "session.status"})
}

func TestASessionsEntriesAreCancelledWithTheirReason(t *testing.T) {
	a := pendingAPI(t)
	session := a.gateSession("running")
	a.post("/gate/open", openBody(session, "run-1", "answer", "toolu_1", nil))
	cancelled := a.post("/pending-input/orphans/cancel", map[string]any{"session_id": session, "reason": "stopped"})
	expect(t, cancelled["removed"], 1.0)
	first := cancelled["logged"].([]any)[0].(map[string]any)["events"].([]any)[0]
	expect(t, first, map[string]any{"v": 1.0, "type": "question.resolved", "sessionId": session,
		"id": rid(session, "run-1", "toolu_1"), "answered": false, "reason": "stopped"})
	// A question's cancel is not an approval's: no approval audit row.
	expect(t, a.count(`SELECT count(*) FROM ai_audit`), 0)
}

func TestAnApprovalCountsOnlyForItsOwnCall(t *testing.T) {
	a := pendingAPI(t)
	session := a.gateSession("running")
	request := rid(session, "run-1", "toolu_1")
	a.post("/gate/open", openBody(session, "run-1", "approval", "toolu_1", nil))
	expect(t, a.get("/pending-input/approval", "request_id", request, "input_hash", hash64),
		map[string]any{"responder": nil})
	a.resolveAs(request, "approved")
	expect(t, a.get("/pending-input/approval", "request_id", request, "input_hash", hash64),
		map[string]any{"responder": me})
	for _, c := range [][2]string{{request, strings.Repeat("b", 64)}, {"../../areas", hash64}, {"never", hash64}} {
		expect(t, a.get("/pending-input/approval", "request_id", c[0], "input_hash", c[1]),
			map[string]any{"responder": nil})
	}
}

func TestADenialIsNoApproval(t *testing.T) {
	a := pendingAPI(t)
	session := a.gateSession("running")
	request := rid(session, "run-1", "toolu_1")
	a.post("/gate/open", openBody(session, "run-1", "approval", "toolu_1", nil))
	a.resolveAs(request, "denied")
	expect(t, a.get("/pending-input/approval", "request_id", request, "input_hash", hash64),
		map[string]any{"responder": nil})
}
