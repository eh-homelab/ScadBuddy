package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/changes"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// The event stream's areas (#7768): the change streams and the realtime socket, over a hub
// that LISTENs on the test's own database.

type streamAPI struct {
	*api
	hub *bus.Hub
}

func newStreams(t *testing.T, env map[string]string) *streamAPI {
	t.Helper()
	pool := testdb.New(t)
	hub := bus.NewHub(pool, nil)
	t.Cleanup(hub.Close)
	st := &streams{hub: hub, env: func(name string) string { return env[name] }}
	router, err := newRouter(pool, http.NotFoundHandler(), nil, "changes, ws", env["SCADBUDDY_AUTH_MODE"], nil, st)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	a.token = a.credential("agent", "api", "crud.agent")
	hub.Start(5 * time.Second)
	return &streamAPI{api: a, hub: hub}
}

// sse is an open change stream.
type sse struct {
	t    *testing.T
	res  *http.Response
	read *bufio.Reader
}

func (s *streamAPI) open(caller, token string, header map[string]string) *sse {
	s.t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	s.t.Cleanup(cancel)
	req, _ := http.NewRequestWithContext(ctx, "GET", s.url+"/api/v1/internal/crud/"+caller+"/changes", nil)
	// As sbfleet's Bus asks for it.
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("Authorization", "Bearer "+token)
	for k, v := range header {
		req.Header.Set(k, v)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		s.t.Fatal(err)
	}
	s.t.Cleanup(func() { res.Body.Close() })
	return &sse{t: s.t, res: res, read: bufio.NewReader(res.Body)}
}

// next is the next frame, its blank line included.
func (e *sse) next() string {
	e.t.Helper()
	got := make(chan string, 1)
	go func() {
		var b strings.Builder
		for {
			line, err := e.read.ReadString('\n')
			b.WriteString(line)
			if err != nil || line == "\n" {
				got <- b.String()
				return
			}
		}
	}()
	select {
	case frame := <-got:
		return frame
	case <-time.After(10 * time.Second):
		e.t.Fatal("no frame within 10 s")
		return ""
	}
}

func (s *streamAPI) publish(kind string, fields ...httpx.Field) string {
	s.t.Helper()
	bus.Publish(context.Background(), s.pool, kind, fields...)
	var payload string
	if err := s.pool.QueryRow(context.Background(),
		`SELECT payload::text FROM events ORDER BY seq DESC LIMIT 1`).Scan(&payload); err != nil {
		s.t.Fatal(err)
	}
	e, err := bus.Decode(payload)
	if err != nil {
		s.t.Fatal(err)
	}
	return e.ID
}

// notify sends a NOTIFY as a Python publisher does, and logs it when logged.
func (s *streamAPI) notify(channel, payload string, logged bool) {
	s.t.Helper()
	if logged {
		e, err := bus.Decode(payload)
		if err != nil {
			s.t.Fatal(err)
		}
		s.sql(`INSERT INTO events (event_id, kind, at, payload) VALUES ($1, $2, now(), $3::jsonb)`, e.ID, e.Kind, payload)
	}
	s.sql(`SELECT pg_notify($1, $2)`, channel, payload)
}

func eventPayload(kind, extra string) (id, payload string) {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	id = hex.EncodeToString(b)
	return id, `{"id":"` + id + `","at":"2026-11-26T10:00:00Z","kind":"` + kind + `"` + extra + `}`
}

func ready(resync, reconnect bool, id string) string {
	return bus.SSE("ready", bus.Object("resync", resync, "reconnect", reconnect), id)
}

