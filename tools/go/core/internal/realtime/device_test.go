package realtime

import (
	"bufio"
	"context"
	"encoding/base64"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const (
	ownID   = "0f6a2b1c-0000-4000-8000-000000000001"
	otherID = "0f6a2b1c-0000-4000-8000-000000000002"
	token   = "Bearer sbdev_" + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)

// tokens is one live device token.
type tokens struct{}

func (tokens) DeviceOf(_ context.Context, authorization string) (string, error) {
	if authorization != token {
		return "", errors.New("no")
	}
	return ownID, nil
}

func (tokens) Live(_ context.Context, authorization string) (string, bool) {
	return ownID, authorization == token
}

func (tokens) CallVisible(context.Context, string, string) bool { return true }

// A device token follows its own topic only: another device's, or any realtime topic, is
// refused. A bad token is a 401 before the handshake, whatever the mode.
func TestRealtimeDeviceOwnTopicOnly(t *testing.T) {
	pool := testdb.New(t)
	hub := bus.NewHub(pool, nil)
	t.Cleanup(hub.Close)
	gate := auth.NewGate(&auth.Resolver{Pool: pool}, auth.Observe, nil)
	sock, err := New(context.Background(), hub, pool, http.NotFoundHandler(), func(string) string { return "" }, gate)
	if err != nil {
		t.Fatal(err)
	}
	sock.Devices = tokens{}
	hub.Start(5 * time.Second)
	server := httptest.NewServer(sock)
	t.Cleanup(server.Close)

	dial := func(authorization string) (net.Conn, *bufio.Reader, int) {
		conn, err := net.Dial("tcp", strings.TrimPrefix(server.URL, "http://"))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { conn.Close() })
		req := "GET /api/v1/ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n" +
			"Sec-WebSocket-Key: " + base64.StdEncoding.EncodeToString([]byte("0123456789abcdef")) + "\r\nAuthorization: " +
			authorization + "\r\n\r\n"
		_, _ = conn.Write([]byte(req))
		read := bufio.NewReader(conn)
		res, err := http.ReadResponse(read, nil)
		if err != nil {
			t.Fatal(err)
		}
		return conn, read, res.StatusCode
	}
	if _, _, status := dial("Bearer sbdev_" + strings.Repeat("B", 43)); status != 401 {
		t.Fatalf("a bad device token: %d", status)
	}
	conn, read, status := dial(token)
	if status != 101 {
		t.Fatalf("a live device token: %d", status)
	}
	send := func(text string) {
		frame := []byte{0x81, 0x80 | byte(len(text)), 0, 0, 0, 0}
		_, _ = conn.Write(append(frame, text...))
	}
	next := func() string {
		_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
		var head [2]byte
		if _, err := io.ReadFull(read, head[:]); err != nil {
			t.Fatal(err)
		}
		payload := make([]byte, int(head[1]&0x7f))
		_, _ = io.ReadFull(read, payload)
		return string(payload)
	}
	for _, topic := range []string{"device:" + otherID, "filament:x", "models", "device:call:" + ownID} {
		send(`{"type":"subscribe","topics":["` + topic + `"]}`)
		if got := next(); !strings.Contains(got, `"type":"error"`) {
			t.Errorf("%s: %s", topic, got)
		}
	}
	send(`{"type":"subscribe","topics":["device:` + ownID + `"]}`)
	if got := next(); !strings.HasPrefix(got, `{"type":"subscribed","topics":["device:`+ownID+`"]`) {
		t.Fatalf("own topic: %s", got)
	}
}
