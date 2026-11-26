package realtime

import (
	"bufio"
	"context"
	"net"
	"testing"
	"time"
)

// A part of a socket that panics ends the socket, not the process.
func TestAPanickingPartEndsItsSocketOnly(t *testing.T) {
	got := guarded(context.Background(), func(context.Context) string { panic("a bug") })
	if got != "panic" {
		t.Fatalf("got %q", got)
	}
}

// A pong is taken only after a ping went out: an unsolicited one does not answer the next
// keepalive ping in advance.
func TestAnUnsolicitedPongIsNotTaken(t *testing.T) {
	server, client := net.Pipe()
	t.Cleanup(func() { server.Close(); client.Close() })
	c := &conn{net: server, rw: bufio.NewReadWriter(bufio.NewReader(server), bufio.NewWriter(server)),
		pong: make(chan struct{}, 1)}
	read := make(chan error, 1)
	go func() {
		_, err := c.read()
		read <- err
	}()
	// A masked pong with no payload, then a masked text frame "x".
	if _, err := client.Write([]byte{0x8a, 0x80, 1, 2, 3, 4, 0x81, 0x81, 1, 2, 3, 4, 'x' ^ 1}); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-read:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no message read")
	}
	select {
	case <-c.pong:
		t.Fatal("an unsolicited pong was taken")
	default:
	}
}
