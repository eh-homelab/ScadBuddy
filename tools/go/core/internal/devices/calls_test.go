package devices

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
)

// start calls method on d as who and answers the call's id.
func (h *harness) start(who string, d paired, body string) string {
	h.t.Helper()
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", who, body)
	expectStatus(h.t, got, 202)
	return got.body["call_id"].(string)
}

// requested reads the next frame on a device's socket: the call it was sent.
func requestedOn(t *testing.T, sock *ws, call string) map[string]any {
	t.Helper()
	f := sock.frame()
	expect(t, f["type"], any("event"))
	expect(t, f["kind"], any("device.call_requested"))
	data := f["data"].(map[string]any)
	expect(t, data["call"], any(call))
	return data
}

func (h *harness) post(d paired, call, what, body string) answer {
	h.t.Helper()
	return h.do("POST", "/api/v1/devices/calls/"+call+"/"+what, d.token, body)
}

func (h *harness) view(who, call string) answer {
	h.t.Helper()
	got := h.do("GET", "/api/v1/devices/calls/"+call, who, "")
	expectStatus(h.t, got, 200)
	return got
}

func kinds(v answer) []string {
	var out []string
	for _, e := range v.body["events"].([]any) {
		out = append(out, e.(map[string]any)["kind"].(string))
	}
	return out
}

func outcome(v answer) any { return v.body["call"].(map[string]any)["outcome"] }

func TestCallEventsInOrder(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	sock := h.pods[0].device(d)
	call := h.start(me, d, `{"method":"nfc.read","args":{},"timeout_s":30}`)
	data := requestedOn(t, sock, call)
	expect(t, data["method"], any("nfc.read"))
	expect(t, data["device"], any(d.id))
	// The page follows the call's own topic.
	page, res := h.pods[0].dial(map[string]string{"Cookie": me})
	expect(t, res.StatusCode, 101)
	page.send(`{"type":"subscribe","topics":["device:call:` + call + `"]}`)
	expect(t, page.frame()["type"], any("subscribed"))

	expectStatus(t, h.post(d, call, "events", `{"events":[
		{"kind":"tag_found","data":{"uid":"9CC69CE3","tech":"mifare_classic","blocks":48}},
		{"kind":"tag_progress","data":{"read":8,"of":48}},
		{"kind":"tag_lost","data":{"read":8,"error":"sector 2: transceive failed"}}]}`), 204)
	expectStatus(t, h.post(d, call, "events", `{"events":[
		{"kind":"tag_found","data":{"uid":"9CC69CE3","resume_from":8}},
		{"kind":"tag_progress","data":{"read":16,"of":48}}]}`), 204)
	for _, want := range []string{"device.tag_found", "device.tag_progress", "device.tag_lost", "device.tag_found", "device.tag_progress"} {
		f := page.frame()
		expect(t, f["kind"], any(want))
		expect(t, f["data"].(map[string]any)["call"], any(call))
	}
	key := h.upload(d, call, "tag_dump", fixtureDump(t))
	done := h.post(d, call, "result", `{"outcome":"done","result":{"read":{"blob_key":"`+key+`","uid":"DC999BE3",
		"tech":"mifare_classic","complete":true,"missing_blocks":[]}}}`)
	expectStatus(t, done, 200)
	expect(t, page.frame()["kind"], any("device.call_done"))

	v := h.view(me, call)
	expect(t, strings.Join(kinds(v), ","), "device.call_requested,device.tag_found,device.tag_progress,device.tag_lost,"+
		"device.tag_found,device.tag_progress,device.call_done")
	expect(t, outcome(v), any("done"))
	expect(t, v.body["result"].(map[string]any)["read_id"], done.body["read_id"])
	first := v.body["events"].([]any)[1].(map[string]any)["data"].(map[string]any)
	expect(t, first["uid"], any("9CC69CE3"))
	expect(t, first["blocks"], any(float64(48)))
	// The call is closed now: no more events.
	expectStatus(t, h.post(d, call, "events", `{"events":[{"kind":"tag_progress","data":{"read":1,"of":2}}]}`), 409)
}

