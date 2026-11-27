package devices

import (
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/realtime"
)

// A delegate service reads the call it made for a person, naming that person as it did in
// the call; another service, or the wrong person, reads nothing.
func TestDelegateReadsItsOwnCall(t *testing.T) {
	h := newHarness(t, auth.Enforce)
	me := h.person("user:i#me")
	d := h.pair(me)
	agent := h.service("agent", "api")
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", agent, `{"method":"nfc.read","on_behalf_of":"user:i#me"}`)
	expectStatus(t, got, 202)
	call := got.body["call_id"].(string)
	expectStatus(t, h.do("GET", "/api/v1/devices/calls/"+call+"?on_behalf_of=user:i%23me", agent, ""), 200)
	expectStatus(t, h.do("GET", "/api/v1/devices/calls/"+call, agent, ""), 404)
	expectStatus(t, h.do("GET", "/api/v1/devices/calls/"+call+"?on_behalf_of=user:i%23you", agent, ""), 404)
	// The projects worker made no call here as a flow.
	expectStatus(t, h.do("GET", "/api/v1/devices/calls/"+call+"?on_behalf_of=user:i%23me",
		h.service("projects", "api"), ""), 404)
	expectStatus(t, h.do("GET", "/api/v1/devices/calls/"+call+"?on_behalf_of=user:i%23me",
		h.service("render", "api"), ""), 403)
	expectStatus(t, h.do("GET", "/api/v1/devices/calls/"+call, me, ""), 200)
}

// An approved approval that was used, revoked or expired before the confirm is named so.
func TestConfirmNamesAnUnusableApproval(t *testing.T) {
	for _, tc := range []struct{ name, change string }{
		{"expired", `usable_until = now() - interval '1 second'`},
		{"used", `consumed_at = now()`},
		{"revoked", `revoked_at = now()`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newHarness(t, auth.Observe)
			me := h.person("user:i#me")
			d := h.pair(me)
			bot := h.assistant("user:i#me")
			got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", bot,
				`{"method":"nfc.write","args":{"records":[{"recordType":"text","text":"PLA"}]}}`)
			expectStatus(t, got, 202)
			call := got.body["call_id"].(string)
			approve(h, call, "approved")
			h.sql(`UPDATE ai_approvals a SET `+tc.change+` FROM device_calls c
				WHERE c.id = $1::uuid AND a.id = c.approval_id`, call)
			c := h.do("POST", "/api/v1/devices/calls/"+call+"/confirm", bot, "")
			expectStatus(t, c, 409)
			expect(t, c.body["reason"], any("approval_"+tc.name))
			expect(t, c.body["detail"], any("The call's approval was "+tc.name+" before the confirm."))
			expect(t, outcome(h.view(me, call)), any("denied"))
		})
	}
}

// Revoking a device takes its calls' approvals out of the pending actions.
func TestRevokeCancelsPendingApprovals(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	bot := h.assistant("user:i#me")
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", bot,
		`{"method":"nfc.write","args":{"records":[{"recordType":"text","text":"PLA"}]}}`), 202)
	expectStatus(t, h.do("DELETE", "/api/v1/devices/"+d.id, me, ""), 204)
	expect(t, h.count(`SELECT count(*) FROM ai_approvals WHERE decision = 'cancelled' AND decided_at IS NOT NULL`), 1)
	expect(t, h.count(`SELECT count(*) FROM ai_approvals WHERE decision IS NULL`), 0)
}

// A check of another person's call topic tells nothing: a socket may follow a call's topic
// only for a device it owns, and a check answers "changed" for every topic it does not
// follow, before and after the call's events alike.
func TestCheckOfAnotherCallTellsNothing(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	_ = h.pods[0].device(d)
	call := h.start(me, d, `{"method":"nfc.read","args":{},"timeout_s":30}`)
	you, res := h.pods[0].dial(map[string]string{"Cookie": h.person("user:i#you")})
	expect(t, res.StatusCode, 101)
	you.send(`{"type":"subscribe","topics":["device:call:` + call + `"]}`)
	expect(t, you.frame()["type"], any("error"))
	check := func(id string) string {
		you.send(`{"type":"check","id":` + id + `,"since":["0"],"topics":["device:call:` + call + `"]}`)
		f := you.frame()
		expect(t, f["type"], any("changed"))
		return fmt.Sprint(f["topics"])
	}
	before := check("1")
	expectStatus(t, h.post(d, call, "events", `{"events":[{"kind":"tag_found","data":{"uid":"9CC69CE3"}}]}`), 204)
	expect(t, check("2"), before)
	expect(t, before, "[device:call:"+call+"]")
}

