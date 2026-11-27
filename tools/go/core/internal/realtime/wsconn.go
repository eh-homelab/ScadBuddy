package realtime

import (
	"bufio"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"
)

// The WebSocket protocol, server side (RFC 6455, https://www.rfc-editor.org/rfc/rfc6455),
// as far as the realtime socket needs it: text and binary messages (fragmented too),
// ping, pong and close. No extension is taken, so no message is compressed.

const (
	opContinue = 0x0
	opText     = 0x1
	opBinary   = 0x2
	opClose    = 0x8
	opPing     = 0x9
	opPong     = 0xa

	// MessageMax is the largest message read: 64 KiB, which holds realtime.py's largest frame
	// (MaxFrameChars characters of at most 4 bytes each). A bigger message closes the socket
	// with 1009 before its bytes are held. uvicorn's limit in the API image is 8 MiB.
	MessageMax = 64 << 10
	// pingEvery and pongWait are uvicorn's ws_ping_interval and ws_ping_timeout (20 s each).
	pingEvery = 20 * time.Second
	pongWait  = 20 * time.Second
)

// guid is RFC 6455 §1.3's.
const guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

// isUpgrade says whether r asks for a WebSocket.
func isUpgrade(r *http.Request) bool {
	return r.Method == http.MethodGet && headerHas(r.Header, "Connection", "upgrade") &&
		headerHas(r.Header, "Upgrade", "websocket")
}

func headerHas(h http.Header, name, token string) bool {
	for _, v := range h.Values(name) {
		for part := range strings.SplitSeq(v, ",") {
			if strings.EqualFold(strings.TrimSpace(part), token) {
				return true
			}
		}
	}
	return false
}

// conn is one accepted socket. Writes are serialised; one reader calls read.
type conn struct {
	net  net.Conn
	rw   *bufio.ReadWriter
	wmu  sync.Mutex
	pong chan struct{}
	// pinged is set while a keepalive ping waits for its pong: only then is a pong taken.
	pinged atomic.Bool
	// take, when set, is the frame rate's: each ping and pong takes one, as a message does.
	take func() bool
	// closed is set once a close frame was sent.
	closed bool
}

// accept answers the handshake (RFC 6455 §4.2.2) and takes the connection over.
func accept(w http.ResponseWriter, r *http.Request) (*conn, error) {
	key := r.Header.Get("Sec-WebSocket-Key")
	// RFC 6455 §4.2.1: the key is 16 bytes in base64.
	raw, err := base64.StdEncoding.DecodeString(key)
	if r.Header.Get("Sec-WebSocket-Version") != "13" || err != nil || len(raw) != 16 {
		w.Header().Set("Sec-WebSocket-Version", "13")
		http.Error(w, "a WebSocket handshake needs version 13 and a key", http.StatusBadRequest)
		return nil, errors.New("bad handshake")
	}
	netConn, rw, err := http.NewResponseController(w).Hijack()
	if err != nil {
		return nil, err
	}
	sum := sha1.Sum([]byte(key + guid))
	_, err = rw.WriteString("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
		"Sec-WebSocket-Accept: " + base64.StdEncoding.EncodeToString(sum[:]) + "\r\n\r\n")
	if err == nil {
		err = rw.Flush()
	}
	if err != nil {
		netConn.Close()
		return nil, err
	}
	_ = netConn.SetDeadline(time.Time{})
	return &conn{net: netConn, rw: rw, pong: make(chan struct{}, 1)}, nil
}

// refuse answers a handshake the app closed before it accepted it, as uvicorn does: 403 with
// no body.
func refuse(w http.ResponseWriter) {
	w.Header().Set("Content-Length", "0")
	w.WriteHeader(http.StatusForbidden)
}

func (c *conn) writeFrame(op byte, payload []byte) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	if c.closed {
		return net.ErrClosed
	}
	if op == opClose {
		c.closed = true
	}
	head := []byte{0x80 | op}
	switch n := len(payload); {
	case n < 126:
		head = append(head, byte(n))
	case n <= 0xffff:
		head = append(head, 126, byte(n>>8), byte(n))
	default:
		head = binary.BigEndian.AppendUint64(append(head, 127), uint64(n))
	}
	_ = c.net.SetWriteDeadline(time.Now().Add(30 * time.Second))
	if _, err := c.rw.Write(head); err != nil {
		return err
	}
	if _, err := c.rw.Write(payload); err != nil {
		return err
	}
	return c.rw.Flush()
}

// text sends one text message.
func (c *conn) text(s string) error { return c.writeFrame(opText, []byte(s)) }

// close sends a close frame with code and reason, then ends the connection.
func (c *conn) close(code int, reason string) {
	payload := binary.BigEndian.AppendUint16(nil, uint16(code))
	_ = c.writeFrame(opClose, append(payload, reason...))
	// The peer answers with its own close frame; a short wait lets it arrive.
	_ = c.net.SetReadDeadline(time.Now().Add(time.Second))
	go func() {
		time.Sleep(time.Second)
		c.net.Close()
	}()
}