func TestEventFrameUnknownKindRefused(t *testing.T) {
	h := newHarness(t, auth.Off)
	d := h.pair("")
	call := h.start("", d, `{"method":"nfc.read"}`)
	for _, body := range []string{
		`{"events":[{"kind":"location","data":{}}]}`,
		// The call's end comes through its result, never as an event.
		`{"events":[{"kind":"call_done","data":{}}]}`,
		`{"events":[{"kind":"tag_found","data":{"call":"another"}}]}`,
	} {
		expectStatus(t, h.post(d, call, "events", body), 422)
	}
	expect(t, h.count(`SELECT count(*) FROM events WHERE kind LIKE 'device.%' AND kind <> 'device.call_requested'`), 0)
	// Another device may not post to this call.
	other := h.pair("")
	expectStatus(t, h.post(other, call, "events", `{"events":[{"kind":"tag_found"}]}`), 404)
}

// Review focus 1: the call is made on one pod and the device's socket is on another, over
// the same database. The call still reaches the device, and its events reach the caller.
func TestCallAcrossPods(t *testing.T) {
	h := newHarness(t, auth.Observe)
	b := h.addPod(auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	sock := b.device(d)
	call := h.start(me, d, `{"method":"code.scan"}`)
	requestedOn(t, sock, call)
	expectStatus(t, b.do("POST", "/api/v1/devices/calls/"+call+"/events", d.token,
		`{"events":[{"kind":"tag_progress","data":{"read":1,"of":1}}]}`), 204)
	v := h.view(me, call)
	expect(t, strings.Join(kinds(v), ","), "device.call_requested,device.tag_progress")
}

// Review focus 2: the device goes away during a call. Once the deadline passes the call ends
// offline; it never hangs. One still online at its deadline ends timeout.
func TestCallDeviceGoneMidCall(t *testing.T) {
	h := newHarness(t, auth.Off)
	d := h.pair("")
	call := h.start("", d, `{"method":"nfc.read","timeout_s":5}`)
	h.clock.add(80 * time.Second)
	h.fire()
	v := h.view("", call)
	expect(t, outcome(v), any("offline"))
	expect(t, kinds(v)[len(kinds(v))-1], "device.call_failed")
	// The call is closed: a late result changes nothing.
	expectStatus(t, h.post(d, call, "result", `{"outcome":"error"}`), 409)
	expect(t, outcome(h.view("", call)), any("offline"))
}

func TestCallTimesOutWhileOnline(t *testing.T) {
	h := newHarness(t, auth.Off)
	d := h.pair("")
	call := h.start("", d, `{"method":"nfc.read","timeout_s":60}`)
	h.clock.add(61 * time.Second)
	// No timer fired: the read of the call ends it.
	v := h.view("", call)
	expect(t, outcome(v), any("timeout"))
	expectStatus(t, h.post(d, call, "result", `{"outcome":"error"}`), 409)
}

func TestCallWaitTimesOut(t *testing.T) {
	h := newHarness(t, auth.Off)
	d := h.pair("")
	go func() {
		time.Sleep(200 * time.Millisecond)
		h.clock.add(2 * time.Second)
	}()
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls?wait=true", "", `{"method":"nfc.read","timeout_s":1}`)
	expectStatus(t, got, 504)
	expect(t, got.body["reason"], any("timeout"))
}

func TestCallWaitAnswersTheResult(t *testing.T) {
	h := newHarness(t, auth.Off)
	d := h.pair("")
	sock := h.pods[0].device(d)
	done := make(chan answer, 1)
	go func() {
		done <- h.do("POST", "/api/v1/devices/"+d.id+"/calls?wait=true", "", `{"method":"nfc.read","timeout_s":30}`)
	}()
	f := sock.frame()
	call := f["data"].(map[string]any)["call"].(string)
	expectStatus(t, h.post(d, call, "result", `{"outcome":"error","error":"no tag"}`), 204)
	got := <-done
	expectStatus(t, got, 200)
	expect(t, outcome(got), any("error"))
	expect(t, got.body["result"].(map[string]any)["error"], any("no tag"))
}

func TestCallOnlyOwnerByDefault(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me, you := h.person("user:i#me"), h.person("user:i#you")
	d := h.pair(me)
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", you, `{"method":"nfc.read"}`)
	expectStatus(t, got, 403)
	expect(t, got.body["reason"], any("not_your_device"))
	// Nor the assistant of another person.
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", h.assistant("user:i#you"), `{"method":"nfc.read"}`), 403)
	call := h.start(me, d, `{"method":"nfc.read"}`)
	expectStatus(t, h.do("GET", "/api/v1/devices/calls/"+call, you, ""), 404)
	// Nor may another person follow the call's events.
	page, _ := h.pods[0].dial(map[string]string{"Cookie": you})
	page.send(`{"type":"subscribe","topics":["device:call:` + call + `"]}`)
	expect(t, page.frame()["type"], any("error"))
}

func TestCallRefusals(t *testing.T) {
	h := newHarness(t, auth.Off)
	made := h.do("POST", "/api/v1/devices/pairings", "", "")
	claimed := h.claim(made.body["code_url"].(string), `["nfc.read","nfc.write"]`)
	d := paired{id: claimed.body["device_id"].(string), token: claimed.body["token"].(string)}
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", "", `{"method":"code.scan"}`)
	expectStatus(t, got, 422)
	expect(t, got.body["reason"], any("method_not_announced"))
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", "", `{"method":"location.get"}`), 422)
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", "", `{"method":"nfc.read","timeout_s":121}`), 422)
	// A write takes NDEF records only: never raw blocks or keys.
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", "",
		`{"method":"nfc.write","args":{"records":[{"recordType":"text","text":"x"}],"blocks":[{"index":4}]}}`), 422)
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", "", `{"method":"nfc.write","args":{}}`), 422)
	h.clock.add(2 * time.Minute)
	got = h.do("POST", "/api/v1/devices/"+d.id+"/calls", "", `{"method":"nfc.read"}`)
	expectStatus(t, got, 409)
	expect(t, got.body["error"], any("offline"))
	expect(t, h.count(`SELECT count(*) FROM device_calls`), 0)
}

