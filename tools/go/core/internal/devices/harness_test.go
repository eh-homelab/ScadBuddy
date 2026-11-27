package devices

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobstore"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/realtime"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

// clock is a test's clock: it moves only when the test moves it.
type clock struct {
	mu sync.Mutex
	at time.Time
}

func (c *clock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.at
}

func (c *clock) add(d time.Duration) {
	c.mu.Lock()
	c.at = c.at.Add(d)
	c.mu.Unlock()
}

// memBlobs is the blob store in memory.
type memBlobs struct {
	mu      sync.Mutex
	objects map[string][]byte
}

func (m *memBlobs) Put(_ context.Context, kind, _ string, body []byte) (blobstore.Committed, error) {
	limit, ok := blobstore.Kinds[kind]
	if !ok {
		return blobstore.Committed{}, httpx.WithReason(422, "kind", "kind_not_allowed")
	}
	if int64(len(body)) > limit {
		return blobstore.Committed{}, httpx.WithReason(413, "too large", "upload_too_large")
	}
	sum := sha256.Sum256(body)
	sha := hex.EncodeToString(sum[:])
	key := kind + "-" + sha
	m.mu.Lock()
	m.objects[key] = append([]byte(nil), body...)
	m.mu.Unlock()
	return blobstore.Committed{Key: key, SHA256: sha, Size: int64(len(body))}, nil
}

func (m *memBlobs) OpenKey(_ context.Context, key string) (io.ReadCloser, blobstore.Ref, error) {
	m.mu.Lock()
	body, ok := m.objects[key]
	m.mu.Unlock()
	if !ok {
		return nil, blobstore.Ref{}, blobstore.ErrBlobNotFound
	}
	return io.NopCloser(bytes.NewReader(body)), blobstore.Ref{Size: int64(len(body))}, nil
}

// harness is one database and one store, served by one or more pods (routers with a hub of
// their own each, as two API pods have).
type harness struct {
	t     *testing.T
	pool  *pgxpool.Pool
	store *Store
	clock *clock
	blobs *memBlobs
	// timers are the deadline checks the store scheduled; fire runs them.
	mu     sync.Mutex
	timers []func()
	pods   []*pod
}

type pod struct {
	h   *harness
	url string
	hub *bus.Hub
}

func newHarness(t *testing.T, mode auth.Mode) *harness {
	t.Helper()
	pool := testdb.New(t)
	h := &harness{t: t, pool: pool, clock: &clock{at: time.Now().UTC().Truncate(time.Second)},
		blobs: &memBlobs{objects: map[string][]byte{}}}
	h.store = &Store{Pool: pool, Keys: testKeys(t), Blobs: h.blobs, Now: h.clock.now, Idle: DefaultIdle,
		HardCap: DefaultHardCap, Poll: 20 * time.Millisecond, Delegates: DefaultDelegates,
		After: func(_ time.Duration, f func()) { h.mu.Lock(); h.timers = append(h.timers, f); h.mu.Unlock() }}
	h.addPod(mode)
	return h
}

func testKeys(t *testing.T) *seal.Keys {
	t.Helper()
	path := t.TempDir() + "/kek"
	key := make([]byte, 32)
	_, _ = rand.Read(key)
	if err := os.WriteFile(path, []byte(base64.StdEncoding.EncodeToString(key)), 0o600); err != nil {
		t.Fatal(err)
	}
	keys, err := seal.LoadKeys(func(name string) string {
		if name == seal.Variable {
			return path
		}
		return ""
	})
	if err != nil || keys == nil {
		t.Fatalf("no KEK: %v", err)
	}
	return keys
}

// addPod serves the store from a new router with its own hub and realtime socket.
func (h *harness) addPod(mode auth.Mode) *pod {
	h.t.Helper()
	hub := bus.NewHub(h.pool, nil)
	h.t.Cleanup(hub.Close)
	gate := auth.NewGate(&auth.Resolver{Pool: h.pool}, mode, nil)
	mux := http.NewServeMux()
	for _, p := range h.store.Public() {
		mux.Handle(p.Pattern, gate.Wrap(p.Route, p.Handler))
	}
	sock, err := realtime.New(context.Background(), hub, h.pool, mux, func(string) string { return "" }, gate)
	if err != nil {
		h.t.Fatal(err)
	}
	sock.Devices = h.store
	hub.Start(5 * time.Second)
	server := httptest.NewServer(sock)
	h.t.Cleanup(server.Close)
	p := &pod{h: h, url: server.URL, hub: hub}
	h.pods = append(h.pods, p)
	return p
}

// fire runs the deadline checks scheduled so far.
func (h *harness) fire() {
	h.mu.Lock()
	timers := h.timers
	h.timers = nil
	h.mu.Unlock()
	for _, f := range timers {
		f()
	}
}

func (h *harness) sql(query string, args ...any) {
	h.t.Helper()
	if _, err := h.pool.Exec(context.Background(), query, args...); err != nil {
		h.t.Fatal(err)
	}
}

func (h *harness) count(query string, args ...any) int {
	h.t.Helper()
	var n int
	if err := h.pool.QueryRow(context.Background(), query, args...).Scan(&n); err != nil {
		h.t.Fatal(err)
	}
	return n
}

// person is a signed-in person's session cookie.
func (h *harness) person(principal string) string {
	h.t.Helper()
	b := make([]byte, 32)
	_, _ = rand.Read(b)
	token := base64.RawURLEncoding.EncodeToString(b)
	h.sql(`INSERT INTO auth_sessions (id_hash, principal, issuer, subject, kind, created_at, last_seen_at,
		idle_expires_at, absolute_expires_at) VALUES (sha256($1::bytea), $2, 'i', $2, 'login', now(), now(),
		now() + interval '1 day', now() + interval '2 days')`, []byte(token), principal)
	return "__Host-scadbuddy_session=" + token
}

