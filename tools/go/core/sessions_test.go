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

// The sessions areas, as backend/tests/crud/test_crud_agent_sessions.py and the others check
// Python's.

const sessionAreas = "sessions, session-resources, session-order-costs, publish, session-reads," +
	" session-sweeps, forget-rows, migrations, auth-sessions, session-edits, forget," +
	" workspace-blob-refs, events, flows, session-events"

func agentAPI(t *testing.T) *api { return newAPI(t, sessionAreas) }

var you = map[string]any{"kind": "browser", "id": "browser", "label": "You"}
var tokenOwner = map[string]any{"kind": "bearer", "id": "token:42", "label": "CI token"}

func newSession(extra ...any) map[string]any {
	s := map[string]any{
		"id": uuid(), "origin": "chat", "owner": you, "title": "A chat", "tags": []string{"t"},
		"scope": map[string]any{"model": "m"}, "runtime": "unified", "max_turns": 20, "budget_usd": 5,
	}
	for i := 0; i < len(extra); i += 2 {
		s[extra[i].(string)] = extra[i+1]
	}
	return s
}

func (a *api) value(query string, args ...any) any {
	a.t.Helper()
	var v any
	if err := a.pool.QueryRow(context.Background(), query, args...).Scan(&v); err != nil {
		a.t.Fatal(err)
	}
	return v
}

func (a *api) create(s map[string]any) map[string]any {
	a.t.Helper()
	return a.ok("POST", "/sessions", js(s)).body["session"].(map[string]any)
}

func TestASessionIsInsertedAndReadBack(t *testing.T) {
	a := agentAPI(t)
	s := newSession("browser", map[string]any{"viewport": map[string]any{"width": 1280, "height": 800}})
	row := a.create(s)
	expect(t, row["id"], s["id"])
	expect(t, row["status"], "idle")
	expect(t, row["creator_id"], "browser")
	expect(t, row["budget_usd"], 5.0)
	expect(t, row["pool_cost_usd"], 0.0)
	expect(t, row["tags"], []any{"t"})
	expect(t, row["scope"], map[string]any{"model": "m"})
	expect(t, row["browser"], map[string]any{"viewport": map[string]any{"width": 1280.0, "height": 800.0}})
	expect(t, row["turn_active"], false)
	expect(t, row["background_tools"], true)
	if !strings.HasSuffix(row["claimed_at"].(string), "Z") || len(row["created_at"].(string)) != 24 {
		t.Fatalf("timestamps: %v %v", row["claimed_at"], row["created_at"])
	}
	got := a.ok("GET", "/sessions/"+s["id"].(string), "").body["session"].(map[string]any)
	expect(t, got["title"], "A chat")
	expect(t, a.ok("GET", "/sessions/"+uuid(), "").raw, `{"session":null}`)

	again := a.do("POST", base+"/sessions", js(s))
	expect(t, again.status, 409)
	expect(t, again.body["reason"], "exists")

	child := newSession("parent_id", s["id"])
	expect(t, a.create(child)["browser"], row["browser"])
	gone := a.do("POST", base+"/sessions", js(newSession("parent_id", uuid())))
	expect(t, gone.body["reason"], "parent_gone")
}

// An agent pod of an earlier release may send null during a roll (#7323).
func TestASessionThatNamesNoRuntimeIsUnified(t *testing.T) {
	a := agentAPI(t)
	absent := newSession()
	delete(absent, "runtime")
	expect(t, a.create(absent)["runtime"], "unified")
	expect(t, a.create(newSession("runtime", nil))["runtime"], "unified")
}

