package main

import (
	"bufio"
	"encoding/binary"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/changes"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/realtime"
)

// The event stream's limits and refusals (#7768, the review of PR #8027).

// A NOTIFY-only kind that no build knows (unknown_feature.changed) is an opaque event and goes
// out with its id, as hub.py sends it. The log does not hold it, so a gap after it, or a
// reconnect from it, is a resync, as in Python.
func TestANotifyOnlyOpaqueEventsIDIsAResyncPlace(t *testing.T) {
	s := newStreams(t, nil)
	stream := s.open("agent", s.token, nil)
	expect(t, stream.next(), ready(false, false, "0"))
	logged := s.publish("model.updated", httpx.Field{Key: "slug", Value: "a"})
	expect(t, strings.HasPrefix(stream.next(), "id: "+logged+"\n"), true)
	id, payload := eventPayload("unknown_feature.changed", `,"operation_id":"op"`)
	s.notify(bus.Channel, payload, false)
	expect(t, stream.next(), bus.ChangeFrame(bus.Channel, payload, id))
	s.hub.Gap()
	expect(t, stream.next(), bus.SSE("reconnected", bus.Object("resync", true), logged))

	again := s.open("agent", s.token, map[string]string{"Last-Event-ID": id})
	expect(t, again.next(), ready(true, true, logged))
}

// inventory_operation.changed is a typed NOTIFY-only kind (#6949 K1, bus.Event.NotifyOnly):
// its change frame carries no id, as a session. kind's does.
func TestATypedInventoryOperationFrameHasNoID(t *testing.T) {
	s := newStreams(t, nil)
	stream := s.open("agent", s.token, nil)
	expect(t, stream.next(), ready(false, false, "0"))
	_, payload := eventPayload("inventory_operation.changed", `,"operation_id":"op"`)
	s.notify(bus.Channel, payload, false)
	expect(t, stream.next(), bus.ChangeFrame(bus.Channel, payload, ""))
}

// The per-credential cap counts the TCP peer, as uvicorn --no-proxy-headers gives Python's
// stream_key: an X-Forwarded-For value does not make a new peer.
func TestAChangeStreamsCapIgnoresXForwardedFor(t *testing.T) {
	s := newStreams(t, nil)
	token := s.credential("agent", "crud.agent")
	for i := range 4 {
		stream := s.open("agent", token, map[string]string{"X-Forwarded-For": "10.0.0." + string(rune('1'+i))})
		expect(t, stream.next(), ready(false, false, "0"))
	}
	full := s.open("agent", token, map[string]string{"X-Forwarded-For": "10.0.0.9"})
	expect(t, full.res.StatusCode, 429)
}

