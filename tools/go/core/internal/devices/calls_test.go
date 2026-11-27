package devices

import (
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
		`{"method":"nfc.write","args":{"records":[{"type":"text","text":"x"}],"blocks":[{"index":4}]}}`), 422)
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
	write := `{"method":"nfc.write","args":{"records":[{"type":"text","text":"PLA #12"}]}}`
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
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", me, `{"method":"nfc.write","args":{"records":[{"type":"text","text":"x"}]}}`)
	expectStatus(t, got, 202)
	if _, ok := got.body["pending_action_id"]; ok {
		t.Fatalf("a person's own click needs no approval: %v", got.body)
	}
	requestedOn(t, sock, got.body["call_id"].(string))
	expect(t, h.count(`SELECT count(*) FROM ai_approvals`), 0)
}
