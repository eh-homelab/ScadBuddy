package devices

import (
	"encoding/json"
	"net/http"
	"os"
	"strconv"
	"strings"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
)

// fixtureDump is a real Bambu tag's dump (bambutag's testdata, UID DC999BE3, 48 data blocks,
// no trailers) as a tag_dump blob.
func fixtureDump(t *testing.T) string {
	t.Helper()
	raw, err := os.ReadFile("../bambutag/testdata/dc999be3.txt")
	if err != nil {
		t.Fatal(err)
	}
	d := dump{UID: "DC999BE3", Tech: "mifare_classic", Records: []json.RawMessage{}}
	for _, item := range strings.Fields(string(raw)) {
		index, data, _ := strings.Cut(item, ":")
		i, err := strconv.Atoi(index)
		if err != nil {
			t.Fatal(err)
		}
		d.Blocks = append(d.Blocks, dumpBlock{Index: i, DataHex: data})
	}
	if len(d.Blocks) != 48 {
		t.Fatalf("%d blocks", len(d.Blocks))
	}
	out, _ := json.Marshal(d)
	return string(out)
}

// upload posts a blob for an open call and answers its key.
func (h *harness) upload(d paired, call, kind, body string) string {
	h.t.Helper()
	got := h.do("POST", "/api/v1/devices/uploads?call="+call+"&kind="+kind, d.token, body)
	expectStatus(h.t, got, 200)
	return got.body["key"].(string)
}

func TestNfcReadRecorded(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	call := h.start(me, d, `{"method":"nfc.read"}`)
	key := h.upload(d, call, "tag_dump", fixtureDump(t))
	if !strings.HasPrefix(key, "tag_dump-") {
		t.Fatalf("key %q", key)
	}
	expect(t, h.count(`SELECT count(*) FROM blob_refs WHERE key = $1 AND holder_kind = 'device_call' AND holder_id = $2`, key, call), 1)
	done := h.post(d, call, "result", `{"outcome":"done","result":{"read":{"blob_key":"`+key+`","uid":"dc999be3",
		"tech":"mifare_classic","complete":false,"missing_blocks":[40,41]}}}`)
	expectStatus(t, done, 200)
	expect(t, done.body["uid"], any("DC999BE3"))
	expect(t, done.body["complete"], any(false))
	readID := done.body["read_id"].(string)

	read := h.do("GET", "/api/v1/devices/reads/"+readID, me, "")
	expectStatus(t, read, 200)
	for k, want := range map[string]any{"id": readID, "device_id": d.id, "call_id": call, "kind": "nfc", "uid": "DC999BE3",
		"tech": "mifare_classic", "blob_key": key, "media_type": "application/json", "complete": false} {
		expect(t, read.body[k], want)
	}
	expect(t, len(read.body["missing_blocks"].([]any)), 2)
	blob := h.do("GET", "/api/v1/devices/reads/"+readID+"/blob", me, "")
	expectStatus(t, blob, 200)
	expect(t, blob.header.Get("Content-Type"), "application/json")
	expect(t, blob.header.Get("X-Content-Type-Options"), "nosniff")
	var stored dump
	if err := json.Unmarshal([]byte(blob.raw), &stored); err != nil || len(stored.Blocks) != 48 {
		t.Fatalf("blob %v: %s", err, blob.raw[:80])
	}
	r, err := h.store.ReadOf(t.Context(), readID)
	if err != nil || r.BlobKey != key {
		t.Fatal(r, err)
	}
}

// A delegate reads the read its own call made, naming the person as in the call; another
// person, or a delegate naming the wrong person, gets a 404.
func TestDelegateReadsItsOwnRead(t *testing.T) {
	h := newHarness(t, auth.Enforce)
	me := h.person("user:i#me")
	d := h.pair(me)
	agent := h.service("agent", "api")
	got := h.do("POST", "/api/v1/devices/"+d.id+"/calls", agent, `{"method":"nfc.read","on_behalf_of":"user:i#me"}`)
	expectStatus(t, got, 202)
	call := got.body["call_id"].(string)
	key := h.upload(d, call, "tag_dump", fixtureDump(t))
	done := h.post(d, call, "result", `{"outcome":"done","result":{"read":{"blob_key":"`+key+`","uid":"dc999be3",
		"tech":"mifare_classic","complete":true}}}`)
	expectStatus(t, done, 200)
	read := "/api/v1/devices/reads/" + done.body["read_id"].(string)
	expectStatus(t, h.do("GET", read+"?on_behalf_of=user:i%23me", agent, ""), 200)
	expectStatus(t, h.do("GET", read+"/blob?on_behalf_of=user:i%23me", agent, ""), 200)
	// A service that holds no delegation grant is refused before the lookup, as on calls.
	expectStatus(t, h.do("GET", read+"?on_behalf_of=user:i%23me", h.service("render", "api"), ""), 403)
	expectStatus(t, h.do("GET", read+"?on_behalf_of=user:i%23you", agent, ""), 404)
	expectStatus(t, h.do("GET", read, h.person("user:i#you"), ""), 404)
	expectStatus(t, h.do("GET", read, me, ""), 200)
}

