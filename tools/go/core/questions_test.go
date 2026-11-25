package main

import (
	"net/http"
	"strings"
	"testing"
)

func questionsAPI(t *testing.T) *api { return newAPI(t, "questions") }

var doneCard = []any{map[string]any{"question": "Done", "header": "Done", "multiSelect": false,
	"options": []any{}}}

func doneBody(session, turn, attempt string, extra map[string]any) map[string]any {
	body := map[string]any{"id": uuid(), "session_id": session, "turn_id": turn,
		"attempt": attempt, "tool": "mcp__scadbuddy_questions__request_user_attention",
		"tool_use_id": "toolu_1", "questions": doneCard, "summary": "Rendered the box.",
		"unattended": false}
	for k, v := range extra {
		body[k] = v
	}
	return body
}

func TestQuestionsAreTheAgentsOnly(t *testing.T) {
	a := questionsAPI(t)
	session := a.gateSession("running")
	turn := a.placed(session, "run-1/1")
	for _, caller := range []string{"print", "projects", "render"} {
		token := a.credential(caller, "crud."+caller)
		got := a.do("POST", base+"/questions/done", js(doneBody(session, turn, "run-1/1", nil)),
			func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+token) })
		expect(t, got.status, 403)
	}
	none := a.do("POST", base+"/questions/pending", js(map[string]any{"kind": "browser", "id": "browser"}),
		func(r *http.Request) { r.Header.Del("Authorization") })
	expect(t, none.status, 401)
	expect(t, a.count(`SELECT count(*) FROM ai_questions`), 0)
}