// A call whose approval wait has passed is never sent, even when no timer ended it: the
// confirm ends it denied, and no device.call_requested is logged. sendIn sends only an open,
// unsent call.
func TestConfirmAfterTheDeadlineSendsNothing(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	bot := h.assistant("user:i#me")
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", bot,
		`{"method":"nfc.write","args":{"records":[{"recordType":"text","text":"PLA"}]}}`)
	expectStatus(t, got, 202)
	call := got.body["call_id"].(string)
	approve(h, call, "approved")
	h.sql(`UPDATE ai_approvals a SET usable_until = now() + interval '1 day' FROM device_calls c
		WHERE c.id = $1::uuid AND a.id = c.approval_id`, call)
	h.clock.add(ApprovalTTL + time.Second)
	c := h.do("POST", "/api/v1/devices/calls/"+call+"/confirm", bot, "")
	expectStatus(t, c, 409)
	expect(t, c.body["reason"], any("approval_expired"))
	expect(t, h.count(`SELECT count(*) FROM events WHERE kind = 'device.call_requested' AND payload ->> 'call' = $1`, call), 0)
	expect(t, h.count(`SELECT count(*) FROM ai_approvals WHERE consumed_at IS NOT NULL`), 0)
	err := pgx.BeginFunc(t.Context(), h.pool, func(tx pgx.Tx) error {
		return sendIn(t.Context(), tx, call, d.id, "nfc.write", []byte(`{}`), h.clock.now(), h.clock.now().Add(time.Minute))
	})
	if !errors.Is(err, errNotOpen) {
		t.Fatalf("sendIn on an ended call: %v", err)
	}
	expect(t, h.count(`SELECT count(*) FROM events WHERE kind = 'device.call_requested' AND payload ->> 'call' = $1`, call), 0)
}

// A call that ends while it waits for its approval takes the approval out of the pending
// actions.
func TestExpireCancelsTheApproval(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	bot := h.assistant("user:i#me")
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", bot,
		`{"method":"nfc.write","args":{"records":[{"recordType":"text","text":"PLA"}]}}`)
	expectStatus(t, got, 202)
	call := got.body["call_id"].(string)
	h.clock.add(ApprovalTTL + time.Second)
	expect(t, outcome(h.view(me, call)), any("denied"))
	expect(t, h.count(`SELECT count(*) FROM ai_approvals WHERE decision = 'cancelled'`), 1)
}

// One subscribe frame may name at most realtime.MaxCallTopics distinct call topics.
func TestSubscribeCapsCallTopics(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	_ = h.pods[0].device(d)
	var topics []string
	for range realtime.MaxCallTopics + 1 {
		topics = append(topics, `"device:call:`+h.start(me, d, `{"method":"nfc.read"}`)+`"`)
	}
	page, res := h.pods[0].dial(map[string]string{"Cookie": me})
	expect(t, res.StatusCode, 101)
	page.send(`{"type":"subscribe","topics":[` + strings.Join(topics, ",") + `]}`)
	expect(t, page.frame()["type"], any("error"))
	page.send(`{"type":"subscribe","topics":[` + strings.Join(topics[:realtime.MaxCallTopics], ",") + `]}`)
	expect(t, page.frame()["type"], any("subscribed"))
	// The cap counts distinct topics: one call named more times than the cap is one topic.
	page.send(`{"type":"subscribe","topics":[` + strings.Repeat(topics[0]+",", realtime.MaxCallTopics) + topics[0] + `]}`)
	expect(t, page.frame()["type"], any("subscribed"))
}

// A frame that repeats one call topic reads who may follow it once, however many times it
// names it. A frame holds at most realtime.MaxFrameChars characters, so as many copies as
// fit (about 300) is the most one frame can send.
func TestRepeatedCallTopicIsReadOnce(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	_ = h.pods[0].device(d)
	topic := `"device:call:` + h.start(me, d, `{"method":"nfc.read"}`) + `"`
	frame := `{"type":"subscribe","topics":[` + topic
	for len(frame)+len(topic)+3 < realtime.MaxFrameChars {
		frame += "," + topic
	}
	frame += "]}"
	page, res := h.pods[0].dial(map[string]string{"Cookie": me})
	expect(t, res.StatusCode, 101)
	before := h.visibleReads.Load()
	page.send(frame)
	expect(t, page.frame()["type"], any("subscribed"))
	expect(t, h.visibleReads.Load()-before, int64(1))
	// Past the cap a distinct call topic is refused unread.
	var topics []string
	for range realtime.MaxCallTopics + 1 {
		topics = append(topics, `"device:call:`+h.start(me, d, `{"method":"nfc.read"}`)+`"`)
	}
	before = h.visibleReads.Load()
	page.send(`{"type":"subscribe","topics":[` + strings.Join(topics, ",") + `]}`)
	expect(t, page.frame()["type"], any("error"))
	expect(t, h.visibleReads.Load()-before, int64(realtime.MaxCallTopics))
}

// One rule says who may read a call and who may follow its events.
func TestVisibleTo(t *testing.T) {
	for _, tc := range []struct {
		kind, who, callerKind string
		want                  bool
	}{
		{"", "user:i#owner", "person", true},
		{"", "user:i#me", "flow", true},
		{"", "user:i#you", "person", false},
		{"assistant", "user:i#me", "assistant", true},
		{"assistant", "user:i#me", "flow", false},
		{"flow", "user:i#owner", "flow", false},
		{"", "", "person", false},
	} {
		if got := visibleTo("user:i#owner", "user:i#me", tc.callerKind, tc.kind, tc.who); got != tc.want {
			t.Errorf("%+v: %v", tc, got)
		}
	}
}
