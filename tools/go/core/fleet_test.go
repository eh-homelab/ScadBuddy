package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// The agent fleet chat's routes (internal/fleet), as the fleet MCP server's store calls them
// (spec 2026-11-25-agent-fleet-messaging-design §4.2).

const fleetBase = "/api/v1/internal/crud/fleet"

func newFleet(t *testing.T) *api {
	t.Helper()
	a := newAPI(t, "fleet")
	a.token = a.credential("fleet", "crud.fleet")
	return a
}

func (a *api) fleet(method, path, body string, status int) answer {
	a.t.Helper()
	out := a.do(method, fleetBase+path, body)
	if out.status != status {
		a.t.Fatalf("%s %s: %d %s, want %d", method, path, out.status, out.raw, status)
	}
	return out
}

// agent touches a new agent and is its session id.
func (a *api) agent() string {
	a.t.Helper()
	id := uuid()
	a.fleet("POST", "/agents/"+id+"/touch", "", 200)
	return id
}

func handle(session string) string { return session[:8] }

func (a *api) send(from, to, text string) answer {
	a.t.Helper()
	key := "agent"
	if !isHandle(to) {
		key = "channel"
	}
	return a.do("POST", fleetBase+"/messages", object("from", from, key, to, "text", text))
}

func isHandle(s string) bool {
	return len(s) == 8 && strings.Trim(s, "0123456789abcdef") == ""
}

func (a *api) inbox(session, query string) (msgs []map[string]any, more bool) {
	a.t.Helper()
	out := a.fleet("GET", "/agents/"+session+"/inbox"+query, "", 200)
	for _, m := range out.body["messages"].([]any) {
		msgs = append(msgs, m.(map[string]any))
	}
	return msgs, out.body["more"].(bool)
}

func seqs(msgs []map[string]any) []float64 {
	var out []float64
	for _, m := range msgs {
		out = append(out, m["seq"].(float64))
	}
	return out
}

func TestTheFleetRoutesServeTheStore(t *testing.T) {
	a := newFleet(t)
	ann, bob := a.agent(), a.agent()
	touched := a.fleet("POST", "/agents/"+ann+"/touch", "", 200)
	expect(t, touched.body["handle"], handle(ann))
	expect(t, a.count(`SELECT count(*) FROM fleet_agents`), 2)

	agents := a.fleet("GET", "/agents", "", 200).body["agents"].([]any)
	var handles []string
	for _, x := range agents {
		handles = append(handles, x.(map[string]any)["handle"].(string))
		if _, err := time.Parse(time.RFC3339Nano, x.(map[string]any)["last_seen"].(string)); err != nil {
			t.Fatal(err)
		}
	}
	slices.Sort(handles)
	want := []string{handle(ann), handle(bob)}
	slices.Sort(want)
	expect(t, handles, want)

	a.fleet("PUT", "/channels/ops/members/"+ann, "", 200)
	a.fleet("PUT", "/channels/ops/members/"+ann, "", 200)
	a.fleet("PUT", "/channels/ops/members/"+bob, "", 200)

	dm := a.send(ann, handle(bob), "hello bob")
	expect(t, dm.status, 200)
	recipients := dm.body["recipients"].([]any)
	slices.SortFunc(recipients, func(x, y any) int { return strings.Compare(x.(string), y.(string)) })
	both := []any{ann, bob}
	slices.SortFunc(both, func(x, y any) int { return strings.Compare(x.(string), y.(string)) })
	expect(t, recipients, both)
	post := a.send(bob, "ops", "hello ops")
	expect(t, post.status, 200)
	expect(t, post.body["seq"].(float64) > dm.body["seq"].(float64), true)

	chans := a.fleet("GET", "/agents/"+bob+"/channels", "", 200).body["channels"].([]any)
	expect(t, len(chans), 1)
	ops := chans[0].(map[string]any)
	expect(t, ops["name"], "ops")
	expect(t, ops["unread"], float64(1))
	expect(t, len(ops["members"].([]any)), 2)

	peeked, more := a.inbox(bob, "?peek=true")
	expect(t, more, false)
	expect(t, seqs(peeked), []float64{dm.body["seq"].(float64), post.body["seq"].(float64)})
	expect(t, peeked[0]["from"], handle(ann))
	expect(t, peeked[0]["to"], handle(bob))
	expect(t, peeked[0]["text"], "hello bob")
	expect(t, peeked[1]["channel"], "ops")
	if _, ok := peeked[1]["to"]; ok {
		t.Fatalf("a channel post names a to: %v", peeked[1])
	}
	if _, err := time.Parse(time.RFC3339Nano, peeked[0]["sent_at"].(string)); err != nil {
		t.Fatal(err)
	}

	first, more := a.inbox(bob, "?limit=1")
	expect(t, more, true)
	expect(t, seqs(first), seqs(peeked)[:1])
	rest, more := a.inbox(bob, "")
	expect(t, more, false)
	expect(t, seqs(rest), seqs(peeked)[1:])
	empty, _ := a.inbox(bob, "")
	expect(t, len(empty), 0)
	expect(t, a.fleet("GET", "/agents/"+bob+"/channels", "", 200).body["channels"].([]any)[0].(map[string]any)["unread"], float64(0))

	a.fleet("PUT", "/agents/"+bob+"/cursor", `{"seq":0}`, 200)
	again, _ := a.inbox(bob, "")
	expect(t, seqs(again), seqs(peeked))
	a.fleet("PUT", "/agents/"+bob+"/cursor", `{"seq":999999}`, 200)
	a.fleet("PUT", "/agents/"+bob+"/cursor", `{"seq":-1}`, 422)

	// A DM is in the sender's inbox too.
	mine, _ := a.inbox(ann, "")
	expect(t, seqs(mine), seqs(peeked))

	a.fleet("DELETE", "/channels/ops/members/"+bob, "", 200)
	a.fleet("DELETE", "/channels/ops/members/"+bob, "", 403)
	expect(t, a.count(`SELECT count(*) FROM fleet_channels WHERE name = 'ops'`), 1)
}

