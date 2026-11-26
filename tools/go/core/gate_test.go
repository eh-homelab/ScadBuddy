package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

// The gate's and the audit log's routes, as backend/tests/crud/test_crud_agent_gate.py (removed in #7667) and
// test_crud_agent_audit.py (removed in #7667) check them.

var me = map[string]any{"kind": "browser", "id": "browser", "label": "You"}
var system = map[string]any{"kind": "system", "id": "scadbuddy", "label": "ScadBuddy"}

const hash64 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

func gateAPI(t *testing.T) *api { return newAPI(t, "gate, audit") }

// gateSession is a session in status.
func (a *api) gateSession(status string) string {
	a.t.Helper()
	id := uuid()
	a.sql(`INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,
		creator_id, status, max_turns, budget_usd) VALUES ($1::text::uuid, 'chat', 'browser',
		'browser', 'You', 'browser', 'browser', $2, 10, 1)`, id, status)
	return id
}

func rid(session, run, toolUse string) string {
	return "durable:" + session + ":" + run + ":" + toolUse
}

func openBody(session, run, kind, toolUse string, extra map[string]any) map[string]any {
	body := map[string]any{
		"request_id": rid(session, run, toolUse), "session_id": session,
		"workflow_id": "session-" + session, "workflow_run_id": run, "kind": kind,
		"tool_use_id": toolUse, "requested_by": me,
		"expires_at": time.Now().Add(10 * time.Minute).UTC().Format(time.RFC3339Nano),
	}
	if kind == "approval" {
		body["tool"], body["summary"], body["input_hash"] = "print_output", `{"output":"box"}`, hash64
		body["prompt"], body["responders"] = "", []string{"browser", "grant"}
	} else {
		body["tool"], body["summary"], body["input_hash"] = "ask_user", "", nil
		body["prompt"], body["responders"] = "Which colour?", []string{"browser"}
		body["questions"] = []any{map[string]any{"question": "Which colour?", "header": "C",
			"multiSelect": false, "options": []any{}}}
	}
	for k, v := range extra {
		body[k] = v
	}
	return body
}

// record is the entry's record as the agent's turn writes it (GateRecord).
func record(body map[string]any) map[string]any {
	r := map[string]any{"id": body["request_id"], "kind": body["kind"], "inputHash": body["input_hash"],
		"requestedBy":    map[string]any{"kind": "bearer", "id": "agent-1"},
		"sessionOwner":   map[string]any{"kind": "browser", "id": "browser"},
		"sessionCreator": map[string]any{"kind": "browser", "id": "browser"}}
	if body["kind"] == "answer" {
		r["questions"] = []any{map[string]any{"question": "Which colour?", "multiSelect": false}}
	}
	return r
}

func js(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return string(b)
}

func (a *api) post(path string, body any) map[string]any {
	a.t.Helper()
	return a.ok("POST", path, js(body)).body
}

func eventTypes(logged any) []string {
	var out []string
	for _, e := range logged.(map[string]any)["events"].([]any) {
		out = append(out, e.(map[string]any)["type"].(string))
	}
	return out
}

func TestTheGateIsTheAgentsOnly(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	for _, caller := range []string{"print", "projects", "render"} {
		token := a.credential(caller, "crud."+caller)
		got := a.do("POST", base+"/gate/open", js(openBody(s, "run-1", "approval", "toolu_1", nil)),
			func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+token) })
		expect(t, got.status, 403)
	}
	expect(t, a.count(`SELECT count(*) FROM ai_pending_input`), 0)
}