func TestAChangeStreamSendsWhatTheListenerHears(t *testing.T) {
	s := newStreams(t, nil)
	stream := s.open("agent", s.token, nil)
	expect(t, stream.res.StatusCode, 200)
	expect(t, stream.res.Header.Get("Content-Type"), "text/event-stream; charset=utf-8")
	expect(t, stream.res.Header.Get("X-ScadBuddy-Crud"), "1")
	expect(t, stream.next(), ready(false, false, "0"))

	id, payload := eventPayload("model.updated", `,"slug":"é-model"`)
	s.notify(bus.Channel, payload, true)
	expect(t, stream.next(), bus.ChangeFrame(bus.Channel, payload, id))
	s.notify("scadbuddy_input", `{"id":"gate"}`, false)
	expect(t, stream.next(), "event: change\ndata: {\"c\":\"scadbuddy_input\",\"p\":\"{\\\"id\\\":\\\"gate\\\"}\"}\n\n")
	_, session := eventPayload("session.message", `,"session_id":"s","seq":1`)
	s.notify(bus.Channel, session, false)
	expect(t, stream.next(), bus.ChangeFrame(bus.Channel, session, ""))
	// A fleet. kind goes to the fleet caller's stream only.
	_, fleetMsg := eventPayload("fleet.message", `,"topics":[],"seq":1`)
	s.notify(bus.Channel, fleetMsg, true)
	s.notify("scadbuddy_warm_evict", "*", false)
	expect(t, stream.next(), "event: change\ndata: {\"c\":\"scadbuddy_warm_evict\",\"p\":\"*\"}\n\n")
}

func TestTheFleetAndCameraStreamsCarryOnlyTheirKinds(t *testing.T) {
	s := newStreams(t, nil)
	fleet := s.open("fleet", s.credential("fleet", "crud.fleet"), nil)
	camera := s.open("camera-relay", s.credential("camera-relay", "crud.camera-relay"), nil)
	expect(t, fleet.next(), ready(false, false, "0"))
	expect(t, camera.next(), ready(false, false, "0"))
	s.notify("scadbuddy_input", `{}`, false)
	_, model := eventPayload("model.updated", `,"slug":"x"`)
	s.notify(bus.Channel, model, true)
	id, msg := eventPayload("fleet.message", `,"topics":["fleet:agent:x"],"seq":1`)
	s.notify(bus.Channel, msg, true)
	expect(t, fleet.next(), bus.ChangeFrame(bus.Channel, msg, id))
	printer, changed := eventPayload("printer.changed", `,"printer_id":"p"`)
	s.notify(bus.Channel, changed, true)
	expect(t, camera.next(), bus.ChangeFrame(bus.Channel, changed, printer))
}

func TestAReconnectReplaysTheLogAfterItsPlace(t *testing.T) {
	s := newStreams(t, nil)
	first := s.publish("fleet.message", httpx.Field{Key: "seq", Value: 1})
	second := s.publish("fleet.message", httpx.Field{Key: "seq", Value: 2},
		httpx.Field{Key: "text", Value: "é"})
	var stored string
	if err := s.pool.QueryRow(context.Background(), `SELECT payload::text FROM events WHERE event_id = $1`,
		second).Scan(&stored); err != nil {
		t.Fatal(err)
	}
	e, _ := bus.Decode(stored)
	token := s.credential("fleet", "crud.fleet")

	stream := s.open("fleet", token, map[string]string{"Last-Event-ID": first})
	expect(t, stream.next(), bus.ChangeFrame(bus.Channel, e.Encoded(), second))
	expect(t, stream.next(), ready(false, true, second))

	// The agent's stream scans past the fleet kinds it leaves out: its place moves all the same.
	agent := s.open("agent", s.token, map[string]string{"Last-Event-ID": "0"})
	expect(t, agent.next(), ready(false, true, second))

	for _, place := range []string{"never-logged", "not an id", strings.Repeat("a", 65)} {
		lost := s.open("fleet", token, map[string]string{"Last-Event-ID": place})
		expect(t, lost.next(), ready(true, true, second))
		lost.res.Body.Close()
	}
}