func TestAnUnknownAgentIs404(t *testing.T) {
	a := newFleet(t)
	ann, ghost := a.agent(), uuid()
	expect(t, a.send(ann, handle(ghost), "hi").status, 404)
	expect(t, a.send(ghost, handle(ann), "hi").status, 404)
	a.fleet("GET", "/agents/"+ghost+"/inbox", "", 404)
	a.fleet("GET", "/agents/"+ghost+"/channels", "", 404)
	a.fleet("PUT", "/agents/"+ghost+"/cursor", `{"seq":0}`, 404)
	a.fleet("PUT", "/channels/ops/members/"+ghost, "", 404)
	a.fleet("DELETE", "/channels/ops/members/"+ghost, "", 404)
	a.fleet("GET", "/agents/NOT-A-UUID/inbox", "", 422)
	a.fleet("PUT", "/channels/Bad_Name/members/"+ann, "", 422)
}

func TestAPostNeedsMembership(t *testing.T) {
	a := newFleet(t)
	ann, bob := a.agent(), a.agent()
	expect(t, a.send(ann, "ops", "hi").status, 403)
	a.fleet("PUT", "/channels/ops/members/"+bob, "", 200)
	got := a.send(ann, "ops", "hi")
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "not-member")
	expect(t, a.count(`SELECT count(*) FROM fleet_messages`), 0)
}

func TestAJoinAfterAPostGetsNoDelivery(t *testing.T) {
	a := newFleet(t)
	ann, bob := a.agent(), a.agent()
	a.fleet("PUT", "/channels/ops/members/"+ann, "", 200)
	post := a.send(ann, "ops", "before bob")
	expect(t, post.body["recipients"], []any{ann})
	a.fleet("PUT", "/channels/ops/members/"+bob, "", 200)
	got, _ := a.inbox(bob, "")
	expect(t, len(got), 0)
}

func TestConcurrentPostsGetDistinctSeqs(t *testing.T) {
	a := newFleet(t)
	ann := a.agent()
	a.fleet("PUT", "/channels/ops/members/"+ann, "", 200)
	const n = 20
	got := make([]float64, n)
	var wg sync.WaitGroup
	for i := range n {
		wg.Go(func() {
			out := a.send(ann, "ops", fmt.Sprint("post ", i))
			if out.status != 200 {
				t.Errorf("post %d: %d %s", i, out.status, out.raw)
				return
			}
			got[i] = out.body["seq"].(float64)
		})
	}
	wg.Wait()
	distinct := slices.Clone(got)
	slices.Sort(distinct)
	expect(t, len(slices.Compact(distinct)), n)
	expect(t, a.count(`SELECT count(*) FROM fleet_deliveries`), n)
}

// listen LISTENs on the event bus's channel.
func (a *api) listen() *pgx.Conn {
	a.t.Helper()
	conn, err := pgx.Connect(context.Background(), a.pool.Config().ConnString())
	if err != nil {
		a.t.Fatal(err)
	}
	a.t.Cleanup(func() { conn.Close(context.Background()) })
	if _, err := conn.Exec(context.Background(), `LISTEN scadbuddy_events`); err != nil {
		a.t.Fatal(err)
	}
	return conn
}

func heard(t *testing.T, conn *pgx.Conn, wait time.Duration) (map[string]any, string, bool) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), wait)
	defer cancel()
	n, err := conn.WaitForNotification(ctx)
	if err != nil {
		return nil, "", false
	}
	var event map[string]any
	if err := json.Unmarshal([]byte(n.Payload), &event); err != nil {
		t.Fatal(err)
	}
	return event, n.Payload, true
}

