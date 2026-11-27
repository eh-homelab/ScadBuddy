package main

import (
	"net/http"
	"strings"
	"sync"
	"testing"
)

// The classic approvals' routes, as backend/tests/crud/test_crud_agent_approvals.py checks them.

var asker = map[string]any{"kind": "bearer", "id": "agent-1", "label": "Agent one"}
var askerWho = map[string]any{"kind": "bearer", "id": "agent-1"}

func newApproval(session any, extra map[string]any) map[string]any {
	body := map[string]any{"id": uuid(), "session_id": session, "turn_id": nil,
		"tool_use_id": "toolu_1", "tool": "print_output", "input_summary": `{"output":"box"}`,
		"input_hash": hash64, "tier": "outward", "requested_by": asker, "expiry_s": 600}
	for k, v := range extra {
		body[k] = v
	}
	return body
}

func settleBody(id, decision string, extra map[string]any) map[string]any {
	body := map[string]any{"id": id, "decision": decision, "expiry_s": 300}
	if decision == "approved" || decision == "denied" {
		body["by"] = me
	}
	for k, v := range extra {
		body[k] = v
	}
	return body
}

func ids(rows any) []string {
	out := []string{}
	for _, r := range rows.([]any) {
		out = append(out, r.(map[string]any)["id"].(string))
	}
	return out
}

func TestApprovalsAreTheAgentsOnly(t *testing.T) {
	a := pendingAPI(t)
	for _, caller := range []string{"print", "projects", "render"} {
		token := a.credential(caller, "crud."+caller)
		got := a.do("POST", base+"/approvals/create", js(newApproval(nil, nil)), func(r *http.Request) {
			r.Header.Set("Authorization", "Bearer "+token)
		})
		expect(t, got.status, 403)
	}
	got := a.do("POST", base+"/approvals/settle", js(settleBody("x", "approved", nil)),
		func(r *http.Request) { r.Header.Del("Authorization") })
	expect(t, got.status, 401)
	expect(t, a.count(`SELECT count(*) FROM ai_approvals`), 0)
}

func TestAnApprovalBodyOutOfBoundsIsRefused(t *testing.T) {
	a := pendingAPI(t)
	for _, body := range []map[string]any{
		newApproval(nil, map[string]any{"id": "../x"}),
		newApproval("../not-a-session", nil),
		newApproval(nil, map[string]any{"input_summary": strings.Repeat("s", 64*1024+1)}),
		newApproval(nil, map[string]any{"extra": 1}),
		newApproval(nil, map[string]any{"tier": "loud"}),
	} {
		expect(t, a.status("POST", "/approvals/create", body), 422)
	}
	// Only the system expires or cancels: a person's decision names who made it.
	expect(t, a.status("POST", "/approvals/settle", map[string]any{"id": uuid(), "decision": "approved"}), 422)
	expect(t, a.status("POST", "/approvals/due", map[string]any{"limit": 501}), 422)
	expect(t, a.count(`SELECT count(*) FROM ai_approvals`), 0)
}

func TestASessionApprovalIsMadeWithItsEvents(t *testing.T) {
	a := pendingAPI(t)
	session := a.gateSession("running")
	made := a.post("/approvals/create", newApproval(session, nil))
	approval := made["approval"].(map[string]any)
	id := approval["id"].(string)
	batches := made["logged"].([]any)
	expect(t, len(batches), 1)
	expect(t, eventTypes(batches[0]), []string{"approval.required", "input.requested"})
	evs := batches[0].(map[string]any)["events"].([]any)
	expect(t, evs[0], map[string]any{"v": 1.0, "type": "approval.required", "sessionId": session,
		"id": id, "tool": "toolu_1", "summary": `print_output {"output":"box"}`, "risk": "outward"})
	expect(t, evs[1].(map[string]any)["entry"].(map[string]any)["id"], "approval:"+id)
	expect(t, approval["decision"], nil)
	expect(t, approval["due"], false)
	expect(t, a.get("/approvals/row", "id", id)["approval"].(map[string]any)["id"], id)
	expect(t, ids(a.post("/approvals/list", map[string]any{"session_id": session, "pending": true})["approvals"]), []string{id})
}