func TestAGapReplaysAndSaysReconnected(t *testing.T) {
	s := newStreams(t, nil)
	stream := s.open("agent", s.token, nil)
	expect(t, stream.next(), ready(false, false, "0"))
	id, payload := eventPayload("model.updated", `,"slug":"gap"`)
	// Logged while nobody here heard it: the gap's replay finds it.
	s.sql(`INSERT INTO events (event_id, kind, at, payload) VALUES ($1, 'model.updated', now(), $2::jsonb)`, id, payload)
	s.hub.Gap()
	e, _ := bus.Decode(payload)
	expect(t, stream.next(), bus.ChangeFrame(bus.Channel, e.Encoded(), id))
	expect(t, stream.next(), bus.SSE("reconnected", bus.Object("resync", false), id))
	// The live copy of a replayed event is not sent again.
	s.sql(`SELECT pg_notify($1, $2)`, bus.Channel, payload)
	s.notify("scadbuddy_warm_evict", "after", false)
	expect(t, stream.next(), "event: change\ndata: {\"c\":\"scadbuddy_warm_evict\",\"p\":\"after\"}\n\n")
	// A pruned gap is a resync.
	s.sql(`DELETE FROM events`)
	s.hub.Gap()
	expect(t, stream.next(), bus.SSE("reconnected", bus.Object("resync", true), "0"))
}

func TestAQuietStreamSendsAKeepalive(t *testing.T) {
	before := changes.Keepalive
	changes.Keepalive = 50 * time.Millisecond
	t.Cleanup(func() { changes.Keepalive = before })
	s := newStreams(t, nil)
	stream := s.open("agent", s.token, nil)
	expect(t, stream.next(), ready(false, false, "0"))
	expect(t, stream.next(), ": keepalive\n\n")
}

func TestAChangeStreamRefusesAWrongCredentialAndOneTooMany(t *testing.T) {
	s := newStreams(t, nil)
	base := "/api/v1/internal/crud/"
	s.token = "sbsvc_0000000000000000.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
	expect(t, s.do("GET", base+"agent/changes", "").status, 401)
	s.token = s.credential("agent", "crud.agent")
	expect(t, s.do("GET", base+"fleet/changes", "").status, 403)
	expect(t, s.do("GET", base+"fleet/changes", "").body["reason"], "wrong_caller")
	s.token = s.credential("agent", "api")
	expect(t, s.do("GET", base+"agent/changes", "").body["reason"], "out_of_scope")

	token := s.credential("agent", "crud.agent")
	for range 4 {
		expect(t, s.open("agent", token, nil).next(), ready(false, false, "0"))
	}
	s.token = token
	full := s.do("GET", base+"agent/changes", "")
	expect(t, full.status, 429)
	expect(t, full.header.Get("Content-Type"), "application/problem+json")
	expect(t, full.body["detail"], "this credential has 4 streams open")
	// Another credential, and the fleet's own budget, still have room.
	expect(t, s.open("agent", s.credential("agent", "crud.agent"), nil).next(), ready(false, false, "0"))
	fleet := s.credential("fleet", "crud.fleet")
	for range changes.FleetStreamsMax {
		s.open("fleet", fleet, nil).next()
	}
	s.token = fleet
	expect(t, s.do("GET", base+"fleet/changes", "").body["detail"], "8 change streams are open for fleet")
}

// ws is a test's WebSocket client.
type ws struct {
	t    *testing.T
	conn net.Conn
	read *bufio.Reader
}

func (s *streamAPI) dial(header map[string]string) (*ws, *http.Response) {
	s.t.Helper()
	conn, err := net.Dial("tcp", strings.TrimPrefix(s.url, "http://"))
	if err != nil {
		s.t.Fatal(err)
	}
	s.t.Cleanup(func() { conn.Close() })
	key := base64.StdEncoding.EncodeToString([]byte("0123456789abcdef"))
	if k, ok := header["Sec-WebSocket-Key"]; ok {
		key = k
	}
	req := "GET /api/v1/ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
		"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + key + "\r\n"
	for k, v := range header {
		if k == "Sec-WebSocket-Key" {
			continue
		}
		req += k + ": " + v + "\r\n"
	}
	if _, err := conn.Write([]byte(req + "\r\n")); err != nil {
		s.t.Fatal(err)
	}
	read := bufio.NewReader(conn)
	res, err := http.ReadResponse(read, nil)
	if err != nil {
		s.t.Fatal(err)
	}
	return &ws{t: s.t, conn: conn, read: read}, res
}