func TestAGateBodyOutOfBoundsIsRefusedBeforeAnySQL(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	for name, extra := range map[string]map[string]any{
		"summary":    {"summary": strings.Repeat("x", 64*1024+1)},
		"session":    {"session_id": "../not-a-session"},
		"request id": {"request_id": strings.Repeat("d", 2049)},
		"responders": {"responders": []string{"browser", "grant", "browser"}},
		"record":     {"gate": map[string]any{"big": strings.Repeat("x", 1024*1024)}},
		"extra":      {"extra": 1},
		"no expiry":  {"expires_at": "soon"},
	} {
		if got := a.do("POST", base+"/gate/open", js(openBody(s, "run-1", "approval", "toolu_1", extra))); got.status != 422 {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	resolve := map[string]any{"request_id": rid(s, "run-1", "toolu_1"), "outcome": "answered", "responder": me}
	resolve["response"] = map[string]any{"kind": "answer", "text": strings.Repeat("x", 2*529024+1)}
	expect(t, a.do("POST", base+"/gate/resolve", js(resolve)).status, 422)
	delete(resolve, "response")
	resolve["response_hash"] = "../x"
	expect(t, a.do("POST", base+"/gate/resolve", js(resolve)).status, 422)
	expect(t, a.count(`SELECT count(*) FROM ai_pending_input`), 0)
}

func TestAnOpenWritesTheEntryItsEventsAndTheStatusOnce(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	body := openBody(s, "run-1", "approval", "toolu_1", nil)
	first := a.post("/gate/open", body)
	expect(t, first["opened"], true)
	batch := first["logged"].([]any)[0].(map[string]any)
	expect(t, batch["session_id"], s)
	expect(t, eventTypes(batch), []string{"approval.required", "input.requested", "session.status"})
	expect(t, batch["seqs"], []any{1.0, 2.0, 3.0})
	want := `{"v":1,"type":"approval.required","sessionId":"` + s + `","id":"` + rid(s, "run-1", "toolu_1") +
		`","tool":"toolu_1","summary":"print_output {\"output\":\"box\"}","risk":"outward"}`
	expect(t, a.text(`SELECT event FROM ai_session_events WHERE seq = 1`), want)
	entry := a.text(`SELECT event FROM ai_session_events WHERE seq = 2`)
	if !strings.Contains(entry, `"requested_by":{"id":"browser","kind":"browser","label":"You"},"responders":["browser","grant"]`) {
		t.Fatalf("the entry is not as Python writes it: %s", entry)
	}
	expect(t, a.text(`SELECT status FROM ai_sessions`), "waiting_approval")
	expect(t, a.ok("POST", "/gate/open", js(body)).raw, `{"opened":false,"logged":[]}`)
	expect(t, a.types(s), []string{"approval.required", "input.requested", "session.status"})
}

func TestAResolveWritesOnceWithItsAuditAndNotify(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	id := rid(s, "run-1", "toolu_1")
	a.post("/gate/open", openBody(s, "run-1", "approval", "toolu_1", nil))
	conn, err := a.pool.Acquire(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Release()
	if _, err := conn.Exec(context.Background(), "LISTEN scadbuddy_input"); err != nil {
		t.Fatal(err)
	}
	body := map[string]any{"request_id": id, "outcome": "approved", "responder": me}
	resolved := a.post("/gate/resolve", body)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	note, err := conn.Conn().WaitForNotification(ctx)
	if err != nil {
		t.Fatal(err)
	}
	expect(t, note.Payload, id)
	expect(t, resolved["resolved"], true)
	expect(t, eventTypes(resolved["logged"].([]any)[0]), []string{"approval.resolved", "input.resolved", "session.status"})
	expect(t, a.text(`SELECT status FROM ai_sessions`), "running")
	expect(t, a.text(`SELECT outcome || ' ' || responder::text || ' ' || tool FROM ai_input_responses`),
		`approved {"id": "browser", "kind": "browser", "label": "You"} print_output`)
	expect(t, a.text(`SELECT kind || ' ' || action || ' ' || outcome || ' ' || tool_use_id FROM ai_audit`),
		"approval approved ok toolu_1")
	expect(t, a.ok("POST", "/gate/resolve", js(body)).raw, `{"resolved":false,"logged":[]}`)
	expect(t, a.post("/gate/open", openBody(s, "run-1", "approval", "toolu_1", nil))["opened"], false)
}

func TestTwoResolvesOfOneRequestWriteOnce(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	a.post("/gate/open", openBody(s, "run-1", "approval", "toolu_1", nil))
	var wg sync.WaitGroup
	results := make(chan bool, 2)
	for _, outcome := range []string{"approved", "denied"} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			got := a.do("POST", base+"/gate/resolve", js(map[string]any{
				"request_id": rid(s, "run-1", "toolu_1"), "outcome": outcome, "responder": me}))
			results <- got.body["resolved"] == true
		}()
	}
	wg.Wait()
	close(results)
	won := 0
	for r := range results {
		if r {
			won++
		}
	}
	expect(t, won, 1)
	expect(t, a.count(`SELECT count(*) FROM ai_input_responses`), 1)
	expect(t, a.count(`SELECT count(*) FROM ai_audit`), 1)
}

func TestAnOpenOfANewRunCancelsTheOldRunsEntries(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	a.post("/gate/open", openBody(s, "run-1", "approval", "toolu_1", nil))
	opened := a.post("/gate/open", openBody(s, "run-2", "approval", "toolu_2", nil))
	expect(t, opened["opened"], true)
	logged := opened["logged"].([]any)
	reset, fresh := logged[0].(map[string]any), logged[1].(map[string]any)
	expect(t, eventTypes(reset), []string{"approval.resolved", "input.resolved", "session.status"})
	expect(t, reset["events"].([]any)[1].(map[string]any)["reason"], "reset")
	seqs := reset["seqs"].([]any)
	expect(t, fresh["seqs"].([]any)[0], seqs[len(seqs)-1].(float64)+1)
	expect(t, a.text(`SELECT request_id || ' ' || outcome || ' ' || reason FROM ai_input_responses`),
		rid(s, "run-1", "toolu_1")+" cancelled reset")
}

func TestAnUnansweredQuestionIsAuditedAndATimerSaysWhy(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	opened := a.post("/gate/open", openBody(s, "run-1", "answer", "toolu_1",
		map[string]any{"attention": map[string]any{"reason": "done", "on_timeout": nil}}))
	asked := opened["logged"].([]any)[0].(map[string]any)["events"].([]any)[0].(map[string]any)
	attention := asked["attention"].(map[string]any)
	if attention["onTimeout"] != nil || attention["expiresAt"] == "" {
		t.Fatal(attention)
	}
	resolved := a.post("/gate/resolve", map[string]any{"request_id": rid(s, "run-1", "toolu_1"),
		"outcome": "timed_out", "responder": system})
	first := resolved["logged"].([]any)[0].(map[string]any)["events"].([]any)[0].(map[string]any)
	expect(t, first["reason"], "nobody replied in time (on_timeout: proceed)")
	expect(t, a.text(`SELECT kind || ' ' || action || ' ' || outcome FROM ai_audit`), "question timed_out refused")
}

func TestATabWaitEndedByTheTabSaysReconnected(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	a.post("/gate/open", openBody(s, "run-1", "answer", "toolu_1", nil))
	resolved := a.post("/gate/resolve", map[string]any{"request_id": rid(s, "run-1", "toolu_1"),
		"outcome": "cancelled", "responder": system, "response": map[string]any{"reconnected": true},
		"reason": "tab back"})
	first := resolved["logged"].([]any)[0].(map[string]any)["events"].([]any)[0].(map[string]any)
	expect(t, first["reconnected"], true)
}

var approve = map[string]any{"kind": "approval", "decision": "approve"}

func decideBody(request string, response any, role string, extra map[string]any) map[string]any {
	body := map[string]any{"request_id": request, "response": response, "responder": me, "role": role}
	for k, v := range extra {
		body[k] = v
	}
	return body
}

func (a *api) openRecorded(s, kind string) string {
	body := openBody(s, "run-1", kind, "toolu_1", nil)
	body["gate"] = record(body)
	a.post("/gate/open", body)
	return body["request_id"].(string)
}

func TestADecideValidatesAndWritesOnce(t *testing.T) {
	a := gateAPI(t)
	id := a.openRecorded(a.gateSession("running"), "approval")
	decided := a.post("/gate/decide", decideBody(id, approve, "browser", nil))
	expect(t, decided["decided"], map[string]any{"kind": "approval", "outcome": "approved"})
	expect(t, eventTypes(decided["logged"].([]any)[0])[:2], []string{"approval.resolved", "input.resolved"})
	expect(t, a.text(`SELECT outcome || ' ' || response::text || ' ' || response_hash FROM ai_input_responses`),
		`approved {"decision": "approve"} 9e837029aee951b4967877d68cf687a78aaa55a120465cea04be3c7d7f2101eb`)
	expect(t, a.ok("GET", "/gate/response?request_id="+id, "").raw,
		`{"record":{"outcome":"approved","response":{"decision":"approve"},"responder":{"id":"browser","kind":"browser","label":"You"},"reason":null}}`)
	again := a.post("/gate/decide", decideBody(id, approve, "browser", nil))
	expect(t, again["refused"].(map[string]any)["code"], "stale")
}

func TestADecideRefusesWhatTheValidatorRefuses(t *testing.T) {
	a := gateAPI(t)
	id := a.openRecorded(a.gateSession("running"), "approval")
	own := map[string]any{"kind": "bearer", "id": "agent-1", "label": "Agent"}
	for _, c := range []struct {
		body map[string]any
		code string
	}{
		{decideBody(id, approve, "run_grant", nil), "forbidden"},
		{decideBody(id, approve, "parent", nil), "forbidden"},
		{decideBody(id, approve, "grant", map[string]any{"responder": own}), "self"},
		{decideBody(id, map[string]any{"kind": "approval", "decision": "maybe"}, "browser", nil), "malformed"},
		{decideBody(id, map[string]any{"kind": "approval", "decision": "approve", "input_hash": strings.Repeat("b", 64)}, "browser", nil), "input_mismatch"},
		{decideBody(id, map[string]any{"kind": "answer", "text": "hi"}, "browser", nil), "malformed"},
	} {
		got := a.post("/gate/decide", c.body)
		if got["refused"].(map[string]any)["code"] != c.code {
			t.Errorf("%v: %v", c.body, got)
		}
	}
	expect(t, a.count(`SELECT count(*) FROM ai_input_responses`), 0)
	a.sql(`UPDATE ai_pending_input SET expires_at = now() - interval '1 second'`)
	expect(t, a.post("/gate/decide", decideBody(id, approve, "browser", nil))["refused"].(map[string]any)["code"], "resolving")
}

func TestAResponseTooLargeInBytesIsTheValidatorsRefusal(t *testing.T) {
	a := gateAPI(t)
	id := a.openRecorded(a.gateSession("running"), "answer")
	emoji := strings.Repeat("\U0001F600", 529024/4+1)
	got := a.post("/gate/decide", decideBody(id, map[string]any{"kind": "answer", "answers": []string{emoji}}, "browser", nil))
	expect(t, got["refused"].(map[string]any)["code"], "too_large")
}

func TestADecideKeepsTheAgentsHashAndAWorkflowsEntryIsTheWorkflows(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	id := a.openRecorded(s, "approval")
	digest := strings.Repeat("c", 64)
	expect(t, a.do("POST", base+"/gate/decide", js(decideBody(id, approve, "browser", map[string]any{"response_hash": "../x"}))).status, 422)
	a.post("/gate/decide", decideBody(id, approve, "browser", map[string]any{"response_hash": digest}))
	expect(t, a.text(`SELECT response_hash FROM ai_input_responses`), digest)

	other := a.gateSession("running")
	a.post("/gate/open", openBody(other, "run-1", "approval", "toolu_1", nil))
	expect(t, a.post("/gate/decide", decideBody(rid(other, "run-1", "toolu_1"), approve, "browser", nil))["workflow"], true)
	for _, bad := range []string{"../../areas", "durable:not-a-uuid", "x"} {
		expect(t, a.post("/gate/decide", decideBody(bad, approve, "browser", nil))["refused"].(map[string]any)["code"], "stale")
		expect(t, a.ok("GET", "/gate/response?request_id="+bad, "").raw, `{"record":null}`)
	}
}

func TestADecideWaitsForTheAgentToFindTheRunLive(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	id := a.openRecorded(s, "approval")
	first := a.post("/gate/decide", decideBody(id, approve, "browser", map[string]any{"check_live": true}))
	expect(t, first["live_check"], map[string]any{"workflow_id": "session-" + s, "run_id": "run-1"})
	other := a.post("/gate/decide", decideBody(id, approve, "browser", map[string]any{"check_live": true, "live_run": "run-0"}))
	if other["live_check"] == nil {
		t.Fatal(other)
	}
	live := a.post("/gate/decide", decideBody(id, approve, "browser", map[string]any{"check_live": true, "live_run": "run-1"}))
	expect(t, live["decided"].(map[string]any)["outcome"], "approved")
}

func TestAnAnswerIsDecidedIntoItsAnswers(t *testing.T) {
	a := gateAPI(t)
	id := a.openRecorded(a.gateSession("running"), "answer")
	decided := a.post("/gate/decide", decideBody(id, map[string]any{"kind": "answer",
		"answers": map[string]any{"Which colour?": "Red"}}, "browser", nil))
	expect(t, decided["decided"], map[string]any{"kind": "answer", "outcome": "answered"})
	resolved := decided["logged"].([]any)[0].(map[string]any)["events"].([]any)[0].(map[string]any)
	expect(t, resolved["answers"], []any{"Red"})
	expect(t, resolved["by"], me)
}

func (a *api) grantRow(grant string) {
	a.sql(`INSERT INTO flow_grants (id, run_id, status, spec, requested_by, expires_at)
		VALUES ($1, 'run-' || $1, 'approved', '{}'::jsonb, '{}'::jsonb, now() + interval '1 hour')`, grant)
}

func (a *api) use(grant, request, session string) {
	a.sql(`INSERT INTO flow_grant_uses (request_id, grant_id, run_id, fn, input_hash, rule_index,
		session_id) VALUES ($1, $2, 'run-' || $2, 'tool', $3, 0, $4)`, request, grant, hash64, session)
}

func grantBody(session, request, grant, digest string) map[string]any {
	return map[string]any{"request_id": request, "session_id": session, "tool": "print_output",
		"summary": "box", "input_hash": digest, "grant_id": grant, "requested_by": me}
}

func TestAGrantApprovesOnlyWhileTheBackendsRecordHolds(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	id := rid(s, "run-1", "toolu_1")
	a.grantRow("g1")
	a.use("g1", id, s)
	other := a.gateSession("running")
	for _, body := range []map[string]any{
		grantBody(s, id, "g1", strings.Repeat("d", 64)),
		grantBody(s, rid(s, "run-1", "toolu_9"), "g1", hash64),
		grantBody(other, id, "g1", hash64),
		grantBody(s, id, "g2", hash64),
		grantBody(s, id, "../g1", hash64),
	} {
		expect(t, a.ok("POST", "/gate/grant", js(body)).raw, `{"granted":false}`)
	}
	expect(t, a.count(`SELECT count(*) FROM ai_input_responses`), 0)
	expect(t, a.ok("POST", "/gate/grant", js(grantBody(s, id, "g1", hash64))).raw, `{"granted":true}`)
	expect(t, a.ok("POST", "/gate/grant", js(grantBody(s, id, "g1", hash64))).raw, `{"granted":true}`)
	expect(t, a.text(`SELECT outcome || ' ' || responder::text FROM ai_input_responses`),
		`approved {"id": "g1", "kind": "run_grant", "label": "Run grant"}`)
	expect(t, a.count(`SELECT count(*) FROM ai_audit`), 1)
	for _, change := range []string{
		"UPDATE flow_grants SET revoked_at = now()",
		"UPDATE flow_grants SET status = 'declined'",
	} {
		next := rid(s, "run-1", "toolu_"+change[24:27])
		a.use("g1", next, s)
		a.sql(change)
		expect(t, a.ok("POST", "/gate/grant", js(grantBody(s, next, "g1", hash64))).raw, `{"granted":false}`)
		a.sql(`UPDATE flow_grants SET revoked_at = NULL, status = 'approved'`)
	}
}

func TestARevocationWaitsForAGrantUnderItsShareLock(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	id := rid(s, "run-1", "toolu_1")
	a.grantRow("g1")
	a.use("g1", id, s)
	got := a.waits(func() answer {
		return a.do("POST", base+"/gate/grant", js(grantBody(s, id, "g1", hash64)))
	}, "UPDATE flow_grants SET revoked_at = now() WHERE id = 'g1'")
	expect(t, got.raw, `{"granted":false}`)
}

func TestADenyWritesOnce(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	body := map[string]any{"request_id": rid(s, "run-1", "toolu_1"), "session_id": s,
		"run_id": "flow-run-1", "tool": "print_output", "summary": "box", "input_hash": hash64,
		"reason": "outside", "requested_by": me}
	expect(t, a.ok("POST", "/gate/deny", js(body)).raw, `{"denied":true}`)
	expect(t, a.ok("POST", "/gate/deny", js(body)).raw, `{"denied":false}`)
	expect(t, a.text(`SELECT outcome || ' ' || reason || ' ' || (hops->0->>'actor') FROM ai_input_responses`),
		"denied outside flow")
	expect(t, a.text(`SELECT action || ' ' || outcome FROM ai_audit`), "denied denied")
}

// -- the audit log ---------------------------------------------------------------------------

func auditEntry(extra map[string]any) map[string]any {
	e := map[string]any{"kind": "tool_call", "action": "print_output", "surface": "harness",
		"actor": me, "outcome": "ok"}
	for k, v := range extra {
		e[k] = v
	}
	return e
}

func TestAnAuditBodyOutOfBoundsIsRefused(t *testing.T) {
	a := gateAPI(t)
	for _, extra := range []map[string]any{
		{"detail": strings.Repeat("d", 501)},
		{"input_summary": strings.Repeat("s", 1001)},
		{"actor": map[string]any{"kind": strings.Repeat("k", 51), "id": "browser", "label": "You"}},
		{"kind": "nonsense"},
		{"extra": 1},
	} {
		expect(t, a.do("POST", base+"/audit/record", js(auditEntry(extra))).status, 422)
	}
	expect(t, a.do("POST", base+"/audit/list", `{"before":-1}`).status, 422)
	expect(t, a.count(`SELECT count(*) FROM ai_audit`), 0)
}

func TestAnAuditRowIsWrittenWithItsDurableApprover(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	id := rid(s, "run-1", "toolu_1")
	a.sql(`INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder)
		VALUES ($1, $2::text::uuid, 'approval', 'approved', '{"kind": "browser", "id": "browser", "label": "You"}')`, id, s)
	expect(t, a.ok("POST", "/audit/record", js(auditEntry(map[string]any{"session_id": s,
		"turn_id": "not-a-uuid", "tool_use_id": "toolu_1", "tier": "outward", "input_hash": hash64,
		"request_id": id, "started_at": "2026-11-15T10:00:00.000Z",
		"finished_at": "2026-11-15T10:00:01.250Z", "cost_usd": -0.0000001}))).raw, `{"done":true}`)
	expect(t, a.text(`SELECT concat_ws(' ', session_id, coalesce(turn_id::text, '-'), approved_by_kind,
		approved_by_label, request_id, duration_ms, cost_usd) FROM ai_audit`),
		s+" - browser You "+id+" 1250 0")
	a.post("/audit/record", auditEntry(map[string]any{"kind": "approval", "action": "approved", "request_id": id}))
	expect(t, a.count(`SELECT count(*) FROM ai_audit WHERE kind = 'approval' AND approved_by_kind IS NULL`), 1)
}

func TestAnAuditPageIsNewestFirstWithItsCursor(t *testing.T) {
	a := gateAPI(t)
	s := a.gateSession("running")
	for _, n := range []string{"0", "1", "2"} {
		a.post("/audit/record", auditEntry(map[string]any{"action": "tool_" + n, "session_id": s}))
	}
	a.post("/audit/record", auditEntry(map[string]any{"kind": "approval", "action": "approved"}))
	actions := func(page map[string]any) (out []string) {
		for _, r := range page["rows"].([]any) {
			out = append(out, r.(map[string]any)["action"].(string))
		}
		return out
	}
	first := a.post("/audit/list", map[string]any{"kind": "tool_call", "limit": 2})
	expect(t, actions(first), []string{"tool_2", "tool_1"})
	rows := first["rows"].([]any)
	expect(t, first["next"], rows[len(rows)-1].(map[string]any)["id"])
	var before int
	_ = json.Unmarshal([]byte(first["next"].(string)), &before)
	rest := a.post("/audit/list", map[string]any{"kind": "tool_call", "before": before})
	expect(t, actions(rest), []string{"tool_0"})
	expect(t, rest["next"], nil)
	expect(t, len(a.post("/audit/list", map[string]any{"session_id": s})["rows"].([]any)), 3)
	for _, missing := range []string{uuid(), "../../areas", "not-a-uuid"} {
		expect(t, a.ok("POST", "/audit/list", js(map[string]any{"session_id": missing})).raw, `{"rows":[],"next":null}`)
	}
}

func TestThePruneDeletesOnlyPastTheRetention(t *testing.T) {
	a := gateAPI(t)
	a.post("/audit/record", auditEntry(map[string]any{"action": "new"}))
	a.sql(`INSERT INTO ai_audit (at, kind, action, surface, principal_kind, principal_id,
		principal_label, outcome) VALUES (now() - interval '40 days', 'tool_call', 'old',
		'harness', 'browser', 'browser', 'You', 'ok')`)
	expect(t, a.ok("POST", "/audit/prune", `{"days":30}`).raw, `{"deleted":1}`)
	expect(t, a.text(`SELECT string_agg(action, ',') FROM ai_audit`), "new")
	expect(t, a.ok("POST", "/audit/prune", `{"days":-5}`).raw, `{"deleted":0}`)
}

// TestAGrantApprovesOnlyWhileItsWholeChainIsInForce: the grant it inherits from, and the
// standing grant that seeded that one, are checked as the API's own check of the call does
// (flows/store.py chain_in_force, #5685).
func TestAGrantApprovesOnlyWhileItsWholeChainIsInForce(t *testing.T) {
	for broken, granted := range map[string]bool{"none": true, "parent revoked": false,
		"standing revoked": false, "standing expired": false} {
		t.Run(broken, func(t *testing.T) {
			a := gateAPI(t)
			s := a.gateSession("running")
			id := rid(s, "run-1", "toolu_1")
			a.sql(`INSERT INTO workflow_definitions (id, name, version, created_by) VALUES ('def-1', 'gate', 1, '{}')
				ON CONFLICT DO NOTHING`)
			a.sql(`INSERT INTO flow_triggers (id, name, definition_id, kind, event, created_by)
				VALUES ('trig-1', 'gate', 'def-1', 'event', '{}', '{}')`)
			a.sql(`INSERT INTO flow_trigger_grants (id, trigger_id, status, spec, requested_by,
				revoked_at, expires_at, approval_id) VALUES ('standing', 'trig-1', 'approved', '{}', '{}',
				CASE WHEN $1 THEN now() END,
				now() + CASE WHEN $2 THEN interval '-1 second' ELSE interval '1 hour' END, 'a1')`,
				broken == "standing revoked", broken == "standing expired")
			a.sql(`INSERT INTO flow_grants (id, run_id, status, spec, requested_by, revoked_at,
				expires_at, standing_grant_id, standing_approval_id) VALUES ('parent', 'run-parent',
				'approved', '{}', '{}', CASE WHEN $1 THEN now() END, now() + interval '1 hour',
				'standing', 'a1')`, broken == "parent revoked")
			a.grantRow("g1")
			a.sql(`UPDATE flow_grants SET parent_grant_id = 'parent' WHERE id = 'g1'`)
			a.use("g1", id, s)
			expect(t, a.ok("POST", "/gate/grant", js(grantBody(s, id, "g1", hash64))).raw,
				fmt.Sprintf(`{"granted":%t}`, granted))
			want := 0
			if granted {
				want = 1
			}
			expect(t, a.count(`SELECT count(*) FROM ai_input_responses`), want)
		})
	}
}