// A call that ends while its upload is being stored gets no hold: the hold is written only
// while the call is locked open.
func TestUploadAfterTheCallEndsHoldsNothing(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	call := h.start(me, d, `{"method":"nfc.read"}`)
	// The call ends after the first check and the Put, before the hold.
	h.blobs.onPut = func() {
		h.sql(`UPDATE device_calls SET outcome = 'timeout', ended_at = now() WHERE id = $1::uuid`, call)
	}
	got := h.do("POST", "/api/v1/devices/uploads?call="+call+"&kind=tag_dump", d.token, fixtureDump(t))
	expectStatus(t, got, 409)
	expect(t, got.body["reason"], any("call_closed"))
	h.blobs.mu.Lock()
	stored := len(h.blobs.objects)
	h.blobs.mu.Unlock()
	expect(t, stored, 1)
	expect(t, h.count(`SELECT count(*) FROM blob_refs WHERE holder_kind = 'device_call'`), 0)
}

// Review focus 5: a result that names a blob the device did not upload for this call, or of
// a kind its method does not make, is refused: no device_reads row, and the call ends error.
func TestResultRefusesForeignBlob(t *testing.T) {
	h := newHarness(t, auth.Off)
	d := h.pair("")
	other := h.start("", d, `{"method":"nfc.read"}`)
	foreign := h.upload(d, other, "tag_dump", fixtureDump(t))
	photoCall := h.start("", d, `{"method":"camera.capture"}`)
	photo := h.upload(d, photoCall, "scan_image", "\xff\xd8\xff\xe0 a jpeg")

	for _, key := range []string{foreign, photo, "tag_dump-" + strings.Repeat("0", 64)} {
		call := h.start("", d, `{"method":"nfc.read"}`)
		got := h.post(d, call, "result", `{"outcome":"done","result":{"read":{"blob_key":"`+key+`","uid":"DC999BE3"}}}`)
		expectStatus(t, got, 422)
		expect(t, got.body["reason"], any("foreign_blob"))
		v := h.view("", call)
		expect(t, outcome(v), any("error"))
		expect(t, kinds(v)[len(kinds(v))-1], "device.call_failed")
	}
	// A photo named by a scan call's own upload goes through; its type is the bytes'.
	got := h.post(d, photoCall, "result", `{"outcome":"done","result":{"read":{"blob_key":"`+photo+`","media_type":"image/png"}}}`)
	expectStatus(t, got, 200)
	read := h.do("GET", "/api/v1/devices/reads/"+got.body["read_id"].(string), "", "")
	expect(t, read.body["media_type"], any("image/jpeg"))
	expect(t, read.body["kind"], any("photo"))
	expect(t, h.count(`SELECT count(*) FROM device_reads`), 1)
}

func TestUploadKindMustBeTheMethods(t *testing.T) {
	h := newHarness(t, auth.Off)
	d := h.pair("")
	call := h.start("", d, `{"method":"code.scan"}`)
	got := h.do("POST", "/api/v1/devices/uploads?call="+call+"&kind=tag_dump", d.token, fixtureDump(t))
	expectStatus(t, got, 422)
	expect(t, got.body["reason"], any("kind_mismatch"))
	got = h.do("POST", "/api/v1/devices/uploads?call="+call+"&kind=scan_image", d.token, "GIF89a not one")
	expect(t, got.status, http.StatusUnsupportedMediaType)
	// Another device may not upload for this call.
	expectStatus(t, h.do("POST", "/api/v1/devices/uploads?call="+call+"&kind=scan_image", h.pair("").token, "\xff\xd8\xffx"), 404)
}

func TestDumpRefusesTrailer(t *testing.T) {
	h := newHarness(t, auth.Off)
	d := h.pair("")
	call := h.start("", d, `{"method":"nfc.read"}`)
	for _, index := range []int{3, 7, 63} {
		body := `{"uid":"DC999BE3","tech":"mifare_classic","blocks":[{"index":` + strconv.Itoa(index) +
			`,"data_hex":"ffffffffffffff078069ffffffffffff"}],"records":[]}`
		got := h.do("POST", "/api/v1/devices/uploads?call="+call+"&kind=tag_dump", d.token, body)
		expectStatus(t, got, 422)
		expect(t, got.body["reason"], any("key_trailer"))
	}
	for _, body := range []string{`{"uid":"DC999BE3","blocks":[],"keys":["ffffffffffff"]}`, `{"uid":"zz"}`, `not json`} {
		expectStatus(t, h.do("POST", "/api/v1/devices/uploads?call="+call+"&kind=tag_dump", d.token, body), 422)
	}
	expect(t, len(h.blobs.objects), 0)
}

func TestReadsOnlyWhoMaySeeTheCall(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me, you := h.person("user:i#me"), h.person("user:i#you")
	d := h.pair(me)
	call := h.start(me, d, `{"method":"nfc.read"}`)
	key := h.upload(d, call, "tag_dump", fixtureDump(t))
	done := h.post(d, call, "result", `{"outcome":"done","result":{"read":{"blob_key":"`+key+`","uid":"DC999BE3"}}}`)
	readID := done.body["read_id"].(string)
	expectStatus(t, h.do("GET", "/api/v1/devices/reads/"+readID, you, ""), 404)
	expectStatus(t, h.do("GET", "/api/v1/devices/reads/"+readID+"/blob", you, ""), 404)
	expectStatus(t, h.do("GET", "/api/v1/devices/reads/"+readID, me, ""), 200)
}