func TestASendIsAFleetMessageEventOnceCommitted(t *testing.T) {
	a := newFleet(t)
	ann, bob := a.agent(), a.agent()
	conn := a.listen()
	dm := a.send(ann, handle(bob), "hi")
	event, payload, ok := heard(t, conn, 5*time.Second)
	if !ok {
		t.Fatal("no event")
	}
	expect(t, event["kind"], "fleet.message")
	expect(t, event["seq"], dm.body["seq"])
	expect(t, event["recipients"], dm.body["recipients"])
	var topics []string
	for _, r := range dm.body["recipients"].([]any) {
		topics = append(topics, "fleet:agent:"+handle(r.(string)))
	}
	expect(t, event["topics"], toAny(topics))
	if !strings.HasPrefix(payload, `{"id":`) || strings.Contains(payload, "hi\"") {
		t.Fatalf("the payload: %s", payload)
	}
	expect(t, a.count(`SELECT count(*) FROM events WHERE kind = 'fleet.message'`), 1)
}

func toAny(s []string) []any {
	out := make([]any, len(s))
	for i, v := range s {
		out[i] = v
	}
	return out
}

func TestAPostToManyRecipientsNamesEachInSomeEvent(t *testing.T) {
	a := newFleet(t)
	var members []string
	for range 300 {
		id := uuid()
		members = append(members, id)
	}
	// Made in SQL: 300 touches and joins over HTTP would be slow.
	a.sql(`INSERT INTO fleet_agents (session_id, handle) SELECT s::uuid, left(s, 8) FROM unnest($1::text[]) s`, members)
	a.sql(`INSERT INTO fleet_channels (name) VALUES ('all')`)
	a.sql(`INSERT INTO fleet_members (channel, session_id) SELECT 'all', s::uuid FROM unnest($1::text[]) s`, members)
	conn := a.listen()
	post := a.send(members[0], "all", "to everyone")
	expect(t, post.status, 200)
	expect(t, len(post.body["recipients"].([]any)), 300)

	named := map[string]bool{}
	events := 0
	for len(named) < 300 {
		event, payload, ok := heard(t, conn, 5*time.Second)
		if !ok {
			t.Fatalf("%d events named %d recipients", events, len(named))
		}
		events++
		if len(payload) > 4000 {
			t.Fatalf("a payload of %d bytes", len(payload))
		}
		expect(t, event["seq"], post.body["seq"])
		topics := event["topics"].([]any)
		if len(topics) > 64 {
			t.Fatalf("%d topics", len(topics))
		}
		expect(t, slices.Contains(topics, any("fleet:channel:all")), true)
		for _, r := range event["recipients"].([]any) {
			named[r.(string)] = true
			expect(t, slices.Contains(topics, any("fleet:agent:"+handle(r.(string)))), true)
		}
	}
	if events < 2 {
		t.Fatalf("one event named all 300")
	}
	for _, m := range members {
		if !named[m] {
			t.Fatalf("%s is in no event", m)
		}
	}
}

func TestOnlyTheFleetCallerReachesTheFleetRoutes(t *testing.T) {
	a := newFleet(t)
	ann := a.agent()
	for name, token := range map[string]string{
		"the agent":     a.credential("agent", "crud.agent", "crud.fleet"),
		"another scope": a.credential("fleet", "crud.agent"),
	} {
		got := a.do("GET", fleetBase+"/agents/"+ann+"/inbox", "", func(r *http.Request) {
			r.Header.Set("Authorization", "Bearer "+token)
		})
		if got.status != 403 {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	none := a.do("GET", fleetBase+"/agents", "", func(r *http.Request) { r.Header.Del("Authorization") })
	expect(t, none.status, 401)
}

func TestAnEventWithATopicOutsideItsKindIsNotPublished(t *testing.T) {
	a := newFleet(t)
	conn := a.listen()
	ctx := context.Background()
	bus.Publish(ctx, a.pool, "fleet.message", httpx.Field{Key: "topics", Value: []string{"fleet:agent:x", "tasks"}})
	bus.Publish(ctx, a.pool, "fleet.message", httpx.Field{Key: "topics", Value: []string{"fleetx:agent:x"}})
	bus.Publish(ctx, a.pool, "fleet.message", httpx.Field{Key: "topics", Value: "fleet:agent:x"})
	expect(t, a.count(`SELECT count(*) FROM events`), 0)
	bus.Publish(ctx, a.pool, "fleet.message", httpx.Field{Key: "topics", Value: []string{"fleet:agent:x"}})
	bus.Publish(ctx, a.pool, "plugin_packages.changed", httpx.Field{Key: "name", Value: "x"})
	expect(t, a.count(`SELECT count(*) FROM events`), 2)
	event, _, ok := heard(t, conn, 5*time.Second)
	expect(t, ok, true)
	expect(t, event["topics"], []any{"fleet:agent:x"})
}
