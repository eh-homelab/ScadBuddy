package sbfleet

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync/atomic"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetchat"
)

const (
	// messageKind is the event core publishes after a send (internal/fleet).
	messageKind = "fleet.message"
	// The back-off between streams: 500 ms doubling to 30 s, as camera/follow.py.
	minDelay = 500 * time.Millisecond
	maxDelay = 30 * time.Second
	// silence ends a stream that sent nothing, not even its keepalive (every 15 s,
	// agent_changes/hub.py KEEPALIVE_SECONDS), for this long: a half-open connection.
	silence = 45 * time.Second
	// lineMax bounds one line of the stream; an event's payload is under 8000 bytes
	// (Postgres's NOTIFY cap).
	lineMax = 1 << 20
)

type bus struct {
	c                  *client
	minDelay, maxDelay time.Duration
	silence            time.Duration
}

// NewBus returns a fleetchat.Bus over the API's fleet change stream at baseURL (core's
// URL: core forwards the route to the Python API). Publish does nothing, since core
// publishes fleet.message itself on a send. Subscribe follows the stream until its
// context ends.
func NewBus(baseURL string, token func() (string, error), opts ...Option) fleetchat.Bus {
	return newBus(baseURL, token, opts...)
}

func newBus(baseURL string, token func() (string, error), opts ...Option) *bus {
	// No overall timeout: the stream is long. The answer's headers still come in time.
	c := newClient(baseURL, token, 0, opts)
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.ResponseHeaderTimeout = requestTimeout
	c.http.Transport = transport
	return &bus{c: c, minDelay: minDelay, maxDelay: maxDelay, silence: silence}
}

// Publish does nothing: core publishes fleet.message in the send's route.
func (b *bus) Publish(context.Context, []string) error { return nil }

// next is the delay after d: minDelay first, then doubling up to maxDelay.
func (b *bus) next(d time.Duration) time.Duration {
	if d <= 0 {
		return b.minDelay
	}
	return min(2*d, b.maxDelay)
}

// Subscribe follows the change stream until ctx ends, and returns ctx.Err() then. It
// reconnects after every failure and after the server ends a stream, sending the last
// id it saw as Last-Event-ID so the server replays what came between. A refused
// credential (401, 403) is retried too: the credential is renewed (OnUnauthorized, or a
// static file that rotates).
func (b *bus) Subscribe(ctx context.Context, f func(fleetchat.Event)) error {
	var last string
	var delay time.Duration
	for {
		framed, err := b.follow(ctx, &last, f)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if framed {
			delay = 0
		}
		delay = b.next(delay)
		if err != nil {
			slog.WarnContext(ctx, "sbfleet: the fleet change stream failed; reconnecting", "err", err, "in", delay)
		} else {
			slog.InfoContext(ctx, "sbfleet: the fleet change stream ended; reconnecting", "in", delay)
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
}

// follow reads one stream until it ends, and says whether it delivered a frame. last
// is the newest frame id seen, sent as Last-Event-ID and moved by each frame.
func (b *bus) follow(ctx context.Context, last *string, f func(fleetchat.Event)) (bool, error) {
	streamCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	var silent atomic.Bool
	watchdog := time.AfterFunc(b.silence, func() {
		silent.Store(true)
		cancel()
	})
	defer watchdog.Stop()

	header := http.Header{"Accept": {"text/event-stream"}}
	if *last != "" {
		header.Set("Last-Event-ID", *last)
	}
	res, err := b.c.send(streamCtx, http.MethodGet, "/changes", nil, header)
	if err != nil {
		return false, err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return false, problemOf(res)
	}
	slog.InfoContext(ctx, "sbfleet: the fleet change stream connected", "resumed", *last != "")

	framed := false
	var event, id string
	var data []string
	hasID := false
	scan := bufio.NewScanner(res.Body)
	scan.Buffer(make([]byte, 0, 64<<10), lineMax)
	for scan.Scan() {
		watchdog.Reset(b.silence)
		line := strings.TrimSuffix(scan.Text(), "\r")
		if line == "" {
			if hasID {
				*last = id
			}
			if event != "" || len(data) > 0 {
				framed = true
				dispatch(ctx, event, strings.Join(data, "\n"), f)
			}
			event, id, data, hasID = "", "", nil, false
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue // a comment: the keepalive
		}
		field, value, _ := strings.Cut(line, ":")
		value = strings.TrimPrefix(value, " ")
		switch field {
		case "event":
			event = value
		case "data":
			data = append(data, value)
		case "id":
			id, hasID = value, true
		}
	}
	err = scan.Err()
	if silent.Load() && ctx.Err() == nil {
		return framed, fmt.Errorf("no frame or keepalive for %s", b.silence)
	}
	return framed, err
}

// dispatch turns one frame into an Event: a fleet.message change names its recipients.
// The rest say that events may have been lost, so every watched inbox may have changed:
// ready with resync (the server could not place Last-Event-ID), ready without reconnect
// (a stream with no Last-Event-ID starts at the newest event, so what came before it was
// never seen), reconnected, and a ready or change frame that cannot be read. ready with
// reconnect and no resync follows a replay that lost nothing (every fleet.* event is
// logged), so it is no Event.
func dispatch(ctx context.Context, event, data string, f func(fleetchat.Event)) {
	switch event {
	case "ready":
		var d struct {
			Resync    bool `json:"resync"`
			Reconnect bool `json:"reconnect"`
		}
		if err := json.Unmarshal([]byte(data), &d); err != nil {
			slog.WarnContext(ctx, "sbfleet: a ready frame that cannot be read; resyncing", "err", err)
			f(fleetchat.Event{Resync: true})
			return
		}
		if d.Resync || !d.Reconnect {
			slog.InfoContext(ctx, "sbfleet: the fleet change stream may have lost events; resyncing every inbox",
				"frame", "ready", "resync", d.Resync, "reconnect", d.Reconnect)
			f(fleetchat.Event{Resync: true})
		}
	case "reconnected":
		slog.InfoContext(ctx, "sbfleet: the fleet change stream may have lost events; resyncing every inbox",
			"frame", "reconnected")
		f(fleetchat.Event{Resync: true})
	case "change":
		recipients, err := recipientsOf(data)
		if err != nil {
			slog.WarnContext(ctx, "sbfleet: a change frame that cannot be read; resyncing", "err", err)
			f(fleetchat.Event{Resync: true})
			return
		}
		if len(recipients) > 0 {
			f(fleetchat.Event{SessionIDs: recipients})
		}
	}
}

// recipientsOf is a fleet.message change's recipients; none for another kind. The
// frame's data is {"c": channel, "p": the stored payload, a JSON string}.
func recipientsOf(data string) ([]string, error) {
	var frame struct {
		P string `json:"p"`
	}
	if err := json.Unmarshal([]byte(data), &frame); err != nil {
		return nil, err
	}
	var payload struct {
		Kind       string   `json:"kind"`
		Recipients []string `json:"recipients"`
	}
	if err := json.Unmarshal([]byte(frame.P), &payload); err != nil {
		return nil, errors.Join(errors.New("the payload is not JSON"), err)
	}
	if payload.Kind != messageKind {
		return nil, nil
	}
	return payload.Recipients, nil
}