func approve(h *harness, call, decision string) {
	h.t.Helper()
	usable := "NULL"
	if decision == "approved" {
		usable = "now() + interval '10 minutes'"
	}
	h.sql(`UPDATE ai_approvals a SET decision = $2, decided_at = now(), usable_until = `+usable+`
		FROM device_calls c WHERE c.id = $1::uuid AND a.id = c.approval_id`, call, decision)
}

// An assistant's nfc.write waits for a person: a pending action, and nothing goes to the
// device until the assistant confirms the approved call.
func TestWriteFromAssistantNeedsApproval(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	sock := h.pods[0].device(d)
	bot := h.assistant("user:i#me")
	write := `{"method":"nfc.write","args":{"records":[{"recordType":"text","text":"PLA #12"}]}}`
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", bot, write)
	expectStatus(t, got, 202)
	call, pending := got.body["call_id"].(string), got.body["pending_action_id"].(string)
	expect(t, h.count(`SELECT count(*) FROM ai_approvals WHERE id = $1::uuid AND session_id IS NULL AND tool = 'device_call'
		AND tier = 'outward' AND decision IS NULL AND requested_by_id = 'user:i#me'`, pending), 1)
	expect(t, h.count(`SELECT count(*) FROM device_calls WHERE id = $1::uuid AND sent_at IS NULL`, call), 1)
	confirm := func(who string) answer { return h.do("POST", "/api/v1/devices/calls/"+call+"/confirm", who, "") }
	c := confirm(bot)
	expectStatus(t, c, 409)
	expect(t, c.body["reason"], any("approval_pending"))
	approve(h, call, "approved")
	// Only the caller that made it confirms it.
	expectStatus(t, confirm(me), 404)
	expectStatus(t, confirm(bot), 202)
	data := requestedOn(t, sock, call)
	expect(t, data["method"], any("nfc.write"))
	expect(t, h.count(`SELECT count(*) FROM ai_approvals WHERE id = $1::uuid AND consumed_at IS NOT NULL`, pending), 1)
	expectStatus(t, confirm(bot), 409)

	// A denied one ends the call denied, and nothing is sent.
	got = h.do("POST", "/api/v1/devices/"+d.id+"/calls", bot, write)
	denied := got.body["call_id"].(string)
	approve(h, denied, "denied")
	c = h.do("POST", "/api/v1/devices/calls/"+denied+"/confirm", bot, "")
	expectStatus(t, c, 409)
	expect(t, c.body["reason"], any("approval_denied"))
	expect(t, outcome(h.view(me, denied)), any("denied"))
}