func (w *ws) send(op byte, text string) {
	w.t.Helper()
	mask := []byte{1, 2, 3, 4}
	frame := []byte{0x80 | op}
	if n := len(text); n < 126 {
		frame = append(frame, 0x80|byte(n))
	} else {
		frame = binary.BigEndian.AppendUint16(append(frame, 0x80|126), uint16(n))
	}
	frame = append(frame, mask...)
	for i := range len(text) {
		frame = append(frame, text[i]^mask[i%4])
	}
	if _, err := w.conn.Write(frame); err != nil {
		w.t.Fatal(err)
	}
}

// next is the next message's opcode and payload.
func (w *ws) next() (byte, string) {
	w.t.Helper()
	_ = w.conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	var head [2]byte
	if _, err := io.ReadFull(w.read, head[:]); err != nil {
		w.t.Fatal(err)
	}
	n := int(head[1] & 0x7f)
	if n == 126 {
		var ext [2]byte
		_, _ = io.ReadFull(w.read, ext[:])
		n = int(binary.BigEndian.Uint16(ext[:]))
	}
	payload := make([]byte, n)
	if _, err := io.ReadFull(w.read, payload); err != nil {
		w.t.Fatal(err)
	}
	return head[0] & 0x0f, string(payload)
}

func (w *ws) text() string {
	w.t.Helper()
	op, payload := w.next()
	expect(w.t, op, byte(1))
	return payload
}

func TestTheRealtimeSocketFollowsTopics(t *testing.T) {
	s := newStreams(t, map[string]string{"SCADBUDDY_PUBLIC_URL": "https://scadbuddy.example"})
	sock, res := s.dial(map[string]string{"Origin": "https://scadbuddy.example"})
	expect(t, res.StatusCode, 101)
	sock.send(1, `{"type":"subscribe","topics":["models","model:a","models"]}`)
	expect(t, sock.text(), `{"type":"subscribed","topics":["models","model:a"]}`)

	id := s.publish("model.updated", httpx.Field{Key: "slug", Value: "a"})
	expect(t, sock.text(), `{"type":"event","id":"`+id+`","kind":"model.updated","topics":["models","model:a"],"data":{"slug":"a"}}`)
	// Not followed: no frame. The next one shows nothing came between.
	s.publish("font.installed", httpx.Field{Key: "family", Value: "x"})
	followFonts(t, s, sock)

	// A check: since the first event, which topics changed.
	sock.send(1, `{"type":"check","id":7,"since":["`+id+`"],"topics":["models","model:a","orders"]}`)
	expect(t, sock.text(), `{"type":"changed","id":7,"topics":["orders"]}`)
	c := s.publish("model.updated", httpx.Field{Key: "slug", Value: "c"})
	expect(t, sock.text(), `{"type":"event","id":"`+c+`","kind":"model.updated","topics":["models"],"data":{"slug":"c"}}`)
	sock.send(1, `{"type":"check","id":true,"since":["`+id+`","`+id+`"],"topics":["models","model:a"]}`)
	expect(t, sock.text(), `{"type":"changed","id":true,"topics":["models"]}`)
	sock.send(1, `{"type":"check","id":8,"since":["nope"],"topics":["model:a"]}`)
	expect(t, sock.text(), `{"type":"changed","id":8,"topics":["model:a"]}`)

	sock.send(1, `{"type":"subscribe","topics":["bogus",5,"tasks:é"]}`)
	expect(t, sock.text(), `{"type":"error","message":"unknown topics: ['bogus', 5]"}`)
	sock.send(1, `nope`)
	expect(t, sock.text(), `{"type":"error","message":"not JSON"}`)
	sock.send(2, `{}`)
	expect(t, sock.text(), `{"type":"error","message":"expected a text frame"}`)
	sock.send(1, `{"type":"check","id":1.5,"since":["x"],"topics":[]}`)
	expect(t, sock.text(), `{"type":"error","message":"a check needs an id, since and topics"}`)

	s.hub.Gap()
	expect(t, sock.text(), `{"type":"resync"}`)
	sock.send(1, `{"type":"subscribe","topics":["fonts"]}`)
	var ack map[string]any
	_ = json.Unmarshal([]byte(sock.text()), &ack)
	if _, ok := ack["last"]; ok {
		t.Errorf("a subscribe after a gap carries last: %v", ack)
	}
}

