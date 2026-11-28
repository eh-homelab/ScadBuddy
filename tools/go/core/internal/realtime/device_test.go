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

func (tokens) Live(_ context.Context, authorization string) (string, bool, error) {
	return ownID, authorization == token, nil
}

func (tokens) CallVisible(context.Context, string, string) bool { return true }

// deviceServer is a socket with one live device token, under mode.
func deviceServer(t *testing.T, mode auth.Mode) *httptest.Server {
	t.Helper()
	pool := testdb.New(t)
	hub := bus.NewHub(pool, nil)
	t.Cleanup(hub.Close)
	gate := auth.NewGate(&auth.Resolver{Pool: pool}, mode, nil)
	sock, err := New(context.Background(), hub, pool, http.NotFoundHandler(), func(string) string { return "" }, gate)
	if err != nil {
		t.Fatal(err)
	}
	sock.Devices = tokens{}
	hub.Start(5 * time.Second)
	server := httptest.NewServer(sock)
	t.Cleanup(server.Close)
	return server
}

// dialAt opens a handshake on path, with authorization when it is not empty.
func dialAt(t *testing.T, server *httptest.Server, path, authorization string) (net.Conn, *bufio.Reader, int) {
	t.Helper()
	conn, err := net.Dial("tcp", strings.TrimPrefix(server.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	req := "GET " + path + " HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n" +
		"Sec-WebSocket-Key: " + base64.StdEncoding.EncodeToString([]byte("0123456789abcdef")) + "\r\n"
	if authorization != "" {
		req += "Authorization: " + authorization + "\r\n"
	}
	_, _ = conn.Write([]byte(req + "\r\n"))
	read := bufio.NewReader(conn)
	res, err := http.ReadResponse(read, nil)
	if err != nil {
		t.Fatal(err)
	}
	return conn, read, res.StatusCode
}

// The device path opens for a live device token only, in every mode: no credential, a
// person's access token or a bad device token is a 401 before the handshake, even in off and
// observe, where the person's path serves a signed-out socket. The person's path never takes
// a device token.
func TestDevicePathOnlyTakesADeviceToken(t *testing.T) {
	for _, mode := range []auth.Mode{auth.Off, auth.Observe, auth.Enforce} {
		server := deviceServer(t, mode)
		for _, authorization := range []string{"", "Bearer sbpat_" + strings.Repeat("A", 43),
			"Bearer sbdev_" + strings.Repeat("B", 43), "Basic " + strings.TrimPrefix(token, "Bearer ")} {
			if _, _, status := dialAt(t, server, DevicePath, authorization); status != 401 {
				t.Errorf("%s: %q on the device path: %d", mode, authorization, status)
			}
		}
		if _, _, status := dialAt(t, server, DevicePath, token); status != 101 {
			t.Errorf("%s: a live device token on the device path: %d", mode, status)
		}
		if _, _, status := dialAt(t, server, Path, token); status != 401 {
			t.Errorf("%s: a device token on the person's path: %d", mode, status)
		}
	}
}

// Only an upgrade is the socket: a plain GET of the device path, even with a live device
// token, goes on to the next handler, so a public route for the path opens nothing more.
func TestDevicePathPlainGetGoesOn(t *testing.T) {
	server := deviceServer(t, auth.Observe)
	req, err := http.NewRequest("GET", server.URL+DevicePath, nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", token)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != http.StatusNotFound {
		t.Fatalf("a plain GET of the device path: %d, want the next handler's 404", res.StatusCode)
	}
}

// A device token follows its own topic only: another device's, or any realtime topic, is
// refused.
func TestRealtimeDeviceOwnTopicOnly(t *testing.T) {
	server := deviceServer(t, auth.Observe)
	conn, read, status := dialAt(t, server, DevicePath, token)
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