// assistant is a person's access token, as the assistant over MCP holds one.
func (h *harness) assistant(principal string) string {
	h.t.Helper()
	id := make([]byte, 8)
	_, _ = rand.Read(id)
	b := make([]byte, 32)
	_, _ = rand.Read(b)
	token := "sbpat_" + hex.EncodeToString(id) + "." + base64.RawURLEncoding.EncodeToString(b)
	h.sql(`INSERT INTO personal_access_tokens (id, principal, name, scope, token_hash, expires_at)
		VALUES ($1, $2, 'n', 'write', sha256($3::bytea), now() + interval '1 day')`, hex.EncodeToString(id), principal, []byte(token))
	return token
}

// service is a service credential of caller with scopes.
func (h *harness) service(caller string, scopes ...string) string {
	h.t.Helper()
	id, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(id)
	_, _ = rand.Read(secret)
	token := "sbsvc_" + hex.EncodeToString(id) + "." + base64.RawURLEncoding.EncodeToString(secret)
	h.sql(`INSERT INTO service_credentials (id, caller, scopes, secret_hash, expires_at, proof)
		VALUES ($1, $2, $3, sha256($4::bytea), now() + interval '15 minutes', 'inprocess')`,
		hex.EncodeToString(id), caller, scopes, []byte(token))
	return token
}

type answer struct {
	status int
	body   map[string]any
	raw    string
	header http.Header
}

// do sends a request to the first pod. who is a cookie ("__Host-…"), a bearer token, or "".
func (h *harness) do(method, path, who, body string) answer {
	return h.pods[0].do(method, path, who, body)
}

func (p *pod) do(method, path, who, body string) answer {
	p.h.t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req, err := http.NewRequest(method, p.url+path, reader)
	if err != nil {
		p.h.t.Fatal(err)
	}
	req.Header.Set(auth.RequestHeader, "1")
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	switch {
	case strings.HasPrefix(who, "__Host-"):
		req.Header.Set("Cookie", who)
	case who != "":
		req.Header.Set("Authorization", "Bearer "+who)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		p.h.t.Fatal(err)
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	out := answer{status: res.StatusCode, raw: string(raw), header: res.Header}
	_ = json.Unmarshal(raw, &out.body)
	return out
}

func expect[T comparable](t *testing.T, got, want T) {
	t.Helper()
	if got != want {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func expectStatus(t *testing.T, a answer, want int) {
	t.Helper()
	if a.status != want {
		t.Fatalf("status %d, want %d: %s", a.status, want, a.raw)
	}
}

// paired is a device paired to the person with cookie who: its id and token.
type paired struct{ id, token string }

var allMethods = `["nfc.read","nfc.write","code.scan","camera.capture"]`

func (h *harness) pair(who string) paired {
	h.t.Helper()
	got := h.do("POST", "/api/v1/devices/pairings", who, "")
	expectStatus(h.t, got, 200)
	claimed := h.claim(got.body["code_url"].(string), allMethods)
	expectStatus(h.t, claimed, 200)
	return paired{id: claimed.body["device_id"].(string), token: claimed.body["token"].(string)}
}

func (h *harness) claim(codeURL, capabilities string) answer {
	h.t.Helper()
	path := codeURL[strings.Index(codeURL, "/api/"):]
	return h.do("POST", path, "", `{"name":"Pixel","model":"Pixel 9","app_version":"1.4.0","capabilities":`+capabilities+`}`)
}

// ws is a test's WebSocket client.
type ws struct {
	t    *testing.T
	conn net.Conn
	read *bufio.Reader
}

func (p *pod) dial(header map[string]string) (*ws, *http.Response) {
	p.h.t.Helper()
	conn, err := net.Dial("tcp", strings.TrimPrefix(p.url, "http://"))
	if err != nil {
		p.h.t.Fatal(err)
	}
	p.h.t.Cleanup(func() { conn.Close() })
	req := "GET /api/v1/ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
		"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + base64.StdEncoding.EncodeToString([]byte("0123456789abcdef")) + "\r\n"
	for k, v := range header {
		req += k + ": " + v + "\r\n"
	}
	if _, err := conn.Write([]byte(req + "\r\n")); err != nil {
		p.h.t.Fatal(err)
	}
	read := bufio.NewReader(conn)
	res, err := http.ReadResponse(read, nil)
	if err != nil {
		p.h.t.Fatal(err)
	}
	return &ws{t: p.h.t, conn: conn, read: read}, res
}

func (w *ws) send(text string) {
	w.t.Helper()
	mask := []byte{1, 2, 3, 4}
	frame := []byte{0x81}
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

// frame is the next text message that is not a ping, as JSON.
func (w *ws) frame() map[string]any {
	w.t.Helper()
	for {
		op, payload := w.next()
		if op != 1 {
			w.t.Fatalf("opcode %d, payload %q", op, payload)
		}
		var v map[string]any
		if err := json.Unmarshal([]byte(payload), &v); err != nil {
			w.t.Fatal(err)
		}
		if v["type"] != "ping" {
			return v
		}
	}
}

// device opens the device's realtime socket and follows its own topic.
func (p *pod) device(d paired) *ws {
	p.h.t.Helper()
	sock, res := p.dial(map[string]string{"Authorization": "Bearer " + d.token})
	expect(p.h.t, res.StatusCode, 101)
	sock.send(`{"type":"subscribe","topics":["device:` + d.id + `"]}`)
	expect(p.h.t, sock.frame()["type"], "subscribed")
	return sock
}