// followFonts follows fonts, with the last event heard in its ack, and reads the font event
// that comes after it: no frame came for a topic not followed.
func followFonts(t *testing.T, s *streamAPI, sock *ws) {
	t.Helper()
	sock.send(1, `{"type":"subscribe","topics":["fonts"]}`)
	want := `{"type":"subscribed","topics":["fonts"],"last":"` + lastID(t, s) + `"}`
	if got := sock.text(); got != want {
		// The listener may not have heard the last event yet; then last is the one before.
		var ack map[string]any
		_ = json.Unmarshal([]byte(got), &ack)
		expect(t, ack["type"], "subscribed")
	}
	id := s.publish("font.installed", httpx.Field{Key: "family", Value: "y"})
	expect(t, sock.text(), `{"type":"event","id":"`+id+`","kind":"font.installed","topics":["fonts"],"data":{"family":"y"}}`)
}

func lastID(t *testing.T, s *streamAPI) string {
	var id string
	if err := s.pool.QueryRow(context.Background(), `SELECT event_id FROM events ORDER BY seq DESC LIMIT 1`).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

func TestTheRealtimeSocketRefusesAForeignOriginAndASignedOutCaller(t *testing.T) {
	s := newStreams(t, map[string]string{"SCADBUDDY_PUBLIC_URL": "https://scadbuddy.example"})
	_, res := s.dial(map[string]string{"Origin": "https://evil.example"})
	expect(t, res.StatusCode, 403)
	_, res = s.dial(map[string]string{"Origin": "http://localhost:5173"})
	expect(t, res.StatusCode, 101)

	enforced := newStreams(t, map[string]string{"SCADBUDDY_AUTH_MODE": "enforce"})
	_, res = enforced.dial(nil)
	expect(t, res.StatusCode, 403)
	_, res = enforced.dial(map[string]string{"Authorization": "Bearer " + enforced.credential("agent", "api")})
	expect(t, res.StatusCode, 101)
}

func TestARevokedSessionClosesItsSocket(t *testing.T) {
	s := newStreams(t, map[string]string{"SCADBUDDY_AUTH_MODE": "observe"})
	token := strings.Repeat("a", 43)
	s.sql(`INSERT INTO auth_sessions (id_hash, principal, issuer, subject, kind, created_at, last_seen_at,
		idle_expires_at, absolute_expires_at) VALUES (sha256($1::bytea), 'user:i#s', 'i', 's', 'login', now(), now(),
		now() + interval '1 day', now() + interval '2 days')`, []byte(token))
	sock, res := s.dial(map[string]string{"Cookie": "__Host-scadbuddy_session=" + token})
	expect(t, res.StatusCode, 101)
	s.publish("auth.session_revoked", httpx.Field{Key: "principal", Value: "user:other#x"})
	s.publish("auth.session_revoked", httpx.Field{Key: "principal", Value: "user:i#s"})
	op, payload := sock.next()
	expect(t, op, byte(8))
	expect(t, int(binary.BigEndian.Uint16([]byte(payload))), 4401)
	expect(t, payload[2:], "signed out")
}