func TestWriteFromPersonGoesAtOnce(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	sock := h.pods[0].device(d)
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", me, `{"method":"nfc.write","args":{"records":[{"recordType":"text","text":"x"}]}}`)
	expectStatus(t, got, 202)
	if _, ok := got.body["pending_action_id"]; ok {
		t.Fatalf("a person's own click needs no approval: %v", got.body)
	}
	requestedOn(t, sock, got.body["call_id"].(string))
	expect(t, h.count(`SELECT count(*) FROM ai_approvals`), 0)
}

// Only a service that holds the delegation grant may name a person in on_behalf_of: the
// agent and the projects worker by default, or a credential with scope devices.call.
func TestDelegationNeedsTheGrant(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	body := `{"method":"nfc.read","on_behalf_of":"user:i#me"}`
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", h.service("render", "api"), body)
	expectStatus(t, got, 403)
	expect(t, got.body["reason"], any("no_delegation"))
	expect(t, h.count(`SELECT count(*) FROM device_calls`), 0)
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", h.service("agent", "api"), body), 202)
	expect(t, h.count(`SELECT count(*) FROM device_calls WHERE caller_kind = 'assistant' AND caller = 'user:i#me'`), 1)
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", h.service("projects", "flow.host"), body), 202)
	expect(t, h.count(`SELECT count(*) FROM device_calls WHERE caller_kind = 'flow'`), 1)
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", h.service("render", "api", DelegateScope), body), 202)
	// A delegate still acts only for the device's owner.
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", h.service("agent", "api"),
		`{"method":"nfc.read","on_behalf_of":"user:i#you"}`), 403)
	// The grant is a setting: with SCADBUDDY_DEVICE_DELEGATES empty, the agent holds none.
	h.store.Delegates = delegates(func(string) (string, bool) { return "", true })
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", h.service("agent", "api"), body), 403)
}

// The pending action shows what will be written, and the confirm refuses a call whose
// records changed after the approval.
func TestConfirmRefusesChangedRecords(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	bot := h.assistant("user:i#me")
	long := strings.Repeat("x", 200)
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", bot,
		`{"method":"nfc.write","args":{"records":[{"recordType":"text","text":"PLA #12","lang":"en"},
		{"recordType":"url","text":"https://spools.example/`+long+`"}]}}`)
	expectStatus(t, got, 202)
	call := got.body["call_id"].(string)
	var summary string
	if err := h.pool.QueryRow(t.Context(), `SELECT input_summary FROM ai_approvals WHERE id = $1::uuid`,
		got.body["pending_action_id"]).Scan(&summary); err != nil {
		t.Fatal(err)
	}
	// The count and every type always; a URI's scheme and host in full, its path cut.
	expect(t, summary, `nfc.write on "Pixel": 2 records; 1. text "PLA #12" lang en; 2. url "https://spools.example/`+
		strings.Repeat("x", 79)+`…"`)
	approve(h, call, "approved")
	h.sql(`UPDATE device_calls SET args = '{"records":[{"recordType":"text","text":"something else"}]}' WHERE id = $1::uuid`, call)
	c := h.do("POST", "/api/v1/devices/calls/"+call+"/confirm", bot, "")
	expectStatus(t, c, 409)
	expect(t, c.body["reason"], any("approval_mismatch"))
	expect(t, outcome(h.view(me, call)), any("denied"))
	expect(t, h.count(`SELECT count(*) FROM ai_approvals WHERE consumed_at IS NOT NULL`), 0)
}

