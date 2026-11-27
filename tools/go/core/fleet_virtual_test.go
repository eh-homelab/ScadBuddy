package main

import (
	"testing"
)

// Virtual senders (#8026, #7971 part 6): reserved names that only the `fleet-rotator` caller
// posts as, and that no session can send a DM to.

const virtualBase = "/api/v1/internal/crud/fleet-rotator/virtual-messages"

func (a *api) virtual(token, body string) answer {
	a.t.Helper()
	fleet := a.token
	a.token = token
	defer func() { a.token = fleet }()
	return a.do("POST", virtualBase, body)
}

func TestADMToAVirtualSenderGetsItsRejectMessage(t *testing.T) {
	a := newFleet(t)
	ann := a.agent()
	var reject string
	if err := a.pool.QueryRow(t.Context(),
		`SELECT reject_message FROM fleet_virtual_senders WHERE name = 'rotator'`).Scan(&reject); err != nil {
		t.Fatal(err)
	}
	dm := func(to string) answer {
		return a.do("POST", fleetBase+"/messages", object("from", ann, "agent", to, "text", "are you there?"))
	}
	out := dm("rotator")
	expect(t, out.status, 422)
	expect(t, out.body["reason"], "virtual-sender")
	expect(t, out.body["detail"], reject)
	expect(t, dm("github").body["reason"], "virtual-sender")
	expect(t, a.count(`SELECT count(*) FROM fleet_messages`), 0)

	// A name that is neither an agent nor a virtual sender is an unknown agent.
	expect(t, dm("nobody").body["reason"], "unknown-agent")
	// An agent field that is no handle and no name is a 422.
	expect(t, dm("Not_A_Name").status, 422)
}

func TestTheVirtualSenderNamesCanNeverBeAHandle(t *testing.T) {
	a := newFleet(t)
	for _, name := range []string{"0a1b2c3d", "12345", "deadbeef", "Rotator", "a_b", ""} {
		_, err := a.pool.Exec(t.Context(), `INSERT INTO fleet_virtual_senders (name, description, reject_message)
			VALUES ($1, 'x', 'x')`, name)
		if err == nil {
			t.Errorf("the table took the name %q", name)
		}
	}
	expect(t, a.count(`SELECT count(*) FROM fleet_virtual_senders WHERE name IN ('rotator', 'github')`), 2)
}

func TestOnlyTheRotatorCallerPostsAsAVirtualSender(t *testing.T) {
	a := newFleet(t)
	ann := a.agent()
	a.fleet("PUT", "/channels/ops/members/"+ann, "", 200)
	body := object("sender", "rotator", "channel", "ops", "text", "hi")

	// The fleet chat server's credential, which serves the unauthenticated MCP, is refused.
	refused := a.virtual(a.token, body)
	expect(t, refused.status, 403)
	expect(t, refused.body["reason"], "wrong_caller")
	// And the fleet routes take no sender field.
	expect(t, a.do("POST", fleetBase+"/messages",
		object("from", ann, "sender", "rotator", "channel", "ops", "text", "hi")).status, 422)
	expect(t, a.count(`SELECT count(*) FROM fleet_messages`), 0)
}

func TestTheRotatorPostsToAChannelAndDMsASession(t *testing.T) {
	a := newFleet(t)
	rotator := a.credential("fleet-rotator", "crud.fleet-rotator")
	ann, bob, cat := a.agent(), a.agent(), a.agent()
	a.fleet("PUT", "/channels/ops/members/"+ann, "", 200)
	a.fleet("PUT", "/channels/ops/members/"+bob, "", 200)

	post := a.virtual(rotator, object("sender", "rotator", "channel", "ops", "text", "logins moved"))
	expect(t, post.status, 200)
	expect(t, len(post.body["recipients"].([]any)), 2)
	for _, s := range []string{ann, bob} {
		msgs, _ := a.inbox(s, "")
		expect(t, len(msgs), 1)
		expect(t, msgs[0]["from"], "rotator")
		expect(t, msgs[0]["channel"], "ops")
		expect(t, msgs[0]["text"], "logins moved")
	}
	none, _ := a.inbox(cat, "")
	expect(t, len(none), 0)

	dm := a.virtual(rotator, object("sender", "rotator", "agent", handle(cat), "text", "Continue the task you were doing."))
	expect(t, dm.status, 200)
	expect(t, dm.body["recipients"], []any{cat})
	msgs, _ := a.inbox(cat, "")
	expect(t, len(msgs), 1)
	expect(t, msgs[0]["from"], "rotator")
	expect(t, msgs[0]["to"], handle(cat))
	expect(t, msgs[0]["text"], "Continue the task you were doing.")
	for _, s := range []string{ann, bob} {
		other, _ := a.inbox(s, "")
		expect(t, len(other), 0)
	}

	unknown := a.virtual(rotator, object("sender", "nobody", "channel", "ops", "text", "hi"))
	expect(t, unknown.status, 404)
	expect(t, unknown.body["reason"], "unknown-sender")
	expect(t, a.virtual(rotator, object("sender", "rotator", "channel", "nope", "text", "hi")).body["reason"], "unknown-channel")
	expect(t, a.virtual(rotator, object("sender", "rotator", "agent", "ffffffff", "text", "hi")).body["reason"], "unknown-agent")
	expect(t, a.virtual(rotator, object("sender", "rotator", "agent", "github", "text", "hi")).status, 422)
	expect(t, a.virtual(rotator, object("sender", "rotator", "text", "hi")).status, 422)
	expect(t, a.virtual(rotator, object("sender", "rotator", "channel", "ops", "agent", handle(cat), "text", "hi")).status, 422)
	expect(t, a.virtual(rotator, object("sender", "rotator", "channel", "ops", "text", "")).status, 422)
}