// A client that stops reading loses its stream once a write waits past changes.WriteTimeout,
// so its slot under the cap is free again.
func TestAStalledChangeStreamFreesItsSlot(t *testing.T) {
	before := changes.WriteTimeout
	changes.WriteTimeout = 200 * time.Millisecond
	t.Cleanup(func() { changes.WriteTimeout = before })
	s := newStreams(t, nil)
	token := s.credential("agent", "crud.agent")
	for range 4 {
		conn, err := net.Dial("tcp", strings.TrimPrefix(s.url, "http://"))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { conn.Close() })
		_ = conn.(*net.TCPConn).SetReadBuffer(4096)
		req := "GET /api/v1/internal/crud/agent/changes HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + token + "\r\n\r\n"
		if _, err := conn.Write([]byte(req)); err != nil {
			t.Fatal(err)
		}
		res, err := http.ReadResponse(bufio.NewReader(conn), nil)
		if err != nil {
			t.Fatal(err)
		}
		expect(t, res.StatusCode, 200)
		// Nothing more is read from conn.
	}
	s.token = token
	expect(t, s.do("GET", "/api/v1/internal/crud/agent/changes", "").status, 429)
	s.sql(`SELECT pg_notify('scadbuddy_warm_evict', g::text || repeat('x', 7000)) FROM generate_series(1, 3000) g`)
	deadline := time.Now().Add(20 * time.Second)
	for {
		stream := s.open("agent", token, nil)
		if stream.res.StatusCode == 200 {
			stream.res.Body.Close()
			return
		}
		stream.res.Body.Close()
		if time.Now().After(deadline) {
			t.Fatal("the stalled streams still hold their slots")
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// closeOf reads frames until the server's close frame, and gives its code and reason.
func closeOf(t *testing.T, sock *ws) (int, string) {
	t.Helper()
	for {
		op, payload := sock.next()
		if op != 8 {
			continue
		}
		if len(payload) < 2 {
			return 1005, ""
		}
		return int(binary.BigEndian.Uint16([]byte(payload))), payload[2:]
	}
}

// A frame whose header claims more than 64 KiB closes the socket with 1009 at once: nothing
// is held for bytes that have not come. 64 KiB still holds Python's largest frame, 16,384
// characters of up to 4 bytes each.
func TestAnOversizedWebSocketFrameClosesAtOnce(t *testing.T) {
	s := newStreams(t, nil)
	sock, res := s.dial(nil)
	expect(t, res.StatusCode, 101)
	head := binary.BigEndian.AppendUint64([]byte{0x81, 0x80 | 127}, 64<<10+1)
	if _, err := sock.conn.Write(append(head, 1, 2, 3, 4)); err != nil {
		t.Fatal(err)
	}
	code, _ := closeOf(t, sock)
	expect(t, code, 1009)
	expect(t, realtime.MessageMax, 4*realtime.MaxFrameChars)
}

// A client's close is answered per RFC 6455 §7.4: a bad close payload is a protocol error
// (1002), a reason that is not UTF-8 is 1007, and a good code is echoed.
func TestAWebSocketCloseIsCheckedAsRFC6455Says(t *testing.T) {
	s := newStreams(t, nil)
	code := func(n int) string { return string(binary.BigEndian.AppendUint16(nil, uint16(n))) }
	for _, c := range []struct {
		name, payload string
		want          int
	}{
		{"one byte", "\x03", 1002},
		{"1005 sent", code(1005), 1002},
		{"1006 sent", code(1006), 1002},
		{"1015 sent", code(1015), 1002},
		{"below 1000", code(999), 1002},
		{"5000", code(5000), 1002},
		{"a reason not UTF-8", code(1000) + "\xff", 1007},
		{"a normal close", code(1000) + "bye", 1000},
		{"an app's code", code(4000), 4000},
		{"no payload", "", 1005},
	} {
		sock, res := s.dial(nil)
		expect(t, res.StatusCode, 101)
		sock.send(8, c.payload)
		got, _ := closeOf(t, sock)
		if got != c.want {
			t.Errorf("%s: closed with %d, want %d", c.name, got, c.want)
		}
	}
}

// Pings count in the frame rate: a flood closes the socket as a flood of messages does.
func TestAPingFloodClosesTheSocket(t *testing.T) {
	s := newStreams(t, nil)
	sock, res := s.dial(nil)
	expect(t, res.StatusCode, 101)
	for range realtime.RateBurst + 10 {
		sock.send(9, "")
	}
	code, reason := closeOf(t, sock)
	expect(t, code, 1008)
	expect(t, reason, "too many frames")
}

// A handshake's key is base64 of 16 bytes (RFC 6455 §4.2.1); another is a 400.
func TestAWebSocketKeyMustBeSixteenBytes(t *testing.T) {
	s := newStreams(t, nil)
	for _, key := range []string{"abc", "AAAAAAAAAAAAAAAAAAAAAAAAAAAA", "not base64 at all!!!!!=="} {
		_, res := s.dial(map[string]string{"Sec-WebSocket-Key": key})
		expect(t, res.StatusCode, 400)
	}
}

// A session revoked while its revocation event was lost (an overflow or a LISTEN outage) is
// found at the resync that the loss causes: the socket closes as signed out.
func TestARevokedSessionIsFoundAtAResync(t *testing.T) {
	s := newStreams(t, map[string]string{"SCADBUDDY_AUTH_MODE": "observe"})
	token := strings.Repeat("b", 43)
	s.sql(`INSERT INTO auth_sessions (id_hash, principal, issuer, subject, kind, created_at, last_seen_at,
		idle_expires_at, absolute_expires_at) VALUES (sha256($1::bytea), 'user:i#s', 'i', 's', 'login', now(), now(),
		now() + interval '1 day', now() + interval '2 days')`, []byte(token))
	sock, res := s.dial(map[string]string{"Cookie": "__Host-scadbuddy_session=" + token})
	expect(t, res.StatusCode, 101)
	s.hub.Gap()
	expect(t, sock.text(), `{"type":"resync"}`)
	s.sql(`UPDATE auth_sessions SET revoked_at = now() WHERE id_hash = sha256($1::bytea)`, []byte(token))
	s.hub.Gap()
	code, reason := closeOf(t, sock)
	expect(t, code, realtime.SignedOut)
	expect(t, reason, "signed out")
}
