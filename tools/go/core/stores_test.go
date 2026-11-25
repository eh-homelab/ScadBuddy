package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"
	"testing"
)

// The agent's small stores (internal/stores), as backend/tests/api/test_crud_agent_stores.py
// checks them.

const storeAreas = "activity-kinds, browser-origins, bridge-tabs, pairings, mcp-tokens"

func sha(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}

func (a *api) count(query string, args ...any) int {
	a.t.Helper()
	var n int
	if err := a.pool.QueryRow(context.Background(), query, args...).Scan(&n); err != nil {
		a.t.Fatal(err)
	}
	return n
}

func (a *api) text(query string, args ...any) string {
	a.t.Helper()
	var s string
	if err := a.pool.QueryRow(context.Background(), query, args...).Scan(&s); err != nil {
		a.t.Fatal(err)
	}
	return s
}

func TestActivityKindsAreUpsertedAndTheRestRetired(t *testing.T) {
	a := newAPI(t, storeAreas)
	first := `{"kinds":[{"key":"agent.a","label":"A","facets":[]},
		{"key":"agent.b","label":"B","facets":[{"name":"n","type":"number","label":"N"}]}]}`
	expect(t, a.ok("POST", "/activity-kinds", first).raw, `{"ok":true}`)
	a.ok("POST", "/activity-kinds", `{"kinds":[{"key":"agent.a","label":"A2"}]}`)
	expect(t, a.text(`SELECT string_agg(key || ':' || label || ':' || (retired_at IS NOT NULL)::text, ',' ORDER BY key)
		FROM ai_activity_kinds`), "agent.a:A2:false,agent.b:B:true")
	a.ok("POST", "/activity-kinds", first)
	expect(t, a.count(`SELECT count(*) FROM ai_activity_kinds WHERE retired_at IS NULL`), 2)
	expect(t, a.text(`SELECT facets::text FROM ai_activity_kinds WHERE key = 'agent.b'`),
		`[{"name": "n", "type": "number", "label": "N"}]`)

	for _, kind := range []string{`{"key":"render.x","label":"x"}`, `{"key":"agent.x","label":"x","extra":1}`,
		`{"key":"agent.x","label":"x","facets":[{"name":"n","type":"blob","label":"N"}]}`} {
		got := a.do("POST", base+"/activity-kinds", `{"kinds":[`+kind+`]}`)
		expect(t, got.status, 422)
	}
}

func TestAnOriginApprovalNeverNarrowsAndARevokeWithdrawsIt(t *testing.T) {
	a := newAPI(t, storeAreas)
	session := a.session(nil)
	approve := func(scope string) answer {
		return a.ok("POST", "/browser-origins/approve",
			object("session_id", session, "origin", "https://a.example", "scope", scope))
	}
	expect(t, approve("session").raw, `{"done":true}`)
	approve("once")
	expect(t, a.ok("GET", "/browser-origins?session_id="+session, "").raw,
		`{"origins":[{"origin":"https://a.example","scope":"session"}]}`)
	listed := a.ok("GET", "/browser-origins/approved", "").body["approved"].([]any)
	expect(t, len(listed), 1)
	expect(t, listed[0].(map[string]any)["title"], "")
	expect(t, a.ok("POST", "/browser-origins/approve",
		object("session_id", uuid(), "origin", "https://a.example", "scope", "once")).raw, `{"done":false}`)
	expect(t, a.ok("POST", "/browser-origins/revoke",
		object("session_id", session, "origin", "https://a.example")).raw, `{"done":true}`)
	expect(t, a.ok("GET", "/browser-origins?session_id="+session, "").raw, `{"origins":[]}`)

	bad := a.do("POST", base+"/browser-origins/approve",
		object("session_id", session, "origin", "https://u@a.example", "scope", "once"))
	expect(t, bad.status, 422)
	expect(t, a.do("GET", base+"/browser-origins", "").status, 422)
}

func TestASessionTabWriteHoldsToTheTabItSaw(t *testing.T) {
	a := newAPI(t, storeAreas)
	session := a.session(nil)
	write := func(body string) string { return a.ok("POST", "/session-tabs", body).raw }
	expect(t, write(object("session_id", session, "tab_id", "t1", "conditional", true)), `{"written":true}`)
	expect(t, write(object("session_id", session, "tab_id", "t2", "conditional", true)), `{"written":false}`)
	expect(t, write(object("session_id", session, "tab_id", "t2", "conditional", true, "seen", "t0")),
		`{"written":false}`)
	expect(t, write(object("session_id", session, "tab_id", "t2", "conditional", true, "seen", "t1")),
		`{"written":true}`)
	expect(t, write(object("session_id", session, "tab_id", "t3")), `{"written":true}`)
	expect(t, a.ok("GET", "/session-tabs?session_id="+session, "").raw, `{"tab_id":"t3"}`)
	expect(t, a.ok("GET", "/session-tabs?session_id="+uuid(), "").raw, `{"tab_id":null}`)
	gone := a.do("POST", base+"/session-tabs", object("session_id", uuid(), "tab_id", "t"))
	expect(t, gone.status, 404)
	expect(t, gone.body["detail"], "no such session")
}