// A session's row is no one's to decide through this list (#7323): it takes no place under the
// cap, so leftover rows never hide an MCP prepare.
func TestThePendingListHoldsMCPPreparesOnly(t *testing.T) {
	a := pendingAPI(t)
	a.post("/approvals/create", newApproval(a.gateSession("running"), nil))
	body := map[string]any{"approval": newApproval(nil, nil), "per_principal": 5, "total": 50, "evict_reason": "full"}
	prepare := a.post("/approvals/create-prepared", body)["approval"].(map[string]any)["id"].(string)
	listed := a.post("/approvals/list", map[string]any{"session_id": nil, "pending": true})["approvals"]
	expect(t, ids(listed), []string{prepare})
}

func TestADecisionIsWrittenOnceWithItsAuditRow(t *testing.T) {
	a := pendingAPI(t)
	session := a.gateSession("running")
	id := a.post("/approvals/create", newApproval(session, nil))["approval"].(map[string]any)["id"].(string)
	settled := a.post("/approvals/settle", settleBody(id, "approved", map[string]any{"traceparent": trace}))
	row := settled["approval"].(map[string]any)
	expect(t, row["decision"], "approved")
	expect(t, row["decided_by_label"], "You")
	expect(t, row["decision_traceparent"], trace)
	if row["usable_until"] == nil {
		t.Fatal("no usable_until")
	}
	batch := settled["logged"].([]any)[0]
	expect(t, eventTypes(batch), []string{"approval.resolved", "input.resolved"})
	expect(t, batch.(map[string]any)["events"].([]any)[0].(map[string]any)["by"], me)
	expect(t, a.text(`SELECT concat_ws('|', kind, action, surface, principal_label, outcome,
		approval_id::text, detail) FROM ai_audit`),
		"approval|approved|http|You|ok|"+id+"|print_output (requested by Agent one)")
	// The first decision wins: a second writes nothing.
	expect(t, a.ok("POST", "/approvals/settle", js(settleBody(id, "denied", nil))).raw, `{"approval":null,"logged":[]}`)
	expect(t, a.count(`SELECT count(*) FROM ai_audit`), 1)
	expect(t, a.types(session), []string{"approval.required", "input.requested", "approval.resolved", "input.resolved"})
}

func TestTwoDecisionsOfOneApprovalWriteOnce(t *testing.T) {
	a := pendingAPI(t)
	id := a.post("/approvals/create", newApproval(nil, nil))["approval"].(map[string]any)["id"].(string)
	var wg sync.WaitGroup
	var mu sync.Mutex
	won := 0
	for i := 0; i < 6; i++ {
		decision := []string{"approved", "denied"}[i%2]
		wg.Add(1)
		go func() {
			defer wg.Done()
			got := a.do("POST", base+"/approvals/settle", js(settleBody(id, decision, nil)))
			mu.Lock()
			defer mu.Unlock()
			if got.status != 200 {
				t.Errorf("%d %s", got.status, got.raw)
			}
			if got.body["approval"] != nil {
				won++
			}
		}()
	}
	wg.Wait()
	expect(t, won, 1)
	expect(t, a.count(`SELECT count(*) FROM ai_audit`), 1)
}