func TestAQuestionBodyOutOfBoundsIsRefused(t *testing.T) {
	a := questionsAPI(t)
	session := a.gateSession("running")
	turn := a.placed(session, "run-1/1")
	for name, extra := range map[string]map[string]any{
		"an id":           {"id": "../x"},
		"a long summary":  {"summary": strings.Repeat("s", 64*1024+1)},
		"a large card":    {"questions": []any{map[string]any{"big": strings.Repeat("x", 1024*1024)}}},
		"an extra":        {"extra": 1},
		"a card no list":  {"questions": map[string]any{}},
		"a card of texts": {"questions": []any{"x"}},
		"no unattended":   {"unattended": nil},
	} {
		if got := a.do("POST", base+"/questions/done", js(doneBody(session, turn, "run-1/1", extra))); got.status != 422 {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	answers := make([]string, 17)
	for i := range answers {
		answers[i] = "a"
	}
	tooMany := map[string]any{"id": uuid(), "session_id": session, "answers": answers, "by": me}
	expect(t, a.status("POST", "/questions/answer", tooMany), 422)
	expect(t, a.status("POST", "/questions/attention-count", map[string]any{"own": true}), 422)
}

func TestADonePostLandsOnlyForThePlacementThatHoldsTheTurn(t *testing.T) {
	a := questionsAPI(t)
	session := a.gateSession("running")
	turn := a.placed(session, "run-1/1")
	none := map[string]any{"asked": false, "limited": nil, "logged": []any{}}
	for _, refused := range []map[string]any{
		doneBody(session, turn, "run-1/2", nil),
		doneBody(session, uuid(), "run-1/1", nil),
		doneBody(session, "not-a-uuid", "run-1/1", nil),
		doneBody("../x", turn, "run-1/1", nil),
	} {
		expect(t, a.post("/questions/done", refused), none)
	}
	a.sql(`UPDATE ai_sessions SET owner_kind = 'bearer' WHERE id = $1::text::uuid`, session)
	expect(t, a.post("/questions/done", doneBody(session, turn, "run-1/1", nil))["asked"], false)
	expect(t, a.count(`SELECT count(*) FROM ai_questions`), 0)
}

func TestADonePostReplacesTheLastAndIsCapped(t *testing.T) {
	a := questionsAPI(t)
	session := a.gateSession("running")
	turn := a.placed(session, "run-1/1")
	first := doneBody(session, turn, "run-1/1", nil)
	posted := a.post("/questions/done", first)
	expect(t, []any{posted["asked"], posted["limited"]}, []any{true, nil})
	batch := posted["logged"].([]any)[0].(map[string]any)
	expect(t, eventTypes(batch), []string{"question.asked", "input.requested"})
	events := batch["events"].([]any)
	expect(t, events[0], map[string]any{"v": 1.0, "type": "question.asked", "sessionId": session,
		"id": first["id"], "tool": "toolu_1", "questions": doneCard,
		"attention": map[string]any{"reason": "done", "summary": "Rendered the box."}})
	entry := events[1].(map[string]any)["entry"].(map[string]any)
	expect(t, []any{entry["id"], entry["prompt"], entry["expires_at"], entry["requested_by"]},
		[]any{"question:" + first["id"].(string), "Done", nil, nil})
	expect(t, entry["attention"], map[string]any{"reason": "done", "on_timeout": nil,
		"summary": "Rendered the box."})
	// The log holds the events as the Python API wrote them, key order included.
	expect(t, a.text(`SELECT event FROM ai_session_events WHERE session_id = $1::text::uuid
		ORDER BY seq LIMIT 1`, session),
		`{"v":1,"type":"question.asked","sessionId":"`+session+`","id":"`+first["id"].(string)+
			`","tool":"toolu_1","questions":[{"header":"Done","multiSelect":false,"options":[],`+
			`"question":"Done"}],"attention":{"reason":"done","summary":"Rendered the box."}}`)
	second := a.post("/questions/done", doneBody(session, turn, "run-1/1", nil))
	expect(t, eventTypes(second["logged"].([]any)[0]),
		[]string{"question.resolved", "input.resolved", "question.asked", "input.requested"})
	expect(t, a.text(`SELECT action || ' ' || outcome FROM ai_audit WHERE kind = 'question'`),
		"cancelled refused")
	a.post("/questions/done", doneBody(session, turn, "run-1/1", nil))
	limited := a.post("/questions/done", doneBody(session, turn, "run-1/1", nil))
	expect(t, limited["asked"], false)
	if !strings.Contains(limited["limited"].(string), "already posted 3") {
		t.Fatalf("limited %v", limited["limited"])
	}
	expect(t, a.count(`SELECT count(*) FROM ai_audit WHERE action = 'rate_limited'`), 1)
}

func TestASummaryIsListedAndDismissedOnce(t *testing.T) {
	a := questionsAPI(t)
	session := a.gateSession("running")
	turn := a.placed(session, "run-1/1")
	body := doneBody(session, turn, "run-1/1", nil)
	a.post("/questions/done", body)
	who := map[string]any{"kind": "browser", "id": "browser"}
	listed := a.post("/questions/pending", who)["questions"].([]any)
	expect(t, len(listed), 1)
	expect(t, []any{listed[0].(map[string]any)["id"], listed[0].(map[string]any)["summary"]},
		[]any{body["id"], "Rendered the box."})
	for only, found := range map[string]int{session: 1, uuid(): 0} {
		one := a.post("/questions/pending?session_id="+only, who)["questions"].([]any)
		expect(t, len(one), found)
	}
	expect(t, a.status("POST", "/questions/pending?session_id=nope", who), 422)
	entry := a.get("/questions/entry", "id", body["id"].(string))["entry"].(map[string]any)
	expect(t, []any{entry["outcome"], entry["session_id"]}, []any{nil, session})
	expect(t, a.get("/questions/entry", "id", "../x"), map[string]any{"entry": nil})
	dismiss := map[string]any{"id": body["id"], "session_id": session, "answers": []string{"ok"},
		"by": me, "input_hash": hash64}
	written := a.post("/questions/answer", dismiss)
	expect(t, eventTypes(written["logged"].([]any)[0]), []string{"question.resolved", "input.resolved"})
	expect(t, a.text(`SELECT input_hash || ' ' || detail FROM ai_audit WHERE action = 'answered'`),
		hash64+" mcp__scadbuddy_questions__request_user_attention question "+body["id"].(string)+": 1 answer")
	again := a.do("POST", base+"/questions/answer", js(dismiss))
	expect(t, []any{again.status, again.body["reason"]}, []any{409, "conflict"})
	a.post("/questions/done", doneBody(session, turn, "run-1/1", nil))
	blank := map[string]any{"id": listedID(a, who), "session_id": session, "answers": []string{"\u2003\x1f"},
		"by": me}
	invalid := a.do("POST", base+"/questions/answer", js(blank))
	expect(t, []any{invalid.status, invalid.body["reason"]}, []any{409, "invalid"})
	wrong := map[string]any{"session_id": session, "answers": []string{"a", "b"}, "by": me}
	for _, missing := range []string{uuid(), "../x"} {
		wrong["id"] = missing
		refused := a.do("POST", base+"/questions/answer", js(wrong))
		expect(t, []any{refused.status, refused.body["reason"]}, []any{409, "not_found"})
	}
	types := a.types(session)
	expect(t, types[len(types)-2:], []string{"question.asked", "input.requested"})
}

func listedID(a *api, who map[string]any) string {
	return a.post("/questions/pending", who)["questions"].([]any)[0].(map[string]any)["id"].(string)
}

func TestTheAttentionCountIsReadOnlyUnderItsLease(t *testing.T) {
	a := questionsAPI(t)
	session := a.gateSession("running")
	for _, call := range []string{"toolu_0", "toolu_1"} {
		a.sql(`INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder,
			tool, requested_at) VALUES ($1, $2::text::uuid, 'answer', 'timed_out', '{}'::jsonb,
			'wait_for_user', now())`, rid(session, "run-1", call), session)
	}
	a.sql(`INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder, tool,
		requested_at) VALUES ($1, $2::text::uuid, 'answer', 'cancelled', '{}'::jsonb,
		'wait_for_user', now())`, rid(session, "run-1", "tab:toolu_9"), session)
	me := uuid()
	for _, token := range []string{"../x", uuid(), me} {
		got := a.do("POST", base+"/questions/attention-count", js(map[string]any{"own": true, "lease_token": token}))
		expect(t, []any{got.status, got.body["reason"]}, []any{409, "lease-lost"})
	}
	a.sql(`INSERT INTO agent_leases (key, token, expires_at) VALUES ('attention-rate',
		$1::text::uuid, now() + interval '1 minute')`, me)
	expect(t, a.post("/questions/attention-count", map[string]any{"own": true, "lease_token": me}),
		map[string]any{"n": 2.0})
	expect(t, a.post("/questions/attention-count", map[string]any{"own": false, "lease_token": me}),
		map[string]any{"n": 1.0})
	a.sql(`UPDATE agent_leases SET expires_at = now() - interval '1 second'`)
	expect(t, a.status("POST", "/questions/attention-count", map[string]any{"own": true, "lease_token": me}), 409)
}

func TestATurnsDoneFactsAreRead(t *testing.T) {
	a := questionsAPI(t)
	session := a.gateSession("running")
	turn := a.placed(session, "run-1/1")
	since := a.text(`SELECT now()::text`)
	a.sql(`INSERT INTO ai_session_resources (session_id, tool, resource_type, resource_id, action)
		VALUES ($1::text::uuid, 'render_model', 'model', 'box', 'created')`, session)
	facts := a.post("/questions/done-facts", map[string]any{"session_id": session, "turn_id": turn, "since": since})
	expect(t, facts["windows"], []any{})
	touches := facts["touches"].([]any)
	expect(t, len(touches), 1)
	touch := touches[0].(map[string]any)
	expect(t, []any{touch["in_turn"], touch["in_away"], touch["before_asked"], touch["tool"]},
		[]any{true, false, false, "render_model"})
	missing := map[string]any{"session_id": "../x", "turn_id": turn, "since": since}
	expect(t, a.post("/questions/done-facts", missing), map[string]any{"windows": []any{}, "touches": []any{}})
}

func TestADoneFactsWindowIsAnAttentionNobodyAnswered(t *testing.T) {
	a := questionsAPI(t)
	session := a.gateSession("running")
	turn := a.placed(session, "run-1/1")
	since := a.text(`SELECT (now() - interval '1 minute')::text`)
	a.sql(`INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder, tool,
		requested_at, created_at) VALUES ($1, $2::text::uuid, 'answer', 'timed_out', '{}'::jsonb,
		'wait_for_user', now() - interval '30 seconds', now() - interval '20 seconds')`,
		rid(session, "run-1", "toolu_abcdefgh12345678"), session)
	a.sql(`INSERT INTO ai_session_resources (session_id, at, tool, resource_type, resource_id, action)
		VALUES ($1::text::uuid, now() - interval '25 seconds', 'render_model', 'model', 'box', 'created')`, session)
	facts := a.post("/questions/done-facts", map[string]any{"session_id": session, "turn_id": turn, "since": since})
	windows := facts["windows"].([]any)
	expect(t, len(windows), 1)
	window := windows[0].(map[string]any)
	expect(t, []any{window["id"], window["away_until"]}, []any{"12345678", nil})
	touch := facts["touches"].([]any)[0].(map[string]any)
	expect(t, []any{touch["in_turn"], touch["in_away"], touch["before_asked"]}, []any{true, true, false})
}

func TestAGateAnswerIsTheAnsweredResponse(t *testing.T) {
	a := questionsAPI(t)
	session := a.gateSession("running")
	request := rid(session, "run-1", "toolu_1")
	expect(t, a.get("/questions/gate-answer", "request_id", request), map[string]any{"answer": nil})
	a.sql(`INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, response,
		responder, tool, requested_at) VALUES ($1, $2::text::uuid, 'answer', 'answered',
		'{"answers":["red"]}'::jsonb, '{}'::jsonb, 'ask_user', now())`, request, session)
	got := a.get("/questions/gate-answer", "request_id", request)["answer"].(map[string]any)
	expect(t, []any{got["session_id"], got["tool"], got["response"]},
		[]any{session, "ask_user", map[string]any{"answers": []any{"red"}}})
}