func TestATabOwnerIsClaimedRefreshedAndReleased(t *testing.T) {
	a := newAPI(t, storeAreas)
	a.ok("POST", "/tab-owners/claim", object("tab_id", "t1", "queue", "q1"))
	owner := a.ok("GET", "/tab-owners?tab_id=t1", "").body["owner"].(map[string]any)
	expect(t, owner["queue"], "q1")
	// Another queue's refresh takes nothing; its release removes nothing.
	a.ok("POST", "/tab-owners/refresh", `{"tab_ids":["t1","t2"],"queue":"q2"}`)
	expect(t, a.ok("GET", "/tab-owners?tab_id=t1", "").body["owner"].(map[string]any)["queue"], "q1")
	expect(t, a.ok("GET", "/tab-owners?tab_id=t2", "").body["owner"].(map[string]any)["queue"], "q2")
	a.ok("POST", "/tab-owners/release", object("tab_id", "t1", "queue", "q2"))
	// A release before the claim's time keeps it.
	a.ok("POST", "/tab-owners/release", object("tab_id", "t1", "queue", "q1",
		"claimed_before", "2000-01-01 00:00:00+00"))
	expect(t, a.ok("GET", "/tab-owners?tab_id=t1", "").body["owner"] != nil, true)
	a.ok("POST", "/tab-owners/release", object("tab_id", "t1", "queue", "q1",
		"claimed_before", owner["claimed_at"]))
	expect(t, a.ok("GET", "/tab-owners?tab_id=t1", "").raw, `{"owner":null}`)
	bad := a.do("POST", base+"/tab-owners/release", object("tab_id", "t2", "queue", "q2", "claimed_before", "soon"))
	expect(t, bad.status, 422)
	expect(t, bad.body["detail"], "claimed_before is not a timestamp")
	a.ok("POST", "/tab-owners/release-all", object("queue", "q2"))
	expect(t, a.count(`SELECT count(*) FROM ai_tab_owners`), 0)
}

func TestATabImageIsItsBytesAndIsTakenOnce(t *testing.T) {
	a := newAPI(t, storeAreas)
	png := "\x89PNG\r\n\x1a\n" + strings.Repeat("\x00", 32)
	name := sha(png) + ".png"
	put := func(name, body, kind string) answer {
		return a.do("PUT", base+"/tab-images/"+name, body, func(r *http.Request) { r.Header.Set("Content-Type", kind) })
	}
	expect(t, put(name, png, "image/png").status, 204)
	expect(t, put(name, png, "image/jpeg").status, 422)
	expect(t, put(sha("other")+".png", png, "image/png").status, 422)
	expect(t, put("nope.png", png, "image/png").body["errors"].([]any)[0].(map[string]any)["loc"],
		[]any{"path", "name"})
	expect(t, put(name, strings.Repeat("x", 5*1024*1024/4*3+1), "image/png").status, 413)
	took := a.do("POST", base+"/tab-images/"+name+"/take", "")
	expect(t, took.status, 200)
	expect(t, took.header.Get("Content-Type"), "image/png")
	expect(t, took.raw, png)
	expect(t, a.do("POST", base+"/tab-images/"+name+"/take", "").status, 204)
}