func TestABadSessionIsRefusedByField(t *testing.T) {
	a := agentAPI(t)
	for name, c := range map[string]struct {
		body map[string]any
		loc  string
	}{
		"an unknown origin": {newSession("origin", "web"), "body.origin"},
		"no budget":         {newSession("budget_usd", 0), "body.budget_usd"},
		"a budget past 100": {newSession("budget_usd", 100.5), "body.budget_usd"},
		"a float viewport": {newSession("browser", map[string]any{
			"viewport": map[string]any{"width": 1280.0, "height": 100}}), "body.browser.viewport.height"},
		"an extra member":   {newSession("extra", 1), "body.extra"},
		"a bad parent":      {newSession("parent_id", "X"), "body.parent_id"},
		"a background true": {newSession("background_tools", true), "body.background_tools"},
		"a null fork cut": {newSession("fork_of", map[string]any{"parent_id": uuid(), "cut": nil}),
			"body.fork_of.cut"},
		"a large scope": {newSession("scope", map[string]any{"x": strings.Repeat("é", 11000)}),
			"body.scope"},
	} {
		got := a.do("POST", base+"/sessions", js(c.body))
		if got.status != 422 || !strings.Contains(got.raw, `"loc":["`+strings.ReplaceAll(c.loc, ".", `","`)+`"]`) {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	expect(t, a.value(`SELECT count(*) FROM ai_sessions`), int64(0))
}

func TestStartsAreCountedUnderTheOwnersLock(t *testing.T) {
	a := agentAPI(t)
	limit := map[string]any{"max": 2, "window_ms": 60000}
	var wg sync.WaitGroup
	statuses := make([]int, 5)
	for i := range statuses {
		wg.Add(1)
		go func() {
			defer wg.Done()
			statuses[i] = a.do("POST", base+"/sessions", js(newSession("limit", limit))).status
		}()
	}
	wg.Wait()
	created := 0
	for _, s := range statuses {
		if s == 200 {
			created++
		} else if s != 429 {
			t.Fatalf("a start answered %d", s)
		}
	}
	expect(t, created, 2)
	count := a.ok("GET", "/session-starts?owner_kind=browser&owner_id=browser&window_ms=60000", "")
	expect(t, count.raw, `{"count":2}`)
	expect(t, a.do("GET", base+"/session-starts?owner_kind=x&owner_id=browser&window_ms=1", "").status, 422)
}

func TestAForkOfAForgottenParentIsRefused(t *testing.T) {
	a := agentAPI(t)
	parent := a.create(newSession())["id"].(string)
	a.sql(`INSERT INTO ai_forgotten_subjects (subject) VALUES ($1)`, "session-"+parent)
	got := a.do("POST", base+"/sessions", js(newSession("fork_of", map[string]any{"parent_id": parent, "cut": 0})))
	expect(t, got.status, 404)
	expect(t, got.body["reason"], "forgotten")
}

func TestTheListFiltersByPrincipalAndFile(t *testing.T) {
	a := agentAPI(t)
	mine := a.create(newSession("owner", tokenOwner, "title", "100% mine"))["id"].(string)
	a.create(newSession())
	a.sql(`INSERT INTO ai_session_files (workspace, session_id, path, media_type, data, sha256, size)
		VALUES ($1, $2, 'a/b.STL', 'model/stl', 'x', $3, 1)`, "session:"+mine, mine, strings.Repeat("0", 64))
	list := func(body map[string]any) []string {
		var ids []string
		for _, s := range a.ok("POST", "/sessions/list", js(body)).body["sessions"].([]any) {
			ids = append(ids, s.(map[string]any)["id"].(string))
		}
		return ids
	}
	expect(t, len(list(map[string]any{"principal": nil})), 2)
	expect(t, list(map[string]any{"principal": map[string]any{"kind": "bearer", "id": "token:42"}}), []string{mine})
	expect(t, list(map[string]any{"principal": nil, "search": "100%"}), []string{mine})
	expect(t, list(map[string]any{"principal": nil, "search": "10_"}), []string(nil))
	expect(t, list(map[string]any{"principal": nil, "with_file": true, "accept": []string{".stl"}}), []string{mine})
	expect(t, list(map[string]any{"principal": nil, "with_file": true, "accept": []string{"image/*"}}), []string(nil))
	expect(t, a.do("POST", base+"/sessions/list", `{}`).status, 422)
}

func TestAHandoffAppliesOnlyAsRead(t *testing.T) {
	a := agentAPI(t)
	id := a.create(newSession())["id"].(string)
	who := map[string]any{"kind": "browser", "id": "browser"}
	to := map[string]any{"kind": "bearer", "id": "token:42"}
	expect(t, a.ok("POST", "/sessions/"+id+"/offer", js(map[string]any{
		"owner": to, "to": tokenOwner, "ttl_ms": 60000})).raw, `{"changed":false}`)
	expect(t, a.ok("POST", "/sessions/"+id+"/offer", js(map[string]any{
		"owner": who, "to": tokenOwner, "ttl_ms": 60000})).raw, `{"changed":true}`)
	row := a.ok("GET", "/sessions/"+id, "").body["session"].(map[string]any)
	expect(t, row["offer_label"], "CI token")
	expect(t, a.ok("POST", "/sessions/"+id+"/owner", js(map[string]any{
		"owner": who, "to": tokenOwner, "accepting": true})).raw, `{"changed":true}`)
	row = a.ok("GET", "/sessions/"+id, "").body["session"].(map[string]any)
	expect(t, row["owner_id"], "token:42")
	expect(t, row["offer_kind"], nil)
	expect(t, a.ok("POST", "/sessions/"+id+"/offer/cancel", js(map[string]any{"to": to})).raw, `{"changed":false}`)
}

func TestABudgetIsRaisedOnTheLineageRoot(t *testing.T) {
	a := agentAPI(t)
	root := a.create(newSession())["id"].(string)
	child := a.create(newSession("budget_of", root))["id"].(string)
	by := map[string]any{"kind": "browser", "id": "anyone"}
	expect(t, a.ok("POST", "/sessions/"+child+"/budget", js(map[string]any{
		"by": by, "add_usd": 2.5, "max_usd": 10})).raw, `{"budget_usd":7.5}`)
	expect(t, a.ok("GET", "/sessions/"+root, "").body["session"].(map[string]any)["budget_usd"], 7.5)
	expect(t, a.ok("POST", "/sessions/"+child+"/budget", js(map[string]any{
		"by": by, "add_usd": 5, "max_usd": 10})).raw, `{"budget_usd":null}`)
}

func TestOnlyAFreshForkIsDiscarded(t *testing.T) {
	a := agentAPI(t)
	fresh := a.create(newSession())["id"].(string)
	a.sql(`INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES ($1, '\x00', 'k1')`, "session-"+fresh)
	logged := a.create(newSession())["id"].(string)
	a.append(logged, `{"type":"x"}`)
	expect(t, a.ok("POST", "/sessions/"+fresh+"/discard", "").raw, `{"discarded":true}`)
	expect(t, a.ok("POST", "/sessions/"+logged+"/discard", "").raw, `{"discarded":false}`)
	expect(t, a.value(`SELECT count(*) FROM ai_payload_keys`), int64(0))
	expect(t, a.value(`SELECT count(*) FROM ai_sessions`), int64(1))
}

func TestResourcesAreRecordedAndListed(t *testing.T) {
	a := agentAPI(t)
	id := a.create(newSession())["id"].(string)
	row := map[string]any{"tool": "render", "resource_type": "model", "resource_id": "m",
		"action": "created", "model_slug": "m", "before_id": nil, "after_id": nil}
	expect(t, a.ok("POST", "/sessions/"+id+"/resources", js(map[string]any{"rows": []any{row}})).raw,
		`{"recorded":true}`)
	expect(t, a.ok("POST", "/sessions/"+uuid()+"/resources", js(map[string]any{"rows": []any{row}})).raw,
		`{"recorded":false}`)
	got := a.ok("GET", "/sessions/"+id+"/resources", "").body["resources"].([]any)
	expect(t, got[0].(map[string]any)["tool"], "render")
	row["resource_type"] = "unclassified"
	bad := a.do("POST", base+"/sessions/"+id+"/resources", js(map[string]any{"rows": []any{row}}))
	expect(t, bad.status, 422)
	delete(row, "after_id")
	bad = a.do("POST", base+"/sessions/"+id+"/resources", js(map[string]any{"rows": []any{row}}))
	expect(t, strings.Contains(bad.raw, `["body","rows","0","after_id"]`), true)
}

func TestTheOrderCostsRunUnderTheLease(t *testing.T) {
	a := agentAPI(t)
	id := a.create(newSession())["id"].(string)
	a.ok("POST", "/sessions/"+id+"/order-costs", js(map[string]any{"order_id": "o", "order_item_id": "i"}))
	token := uuid()
	lost := a.do("POST", base+"/sessions/"+id+"/order-costs/report", js(map[string]any{"token": token}))
	expect(t, lost.status, 409)
	expect(t, lost.body["reason"], "lease-lost")
	a.sql(`INSERT INTO agent_leases (key, token, expires_at) VALUES ($1, $2, now() + interval '1 minute')`,
		"order-cost:"+id, token)
	report := a.ok("POST", "/sessions/"+id+"/order-costs/report",
		js(map[string]any{"token": strings.ToUpper(token)})).body["report"].(map[string]any)
	expect(t, report["principal"], "browser")
	expect(t, len(report["pending"].([]any)), 1)
	a.ok("POST", "/sessions/"+id+"/order-costs/attempt", js(map[string]any{
		"token": token, "order_id": "o", "items": []string{"i"}, "amount_usd": 0.5}))
	a.ok("POST", "/sessions/"+id+"/order-costs/settle", js(map[string]any{
		"token": token, "order_id": "o", "outcome": "reported"}))
	expect(t, a.value(`SELECT reported_usd FROM ai_session_order_costs`), 0.5)
	expect(t, a.ok("POST", "/sessions/"+id+"/order-costs/report", js(map[string]any{"token": token})).raw,
		`{"report":null}`)
	expect(t, a.ok("POST", "/order-costs/sweep", `{"limit":10}`).raw, `{"sessions":[]}`)
}

func listen(t *testing.T, a *api, channel string) func() []string {
	t.Helper()
	conn, err := a.pool.Acquire(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(conn.Release)
	if _, err := conn.Exec(context.Background(), "LISTEN "+channel); err != nil {
		t.Fatal(err)
	}
	return func() []string {
		var heard []string
		for {
			ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
			n, err := conn.Conn().WaitForNotification(ctx)
			cancel()
			if err != nil {
				return heard
			}
			heard = append(heard, n.Payload)
		}
	}
}

func TestAPublishNotifiesItsPayload(t *testing.T) {
	a := agentAPI(t)
	heard := listen(t, a, "scadbuddy_events")
	event := `{"id":"` + strings.Repeat("a", 32) + `","at":"é","kind":"session.done","session_id":"` +
		uuid() + `","seq":3.0,"replica":"r🙂"}`
	nullSeq := strings.Replace(event, `"seq":3.0`, `"seq":null`, 1)
	expect(t, a.do("POST", base+"/publish/session-event", nullSeq).status, 422)
	expect(t, a.ok("POST", "/publish/session-event", event).raw, `{"sent":true}`)
	var want map[string]any
	_ = json.Unmarshal([]byte(event), &want)
	got := heard()
	expect(t, len(got), 1)
	if !strings.Contains(got[0], `"at":"\u00e9"`) || !strings.Contains(got[0], `"seq":3,`) ||
		!strings.HasSuffix(got[0], `"replica":"r\ud83d\ude42"}`) || strings.Contains(got[0], "status") {
		t.Fatal(got[0])
	}
	evict := listen(t, a, "scadbuddy_warm_evict")
	a.ok("POST", "/publish/warm-evict", `{"target":"*"}`)
	expect(t, evict(), []string{"*"})
	expect(t, a.do("POST", base+"/publish/warm-evict", `{"target":"x"}`).status, 422)
}

func TestTheOpenCallsAndTheReasoningAreTheSessionsOwn(t *testing.T) {
	a := agentAPI(t)
	id := a.create(newSession())["id"].(string)
	a.append(id, userTurn("t1", "hi"), delta("x"),
		`{"type":"tool.call","id":"c1","name":"a"}`, `{"type":"tool.call","id":"c2","name":"b"}`,
		`{"type":"tool.result","id":"c1"}`, `{"type":"tool.call","id":"c1","name":"c"}`)
	expect(t, a.ok("GET", "/sessions/"+id+"/open-calls?turn_id=t1", "").raw,
		`{"calls":[{"id":"c2","name":"b"},{"id":"c1","name":"c"}]}`)
	a.append(id, `{"type":"assistant.text.delta","messageId":"m","delta":"why"}`,
		`{"type":"tool.call","id":"c3","name":"d"}`)
	expect(t, a.ok("GET", "/sessions/"+id+"/reasoning?tool_use_id=c3", "").raw,
		`{"logged":true,"deltas":[{"message":null,"delta":null},{"message":"m","delta":"why"}]}`)
	expect(t, a.ok("GET", "/sessions/"+id+"/reasoning?tool_use_id=c3&scan=1", "").raw,
		`{"logged":true,"deltas":[]}`)
	expect(t, a.ok("GET", "/sessions/"+id+"/reasoning?tool_use_id=nope", "").raw,
		`{"logged":false,"deltas":[]}`)
}

func TestAForgottenSessionsRowsGoWithItsWarmEvict(t *testing.T) {
	a := agentAPI(t)
	id := a.create(newSession())["id"].(string)
	expect(t, a.ok("POST", "/sessions/live", js(map[string]any{"ids": []string{id, uuid()}})).raw,
		`{"ids":["`+id+`"]}`)
	refused := a.do("POST", base+"/sessions/"+id+"/forget-rows", "")
	expect(t, refused.body["reason"], "not-forgotten")
	a.sql(`INSERT INTO ai_forgotten_subjects (subject) VALUES ($1)`, "session-"+id)
	evict := listen(t, a, "scadbuddy_warm_evict")
	expect(t, a.ok("POST", "/sessions/"+id+"/forget-rows", "").raw, `{"rows":1}`)
	expect(t, evict(), []string{id})
	expect(t, a.ok("POST", "/sessions/live", `{"ids":[]}`).raw, `{"ids":[]}`)
}

func TestTheLedgerAndTheAuthSessionLookup(t *testing.T) {
	a := agentAPI(t)
	var id string
	if err := a.pool.QueryRow(context.Background(), `SELECT id FROM ai_migrations ORDER BY id LIMIT 1`).Scan(&id); err != nil {
		t.Fatal(err)
	}
	got := a.ok("POST", "/migrations/ledger", js(map[string]any{"ids": []string{id, "20990101T0000Z_none"}}))
	expect(t, got.body["found"], true)
	expect(t, len(got.body["rows"].([]any)), 1)
	expect(t, a.do("POST", base+"/migrations/ledger", `{"ids":["x"]}`).status, 422)
	expect(t, a.ok("POST", "/auth-sessions/lookup", js(map[string]any{"id_hash": strings.Repeat("0", 64)})).raw,
		`{"session":null}`)
}

func TestAnEditIsRefusedOrLogsItsEvents(t *testing.T) {
	a := agentAPI(t)
	id := a.create(newSession())["id"].(string)
	who := map[string]any{"kind": "browser", "id": "browser"}
	untold := a.do("POST", base+"/sessions/"+id+"/edit", js(map[string]any{"principal": who, "done": true}))
	expect(t, untold.status, 409)
	expect(t, untold.body["reason"], "untold")
	gone := a.do("POST", base+"/sessions/"+uuid()+"/edit", js(map[string]any{"principal": who}))
	expect(t, gone.status, 404)
	a.sql(`INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions,
		kind, attention_reason, summary) VALUES ($1, $2, gen_random_uuid(), 'attn',
		'toolu_done', '[]', 'attention', 'done', 's')`, uuid(), id)
	evict := listen(t, a, "scadbuddy_warm_evict")
	got := a.ok("POST", "/sessions/"+id+"/edit", js(map[string]any{
		"principal": who, "done": true, "told": true, "archived": true, "title": "T"}))
	logged := got.body["logged"].([]any)[0].(map[string]any)
	expect(t, logged["seqs"], []any{1.0, 2.0, 3.0})
	expect(t, logged["events"].([]any)[0], map[string]any{"v": 1.0, "type": "session.status",
		"sessionId": id, "status": "done"})
	expect(t, len(got.body["dismissed"].([]any)), 1)
	expect(t, evict(), []string{id})
	row := a.ok("GET", "/sessions/"+id, "").body["session"].(map[string]any)
	expect(t, row["title"], "T")
	if row["archived_at"] == nil {
		t.Fatal("not archived")
	}
}

func TestAFlowRunsImagesAreKeptOnce(t *testing.T) {
	a := agentAPI(t)
	flow := "flow-" + uuid()
	name := strings.Repeat("a", 64) + ".png"
	put := func(subject, meta string, blobs ...string) answer {
		var b strings.Builder
		b.WriteString("--x\r\nContent-Disposition: form-data; name=\"images\"\r\n\r\n" + meta + "\r\n")
		for _, blob := range blobs {
			b.WriteString("--x\r\nContent-Disposition: form-data; name=\"blob\"; filename=\"" + name +
				"\"\r\nContent-Type: application/octet-stream\r\n\r\n" + blob + "\r\n")
		}
		b.WriteString("--x--\r\n")
		return a.do("POST", base+"/flow-blobs/"+subject, b.String(), func(r *http.Request) {
			r.Header.Set("Content-Type", "multipart/form-data; boundary=x")
		})
	}
	meta := js([]any{map[string]any{"name": name, "media_type": "image/png", "size": 3}})
	expect(t, put(flow, meta, "abc").raw, `{"names":["`+name+`"]}`)
	expect(t, put(flow, meta, "xyz").raw, `{"names":["`+name+`"]}`)
	got := a.do("GET", base+"/flow-blobs/"+flow+"/"+name, "")
	expect(t, got.raw, "abc")
	expect(t, got.header.Get("X-ScadBuddy-Media-Type"), "image/png")
	expect(t, a.do("GET", base+"/flow-blobs/"+flow+"/"+strings.Repeat("b", 64)+".png", "").body["reason"],
		"no-such-image")
	expect(t, put(flow, meta).status, 422)
	expect(t, put(flow, "not json", "abc").status, 422)
	expect(t, put(flow, meta, "").status, 422)
	a.sql(`INSERT INTO ai_forgotten_subjects (subject) VALUES ($1)`, flow)
	expect(t, put(flow, meta, "abc").status, 410)
}

func TestTheKeysNoForgetDeletesAreSwept(t *testing.T) {
	a := agentAPI(t)
	bridge := "bridge-" + uuid()
	orphan := "session-" + uuid()
	a.sql(`INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id, created_at) VALUES
		($1, '\x00', 'k1', now() - interval '2 hours'), ($2, '\x00', 'k1', now() - interval '2 days')`,
		bridge, orphan)
	expect(t, a.ok("POST", "/payload-key-sweeps", `{}`).raw, `{"orphaned":1,"bridge":1}`)
	a.sql(`INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES ($1, '\x00', 'k1')`, bridge)
	expect(t, a.ok("POST", "/payload-keys/"+bridge+"/drop", "").raw, `{"dropped":true}`)
	expect(t, a.do("POST", base+"/payload-keys/session-"+uuid()+"/drop", "").status, 422)
	expect(t, a.do("POST", base+"/payload-key-sweeps", `{"orphan_min_age_s":1}`).status, 422)
}

func TestWorkspaceHoldsAreHeldListedAndReleased(t *testing.T) {
	a := agentAPI(t)
	key := "workspace-" + strings.Repeat("0", 64)
	hold := js(map[string]any{"key": key, "holder_id": "session:x/a.txt"})
	expect(t, a.do("POST", base+"/workspace-blob-refs", hold).status, 204)
	expect(t, a.do("POST", base+"/workspace-blob-refs", hold).status, 204)
	list := a.ok("GET", "/workspace-blob-refs?limit=1", "")
	expect(t, list.raw, `{"holds":[{"key":"`+key+`","holder_id":"session:x/a.txt"}],`+
		`"next":{"key":"`+key+`","holder_id":"session:x/a.txt"}}`)
	expect(t, a.ok("POST", "/workspace-blob-refs/release", strings.TrimSuffix(hold, "}")+`,"older_than_seconds":60}`).raw,
		`{"released":false}`)
	expect(t, a.ok("POST", "/workspace-blob-refs/release", hold).raw, `{"released":true}`)
	expect(t, a.do("POST", base+"/workspace-blob-refs", `{"key":"x","holder_id":"session:x/a"}`).status, 422)
}

func TestTheEventLogHeadAndGap(t *testing.T) {
	a := agentAPI(t)
	a.sql(`INSERT INTO events (event_id, kind, at, payload) VALUES ('e1', 'x', now(), '{"kind":"x"}'),
		('e2', 'y', now(), '{"kind":"y"}'),
		('e3', 'fleet.message', now(), '{"kind":"fleet.message","recipients":["s1"]}')`)
	head := a.ok("GET", "/events/head", "")
	expect(t, head.body["seq"], "3")
	// The fleet's kinds name its session ids: they are for the fleet caller alone (#7818).
	gap := a.ok("GET", "/events?after=0", "").body["events"].([]any)
	expect(t, len(gap), 2)
	expect(t, gap[0].(map[string]any)["event_id"], "e2")
	expect(t, gap[1].(map[string]any)["payload"], map[string]any{"kind": "x"})
	expect(t, a.do("GET", base+"/events", "").status, 422)
}

func TestTheFlowReadsLeaveOutMalformedEntries(t *testing.T) {
	a := agentAPI(t)
	run := uuid()
	until := time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
	waiting := js([]any{
		map[string]any{"call_id": "c1", "kind": "approval", "fn": "f", "hop": "parent", "hop_until": until},
		map[string]any{"call_id": 7, "kind": "approval", "fn": "f"},
	})
	a.sql(`INSERT INTO workflow_definitions (id, name, version, created_by) VALUES ('def-1', 'reads', 1, '{}')`)
	a.sql(`INSERT INTO workflow_runs (id, definition_id, version, name, status, waiting_on, steps,
		workflow_id, workflow_run_id, started_by, superseded_run_ids)
		VALUES ($1, 'def-1', 1, 'n', 'waiting', $2::jsonb,
		'[{"seq":"1","fn":"f","call_id":"c1","status":"done"},{"seq":1.5}]', 'flow-' || $1, 'e1',
		'{"session":"s","principal":7}', '{}')`, run, waiting)
	queue := a.ok("GET", "/flow-runs/"+run+"/queue", "").body["run"].(map[string]any)
	expect(t, len(queue["waiting_on"].([]any)), 1)
	// A call parked before #7471 has no people_only: it reads as people only.
	expect(t, queue["waiting_on"].([]any)[0].(map[string]any)["people_only"], true)
	expect(t, queue["steps"], []any{map[string]any{"seq": 1.0, "fn": "f", "call_id": "c1",
		"status": "done", "session_id": nil}})
	expect(t, queue["starter_session"], "s")
	starter := a.ok("GET", "/flow-runs/"+run+"/starter?execution=e1", "").body["run"].(map[string]any)
	expect(t, starter["open"], true)
	expect(t, starter["started_by"], map[string]any{"kind": nil, "principal": nil, "session": "s"})
	hop := a.ok("GET", "/flow-runs/parent-hop-approvals?limit=1", "")
	runs := hop.body["runs"].([]any)
	expect(t, len(runs), 1)
	next := hop.body["next"].(map[string]any)
	expect(t, a.ok("GET", fmt.Sprintf("/flow-runs/parent-hop-approvals?after_updated_at=%s&after_id=%s",
		next["after_updated_at"], next["after_id"]), "").body["runs"], []any{})
	expect(t, a.do("GET", base+"/flow-runs/parent-hop-approvals?after_id=x", "").status, 422)
	request := "flow:" + run + ":e1:c1"
	expect(t, a.ok("POST", "/flow-runs/parent-hop/waiting", js(map[string]any{
		"request_ids": []string{"flow:x:y:z", request}})).raw, `{"waiting":["`+request+`"]}`)
	expect(t, len(a.ok("GET", "/flow-runs/waiting", "").body["runs"].([]any)), 1)
	expect(t, a.ok("GET", "/flow-grants/in-force?grant_id=g", "").raw, `{"in_force":false}`)
	checks := a.ok("GET", "/flow-decisions?request_id=r&input_hash=h", "")
	expect(t, checks.raw, `{"approved":false,"approved_unresolved":false,"by_person":false,"lapsed":false}`)
}