// A write's records are parsed strictly: only the types the app writes, each with the one
// value it uses and no other key. The device is sent the parsed form.
func TestWriteRecordsAreStrict(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	sock := h.pods[0].device(d)
	for name, records := range map[string]string{
		"an extra key":    `[{"recordType":"text","text":"x","hex":"00"}]`,
		"a hidden key":    `[{"recordType":"url","text":"https://a.example/","note":"not shown"}]`,
		"an unknown type": `[{"recordType":"smart-poster","records":[]}]`,
		"a local type":    `[{"recordType":":t","hex":"00"}]`,
		"no value":        `[{"recordType":"mime","mediaType":"text/plain"}]`,
		"two values":      `[{"recordType":"mime","mediaType":"text/plain","text":"a","hex":"00"}]`,
		"a relative URI":  `[{"recordType":"url","text":"/path"}]`,
		// Spoofs: what an approval shows first is not the host the URI names.
		"an opaque URI":      `[{"recordType":"url","text":"https:evil.example/scadbuddy.example"}]`,
		"userinfo":           `[{"recordType":"url","text":"https://scadbuddy.example@evil.example/"}]`,
		"userinfo, absolute": `[{"recordType":"absolute-url","text":"https://scadbuddy.example:x@evil.example/"}]`,
		"no host":            `[{"recordType":"url","text":"https:///path"}]`,
		"a backslash":        `[{"recordType":"url","text":"https://scadbuddy.example\\@evil.example/"}]`,
		"upper-case hex":     `[{"recordType":"example.com:t","hex":"AB"}]`,
		"no records":         `[]`,
		"an id, never shown": `[{"recordType":"text","text":"x","id":"y"}]`,
	} {
		got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", me, `{"method":"nfc.write","args":{"records":`+records+`}}`)
		if got.status != 422 {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	expect(t, h.count(`SELECT count(*) FROM device_calls`), 0)
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", me, `{"method":"nfc.write","args":{"records":[
		{"text":"PLA","recordType":"text"},{"recordType":"example.com:spool","hex":"0a0b"},{"recordType":"empty"}]}}`)
	expectStatus(t, got, 202)
	args := requestedOn(t, sock, got.body["call_id"].(string))["args"].(map[string]any)
	sent, _ := json.Marshal(args)
	expect(t, string(sent), `{"records":[{"recordType":"text","text":"PLA"},{"hex":"0a0b","recordType":"example.com:spool"},{"recordType":"empty"}]}`)
}

// An approval shows all of what is written: a write too large to show is refused, not cut.
func TestWriteTooLargeToApproveRefused(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	bot := h.assistant("user:i#me")
	text := strings.Repeat("y", summaryMax)
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", bot,
		`{"method":"nfc.write","args":{"records":[{"recordType":"text","text":"`+text+`"}]}}`)
	expectStatus(t, got, 422)
	expect(t, got.body["reason"], any("write_too_large_to_approve"))
	expect(t, h.count(`SELECT count(*) FROM ai_approvals`), 0)
	expect(t, h.count(`SELECT count(*) FROM device_calls`), 0)
	// A person's own write of it goes: they see it on their page.
	expectStatus(t, h.do("POST", "/api/v1/devices/"+d.id+"/calls", me,
		`{"method":"nfc.write","args":{"records":[{"recordType":"text","text":"`+text+`"}]}}`), 202)
}

// An approval shows a URI's scheme and host in full, the host in punycode, and cuts only
// the rest.
func TestAnApprovalShowsTheHostInPunycode(t *testing.T) {
	args, err := checkArgs("nfc.write", []byte(`{"records":[{"recordType":"url","text":"https://bücher.example:8443/`+
		strings.Repeat("p", 100)+`"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	shown, err := summary("nfc.write", "Pixel", args)
	if err != nil {
		t.Fatal(err)
	}
	expect(t, shown, `nfc.write on "Pixel": 1 record; 1. url "https://xn--bcher-kva.example:8443/`+strings.Repeat("p", 79)+`…"`)
}