func TestAPairingIsAskedAcceptedAndEnded(t *testing.T) {
	a := newAPI(t, storeAreas)
	a.sql(`INSERT INTO ai_mcp_tokens (id, name, tier, token_hash) VALUES
		('11111111-2222-3333-4444-555555555555', 'laptop', 'read', $1)`, sha("t"))
	ask := func(principal string) answer {
		return a.do("POST", base+"/pairings", object("principal_kind", "bearer", "principal_id", principal,
			"fallback_label", "fallback", "code_hash", sha("code")))
	}
	made := ask("token:{11111111-2222-3333-4444-555555555555}")
	expect(t, made.status, 201)
	expect(t, made.body["label"], "MCP token “laptop”")
	id := made.body["id"].(string)
	expect(t, ask("token:nope").body["label"], "fallback")
	ask("token:nope")
	ask("token:nope")
	refused := ask("token:nope")
	expect(t, refused.status, 409)
	expect(t, refused.body["reason"], "pairing-refused")
	expect(t, len(a.ok("GET", "/pairings/pending", "").body["pairings"].([]any)), 4)

	accept := func(code string) map[string]any {
		return a.ok("POST", "/pairings/"+id+"/accept", object("code_hash", sha(code), "tab_id", "tab")).body
	}
	wrong := accept("wrong")
	expect(t, wrong["result"], "wrong_code")
	expect(t, wrong["attempts_left"], float64(4))
	ok := accept("code")
	expect(t, ok["result"], "ok")
	expect(t, ok["principal_id"], "token:{11111111-2222-3333-4444-555555555555}")
	expect(t, accept("code")["result"], "gone")
	paired := a.ok("GET", "/pairings/paired-tab?principal_kind=bearer&principal_id="+
		"token:%7B11111111-2222-3333-4444-555555555555%7D", "").body["pairing"].(map[string]any)
	expect(t, paired["tab_id"], "tab")
	expect(t, len(a.ok("POST", "/pairings/paired-with", `{"tab_ids":["tab","other"]}`).body["pairings"].([]any)), 1)
	expect(t, a.ok("POST", "/pairings/paired-with", `{"tab_ids":[]}`).raw, `{"pairings":[]}`)
	expect(t, a.ok("POST", "/pairings/"+id+"/end", `{"tab_id":"other"}`).raw, `{"done":false}`)
	expect(t, a.ok("POST", "/pairings/"+id+"/end", `{"tab_id":"tab"}`).raw, `{"done":true}`)

	// Five wrong codes deny a request.
	other := a.do("POST", base+"/pairings", object("principal_kind", "oidc", "principal_id", "p",
		"fallback_label", "f", "code_hash", sha("code"))).body["id"].(string)
	for range 4 {
		a.ok("POST", "/pairings/"+other+"/accept", object("code_hash", sha("x"), "tab_id", "t"))
	}
	expect(t, a.ok("POST", "/pairings/"+other+"/accept", object("code_hash", sha("x"), "tab_id", "t")).body["result"],
		"gone")
	expect(t, a.text(`SELECT status FROM ai_browser_pairings WHERE id = $1`, other), "denied")
	expect(t, a.ok("POST", "/pairings/"+other+"/deny", "").raw, `{"done":false}`)
}

func TestAnMCPTokenIsMintedVerifiedAndRevoked(t *testing.T) {
	a := newAPI(t, storeAreas)
	mint := func(body string) answer { return a.do("POST", base+"/mcp-tokens", body) }
	made := mint(object("name", "laptop", "tier", "outward", "token_hash", sha("t"), "approval_grant", true,
		"expires_at", "2099-01-02T03:04:05.5Z"))
	expect(t, made.status, 201)
	token := made.body["token"].(map[string]any)
	expect(t, token["expires_at"], "2099-01-02T03:04:05.500000Z")
	expect(t, token["last_used_at"], nil)
	id := token["id"].(string)
	expect(t, mint(object("name", "again", "tier", "read", "token_hash", sha("t"))).status, 409)
	expect(t, mint(object("name", "x", "tier", "read", "token_hash", sha("u"), "approval_grant", true)).status, 422)
	expect(t, mint(object("name", "x", "tier", "read", "token_hash", sha("u"), "expires_at", "soon")).status, 422)
	// Postgres takes these; pydantic does not, and infinity is no time to write back.
	for _, special := range []string{"infinity", "-Infinity", " now ", "today", "tomorrow", "yesterday", "epoch"} {
		refused := mint(object("name", "x", "tier", "read", "token_hash", sha("u"), "expires_at", special))
		expect(t, refused.status, 422)
		expect(t, refused.body["errors"].([]any)[0].(map[string]any)["loc"], []any{"body", "expires_at"})
	}

	verified := a.ok("POST", "/mcp-tokens/verify", object("token_hash", sha("t"))).raw
	expect(t, verified, `{"token":{"id":"`+id+`","tier":"outward"}}`)
	expect(t, a.ok("GET", "/mcp-tokens/"+id+"/live", "").raw, `{"tier":"outward","approval_grant":true}`)
	expect(t, len(a.ok("GET", "/mcp-tokens", "").body["tokens"].([]any)), 1)
	expect(t, a.ok("POST", "/mcp-tokens/"+id+"/revoke", "").raw, `{"done":true}`)
	expect(t, a.ok("POST", "/mcp-tokens/"+id+"/revoke", "").raw, `{"done":false}`)
	expect(t, a.ok("POST", "/mcp-tokens/verify", object("token_hash", sha("t"))).raw, `{"token":null}`)
	expect(t, a.ok("GET", "/mcp-tokens/"+id+"/live", "").raw, `{"tier":null,"approval_grant":false}`)
}

func TestAReadAreaAnswersItsGetsOnly(t *testing.T) {
	a := newAPI(t, "mcp-tokens:read")
	expect(t, a.ok("GET", "/mcp-tokens", "").raw, `{"tokens":[]}`)
	expect(t, a.do("POST", base+"/mcp-tokens/verify", object("token_hash", sha("t"))).status, http.StatusTeapot)
}
