package devices

import (
	"context"
	"encoding/binary"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
)

func TestHeartbeatMarksOnline(t *testing.T) {
	h := newHarness(t, auth.Off)
	d := h.pair("")
	ctx := context.Background()
	h.clock.add(2 * time.Minute)
	if online, err := h.store.Online(ctx, d.id); err != nil || online {
		t.Fatalf("2 minutes after the claim: online %v, %v", online, err)
	}
	expectStatus(t, h.do("POST", "/api/v1/devices/heartbeat", d.token, ""), 204)
	if online, _ := h.store.Online(ctx, d.id); !online {
		t.Fatal("not online after a heartbeat")
	}
	h.clock.add(74 * time.Second)
	if online, _ := h.store.Online(ctx, d.id); !online {
		t.Fatal("not online 74 s after a heartbeat")
	}
	h.clock.add(2 * time.Second)
	if online, _ := h.store.Online(ctx, d.id); online {
		t.Fatal("online 76 s after a heartbeat")
	}
	list := h.do("GET", "/api/v1/devices", "", "")
	expect(t, list.body["devices"].([]any)[0].(map[string]any)["online"], any(false))
}

// Review focus 3: revoking a device closes its realtime socket at once, and its next
// heartbeat, event, upload or socket gets 401.
func TestRevokeClosesSocket(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me := h.person("user:i#me")
	d := h.pair(me)
	sock := h.pods[0].device(d)
	expectStatus(t, h.do("DELETE", "/api/v1/devices/"+d.id, me, ""), 204)
	for {
		op, payload := sock.next()
		if op == 1 {
			continue // a ping
		}
		expect(t, op, byte(8))
		expect(t, int(binary.BigEndian.Uint16([]byte(payload))), 4401)
		break
	}
	expectStatus(t, h.do("POST", "/api/v1/devices/heartbeat", d.token, ""), 401)
	expectStatus(t, h.do("POST", "/api/v1/devices/calls/"+uuidZero+"/events", d.token, `{"events":[]}`), 401)
	expectStatus(t, h.do("POST", "/api/v1/devices/uploads?call="+uuidZero+"&kind=tag_dump", d.token, `{}`), 401)
	_, res := h.pods[0].dialDevice(map[string]string{"Authorization": "Bearer " + d.token})
	expect(t, res.StatusCode, 401)
}