func TestAnExpiredApprovalIsNotApproved(t *testing.T) {
	a := pendingAPI(t)
	id := a.post("/approvals/create", newApproval(nil, nil))["approval"].(map[string]any)["id"].(string)
	a.sql(`UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = $1::text::uuid`, id)
	expect(t, a.post("/approvals/settle", settleBody(id, "approved", nil))["approval"], nil)
	expect(t, a.post("/approvals/due", map[string]any{"limit": 10}), map[string]any{"ids": []any{id}})
	expect(t, a.post("/approvals/due", map[string]any{"limit": 10, "tried": []string{id, "../x"}}),
		map[string]any{"ids": []any{}})
	expired := a.post("/approvals/settle", settleBody(id, "expired", map[string]any{"reason": "no decision"}))
	expect(t, expired["approval"].(map[string]any)["decision"], "expired")
	expect(t, a.text(`SELECT concat_ws('|', surface, principal_kind, outcome) FROM ai_audit`), "system|system|refused")
}

func TestAPreparedApprovalIsUsedOnceByItsAsker(t *testing.T) {
	a := pendingAPI(t)
	body := map[string]any{"approval": newApproval(nil, nil), "per_principal": 5, "total": 50, "evict_reason": "full"}
	id := a.post("/approvals/create-prepared", body)["approval"].(map[string]any)["id"].(string)
	expect(t, ids(a.post("/approvals/prepared", askerWho)["approvals"]), []string{id})
	use := map[string]any{"id": id, "by": askerWho, "input_hash": hash64}
	none := map[string]any{"approval": nil}
	// Not approved yet: nothing to use.
	expect(t, a.post("/approvals/consume", use), none)
	a.post("/approvals/settle", settleBody(id, "approved", nil))
	for _, change := range []map[string]any{
		{"input_hash": strings.Repeat("b", 64)},
		{"by": map[string]any{"kind": "bearer", "id": "agent-2"}},
		{"id": "../x"},
	} {
		refused := map[string]any{}
		for k, v := range use {
			refused[k] = v
		}
		for k, v := range change {
			refused[k] = v
		}
		expect(t, a.post("/approvals/consume", refused), none)
	}
	if a.post("/approvals/consume", use)["approval"].(map[string]any)["consumed_at"] == nil {
		t.Fatal("not consumed")
	}
	expect(t, a.post("/approvals/consume", use), none)
}

func TestAPrepareKeepsItsBounds(t *testing.T) {
	a := pendingAPI(t)
	prepare := func(by map[string]any, traces []string) map[string]any {
		if traces == nil {
			traces = []string{}
		}
		return a.post("/approvals/create-prepared", map[string]any{
			"approval": newApproval(nil, map[string]any{"requested_by": by}), "per_principal": 2,
			"total": 3, "evict_reason": "too many", "evict_traceparents": traces})
	}
	first := prepare(asker, nil)["approval"].(map[string]any)
	prepare(asker, nil)
	third := prepare(asker, []string{trace})
	// The asker's oldest is cancelled to keep it under its bound, with the span's context.
	evicted := third["evicted"].([]any)
	expect(t, ids(evicted), []string{first["id"].(string)})
	expect(t, evicted[0].(map[string]any)["decision_traceparent"], trace)
	expect(t, evicted[0].(map[string]any)["decision"], "cancelled")
	other := map[string]any{"kind": "bearer", "id": "agent-2", "label": "Agent two"}
	prepare(other, nil)
	// At the total, nobody's row is evicted and nothing is written.
	third2 := map[string]any{"kind": "bearer", "id": "agent-3", "label": "Agent two"}
	expect(t, prepare(third2, nil), map[string]any{"approval": nil, "evicted": []any{}})
}

func TestAMalformedApprovalIDNamesNothing(t *testing.T) {
	a := pendingAPI(t)
	for _, missing := range []string{uuid(), "../../areas", "not-a-uuid"} {
		expect(t, a.get("/approvals/row", "id", missing), map[string]any{"approval": nil})
		expect(t, a.ok("POST", "/approvals/settle", js(settleBody(missing, "approved", nil))).raw,
			`{"approval":null,"logged":[]}`)
		expect(t, a.post("/approvals/list", map[string]any{"session_id": missing}), map[string]any{"approvals": []any{}})
	}
}