// message is one message the client sent.
type message struct {
	text   string
	binary bool
}

// errClosed is the client's close, or a broken connection: the socket is over.
var errClosed = errors.New("the socket closed")

// errRate is a control frame past the frame rate.
var errRate = errors.New("too many frames")

// closeCode says whether a client may send code in a close frame (RFC 6455 §7.4.1 and the
// IANA registry): 1004, 1005, 1006 and 1015 are never sent, and 1016 to 2999 are not assigned.
func closeCode(code int) bool {
	return code >= 1000 && code <= 1003 || code >= 1007 && code <= 1014 || code >= 3000 && code <= 4999
}

// read is the next message. Ping, pong and close frames are answered here.
func (c *conn) read() (message, error) {
	var buf []byte
	op := byte(0)
	for {
		fin, frameOp, payload, err := c.frame()
		if err != nil {
			return message{}, err
		}
		if (frameOp == opPing || frameOp == opPong) && c.take != nil && !c.take() {
			return message{}, errRate
		}
		switch frameOp {
		case opPing:
			_ = c.writeFrame(opPong, payload)
			continue
		case opPong:
			if c.pinged.CompareAndSwap(true, false) {
				select {
				case c.pong <- struct{}{}:
				default:
				}
			}
			continue
		case opClose:
			switch {
			case len(payload) == 0:
				_ = c.writeFrame(opClose, nil)
			case len(payload) == 1 || !closeCode(int(binary.BigEndian.Uint16(payload))):
				c.close(1002, "")
				return message{}, errClosed
			case !utf8.Valid(payload[2:]):
				c.close(1007, "")
				return message{}, errClosed
			default:
				_ = c.writeFrame(opClose, payload[:2])
			}
			c.net.Close()
			return message{}, errClosed
		case opText, opBinary:
			if op != 0 {
				c.close(1002, "")
				return message{}, errClosed
			}
			op = frameOp
		case opContinue:
			if op == 0 {
				c.close(1002, "")
				return message{}, errClosed
			}
		default:
			c.close(1002, "")
			return message{}, errClosed
		}
		if len(buf)+len(payload) > MessageMax {
			c.close(1009, "")
			return message{}, errClosed
		}
		buf = append(buf, payload...)
		if fin {
			if op == opText && !utf8.Valid(buf) {
				c.close(1007, "")
				return message{}, errClosed
			}
			return message{text: string(buf), binary: op == opBinary}, nil
		}
	}
}

func (c *conn) frame() (fin bool, op byte, payload []byte, err error) {
	var head [2]byte
	if _, err = io.ReadFull(c.rw, head[:]); err != nil {
		return false, 0, nil, errClosed
	}
	fin, op = head[0]&0x80 != 0, head[0]&0x0f
	if head[0]&0x70 != 0 || head[1]&0x80 == 0 {
		// A reserved bit with no extension, or a frame the client did not mask.
		c.close(1002, "")
		return false, 0, nil, errClosed
	}
	n := uint64(head[1] & 0x7f)
	switch n {
	case 126:
		var ext [2]byte
		if _, err = io.ReadFull(c.rw, ext[:]); err != nil {
			return false, 0, nil, errClosed
		}
		n = uint64(binary.BigEndian.Uint16(ext[:]))
	case 127:
		var ext [8]byte
		if _, err = io.ReadFull(c.rw, ext[:]); err != nil {
			return false, 0, nil, errClosed
		}
		n = binary.BigEndian.Uint64(ext[:])
	}
	if op >= opClose && (n > 125 || !fin) {
		c.close(1002, "")
		return false, 0, nil, errClosed
	}
	if n > MessageMax {
		c.close(1009, "")
		return false, 0, nil, errClosed
	}
	var mask [4]byte
	if _, err = io.ReadFull(c.rw, mask[:]); err != nil {
		return false, 0, nil, errClosed
	}
	payload = make([]byte, n)
	if _, err = io.ReadFull(c.rw, payload); err != nil {
		return false, 0, nil, errClosed
	}
	for i := range payload {
		payload[i] ^= mask[i%4]
	}
	return fin, op, payload, nil
}

// keepAlive pings every pingEvery and ends the connection when no pong comes within
// pongWait, as uvicorn's keepalive does, until done closes.
func (c *conn) keepAlive(done <-chan struct{}) {
	tick := time.NewTicker(pingEvery)
	defer tick.Stop()
	for {
		select {
		case <-done:
			return
		case <-tick.C:
		}
		c.pinged.Store(true)
		if c.writeFrame(opPing, nil) != nil {
			return
		}
		select {
		case <-done:
			return
		case <-c.pong:
		case <-time.After(pongWait):
			c.close(1011, "keepalive ping timeout")
			return
		}
	}
}
